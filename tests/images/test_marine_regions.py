"""Marine warning interpretation: keep independent hazards and report meanings."""

import copy
import unittest

from image_service.marine_regions import marine_map
from tests.images.test_regional_reports import report


def training_marine(*fields):
    return report('marine', [(14, 3, 7), *fields])


class MarineRegionTests(unittest.TestCase):
    def test_received_sample_has_eight_items_in_seven_areas(self):
        received = report('marine')
        before = copy.deepcopy(vars(received))
        result = marine_map([received])
        self.assertEqual(len(received.marine_forecast_regions_raw), 8)
        self.assertEqual(result.layer, 'marine')
        self.assertEqual(result.title, '海上警報')
        self.assertEqual({notice.code for notice in result.notices},
                         {1020, 1040, 1050, 1150, 3020, 3110, 3140})
        self.assertIn('無着色は警報なしを意味しません', result.note)
        self.assertEqual(vars(received), before)

    def test_icing_and_wind_for_one_area_are_both_preserved(self):
        result = marine_map([report('marine')])
        notices = {notice.code: notice for notice in result.notices}
        self.assertEqual(notices[1050].title, '北海道西方海上')
        self.assertEqual(notices[1050].lines, ('着氷警報', '風警報'))
        self.assertEqual(notices[1020].lines, ('着氷警報',))
        self.assertEqual(notices[3020].lines, ('風警報',))

    def test_repeated_reports_do_not_repeat_identical_warnings(self):
        received = report('marine')
        self.assertEqual(marine_map([received, received]), marine_map([received]))

    def test_release_keeps_its_region_and_is_distinct_from_cancellation(self):
        result = marine_map([training_marine((53, 5, 0))])
        notice = next(item for item in result.notices if item.code == 1020)
        self.assertEqual(notice.lines, ('警報解除',))
        self.assertIn(('警報解除', notice.color), result.legend)
        warning = next(item for item in result.notices if item.code == 1040)
        self.assertNotEqual(notice.color, warning.color)
        with self.assertRaises(ValueError):
            marine_map([training_marine((41, 2, 2))])

    def test_release_and_warning_for_same_area_are_not_silently_resolved(self):
        result = marine_map([training_marine((53, 5, 0), (58, 14, 1050))])
        notice = next(item for item in result.notices if item.code == 1050)
        self.assertEqual(notice.lines, ('警報解除', '着氷警報', '風警報'))
        self.assertEqual(result.notice_lines, ('同一海域の警報と解除を併記（受信内容のまま）',))
        self.assertIn(('警報対象', notice.color), result.legend)

    def test_unknown_region_and_warning_require_whole_report_fallback(self):
        for fields in (((58, 14, 16383),), ((58, 14, 10000),), ((53, 5, 31),)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                marine_map([training_marine(*fields)])

    def test_empty_regions_and_mismatched_arrays_do_not_hide_missing_values(self):
        with self.assertRaises(ValueError):
            marine_map([training_marine((53, 152, 0))])
        received = report('marine')
        received.marine_warning_codes_raw = received.marine_warning_codes_raw[:1]
        with self.assertRaises(ValueError):
            marine_map([received])
        with self.assertRaises(ValueError):
            marine_map([])


if __name__ == '__main__':
    unittest.main()
