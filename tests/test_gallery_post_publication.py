"""Offline post previews must match Service's real collection and formatting.

Every replay uses a fresh in-memory outbox, ephemeral test signing keys and a
mocked Nostr transport. The supplied timestamps only anchor QZQSM decoding;
all arrivals occur at virtual time zero, followed by the 180-second deadline.
No production configuration, existing state, or network is used.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import azarashi
from azarashi.reports import dcr

import main
from image_publication import ImagePublisher
from outbox import Outbox
from scripts.gallery_catalog import load_cases
from scripts.gallery_posts import plan_posts
from qzqsm_samples import decode_reports, expected_nankai_text
import test_main as service_helpers
from test_typhoon import FRAMES


class GalleryPostPublicationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def setUp(self):
        self.enterContext(patch.object(main, 'logger'))
        self.enterContext(patch.object(main.Settings, 'load', side_effect=AssertionError(
            'An offline post comparison must not load service settings')))
        for name, value in (('completed', False), ('reports', {}), ('announcement', None)):
            self.enterContext(patch.object(dcr.NankaiTroughEarthquake, name, value))

    async def assert_matches_service(self, records):
        records = tuple(records)
        expected = plan_posts(records)
        clock = service_helpers.Clock()
        clock.now = 0.
        nostr = service_helpers.fake_nostr()
        image_inputs = {}
        publisher = ImagePublisher('https://images.example.test', 'contract', bytes(range(32)))

        def image_url(entries):
            # Use real URL validation with a public test-only key. The URL is
            # never fetched; it identifies the exact bundle of each sent post.
            url = publisher.url(entries)
            image_inputs[url] = tuple(entries)
            return url

        images = SimpleNamespace(url=Mock(side_effect=image_url))
        with Outbox(':memory:', nostr.identity) as outbox:
            self.assertEqual(outbox.connection.execute('PRAGMA database_list').fetchone()[2], '')
            service = main.Service(nostr, outbox, asyncio.Event(), clock=clock, images=images,
                                   typhoon_wait_seconds=180, nankai_wait_seconds=180)

            async def drain():
                for _ in range(100):
                    if not await service.publish_one():
                        return
                self.fail('Publication did not reach an idle state')

            for nmea, milliseconds in records:
                report = azarashi.decode(nmea, timestamp=datetime.fromtimestamp(
                    milliseconds / 1000, timezone.utc))
                result = await service.accept(report)
                self.assertIn(result, ('queued', 'buffered', 'duplicate', 'ignored'))
                await drain()
            clock.now = 180.
            await drain()
            events = tuple(call.args[0] for call in nostr.send.await_args_list)
            self.assertEqual(len(events), len(expected))
            self.assertEqual(outbox.snapshot(clock())['counts']['pending'], 0)
            nostr.connect.assert_not_awaited()
            nostr.connected.assert_not_awaited()

            for index, (post, event) in enumerate(zip(expected, events, strict=True)):
                with self.subTest(post=index, kind=post.kind):
                    self.assertTrue(event.verify())
                    content = event.content()
                    urls = [url for url in image_inputs if '\n\n' + url + '\n---\n' in content]
                    self.assertLessEqual(len(urls), 1)
                    self.assertEqual(bool(urls), post.image_enabled)
                    self.assertEqual(bool(event.tags()), post.image_enabled)
                    if urls:
                        url, = urls
                        self.assertEqual(content.replace('\n\n' + url, '', 1), post.text)
                        self.assertEqual(image_inputs[url], tuple(records[i] for i in post.record_indexes))
                    else:
                        self.assertEqual(content, post.text)
                    self.assertTrue(post.record_indexes)
                    self.assertEqual(len(post.record_indexes), len(set(post.record_indexes)))
                    self.assertTrue(all(type(i) is int and 0 <= i < len(records)
                                        for i in post.record_indexes))
                    _, separator, original = post.text.partition('\n---\n')
                    self.assertEqual(separator, '\n---\n')
                    self.assertEqual(original.splitlines(),
                                     ['NMEA: ' + records[i][0] for i in post.record_indexes])
        return expected

    async def test_ordinary_marine_post_uses_live_grouped_warning_text(self):
        posts = await self.assert_matches_service(self.cases['marine'].records)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].kind, 'single')
        self.assertEqual(posts[0].record_indexes, (0,))
        self.assertTrue(posts[0].image_enabled)
        self.assertTrue(posts[0].text.startswith('【海上警報】'))
        self.assertIn('\n■ ', posts[0].text)
        self.assertNotIn('【訓練・試験】', posts[0].text)

    async def test_weather_many_is_three_live_posts_instead_of_a_fictional_grouped_post(self):
        records = self.cases['weather-many'].records
        self.assertEqual(len(records), 3)
        posts = await self.assert_matches_service(records)
        self.assertEqual(len(posts), 3)
        self.assertEqual(tuple(post.record_indexes for post in posts), ((0,), (1,), (2,)))
        self.assertTrue(all(post.kind == 'single' and post.image_enabled for post in posts))

    async def test_six_typhoon_frames_share_one_post_and_preserve_image_input_order(self):
        records = tuple((nmea, milliseconds) for milliseconds, nmea in FRAMES)
        self.assertEqual(len(records), 6)
        # A timestamp is a decoding anchor, not a delay in this simulation.
        # Even widely separated anchors must not flush the group early.
        spaced = tuple((nmea, records[0][1] + index * 600_000)
                       for index, (nmea, _) in enumerate(records))
        too_wide = tuple((nmea, records[0][1] + index * 8 * 3_600_000)
                         for index, (nmea, _) in enumerate(records))
        for label, inputs, image_enabled in (
                ('received', records, True), ('reversed', tuple(reversed(records)), True),
                ('date-anchors-only', spaced, True), ('image-input-span-too-wide', too_wide, False)):
            with self.subTest(order=label):
                posts = await self.assert_matches_service(inputs)
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0].kind, 'typhoon')
                self.assertEqual(set(posts[0].record_indexes), set(range(6)))
                self.assertEqual(posts[0].image_enabled, image_enabled)
                self.assertIn('台風', posts[0].text)
                self.assertEqual(posts[0].text.count('\nNMEA: '), 6)

    async def test_training_cancellation_release_and_dcx_guidance_match_live_posts(self):
        for slug, marker in (('eew', '訓練'), ('weather-cancel', '取消'),
                              ('tsunami-release', '警報解除'), ('flood-release', '警報解除'),
                              ('dcx-mtinfo-leave', '追加楕円の範囲から離れる'),
                              ('dcx-secondary-evacuation', 'IC-A-03')):
            with self.subTest(case=slug):
                posts = await self.assert_matches_service(self.cases[slug].records)
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0].kind, 'single')
                self.assertTrue(posts[0].image_enabled)
                self.assertIn(marker, posts[0].text)

    async def test_all_twenty_seven_nankai_pages_make_one_complete_text_only_post(self):
        pages = [report for report in decode_reports('dcr_receiver_20260924')
                 if isinstance(report, dcr.NankaiTroughEarthquake)]
        records = tuple((report.nmea, round(report.timestamp.timestamp() * 1000)) for report in pages)
        self.assertEqual(len(records), 27)
        for inputs in (records, tuple(reversed(records))):
            with self.subTest(reversed=inputs is not records):
                posts = await self.assert_matches_service(inputs)
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0].kind, 'nankai')
                self.assertFalse(posts[0].image_enabled)
                self.assertEqual(set(posts[0].record_indexes), set(range(27)))
                body = posts[0].text.partition('\n---\n')[0].split('\n\n', 1)[1]
                self.assertEqual(body, expected_nankai_text())
                self.assertNotIn('本文未受信', posts[0].text)
                self.assertNotIn('受信中', posts[0].text)


if __name__ == '__main__':
    unittest.main()
