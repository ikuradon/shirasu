"""Reception timelines agree with independently clocked, real Service posts.

The reference service ticks every virtual second rather than using the
planner's deadlines. It signs local events using ephemeral test keys and an
in-memory outbox; transport and image URL creation never fetch anything.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import azarashi
from azarashi.reports import dcr

from shirasu import service as main
from tests.posting_helpers import hashtag_tags
from shirasu.images.alt import image_alt_text
from shirasu.images.publication import ImagePublisher
from shirasu.outbox import Outbox
from shirasu.reports.format import ATTRIBUTION
from tools.preview.gallery_catalog import _field_variant, load_cases
from tools.preview.gallery_timeline import plan_timeline
from tools.preview.timeline_catalog import load_scenarios
from tests.receiver import test_main as service_helpers


class TimelinePublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(main, 'logger'))
        self.enterContext(patch.object(main.Settings, 'load', side_effect=AssertionError(
            'Timeline comparisons must not load production settings')))
        for name, value in (('completed', False), ('reports', {}), ('announcement', None)):
            self.enterContext(patch.object(dcr.NankaiTroughEarthquake, name, value))

    async def assert_matches_service(self, records, arrivals, horizon, *, nankai_topics=True):
        records, arrivals = tuple(records), tuple(arrivals)
        steps = plan_timeline(records, arrivals, until_seconds=horizon,
                              nankai_topics=nankai_topics)
        expected = [(step.elapsed_seconds, post) for step in steps for post in step.posts]
        expected_receipts = [(index, outcome) for step in steps
                             for index, outcome in zip(step.received_indexes, step.outcomes, strict=True)]
        clock = service_helpers.Clock()
        clock.now = 0
        nostr = service_helpers.fake_nostr()
        publisher = ImagePublisher('https://images.example.test', 'timeline', bytes(range(32)))
        image_inputs, delivered, receipts = {}, [], []

        def image_url(entries):
            # Real protocol validation, a public test key, and an inert origin.
            # The capability switch tests unpublished Nankai attachment input;
            # this test does not claim that a deployed renderer supports it.
            url = publisher.url(entries)
            image_inputs[url] = tuple(entries)
            return url

        async def send(event):
            delivered.append((clock.now, event))

        nostr.send.side_effect = send
        images = SimpleNamespace(url=Mock(side_effect=image_url),
                                 supports_nankai_topic=nankai_topics)
        with Outbox(':memory:', nostr.identity) as outbox:
            self.assertEqual(outbox.connection.execute('PRAGMA database_list').fetchone()[2], '')
            service = main.Service(nostr, outbox, asyncio.Event(), clock=clock, images=images,
                                   typhoon_wait_seconds=180, nankai_wait_seconds=180)

            async def drain():
                for _ in range(100):
                    # Match publisher(): clear a previous receipt wake before
                    # polling, so ignored/duplicate receipts cannot reuse it.
                    service.wake.clear()
                    if not await service.publish_one():
                        return
                self.fail('Timeline service did not become idle')

            next_input = 0
            # Independently poll on integer seconds. Deadline processing wins
            # ties, then equal-time arrivals preserve their explicit order.
            for second in range(horizon + 1):
                clock.now = second
                await drain()
                while next_input < len(records) and arrivals[next_input] == second:
                    nmea, milliseconds = records[next_input]
                    report = azarashi.decode(nmea, timestamp=datetime.fromtimestamp(
                        milliseconds / 1000, timezone.utc))
                    result = await service.accept(report)
                    receipts.append((next_input, result))
                    next_input += 1
                    if service.wake.is_set():
                        await drain()

            self.assertEqual(next_input, len(records))
            self.assertEqual(receipts, expected_receipts)
            self.assertEqual(len(delivered), len(expected))
            self.assertEqual(outbox.snapshot(clock())['counts']['pending'], 0)
            nostr.connect.assert_not_awaited()
            nostr.connected.assert_not_awaited()
            self.assertEqual(nostr.send.await_count, len(expected))
            for index, ((actual_time, event), (expected_time, post)) in enumerate(
                    zip(delivered, expected, strict=True)):
                with self.subTest(post=index, kind=post.kind):
                    self.assertEqual(actual_time, expected_time)
                    self.assertTrue(event.verify())
                    self.assertEqual(event.kind().as_u16(), 1)
                    self.assertEqual(event.author().to_hex(), nostr.identity)
                    content = event.content()
                    self.assertEqual(content.count(ATTRIBUTION), 1)
                    self.assertEqual(post.text.count(ATTRIBUTION), 1)
                    self.assertEqual([tag.to_vec() for tag in event.tags() if tag.to_vec()[0] == 't'],
                                     hashtag_tags(event))
                    urls = [url for url in image_inputs if content.startswith(url + '\n\n')]
                    self.assertEqual(len(urls), int(post.image_enabled))
                    imeta = [tag.to_vec() for tag in event.tags() if tag.to_vec()[0] == 'imeta']
                    self.assertEqual(len(imeta), int(post.image_enabled))
                    if urls:
                        url, = urls
                        self.assertEqual(content.removeprefix(url + '\n\n'), post.text)
                        entries = tuple(records[i] for i in post.image_record_indexes)
                        self.assertEqual(image_inputs[url], entries)
                        self.assertIn('url ' + url, imeta[0])
                        self.assertIn('alt ' + image_alt_text(entries), imeta[0])
                    else:
                        self.assertEqual(content, post.text)
                        self.assertEqual(post.image_record_indexes, ())
                    self.assertEqual(post.text.partition('\n---\n')[2].splitlines(),
                                     ['NMEA: ' + records[i][0] for i in post.record_indexes])
        return expected

    async def test_all_reception_scenarios_match_real_delivery_with_both_nankai_capabilities(self):
        scenarios = load_scenarios()
        self.assertEqual(len(scenarios), 4)
        for enabled in (False, True):
            for scenario in scenarios:
                with self.subTest(scenario=scenario.slug, nankai_topics=enabled):
                    await self.assert_matches_service(
                        scenario.records, scenario.arrival_seconds, scenario.until_seconds,
                        nankai_topics=enabled)

    async def test_nankai_completion_at_deadline_posts_notice_then_complete_at_same_second(self):
        case = next(case for case in load_cases() if case.slug == 'nankai-advisory')
        arrivals = (0,) + (180,) * (len(case.records) - 1)
        expected = await self.assert_matches_service(case.records, arrivals, 360)
        self.assertEqual([second for second, _ in expected], [180, 180])
        self.assertIn('【本文未受信】', expected[0][1].text)
        self.assertFalse(expected[0][1].image_enabled)
        self.assertIn('【追加更新】', expected[1][1].text)
        self.assertTrue(expected[1][1].image_enabled)

    async def test_typhoon_point_at_deadline_starts_the_next_collection(self):
        case = next(case for case in load_cases() if case.slug == 'typhoon-cancel-reference')
        records = case.cancel_reference[:2]
        expected = await self.assert_matches_service(records, (0, 180), 360)
        self.assertEqual([second for second, _ in expected], [180, 360])
        self.assertEqual(expected[0][1].record_indexes, (0,))
        self.assertEqual(expected[1][1].record_indexes, (0, 1))
        self.assertIn('【追加更新】', expected[1][1].text)

    async def test_return_to_a_posted_nankai_body_suppresses_duplicate_batch(self):
        case = next(case for case in load_cases() if case.slug == 'nankai-advisory')
        pages = case.records
        conflict = (_field_variant(pages[0][0], ((57, 8, 65),)), pages[0][1])
        records = (*pages, conflict, *pages)
        arrivals = (0,) * len(pages) + (20, 30) + (40,) * (len(pages) - 1)
        expected = await self.assert_matches_service(records, arrivals, 300)
        self.assertEqual([second for second, _ in expected], [0])
        self.assertEqual(expected[0][1].record_indexes, tuple(range(len(pages))))

    async def test_duplicate_batch_does_not_skip_the_next_publisher_poll(self):
        cases = {case.slug: case for case in load_cases()}
        original = cases['nankai-advisory'].records[0]
        conflict = (_field_variant(original[0], ((57, 8, 65),)), original[1])
        other_group = (_field_variant(original[0], ((53, 4, 3),)), original[1])
        records = (original, conflict, original, other_group)
        arrivals = (0, 200, 210, 210)
        for horizon, expected_times in ((390, [180]), (391, [180, 391])):
            with self.subTest(horizon=horizon):
                expected = await self.assert_matches_service(records, arrivals, horizon)
                self.assertEqual([second for second, _ in expected], expected_times)
        # A receipt at the otherwise-idle second wakes the publisher, with the
        # already-queued ordinary post preceding the newly-built second group.
        marine = cases['marine'].records[0]
        expected = await self.assert_matches_service((*records, marine), (*arrivals, 390), 390)
        self.assertEqual([(second, post.kind) for second, post in expected],
                         [(180, 'nankai'), (390, 'single'), (390, 'nankai')])
        # Repeated or ignored frames do not signal Service.wake. They must not
        # turn the duplicate-only poll into an extra same-second publication.
        ignored = (service_helpers.NULL_NMEA, original[1])
        for label, receipt in (('duplicate', original), ('ignored', ignored)):
            for horizon, expected_times in ((390, [180]), (391, [180, 391])):
                with self.subTest(receipt=label, horizon=horizon):
                    expected = await self.assert_matches_service(
                        (*records, receipt), (*arrivals, 390), horizon)
                    self.assertEqual([second for second, _ in expected], expected_times)


if __name__ == '__main__':
    unittest.main()
