"""WebP publication preserves signed text and old queued events without I/O."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

import azarashi
from nostr_sdk import Event, Keys

import image_protocol as protocol
import image_publication as publication
from image_alt import DEFAULT_IMAGE_ALT, image_alt_text
from image_publication import ImagePublisher, image_entry, with_image
import main
from outbox import Outbox
from test_nankai_publication import isolate_assembly, nankai_page
from test_main import Clock, DCR_NMEA, RECEIVED_AT, TYPHOON_NMEA


KEY = bytes(range(32))
ENV = {'SHIRASU_IMAGE_BASE_URL': 'https://images.example.test',
       'SHIRASU_IMAGE_KEY_ID': 'test', 'SHIRASU_IMAGE_SIGNING_KEY': KEY.hex()}


def candidate(test):
    test.enterContext(patch.object(protocol, 'SUPPORTED_VERSIONS', ('v1', 'v2', 'v3', 'v4', 'v5')))
    test.enterContext(patch.object(protocol, 'WEBP_VERSIONS', ('v5',)))
    test.enterContext(patch.object(publication, 'NANKAI_TOPIC_VERSIONS', ('v5',)))


def publisher(encoding):
    return ImagePublisher(ENV['SHIRASU_IMAGE_BASE_URL'], 'test', KEY,
                          version='v5' if encoding == 'w1' else 'v1', encoding=encoding)


def nostr():
    holder = SimpleNamespace(keys=Keys.generate())
    return SimpleNamespace(
        identity=holder.keys.public_key().to_hex(),
        build_event=Mock(side_effect=lambda message, **kwargs: main.Nostr.build_event(holder, message, **kwargs)),
        send=AsyncMock(), connect=AsyncMock(), connected=AsyncMock(return_value=True), close=AsyncMock())


class WebPPublicationConfigTests(unittest.TestCase):
    def test_format_alone_does_not_enable_images_and_partial_configuration_fails(self):
        for encoding in ('png', 'w1', 'unknown'):
            self.assertIsNone(ImagePublisher.from_environment({'SHIRASU_IMAGE_ENCODING': encoding}))
            for absent in ENV:
                partial = {key: value for key, value in ENV.items() if key != absent}
                with self.assertRaises(ValueError):
                    ImagePublisher.from_environment(dict(partial, SHIRASU_IMAGE_ENCODING=encoding))

    def test_default_is_png_and_only_explicit_candidate_supports_fixed_webp(self):
        old = ImagePublisher.from_environment(ENV)
        self.assertEqual((old.version, old.encoding, old.mime), ('v1', 'png', 'image/png'))
        for version in ('v1', 'v2', 'v3', 'v4', 'v5'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                ImagePublisher.from_environment(dict(ENV, SHIRASU_IMAGE_VERSION=version,
                                                       SHIRASU_IMAGE_ENCODING='w1'))
        candidate(self)
        current = ImagePublisher.from_environment(dict(ENV, SHIRASU_IMAGE_VERSION='v5',
                                                       SHIRASU_IMAGE_ENCODING='w1'))
        self.assertEqual((current.version, current.encoding, current.mime), ('v5', 'w1', 'image/webp'))
        self.assertTrue(current.supports_nankai_topic)
        self.assertNotIn(KEY.hex(), repr(current))
        for encoding in ('webp', 'w2', '', None, 1):
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                ImagePublisher(ENV['SHIRASU_IMAGE_BASE_URL'], 'test', KEY, version='v5', encoding=encoding)

    def test_nostr_mime_is_whitelisted_and_covered_by_the_signature(self):
        client = nostr()
        url = 'https://images.example.test/v5/test/payload.signature.w1.webp'
        event = client.build_event('本文\n\n' + url, image_url=url, image_mime='image/webp')
        self.assertTrue(Event.from_json(event.as_json()).verify())
        self.assertEqual([tag.to_vec() for tag in event.tags()],
                         [['imeta', 'url ' + url, 'm image/webp', 'alt ' + DEFAULT_IMAGE_ALT]])
        for mime in ('image/jpeg', 'image/webp\ninvalid', '', None, 1):
            with self.subTest(mime=mime), self.assertRaises(ValueError):
                client.build_event('本文', image_url=url, image_mime=mime)
        self.assertEqual(client.build_event('本文のみ', image_mime='image/webp').tags(), [])


class WebPServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        candidate(self)
        isolate_assembly(self)
        directory = tempfile.TemporaryDirectory(prefix='shirasu-webp-posts-')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'outbox.sqlite3'
        self.client = nostr()
        self.clock = Clock()
        self.stop = asyncio.Event()
        self.outbox = Outbox(self.path, self.client.identity)
        self.addCleanup(lambda: self.outbox.close())
        self.service = main.Service(self.client, self.outbox, self.stop, clock=self.clock, images=publisher('w1'))
        self.report = azarashi.decode(DCR_NMEA, timestamp=RECEIVED_AT)
        self.enterContext(patch.object(main, 'logger'))
        self.enterContext(patch.object(main.Settings, 'load', side_effect=AssertionError('No production settings')))

    def assert_webp(self, event, entries):
        self.assertTrue(event.verify())
        tags = [tag.to_vec() for tag in event.tags()]
        self.assertEqual(len(tags), 1)
        url, = [value[4:] for value in tags[0] if value.startswith('url ')]
        self.assertTrue(url.endswith('.w1.webp'))
        self.assertEqual(protocol.parse_path(urlsplit(url).path, {'test': KEY}), entries)
        self.assertEqual(tags, [['imeta', 'url ' + url, 'm image/webp', 'alt ' + image_alt_text(entries)]])
        self.assertIn('\n\n' + url + '\n---\n', event.content())
        return url

    async def test_real_service_signs_matching_url_mime_alt_and_unchanged_text(self):
        self.assertEqual(await self.service.accept(self.report), 'queued')
        record = self.outbox.next_due(self.clock())
        event = Event.from_json(record['event_json'])
        entries = (image_entry(self.report),)
        url = self.assert_webp(event, entries)
        self.assertEqual(event.content(), with_image(main.report_message(self.report), url))
        self.assertTrue(event.content().endswith('NMEA: ' + DCR_NMEA))
        self.assertTrue(await self.service.publish_one())
        self.assertEqual(self.client.send.await_args.args[0].as_json(), record['event_json'])

    async def test_collected_typhoon_has_one_webp_for_all_received_points(self):
        reports = tuple(azarashi.decode(nmea, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
                        for nmea, ms in zip(TYPHOON_NMEA, (1794228531340, 1794228535353)))
        for report in reports:
            self.assertEqual(await self.service.accept(report), 'buffered')
        self.assertFalse(await self.service.publish_one())
        self.clock.now += 180
        self.assertTrue(await self.service.publish_one())
        event = self.client.send.await_args.args[0]
        self.assert_webp(event, tuple(image_entry(report) for report in reports))
        self.assertIn('受信した2時点', event.content())
        self.assertEqual(event.content().partition('\n---\n')[2].splitlines(),
                         ['NMEA: ' + report.nmea for report in reports])

    async def test_incomplete_nankai_stays_text_only_then_completed_heading_uses_webp(self):
        pages = (nankai_page(b'A' * 18, number=1, total=2), nankai_page(b'B' * 6, number=2, total=2))
        self.assertEqual(await self.service.accept(pages[0]), 'buffered')
        self.clock.now += 180
        self.assertTrue(await self.service.publish_one())
        notice = self.client.send.await_args.args[0]
        self.assertIn('【本文未受信】', notice.content())
        self.assertEqual(notice.tags(), [])
        self.assertEqual(await self.service.accept(pages[1]), 'buffered')
        self.assertTrue(await self.service.publish_one())
        completed = self.client.send.await_args.args[0]
        self.assert_webp(completed, (image_entry(pages[0]),))
        self.assertIn('A' * 18 + 'B' * 6, completed.content())
        self.assertTrue(all('NMEA: ' + page.nmea in completed.content() for page in pages))

    async def test_disabled_images_and_invalid_inputs_preserve_text_only(self):
        self.service.images = None
        plain = self.service._build_event(main.report_message(self.report), (image_entry(self.report),))
        self.assertEqual(plain.tags(), [])
        self.assertEqual(plain.content(), main.report_message(self.report))
        self.service.images = publisher('w1')
        with patch.object(ImagePublisher, 'url', side_effect=ValueError('invalid input')):
            failed = self.service._build_event(main.report_message(self.report), ())
        self.assertEqual(failed.tags(), [])
        self.assertEqual(failed.content(), plain.content())

    async def test_saved_png_and_webp_retries_survive_format_change_and_restart_unchanged(self):
        for old_encoding, new_encoding in (('png', 'w1'), ('w1', 'png')):
            with self.subTest(old_encoding=old_encoding):
                self.outbox.close()
                database = self.path.with_name(old_encoding + '.sqlite3')
                self.outbox = Outbox(database, self.client.identity)
                self.service = main.Service(self.client, self.outbox, self.stop, clock=self.clock,
                                            images=publisher(old_encoding))
                self.assertEqual(await self.service.accept(self.report), 'queued')
                original = self.outbox.next_due(self.clock())
                self.client.send.reset_mock(side_effect=True)
                self.client.send.side_effect = main.DeliveryError('synthetic relay failure')
                self.assertTrue(await self.service.publish_one())
                self.outbox.close()
                self.outbox = Outbox(database, self.client.identity)
                self.service = main.Service(self.client, self.outbox, self.stop, clock=self.clock,
                                            images=publisher(new_encoding))
                self.clock.now += 1
                self.client.send.side_effect = None
                with patch.object(ImagePublisher, 'url', side_effect=AssertionError('No new URL on retry')), \
                        patch.object(main, 'image_alt_text', side_effect=AssertionError('No new ALT on retry')):
                    self.assertTrue(await self.service.publish_one())
                self.assertEqual(self.client.send.await_count, 2)
                for sent in self.client.send.await_args_list:
                    event = sent.args[0]
                    self.assertTrue(event.verify())
                    self.assertEqual(event.as_json(), original['event_json'])
                    self.assertEqual(event.id().to_hex(), original['event_id'])


if __name__ == '__main__':
    unittest.main()
