"""Regional interpretation tests using CRC-valid, explicitly training fixtures."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

import azarashi

from image_service.regional_reports import seismic_intensity_map, tsunami_map

EXAMPLES = Path(__file__).resolve().parents[2] / 'image_service' / 'examples'


def report(name, fields=()):
    row = json.loads((EXAMPLES / (name + '.json')).read_text())[0]
    nmea = row['nmea']
    if fields:
        value = int(nmea.split(',')[2].split('*')[0] + '0', 16)
        for offset, length, replacement in fields:
            shift = 256 - offset - length
            value = (value & ~(((1 << length) - 1) << shift)) | (replacement << shift)
        data, crc = value.to_bytes(32, 'big'), 0
        for bit in range(226):
            carry = ((crc >> 23) & 1) ^ ((data[bit // 8] >> (7 - bit % 8)) & 1)
            crc = (crc << 1) & 0xffffff
            if carry:
                crc ^= 0x864cfb
        value = (value & ~(((1 << 24) - 1) << 6)) | (crc << 6)
        body = '$QZQSM,55,' + f'{value:064X}'[:-1]
        checksum = 0
        for char in body[1:]:
            checksum ^= ord(char)
        nmea = body + f'*{checksum:02X}'
    return azarashi.decode(nmea, timestamp=datetime.fromtimestamp(row['received_ms'] / 1000, timezone.utc))


class SeismicIntensityMapTests(unittest.TestCase):
    def test_training_fixture_maps_received_prefectures_without_inventing_point_data(self):
        received = report('seismic_intensity')
        before = copy.deepcopy(vars(received))
        result = seismic_intensity_map([received])
        self.assertEqual(received.report_classification_no, 7)
        self.assertEqual(result.layer, 'prefectures')
        self.assertEqual({notice.code for notice in result.notices}, {11, 12, 13, 14})
        self.assertEqual(next(n.lines for n in result.notices if n.code == 13), ('受信震度 5弱',))
        self.assertIn('県全体の揺れを示さず', result.note)
        self.assertIn('「揺れなし」も意味しません', result.note)
        self.assertIn('2026/11/09 23:45 JST', result.notice_lines[0])
        self.assertEqual(vars(received), before)

    def test_multiple_values_for_one_prefecture_are_preserved_and_color_uses_received_maximum(self):
        received = report('seismic_intensity', [(72, 6, 14)])
        result = seismic_intensity_map([received, received])
        notices = {notice.code: notice for notice in result.notices}
        self.assertEqual(notices[14].lines, ('受信震度 5強 / 5弱',))
        self.assertIn('色は受信値の最大', result.note)
        single = seismic_intensity_map([report('seismic_intensity')])
        self.assertEqual(notices[14].color, next(n.color for n in single.notices if n.code == 14))

    def test_unknown_region_or_intensity_requires_text_fallback(self):
        for fields in ([(72, 6, 48)], [(69, 3, 0)]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                seismic_intensity_map([report('seismic_intensity', fields)])

    def test_undefined_occurrence_is_not_replaced_by_current_time(self):
        result = seismic_intensity_map([report('seismic_intensity', [(53, 5, 0), (58, 5, 31), (63, 6, 63)])])
        self.assertEqual(result.notice_lines, ('地震発生時刻：不明',))

    def test_different_earthquakes_and_cancelled_reports_are_not_combined(self):
        with self.assertRaises(ValueError):
            seismic_intensity_map([report('seismic_intensity'), report('seismic_intensity', [(63, 6, 46)])])
        with self.assertRaises(ValueError):
            seismic_intensity_map([report('seismic_intensity', [(41, 2, 2)])])

    def test_inconsistent_arrays_and_empty_regions_do_not_silently_drop_values(self):
        received = report('seismic_intensity')
        received.prefectures_raw = received.prefectures_raw[:1]
        with self.assertRaises(ValueError):
            seismic_intensity_map([received])
        with self.assertRaises(ValueError):
            seismic_intensity_map([report('seismic_intensity', [(69, 144, 0)])])


class TsunamiMapTests(unittest.TestCase):
    def test_training_fixture_retains_region_height_date_and_urgent_notice(self):
        received = report('tsunami')
        result = tsunami_map([received])
        self.assertEqual(received.report_classification_no, 7)
        self.assertEqual(result.title, '津波警報')
        self.assertEqual(result.layer, 'tsunami')
        notices = {n.code: n for n in result.notices}
        self.assertEqual(notices[300].lines, ('予想高 3m', '到達予想 11/10 00:15 JST'))
        self.assertEqual(notices[250].lines[1], '到達状況：津波到達中と推測')
        self.assertIn('ただちに避難してください。', result.notice_lines)
        self.assertIn('浸水範囲を示すものではありません', result.note)

    def test_no_data_and_unknown_height_have_explicit_meanings(self):
        result = tsunami_map([report('tsunami', [(84, 1, 0), (85, 5, 30), (90, 6, 62), (96, 4, 14)])])
        notice = next(n for n in result.notices if n.code == 300)
        self.assertEqual(notice.lines, ('予想高 不明', '到達予想：該当情報なし'))

    def test_unknown_warning_region_height_or_arrival_requires_fallback(self):
        for fields in ([(80, 4, 15)], [(100, 10, 1000)], [(96, 4, 7)], [(85, 5, 24)]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                tsunami_map([report('tsunami', fields)])

    def test_release_midnight_placeholder_is_not_shown_as_a_real_arrival(self):
        result = tsunami_map([report('tsunami', [(80, 4, 2), (84, 1, 0), (85, 5, 0), (90, 6, 0)])])
        notice = next(n for n in result.notices if n.code == 300)
        self.assertEqual(result.title, '警報解除')
        self.assertEqual(notice.lines[1], '到達予想：該当情報なし')
        self.assertTrue(any('欠測代用時刻' in line for line in result.notice_lines))

    def test_future_midnight_in_release_is_still_a_real_time(self):
        result = tsunami_map([report('tsunami', [(80, 4, 2), (84, 1, 1), (85, 5, 0), (90, 6, 0)])])
        notice = next(n for n in result.notices if n.code == 300)
        self.assertEqual(notice.lines[1], '到達予想 11/10 09:00 JST')
        self.assertFalse(any('欠測代用時刻' in line for line in result.notice_lines))

    def test_same_region_alternatives_are_not_collapsed_to_maximum_or_first(self):
        result = tsunami_map([report('tsunami'), report('tsunami', [(96, 4, 4), (90, 6, 40)])])
        notice = next(n for n in result.notices if n.code == 300)
        self.assertEqual(notice.lines, ('受信値 1：予想高 3m', '到達予想 11/10 00:15 JST',
                                       '受信値 2：予想高 5m', '到達予想 11/10 00:40 JST'))

    def test_no_tsunami_and_release_are_distinct_from_cancellation(self):
        self.assertEqual(tsunami_map([report('tsunami', [(80, 4, 1)])]).title, '津波なし')
        self.assertEqual(tsunami_map([report('tsunami', [(80, 4, 2)])]).title, '警報解除')
        with self.assertRaises(ValueError):
            tsunami_map([report('tsunami', [(41, 2, 2)])])

    def test_mixed_warning_types_and_empty_reports_require_fallback(self):
        with self.assertRaises(ValueError):
            tsunami_map([report('tsunami'), report('tsunami', [(80, 4, 4)])])
        with self.assertRaises(ValueError):
            tsunami_map([report('tsunami', [(84, 130, 0)])])

    def test_year_rollover_is_visible(self):
        received = report('tsunami')
        received.report_time = datetime(2026, 12, 31, 14, 50, tzinfo=timezone.utc)
        received.expected_tsunami_arrival_times[0] = datetime(2026, 12, 31, 15, 15, tzinfo=timezone.utc)
        notice = next(n for n in tsunami_map([received]).notices if n.code == 300)
        self.assertEqual(notice.lines[1], '到達予想 2027/01/01 00:15 JST')


if __name__ == '__main__':
    unittest.main()
