"""Nankai posts preserve assembled text without creating a new image URL."""

import asyncio
import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import azarashi
from azarashi.reports import dcr

from shirasu import service as main
from shirasu.images.publication import ImagePublisher, image_entry, with_image
from shirasu.reports.format import ATTRIBUTION, report_message
from shirasu.outbox import Outbox
from tests.receiver import test_main as service_helpers
from tests.receiver import test_typhoon as encoded_fixtures


ISSUED_AT = datetime(2026, 11, 10, 0, 10, tzinfo=timezone.utc)


def nankai_page(content=b'', *, number=1, total=1, classification=7,
                 information=0, serial=5, issued_at=ISSUED_AT):
    """Build a real decoded frame with both protocol checksums recalculated."""
    if len(content) > 18:
        raise ValueError('One Nankai page has only 18 text bytes')
    fields = [(14, 3, classification), (17, 4, 4),
              (21, 4, issued_at.month), (25, 5, issued_at.day),
              (30, 5, issued_at.hour), (35, 6, issued_at.minute),
              (41, 2, information), (43, 171, 0), (53, 4, serial),
              (201, 6, number), (207, 6, total)]
    fields.extend((57 + index * 8, 8, value) for index, value in enumerate(content))
    return encoded_fixtures.report(changes=fields, received_ms=round(issued_at.timestamp() * 1000))


def isolate_assembly(test):
    for name, value in (('completed', False), ('reports', {}), ('announcement', None)):
        test.enterContext(patch.object(dcr.NankaiTroughEarthquake, name, value))


class NankaiTextTests(unittest.TestCase):
    def setUp(self):
        isolate_assembly(self)

    def test_single_page_preserves_the_original_text_under_a_clear_header(self):
        report = nankai_page('訓練です。'.encode(), classification=3)
        self.assertIsInstance(report, dcr.NankaiTroughEarthquake)
        self.assertEqual(report_message(report),
                         '【南海トラフ地震関連情報】発表（通常）\n'
                         '情報種別：巨大地震注意\n'
                         '発表時刻：11月10日 09:10 JST\n\n'
                         f'訓練です。\n\n{ATTRIBUTION}\n---\nNMEA: ' + report.nmea)

    def test_training_correction_cancellation_and_priority_remain_explicit(self):
        cases = (
            (2, 0, '【南海トラフ地震関連情報】発表（優先）'),
            (1, 1, '【南海トラフ地震関連情報】訂正（最優先）'),
            (7, 0, '【訓練・試験】南海トラフ地震関連情報（発表）'),
            (7, 1, '【訓練・試験】南海トラフ地震関連情報（訂正）'),
            (2, 2, '【取消】南海トラフ地震関連情報（優先）'),
            (7, 2, '【訓練・試験】【取消】南海トラフ地震関連情報'),
        )
        for classification, information, header in cases:
            with self.subTest(classification=classification, information=information):
                report = nankai_page(b'original', classification=classification,
                                     information=information)
                text = report_message(report)
                self.assertEqual(text.splitlines()[0], header)
                self.assertIn(f'\n\noriginal\n\n{ATTRIBUTION}\n---\n', text)
                if information == 2:
                    self.assertNotIn('発表されました', text)

    def test_undefined_codes_are_retained_instead_of_being_relabelled(self):
        report = nankai_page(b'original', classification=4, information=3, serial=7)
        text = report_message(report)
        for label in (report.report_classification, report.information_type,
                      report.information_serial_code):
            self.assertIn(label, text)
        self.assertIn('地震関連情報(コード番号：7)', text)

    def test_reversed_pages_join_utf8_bytes_before_formatting_without_truncation(self):
        original = 'A訓練用の架空の情報です。\n原文の改行を保持。'
        encoded = original.encode()
        chunks = [encoded[start:start + 18] for start in range(0, len(encoded), 18)]
        with self.assertRaises(UnicodeDecodeError):
            chunks[0].decode('utf-8')  # The fixture must split a multibyte character.
        for count, index in enumerate(reversed(range(len(chunks))), start=1):
            report = nankai_page(chunks[index], number=index + 1, total=len(chunks))
            text = report_message(report)
            if count < len(chunks):
                self.assertIn(f'受信中 ({index + 1}) [{count}/{len(chunks)}]', text)
                self.assertNotIn(original, text)
        self.assertIn('\n\n' + original + f'\n\n{ATTRIBUTION}\n---\n', text)
        self.assertNotIn('受信中', text)
        self.assertNotIn('\ufffd', text)
        self.assertTrue(text.endswith('NMEA: ' + report.nmea))

    def test_invalid_page_position_keeps_the_decoder_explanation(self):
        report = nankai_page(b'original', number=0, total=2)
        self.assertIn(report.extract_text_information(), report_message(report))
        self.assertIn('ページ番号・総ページ数(コード番号：2)', report_message(report))
        self.assertNotIn('\n\noriginal', report_message(report))

    def test_jst_year_boundary_and_formatting_do_not_mutate_assembly(self):
        report = nankai_page(b'original', issued_at=datetime(2026, 12, 31, 16, 5,
                                                            tzinfo=timezone.utc))
        before = copy.deepcopy(report.__dict__)
        pages = dict(dcr.NankaiTroughEarthquake.reports)
        announcement = dcr.NankaiTroughEarthquake.announcement
        self.assertIn('発表時刻：1月1日 01:05 JST', report_message(report))
        self.assertEqual(report.__dict__, before)
        self.assertEqual(dcr.NankaiTroughEarthquake.reports, pages)
        self.assertEqual(dcr.NankaiTroughEarthquake.announcement, announcement)
        self.assertTrue(dcr.NankaiTroughEarthquake.completed)


class NankaiPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate_assembly(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.clock = service_helpers.Clock()
        self.nostr = service_helpers.fake_nostr()
        self.outbox = Outbox(self.path, self.nostr.identity)
        self.addCleanup(lambda: self.outbox.close())
        self.stop = asyncio.Event()
        self.images = ImagePublisher('https://images.example.test', 'k1', bytes(range(32)))
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock,
                                    images=self.images)
        self.enterContext(patch.object(main, 'logger'))

    def reopen(self, *, images=True, **options):
        self.outbox.close()
        self.outbox = Outbox(self.path, self.nostr.identity, **options)
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock,
                                    images=self.images if images else None)

    async def test_new_posts_are_text_only_with_images_enabled_or_disabled(self):
        for enabled in (True, False):
            with self.subTest(images_enabled=enabled):
                self.reopen(images=enabled)
                self.service.state['last_image_error'] = 'prior unrelated image failure'
                report = nankai_page(b'enabled' if enabled else b'disabled',
                                     issued_at=ISSUED_AT.replace(minute=10 if enabled else 20))
                with patch.object(main, 'image_entry', side_effect=AssertionError('No image input')), \
                        patch.object(ImagePublisher, 'url', side_effect=AssertionError('No image URL')):
                    self.assertEqual(await self.service.accept(report), 'buffered')
                    self.assertIsNone(self.outbox.next_due(self.clock()))
                    self.assertTrue(await self.service.publish_one())
                    event = self.nostr.send.await_args.args[0]
                    self.assertEqual(event.content(), report_message(report))
                    self.assertEqual(event.tags(), [])
                    self.assertTrue(event.verify())
                    self.assertNotIn('https://images.example.test', event.content())
                    self.assertFalse(await self.service.publish_one())
                self.assertEqual(self.service.state['last_image_error'], 'prior unrelated image failure')

    async def test_reversed_utf8_pages_survive_restart_and_form_one_post_without_images(self):
        self.reopen(images=False)
        original = 'A訓練用の架空の情報です。'
        encoded = original.encode()
        chunks = [encoded[start:start + 18] for start in range(0, len(encoded), 18)]
        received = []
        for count, index in enumerate(reversed(range(len(chunks))), start=1):
            report = nankai_page(chunks[index], number=index + 1, total=len(chunks))
            received.append(report)
            self.assertEqual(await self.service.accept(report), 'buffered')
            if count < len(chunks):
                self.assertFalse(await self.service.publish_one())
                self.nostr.build_event.assert_not_called()
                self.reopen(images=False)
                # A real restart empties decoder-global state. A later
                # announcement can then replace it before the old page arrives.
                dcr.NankaiTroughEarthquake.reports = {}
                dcr.NankaiTroughEarthquake.announcement = None
                dcr.NankaiTroughEarthquake.completed = False
                nankai_page(b'unrelated', number=1, total=2,
                            issued_at=datetime(2026, 11, 10, 1, tzinfo=timezone.utc))
                self.assertEqual(await self.service.accept(report), 'duplicate')
            else:
                self.assertTrue(await self.service.publish_one())
                event = self.nostr.send.await_args.args[0]
                self.assertIn('\n\n' + original + f'\n\n{ATTRIBUTION}\n---\n', event.content())
                self.assertEqual(event.tags(), [])
                self.assertTrue(event.verify())
                self.assertNotIn('受信中', event.content())
                self.assertNotIn('\ufffd', event.content())
                self.assertNotIn('unrelated', event.content())
                for page in received:
                    self.assertIn('NMEA: ' + page.nmea, event.content())
        self.nostr.send.assert_awaited_once()
        saved = event.as_json()
        self.reopen(images=False)
        for report in received:
            self.assertEqual(await self.service.accept(report), 'duplicate')
        self.assertFalse(await self.service.publish_one())
        rows = self.outbox.connection.execute('SELECT event_json FROM events').fetchall()
        self.assertEqual([row['event_json'] for row in rows], [saved])

    async def test_timeout_notifies_once_without_a_fragment_then_late_completion_posts_full_text(self):
        self.reopen(images=False)
        first = nankai_page(b'PARTIAL_TEXT_ONLY', number=1, total=2)
        self.assertEqual(await self.service.accept(first), 'buffered')
        self.assertFalse(await self.service.publish_one())
        self.clock.now += 100
        self.assertEqual(await self.service.accept(first), 'duplicate')
        self.reopen(images=False)
        self.clock.now = 1179.9
        self.assertFalse(await self.service.publish_one())
        self.clock.now = 1180
        self.assertTrue(await self.service.publish_one())
        partial = self.nostr.send.await_args.args[0]
        self.assertIn('【本文未受信】', partial.content())
        self.assertIn('受信1/2ページ', partial.content())
        self.assertIn('不足ページ：2', partial.content())
        self.assertNotIn('PARTIAL_TEXT_ONLY', partial.content())
        self.assertEqual(partial.tags(), [])
        self.assertFalse(await self.service.publish_one())

        self.reopen(images=False)
        self.clock.now += 20
        self.assertEqual(await self.service.accept(first), 'duplicate')
        self.assertFalse(await self.service.publish_one())
        last = nankai_page(b'COMPLETED', number=2, total=2)
        self.assertEqual(await self.service.accept(last), 'buffered')
        self.assertTrue(await self.service.publish_one())
        completed = self.nostr.send.await_args.args[0]
        self.assertIn('【追加更新】', completed.content())
        self.assertIn(f'\n\nPARTIAL_TEXT_ONLYCOMPLETED\n\n{ATTRIBUTION}\n---\n', completed.content())
        self.assertNotIn('【本文未受信】', completed.content())
        for report in (first, last):
            self.assertIn('NMEA: ' + report.nmea, completed.content())
            self.assertEqual(await self.service.accept(report), 'duplicate')
        self.assertEqual(completed.tags(), [])
        self.assertFalse(await self.service.publish_one())
        self.assertEqual(self.nostr.send.await_count, 2)

    async def test_nankai_typhoon_and_immediate_reports_share_capacity(self):
        self.reopen(max_pending=2)
        page = nankai_page(b'PARTIAL_TEXT_ONLY', number=1, total=2)
        typhoon = encoded_fixtures.report()
        self.assertEqual(await self.service.accept(page), 'buffered')
        self.assertEqual(await self.service.accept(typhoon), 'buffered')
        self.assertEqual(self.outbox.pending_count(), 2)
        immediate = azarashi.decode(service_helpers.MARINE_NMEA, timestamp=ISSUED_AT)
        self.assertEqual(await self.service.accept(immediate), 'full')
        self.assertEqual(await self.service.accept(page), 'duplicate')
        self.assertEqual(await self.service.accept(typhoon), 'duplicate')
        self.assertEqual(self.outbox.pending_count(), 2)
        self.assertEqual(self.service.state['dropped_full'], 1)

    async def test_cancellation_is_immediate_and_suppresses_unfinished_original_text(self):
        original = nankai_page(b'UNFINISHED_TEXT', number=1, total=2)
        self.assertEqual(await self.service.accept(original), 'buffered')
        self.assertFalse(await self.service.publish_one())
        cancelled = nankai_page(b'CANCEL_FRAGMENT', number=1, total=2, information=2)
        self.assertEqual(await self.service.accept(cancelled), 'buffered')
        self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        self.assertIn('【取消】', event.content())
        self.assertIn('この情報は取り消されました。', event.content())
        self.assertNotIn('UNFINISHED_TEXT', event.content())
        self.assertNotIn('CANCEL_FRAGMENT', event.content())
        self.assertEqual(event.tags(), [])
        self.clock.now += 180
        self.assertFalse(await self.service.publish_one())
        self.nostr.send.assert_awaited_once()

    async def test_saved_image_event_keeps_its_signature_through_duplicate_retry_and_restart(self):
        report = nankai_page('訓練です。'.encode())
        old_url = self.images.url((image_entry(report),))
        old_content = with_image(f'{report}\n---\nNMEA: {report.nmea}', old_url)
        old_event = self.nostr.build_event(old_content, image_url=old_url)
        old_json = old_event.as_json()
        self.outbox.enqueue(main.report_key(report), old_json, old_event.id().to_hex(), self.clock())
        self.assertNotEqual(old_content, report_message(report))
        with patch.object(ImagePublisher, 'url', side_effect=AssertionError('Keep saved image URL')):
            self.assertEqual(await self.service.accept(report), 'duplicate')
            self.assertEqual(self.outbox.next_due(self.clock())['event_json'], old_json)
            self.nostr.send.side_effect = main.DeliveryError('relay unavailable')
            self.assertTrue(await self.service.publish_one())
            self.reopen()
            self.clock.now += 2
            self.nostr.send.side_effect = None
            self.assertEqual(await self.service.accept(report), 'duplicate')
            self.assertTrue(await self.service.publish_one())
            self.assertEqual(await self.service.accept(report), 'duplicate')
            self.assertFalse(await self.service.publish_one())
        self.assertEqual(self.nostr.send.await_count, 2)
        for call in self.nostr.send.await_args_list:
            self.assertEqual(call.args[0].as_json(), old_json)
            self.assertTrue(call.args[0].verify())
        self.assertEqual(self.outbox.snapshot(self.clock())['counts'],
                         {'pending': 0, 'sent': 1, 'expired': 0})


if __name__ == '__main__':
    unittest.main()
