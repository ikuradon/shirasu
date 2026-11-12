"""Nankai topic selection preserves raw provenance and complete-text gating."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import azarashi
from azarashi.reports import dcr

from shirasu.reports.nankai import NankaiCollector, _stored_frame
from shirasu.images.nankai import header, is_nankai_entry, nankai_topic_entries
from shirasu.outbox import Outbox
from tests.qzqsm_samples import decode_reports
from tools.preview.gallery_catalog import _field_variant


def changed(entry, fields):
    return _field_variant(entry[0], fields), entry[1]


def decode(entry):
    return azarashi.decode(entry[0], timestamp=datetime.fromtimestamp(entry[1] / 1000, timezone.utc))


class NankaiImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = tuple(sorted((report for report in decode_reports('dcr_receiver_20260924')
                                   if isinstance(report, dcr.NankaiTroughEarthquake)),
                                  key=lambda report: report.page_number))
        cls.entries = tuple((report.nmea, round(report.timestamp.timestamp() * 1000))
                            for report in cls.reports)

    def setUp(self):
        owner = dcr.NankaiTroughEarthquake
        saved = {name: getattr(owner, name) for name in ('completed', 'reports', 'announcement')}
        owner.completed, owner.reports, owner.announcement = False, {}, None
        for name, value in saved.items():
            self.addCleanup(setattr, owner, name, value)

    def batch(self, reports=None, *, due=180):
        with Outbox(':memory:', 'nankai-image-test') as box:
            collector = NankaiCollector(box)
            for report in self.reports if reports is None else reports:
                self.assertEqual(collector.accept(report, 0), 'buffered')
            batch = collector.due(due)
            self.assertIsNotNone(batch)
            return batch

    def test_public_27_page_headers_match_decoder_and_existing_storage_validation(self):
        self.assertEqual(len(self.entries), 27)
        for report, entry in zip(self.reports, self.entries, strict=True):
            with self.subTest(page=report.page_number):
                item = header(entry)
                self.assertTrue(is_nankai_entry(entry))
                self.assertEqual((item.issued_at, item.classification, item.information,
                                  item.serial, item.page, item.total),
                                 (report.report_time, report.report_classification_no,
                                  report.information_type_no, report.information_serial_code_raw,
                                  report.page_number, report.total_page))
                stored = _stored_frame(*entry)
                self.assertEqual((item.issued_at.timestamp(), item.classification, item.information,
                                  item.serial, item.page, item.total), stored[2:8])
        with self.assertRaises(FrozenInstanceError):
            header(self.entries[0]).serial = 4

    def test_keyword_title_and_markers_preserve_training_correction_and_cancellation(self):
        for serial, expected in ((1, '調査中'), (2, '調査中'), (3, '調査中'),
                                 (4, '巨大地震警戒'), (5, '巨大地震注意'),
                                 (6, '調査終了'), (15, 'その他の情報')):
            with self.subTest(serial=serial):
                item = header(changed(self.entries[0], ((53, 4, serial),)))
                self.assertEqual(item.keyword, expected)
                self.assertEqual(item.source_keyword, expected)
                self.assertEqual(item.title, '南海トラフ地震関連情報' if serial == 15 else '南海トラフ地震臨時情報')
        correction = header(changed(self.entries[0], ((41, 2, 1),)))
        self.assertEqual(correction.markers, ('訓練・試験', '訂正'))
        cancellation = header(changed(self.entries[0], ((41, 2, 2),)))
        self.assertEqual((cancellation.keyword, cancellation.source_keyword), ('取消', '巨大地震注意'))
        self.assertEqual(cancellation.markers, ('訓練・試験', '取消'))
        self.assertEqual(cancellation.title, '南海トラフ地震関連情報')

    def test_complete_selects_original_page_one_without_rewriting_receipt_context(self):
        batch = self.batch(tuple(reversed(self.reports)), due=0)
        self.assertEqual(batch.mode, 'complete')
        self.assertEqual(nankai_topic_entries(batch), (self.entries[0],))
        reversed_batch = replace(batch, entries=tuple(reversed(batch.entries)), pages=tuple(reversed(batch.pages)))
        self.assertEqual(nankai_topic_entries(reversed_batch), (self.entries[0],))

    def test_missing_text_has_no_image_and_late_completion_adds_one(self):
        with Outbox(':memory:', 'nankai-image-test') as box:
            collector = NankaiCollector(box)
            for report in self.reports[:-1]:
                collector.accept(report, 0)
            missing = collector.due(180)
            self.assertEqual(missing.mode, 'missing')
            self.assertEqual(nankai_topic_entries(missing), ())
            self.assertEqual(collector.complete(missing, '{}', 'missing-event', 180), 'queued')
            collector.accept(self.reports[-1], 181)
            complete = collector.due(181)
            self.assertTrue(complete.update)
            self.assertEqual(nankai_topic_entries(complete), (self.entries[0],))

    def test_cancellation_selects_minimum_received_page_immediately_without_complete_text(self):
        reports = tuple(decode(changed(self.entries[index], ((41, 2, 2),))) for index in (8, 4, 6))
        batch = self.batch(reports, due=0)
        self.assertEqual(batch.mode, 'cancelled')
        self.assertEqual(nankai_topic_entries(batch), (changed(self.entries[4], ((41, 2, 2),)),))
        self.assertEqual(len(batch.pages), 3)

    def test_rejects_mixed_metadata_missing_or_duplicate_pages_and_modified_body(self):
        batch = self.batch()
        invalid = (
            replace(batch, classification=3), replace(batch, information=1),
            replace(batch, serial=4), replace(batch, total=26), replace(batch, issued_at=batch.issued_at + 60),
            replace(batch, issued_at=float('nan')), replace(batch, issued_at=10 ** 400),
            replace(batch, entries=batch.entries[:-1], pages=batch.pages[:-1]),
            replace(batch, entries=(batch.entries[0],) + batch.entries[:-1]),
            replace(batch, pages=(0,) + batch.pages[1:]), replace(batch, body=b'changed'),
            replace(batch, mode='cancelled'), replace(batch, mode='unknown'),
            replace(batch, entries=(changed(batch.entries[0], ((53, 4, 4),)),) + batch.entries[1:]),
        )
        for value in invalid:
            with self.subTest(value=value.mode, fields=(value.classification, value.serial, value.total)):
                with self.assertRaises(ValueError):
                    nankai_topic_entries(value)

    def test_utf8_must_decode_after_joining_all_original_fragments(self):
        body = (b'A' + ('日' * 9).encode()).ljust(36, b'\0')
        entries = tuple(changed(self.entries[0], ((201, 6, page), (207, 6, 2),
                         *((57 + index * 8, 8, byte) for index, byte in enumerate(body[(page - 1) * 18:page * 18]))))
                        for page in (1, 2))
        reports = tuple(decode(entry) for entry in entries)
        batch = self.batch(reports)
        self.assertTrue(any(self._bad_utf8(report.text_information) for report in reports))
        self.assertTrue(nankai_topic_entries(batch))
        first = changed(entries[0], ((57, 8, 255),))
        invalid = replace(batch, entries=(first,) + batch.entries[1:], body=b'\xff' + batch.body[1:])
        self.assertEqual(nankai_topic_entries(invalid), ())

    @staticmethod
    def _bad_utf8(value):
        try:
            value.decode('utf-8')
        except UnicodeDecodeError:
            return True
        return False

    def test_63_page_text_can_select_one_image_entry_despite_protocol_bundle_limit(self):
        entries = tuple(changed(self.entries[0], ((201, 6, page), (207, 6, 63),
                         *((57 + index * 8, 8, 65) for index in range(18)))) for page in range(1, 64))
        reports = tuple(decode(entry) for entry in entries)
        batch = self.batch(reports, due=0)
        self.assertEqual(len(batch.entries), 63)
        self.assertEqual(nankai_topic_entries(batch), (entries[0],))

    def test_raw_header_does_not_read_or_modify_shared_assembly_or_use_decoder(self):
        owner = dcr.NankaiTroughEarthquake
        saved = (owner.completed, owner.reports, owner.announcement)
        batch = self.batch()
        with patch('azarashi.decode', side_effect=AssertionError('No global decode')):
            self.assertEqual(header(self.entries[0]).serial, 5)
            self.assertTrue(is_nankai_entry(self.entries[0]))
            self.assertEqual(nankai_topic_entries(batch), (self.entries[0],))
        self.assertIs(owner.completed, saved[0])
        self.assertIs(owner.reports, saved[1])
        self.assertIs(owner.announcement, saved[2])

    def test_invalid_metadata_version_date_position_or_entry_is_rejected(self):
        for fields in (((14, 3, 0),), ((41, 2, 3),), ((53, 4, 0),), ((53, 4, 7),),
                       ((201, 6, 0),), ((207, 6, 0),), ((201, 6, 28),), ((214, 6, 2),),
                       ((21, 4, 0),), ((25, 5, 0),), ((30, 5, 31),), ((35, 6, 63),),
                       ((21, 4, 4), (25, 5, 31))):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    header(changed(self.entries[0], fields))
        for entry in (None, ('bad', 0), (self.entries[0][0], True), (self.entries[0][0], -1)):
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    header(entry)
        other = next(report for report in decode_reports('dcr_receiver_20260924')
                     if not isinstance(report, dcr.NankaiTroughEarthquake))
        entry = (other.nmea, round(other.timestamp.timestamp() * 1000))
        self.assertFalse(is_nankai_entry(entry))
        with self.assertRaises(ValueError):
            header(entry)

    def test_crc_is_verified_even_when_nmea_checksum_is_valid(self):
        sentence, ms = self.entries[0]
        body = sentence[1:-3]
        position = 10
        body = body[:position] + ('0' if body[position] != '0' else '1') + body[position + 1:]
        checksum = 0
        for char in body:
            checksum ^= ord(char)
        with self.assertRaises(ValueError):
            header((f'${body}*{checksum:02X}', ms))

    def test_year_context_matches_existing_storage_for_year_rollover_and_leap_day(self):
        for received, fields in ((datetime(2026, 1, 1, tzinfo=timezone.utc), ((21, 4, 12), (25, 5, 31))),
                                 (datetime(2026, 12, 31, tzinfo=timezone.utc), ((21, 4, 1), (25, 5, 1))),
                                 (datetime(2026, 2, 28, tzinfo=timezone.utc), ((21, 4, 2), (25, 5, 29)))):
            sentence, _ = changed(self.entries[0], fields)
            entry = (sentence, round(received.timestamp() * 1000))
            self.assertEqual(header(entry).issued_at.timestamp(), _stored_frame(*entry)[2])

    def test_metadata_module_imports_with_only_the_standard_library(self):
        root = Path(__file__).resolve().parents[2]
        code = ('from shirasu.images.nankai import header\n'
                f'assert header({self.entries[0]!r}).serial == 5\n'
                'import sys\n'
                'assert "azarashi" not in sys.modules\n'
                'assert "nankai" not in sys.modules\n'
                'assert "sqlite3" not in sys.modules\n')
        completed = subprocess.run([sys.executable, '-S', '-c', code], cwd=root,
                                   text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == '__main__':
    unittest.main()
