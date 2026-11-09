"""Verify that grouped marine posts preserve the decoded warning information."""

import copy
from datetime import datetime, timezone
import unittest

import azarashi
from azarashi.definitions.qzss.dcr.information_type import information_type
from azarashi.definitions.qzss.dcr.marine_forecast_region import marine_forecast_region
from azarashi.definitions.qzss.dcr.marine_warning_code import marine_warning_code
from azarashi.definitions.qzss.dcr.report_classification import report_classification

from message_format import report_message


MARINE = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'
DCR = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
DCX = '$QZQSM,55,53B0000DE000000000000000000000000000000000000000000000000E41D10*04'
RECEIVED_AT = datetime(2026, 11, 8, 3, tzinfo=timezone.utc)


class MessageFormatTests(unittest.TestCase):
    def setUp(self):
        self.report = azarashi.decode(MARINE, timestamp=RECEIVED_AT)

    def metadata(self, classification=3, information=0):
        self.report.report_classification_no = classification
        self.report.report_classification = report_classification[classification]
        self.report.information_type_no = information
        self.report.information_type = information_type[information]

    def entries(self, codes, regions, labels=None):
        self.report.marine_warning_codes_raw = list(codes)
        self.report.marine_warning_codes = (list(labels) if labels is not None
                                            else [marine_warning_code[code] for code in codes])
        self.report.marine_forecast_regions_raw = list(regions)
        self.report.marine_forecast_regions = [marine_forecast_region[region] for region in regions]

    def test_real_marine_sample_matches_the_proposed_post_exactly(self):
        expected = (
            '【海上警報】発表（通常）\n'
            '発表時刻：11月8日 17:35 JST\n'
            '\n■ 海上着氷警報\n'
            '・サハリン西方海上\n'
            '・北海道西方海上\n'
            '\n■ 海上風警報\n'
            '・宗谷海峡\n'
            '・北海道西方海上\n'
            '・檜山津軽沖\n'
            '・関東海域南部\n'
            '・沿海州南部沖\n'
            '・能登沖\n'
            f'---\nNMEA: {MARINE}'
        )
        self.assertEqual(report_message(self.report), expected)

    def test_groups_keep_first_warning_order_and_all_repeated_region_entries(self):
        self.entries([20, 10, 20, 10, 20], [1040, 1020, 1050, 1050, 1040])
        text = report_message(self.report)
        self.assertIn('■ 海上風警報\n・宗谷海峡\n・北海道西方海上\n・宗谷海峡', text)
        self.assertIn('■ 海上着氷警報\n・サハリン西方海上\n・北海道西方海上', text)
        self.assertLess(text.index('■ 海上風警報'), text.index('■ 海上着氷警報'))
        self.assertEqual(sum(line.startswith('・') for line in text.splitlines()), 5)

    def test_warning_code_and_label_both_remain_part_of_group_identity(self):
        self.entries([7, 8, 7], [1020, 1040, 1050], labels=['未定義', '未定義', '異なる表示'])
        text = report_message(self.report)
        self.assertEqual(text.count('■ 未定義\n'), 2)
        self.assertIn('■ 異なる表示\n・北海道西方海上', text)
        self.assertEqual(sum(line.startswith('・') for line in text.splitlines()), 3)

    def test_priority_and_correction_labels_are_retained(self):
        for priority, label in ((1, '最優先'), (2, '優先'), (3, '通常')):
            with self.subTest(priority=priority):
                self.metadata(priority, 1)
                text = report_message(self.report)
                self.assertEqual(text.splitlines()[0], f'【海上警報】訂正（{label}）')
                self.assertIn('■ 海上着氷警報', text)

    def test_training_is_prominent_and_preserves_information_type(self):
        for information, label in ((0, '発表'), (1, '訂正')):
            with self.subTest(information=information):
                self.metadata(7, information)
                self.assertEqual(report_message(self.report).splitlines()[0],
                                 f'【訓練・試験】海上警報（{label}）')

    def test_cancellation_is_prominent_and_keeps_priority(self):
        for priority, label in ((1, '最優先'), (2, '優先'), (3, '通常')):
            with self.subTest(priority=priority):
                self.metadata(priority, 2)
                text = report_message(self.report)
                self.assertEqual(text.splitlines()[0], f'【取消】海上警報（{label}）')
                self.assertNotIn('発表されました', text)
                self.assertIn('■ 海上風警報', text)

    def test_training_and_cancellation_are_both_shown(self):
        self.metadata(7, 2)
        text = report_message(self.report)
        self.assertEqual(text.splitlines()[0], '【訓練・試験】【取消】海上警報')
        self.assertIn('■ 海上着氷警報', text)
        self.assertTrue(text.endswith(f'\n---\nNMEA: {MARINE}'))

    def test_undefined_codes_remain_visible(self):
        self.metadata(4, 3)
        self.entries([7, 8], [999, 1000])
        text = report_message(self.report)
        for label in (information_type[3], report_classification[4],
                      marine_warning_code[7], marine_warning_code[8],
                      marine_forecast_region[999], marine_forecast_region[1000]):
            self.assertIn(label, text)

    def test_warning_lifted_is_not_confused_with_cancellation_of_a_report(self):
        self.entries([0, 20], [1020, 1050])
        text = report_message(self.report)
        self.assertEqual(text.splitlines()[0], '【海上警報】発表（通常）')
        self.assertIn('■ 海上警報解除\n・サハリン西方海上', text)
        self.assertNotIn('【取消】', text)

    def test_empty_warning_arrays_do_not_invent_regions_or_a_lifted_warning(self):
        self.entries([], [])
        self.assertEqual(report_message(self.report),
                         '【海上警報】発表（通常）\n'
                         '発表時刻：11月8日 17:35 JST\n'
                         f'---\nNMEA: {MARINE}')

    def test_jst_conversion_handles_a_utc_date_boundary(self):
        self.report.report_time = datetime(2026, 12, 31, 16, 5, tzinfo=timezone.utc)
        self.assertIn('発表時刻：1月1日 01:05 JST', report_message(self.report))

    def test_formatting_does_not_mutate_the_report_or_its_lists(self):
        before = copy.deepcopy(self.report.__dict__)
        report_message(self.report)
        self.assertEqual(self.report.__dict__, before)

    def test_mismatched_arrays_fail_instead_of_silently_losing_items(self):
        names = ('marine_warning_codes', 'marine_warning_codes_raw',
                 'marine_forecast_regions', 'marine_forecast_regions_raw')
        for name in names:
            with self.subTest(name=name):
                report = copy.deepcopy(self.report)
                getattr(report, name).append(getattr(report, name)[0])
                with self.assertRaisesRegex(ValueError, 'equal lengths'):
                    report_message(report)

    def test_missing_or_naive_report_time_is_rejected(self):
        for value in (None, datetime(2026, 11, 8, 8, 35)):
            with self.subTest(value=value):
                self.report.report_time = value
                with self.assertRaisesRegex(ValueError, 'timezone-aware'):
                    report_message(self.report)

    def test_other_report_types_keep_their_existing_post_text(self):
        for sentence in (DCR, DCX):
            with self.subTest(sentence=sentence):
                report = azarashi.decode(sentence, timestamp=RECEIVED_AT)
                self.assertEqual(report_message(report), f'{report}\n---\nNMEA: {report.nmea}')


if __name__ == '__main__':
    unittest.main()
