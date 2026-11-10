"""Replay received broadcasts without network access or production keys."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from azarashi.reports import dcr

import main
from image_publication import ImagePublisher
from outbox import Outbox
from qzqsm_samples import decode_reports, expected_nankai_text
import test_main as service_helpers


class PublicQzqsmPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.clock = service_helpers.Clock()
        self.nostr = service_helpers.fake_nostr()
        self.stop = asyncio.Event()
        self.images = ImagePublisher('https://images.example.test', 'k1', bytes(range(32)))
        self.outbox = Outbox(self.path, self.nostr.identity)
        self.addCleanup(lambda: self.outbox.close())
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock,
                                    images=self.images)
        self.enterContext(patch.object(main, 'logger'))
        for name, value in (('completed', False), ('reports', {}), ('announcement', None)):
            self.enterContext(patch.object(dcr.NankaiTroughEarthquake, name, value))

    def reopen(self, *, images=True):
        self.outbox.close()
        self.outbox = Outbox(self.path, self.nostr.identity)
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock,
                                    images=self.images if images else None)

    def assert_complete_body(self, event, name, pages):
        body, separator, records = event.content().partition('\n---\n')
        self.assertEqual(separator, '\n---\n')
        self.assertEqual(body.split('\n\n', 1)[1], expected_nankai_text(name))
        self.assertIn('【訓練・試験】南海トラフ地震関連情報（発表）', body)
        self.assertIn('情報種別：巨大地震注意', body)
        self.assertNotIn('受信中', body)
        self.assertNotIn('\ufffd', body)
        self.assertEqual(records.splitlines(),
                         ['NMEA: ' + page.nmea for page in sorted(pages, key=lambda r: r.page_number)])
        self.assertEqual(event.tags(), [])
        self.assertTrue(event.verify())

    async def test_all_27_pages_form_one_post_after_restart_in_received_or_reverse_order(self):
        for name, reverse, date in (
                ('dcr_archive_20260821', False, '8月21日'),
                ('dcr_receiver_20260924', True, '9月24日')):
            with self.subTest(recording=name):
                pages = [report for report in decode_reports(name)
                         if isinstance(report, dcr.NankaiTroughEarthquake)]
                self.assertEqual(len(pages), 27)
                order = list(reversed(pages)) if reverse else pages
                sent_before = self.nostr.send.await_count
                # The service must use its durable pages, never decoder-global assembly.
                with patch.object(dcr.NankaiTroughEarthquake, 'extract_text_information',
                                  side_effect=AssertionError('Decoder assembly is not durable')):
                    for index, page in enumerate(order):
                        self.assertEqual(await self.service.accept(page), 'buffered')
                        self.assertEqual(await self.service.accept(page), 'duplicate')
                        self.assertEqual(await self.service.publish_one(), index == len(order) - 1)
                        if index == 12:
                            self.reopen(images=not reverse)
                    self.assertEqual(self.nostr.send.await_count, sent_before + 1)
                    event = self.nostr.send.await_args.args[0]
                    self.assert_complete_body(event, name, pages)
                    self.assertIn(f'発表時刻：{date} 10:35 JST', event.content())
                    saved = event.as_json()
                    self.reopen(images=not reverse)
                    for page in pages:
                        self.assertEqual(await self.service.accept(page), 'duplicate')
                    self.assertFalse(await self.service.publish_one())
                    self.assertEqual(self.nostr.send.await_count, sent_before + 1)
                    row = self.outbox.connection.execute(
                        'SELECT event_json FROM events WHERE event_id = ?',
                        (event.id().to_hex(),)).fetchone()
                    self.assertEqual(row['event_json'], saved)

    async def test_missing_real_page_notifies_once_and_late_page_completes_exact_text(self):
        name = 'dcr_receiver_20260924'
        self.reopen(images=False)
        pages = [report for report in decode_reports(name)
                 if isinstance(report, dcr.NankaiTroughEarthquake)]
        missing = next(report for report in pages if report.page_number == 17)
        received = [report for report in pages if report.page_number != 17]
        for page in received:
            self.assertEqual(await self.service.accept(page), 'buffered')
        self.clock.now = 1179.9
        self.assertEqual(await self.service.accept(received[0]), 'duplicate')
        self.assertFalse(await self.service.publish_one())
        self.reopen(images=False)
        self.clock.now = 1180
        self.assertTrue(await self.service.publish_one())
        notice = self.nostr.send.await_args.args[0]
        body = notice.content().partition('\n---\n')[0]
        self.assertIn('【本文未受信】', body)
        self.assertIn('受信26/27ページ', body)
        self.assertIn('不足ページ：17', body)
        self.assertNotIn(expected_nankai_text(name)[:18], body)
        self.assertEqual(notice.tags(), [])
        self.assertTrue(notice.verify())
        self.assertFalse(await self.service.publish_one())
        self.reopen(images=False)
        self.clock.now += 20
        self.assertFalse(await self.service.publish_one())
        self.assertEqual(await self.service.accept(missing), 'buffered')
        self.assertTrue(await self.service.publish_one())
        completed = self.nostr.send.await_args.args[0]
        self.assert_complete_body(completed, name, pages)
        self.assertIn('【追加更新】', completed.content())
        self.assertFalse(await self.service.publish_one())
        self.assertEqual(self.nostr.send.await_count, 2)

    async def test_dcx_recording_remains_publishable_and_retains_each_original_frame(self):
        # DCX is eligible text. Null/unknown messages, tested in preview separately,
        # are the excluded types; DCX must not be silently dropped as a whole.
        self.reopen(images=False)
        reports = decode_reports('dcx_archive_20260917')
        self.assertEqual(len(reports), 211)
        self.assertEqual({type(report).__name__ for report in reports}, {'LAlert', 'JAlert', 'MTInfo'})
        for report in reports:
            with self.subTest(kind=type(report).__name__, nmea=report.nmea):
                self.assertTrue(main.is_publishable(report))
                self.assertEqual(await self.service.accept(report), 'queued')
                self.assertTrue(await self.service.publish_one())
                event = self.nostr.send.await_args.args[0]
                self.assertEqual(event.content(), f'{report}\n---\nNMEA: {report.nmea}')
                self.assertTrue(event.verify())
                self.assertEqual(event.tags(), [])
        self.assertEqual(self.nostr.send.await_count, 211)
        self.reopen(images=False)
        for report in reports:
            self.assertEqual(await self.service.accept(report), 'duplicate')
        self.assertFalse(await self.service.publish_one())
        self.assertEqual(self.nostr.send.await_count, 211)


if __name__ == '__main__':
    unittest.main()
