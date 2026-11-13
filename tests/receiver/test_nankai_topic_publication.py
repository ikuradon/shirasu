"""Unpublished Nankai heading images preserve complete text and signed retries.

Only ephemeral signing keys, fresh SQLite databases and mocked Nostr transport
are used. Signed image URLs are inspected locally and never fetched.
"""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from shirasu import service as main
from shirasu.images.alt import image_alt_text
from shirasu.images.protocol import MAX_REPORTS, ProtocolError, parse_path
from shirasu.images.publication import ImagePublisher, image_entry
from shirasu.outbox import Outbox
from shirasu.reports.format import ATTRIBUTION
from tests.receiver import test_main as service_helpers
from tests.receiver.test_nankai_publication import isolate_assembly, nankai_page


KEY = bytes(range(32))


def pages_for(text, *, serial=5):
    content = text.encode('utf-8')
    chunks = [content[offset:offset + 18] for offset in range(0, len(content), 18)]
    return tuple(nankai_page(chunk, number=index + 1, total=len(chunks), serial=serial)
                 for index, chunk in enumerate(chunks))


class NankaiTopicPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate_assembly(self)
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-nankai-topic-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'shirasu.outbox.sqlite3'
        self.clock = service_helpers.Clock()
        self.nostr = service_helpers.fake_nostr()
        self.publisher = ImagePublisher('https://images.example.test', 'topic', KEY)
        self.images = SimpleNamespace(url=Mock(side_effect=self.publisher.url), supports_nankai_topic=True)
        self.outbox = Outbox(self.path, self.nostr.identity)
        self.addCleanup(lambda: self.outbox.close())
        self.stop = asyncio.Event()
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock, images=self.images)
        self.enterContext(patch.object(main, 'logger'))
        self.enterContext(patch.object(main.Settings, 'load', side_effect=AssertionError('No production settings')))

    def reopen(self, images):
        self.outbox.close()
        self.outbox = Outbox(self.path, self.nostr.identity)
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock, images=images)

    async def accept(self, reports):
        for report in reports:
            self.assertEqual(await self.service.accept(report), 'buffered')

    def assert_image(self, event, representative):
        self.assertTrue(event.verify())
        tags = [tag.to_vec() for tag in event.tags()]
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0][0], 'imeta')
        url, = [field[4:] for field in tags[0][1:] if field.startswith('url ')]
        self.assertIn('\n\n' + url + '\n---\n', event.content())
        entries = parse_path(urlsplit(url).path, {'topic': KEY})
        self.assertEqual(entries, (image_entry(representative),))
        self.assertIn('alt ' + image_alt_text(entries), tags[0])
        self.assertIn('m image/png', tags[0])
        return event.content().replace('\n\n' + url, '', 1)

    def assert_full_text(self, text, body, reports):
        message, separator, originals = text.partition('\n---\n')
        self.assertEqual(separator, '\n---\n')
        self.assertEqual(message.split('\n\n', 1)[1], body + '\n\n' + ATTRIBUTION)
        self.assertEqual(originals.splitlines(), ['NMEA: ' + report.nmea for report in reports])

    async def test_complete_training_text_is_assembled_before_one_heading_attachment(self):
        body = '訓練用A：巨大地震注意の表示を確認する架空の本文です。'
        reports = pages_for(body)
        with self.assertRaises(UnicodeDecodeError):
            reports[0].text_information.decode('utf-8')
        for report in reversed(reports[1:]):
            await self.accept((report,))
            self.assertFalse(await self.service.publish_one())
        self.images.url.assert_not_called()
        await self.accept(reports[:1])
        self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        text = self.assert_image(event, reports[0])
        self.assert_full_text(text, body, reports)
        self.assertIn('【訓練・試験】', text)
        self.assertIn('巨大地震注意', text)
        self.images.url.assert_called_once_with((image_entry(reports[0]),))
        self.assertEqual(self.service.nankai.waiting_count(), 0)
        self.assertFalse(await self.service.publish_one())

    async def test_investigation_ended_remains_an_issued_result_instead_of_release(self):
        body = '訓練用B：調査終了の表示を確認する架空の本文です。'
        reports = pages_for(body, serial=6)
        await self.accept(reports)
        self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        text = self.assert_image(event, reports[0])
        self.assert_full_text(text, body, reports)
        self.assertIn('調査終了', text)
        self.assertNotIn('【解除】', text)
        self.assertNotIn('【解除】', str([tag.to_vec() for tag in event.tags()]))

    async def test_incomplete_notice_has_no_image_and_late_completion_adds_one(self):
        body = 'A訓練用の本文です。'
        reports = pages_for(body)
        self.assertEqual(len(reports), 2)
        await self.accept(reports[:1])
        self.clock.now += 180
        self.assertTrue(await self.service.publish_one())
        notice = self.nostr.send.await_args.args[0]
        notice_json = notice.as_json()
        self.assertEqual(notice.tags(), [])
        self.assertIn('【本文未受信】', notice.content())
        self.assertNotIn(body, notice.content())
        self.images.url.assert_not_called()
        await self.accept(reports[1:])
        self.assertTrue(await self.service.publish_one())
        completed = self.nostr.send.await_args.args[0]
        text = self.assert_image(completed, reports[0])
        self.assert_full_text(text, body, reports)
        self.assertIn('【追加更新】', text)
        self.assertNotIn('【本文未受信】', text)
        saved = self.outbox.connection.execute('SELECT event_json FROM events WHERE event_id = ?',
                                                (notice.id().to_hex(),)).fetchone()
        self.assertEqual(saved['event_json'], notice_json)
        self.assertEqual(self.nostr.send.await_count, 2)

    async def test_cancellation_attaches_immediately_without_waiting_for_missing_pages(self):
        original = nankai_page(b'ORIGINAL_FRAGMENT', number=1, total=2)
        cancelled = nankai_page(b'CANCEL_FRAGMENT', number=1, total=2, information=2)
        await self.accept((original,))
        self.assertFalse(await self.service.publish_one())
        await self.accept((cancelled,))
        self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        text = self.assert_image(event, cancelled)
        self.assertIn('【訓練・試験】【取消】', text)
        self.assertIn('この情報は取り消されました。', text)
        self.assertNotIn('ORIGINAL_FRAGMENT', text)
        self.assertNotIn('CANCEL_FRAGMENT', text)
        self.assertNotIn('【本文未受信】', text)
        self.assertEqual(text.partition('\n---\n')[2], 'NMEA: ' + cancelled.nmea)
        alt = next(field[4:] for field in event.tags()[0].to_vec() if field.startswith('alt '))
        self.assertIn('【取消】', alt)
        self.assertNotIn('巨大地震注意', alt)
        self.clock.now += 180
        self.assertFalse(await self.service.publish_one())

    async def test_sixty_three_pages_preserve_all_text_but_use_one_url_record(self):
        self.assertGreater(63, MAX_REPORTS)
        chunks = tuple(f'Training page {page:02d}.'.encode().ljust(18, b' ') for page in range(1, 64))
        reports = tuple(nankai_page(chunk, number=page, total=63)
                        for page, chunk in enumerate(chunks, 1))
        await self.accept(reports)
        self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        text = self.assert_image(event, reports[0])
        self.assert_full_text(text, b''.join(chunks).decode(), reports)
        self.assertEqual(text.count('\nNMEA: '), 63)
        self.images.url.assert_called_once_with((image_entry(reports[0]),))

    async def test_invalid_complete_utf8_stays_text_only_with_original_pages(self):
        reports = (nankai_page(b'\xff', number=1, total=2),
                   nankai_page(b'TEST', number=2, total=2))
        await self.accept(reports)
        self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        self.assertTrue(event.verify())
        self.assertIn('本文の文字コードを読み取れません', event.content())
        self.assertEqual(event.tags(), [])
        self.assertEqual(event.content().count('\nNMEA: '), 2)
        self.images.url.assert_not_called()

    async def test_image_url_failure_preserves_full_text_and_completes_collection(self):
        body = '訓練です。'
        reports = pages_for(body)
        self.images.url.side_effect = ProtocolError('test URL encoding failure')
        await self.accept(reports)
        self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        self.assertEqual(event.tags(), [])
        self.assert_full_text(event.content(), body, reports)
        self.assertEqual(self.service.state['last_image_error'], 'ProtocolError')
        self.assertEqual(self.service.nankai.waiting_count(), 0)

    async def test_metadata_selection_failure_preserves_readable_text(self):
        body = '訓練です。'
        reports = pages_for(body)
        await self.accept(reports)
        with patch.object(main, 'nankai_topic_entries', side_effect=ValueError('metadata mismatch')):
            self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        self.assertEqual(event.tags(), [])
        self.assert_full_text(event.content(), body, reports)
        self.images.url.assert_not_called()

    async def test_current_public_versions_never_enable_the_unpublished_topic(self):
        for version in ('v1', 'v2', 'v3', 'v4'):
            self.assertFalse(ImagePublisher('https://images.example.test', 'topic', KEY,
                                            version=version).supports_nankai_topic)
        self.reopen(self.publisher)
        reports = pages_for('訓練です。')
        with patch.object(main, 'nankai_topic_entries', side_effect=AssertionError('Public topic disabled')), \
                patch.object(ImagePublisher, 'url', side_effect=AssertionError('No topic URL')):
            await self.accept(reports)
            self.assertTrue(await self.service.publish_one())
        self.assertEqual(self.nostr.send.await_args.args[0].tags(), [])

    async def test_restart_and_capability_change_reuse_the_saved_signed_image_event(self):
        reports = pages_for('訓練です。')
        await self.accept(reports)
        self.nostr.send.side_effect = main.DeliveryError('test relay unavailable')
        self.assertTrue(await self.service.publish_one())
        original = self.nostr.send.await_args.args[0]
        self.assert_image(original, reports[0])
        saved_json = original.as_json()
        # A published-only capability and new URL key cannot rewrite a retry.
        self.reopen(ImagePublisher('https://new-images.example.test', 'rotated', b'r' * 32))
        self.clock.now += 2
        self.nostr.send.side_effect = None
        with patch.object(main, 'nankai_topic_entries', side_effect=AssertionError('No image selection on retry')), \
                patch.object(main, 'image_alt_text', side_effect=AssertionError('No ALT regeneration')), \
                patch.object(ImagePublisher, 'url', side_effect=AssertionError('No URL regeneration')):
            self.assertEqual(await self.service.accept(reports[0]), 'duplicate')
            self.assertTrue(await self.service.publish_one())
        self.assertEqual(self.nostr.send.await_count, 2)
        for call in self.nostr.send.await_args_list:
            self.assertEqual(call.args[0].as_json(), saved_json)
            self.assertTrue(call.args[0].verify())
        self.assertEqual(self.outbox.snapshot(self.clock())['counts'],
                         {'pending': 0, 'sent': 1, 'expired': 0})


if __name__ == '__main__':
    unittest.main()
