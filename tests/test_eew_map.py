"""EEW forecast meaning, special codes and bundle identity with CRC-valid inputs."""

import copy
from dataclasses import FrozenInstanceError
import unittest

from image_service.eew_map import eew_plan
from test_regional_reports import report


class EEWMapTests(unittest.TestCase):
    def test_training_example_has_one_prediction_and_area_names_only(self):
        received = report('eew')
        before = copy.deepcopy(vars(received))
        plan = eew_plan([received])
        self.assertEqual(received.report_classification_no, 7)
        self.assertEqual(plan.layer, 'eew')
        self.assertEqual(plan.headline, '予測震度 5弱〜6弱')
        self.assertEqual({n.code for n in plan.notices}, {11, 12, 14, 15, 16, 19})
        self.assertEqual(len({n.color for n in plan.notices}), 1)
        self.assertTrue(all(n.lines == () for n in plan.notices))
        self.assertEqual(plan.legend, ())
        self.assertIn('通報全体', plan.subtitle)
        self.assertIn('震度分布ではなく', plan.note)
        self.assertIn('輪郭のない地域も安全を意味しません', plan.note)
        self.assertEqual(plan.notice_lines[0], '最大予測長周期地震動：階級 1〜3')
        self.assertIn('2026/11/10 09:45 JST', plan.notice_lines[1])
        self.assertIn('茨城県沖 / 深さ 50km / M6.8', plan.notice_lines[1])
        self.assertIn('強い揺れに警戒してください。', plan.notice_lines)
        self.assertEqual(vars(received), before)
        with self.assertRaises(FrozenInstanceError):
            plan.headline = 'mutable'

    def test_maximum_priority_is_supported_but_other_priorities_are_not_eew(self):
        self.assertTrue(eew_plan([report('eew', [(14, 3, 1)])]).notices)
        for classification in (0, 2, 3, 4, 5, 6):
            with self.subTest(classification=classification), self.assertRaises(ValueError):
                eew_plan([report('eew', [(14, 3, classification)])])

    def test_cancellation_and_undefined_correction_use_whole_report_fallback(self):
        for information_type in (1, 2, 3):
            with self.subTest(information_type=information_type), self.assertRaises(ValueError):
                eew_plan([report('eew', [(41, 2, information_type)])])

    def test_plum_fixed_values_are_not_presented_as_estimated_magnitude_or_depth(self):
        received = report('eew', [(96, 9, 10), (105, 7, 10)])
        plan = eew_plan([received])
        text = '\n'.join(plan.notice_lines)
        self.assertTrue(received.assumptive)
        self.assertIn('仮定震源（PLUM法）', text)
        self.assertIn('仮定発生時刻', text)
        self.assertIn('地名 茨城県沖', text)
        self.assertNotIn('M1.0', text)
        self.assertNotIn('10km', text)
        self.assertIn('仮定震源の深さ・Mは固定値', plan.note)
        self.assertEqual(plan.headline, '予測震度 5弱〜6弱')

    def test_inconsistent_assumptive_flag_is_rejected(self):
        received = report('eew')
        received.assumptive = True
        with self.assertRaises(ValueError):
            eew_plan([received])

    def test_same_earthquake_combines_area_flags_and_deduplicates_advice(self):
        original = report('eew')
        extra = report('eew', [(149, 1, 1)])  # DCR code20, Niigata
        plan = eew_plan([original, extra, original])
        self.assertEqual({n.code for n in plan.notices}, {11, 12, 14, 15, 16, 19, 20})
        self.assertEqual(plan.notice_lines.count('強い揺れに警戒してください。'), 1)

    def test_local_and_prefectural_flags_remain_distinct(self):
        plan = eew_plan([report('eew', [(188, 1, 1)])])  # DCR59 Kanto
        self.assertIn(59, {n.code for n in plan.notices})
        self.assertIn(16, {n.code for n in plan.notices})

    def test_different_earthquake_or_forecast_fields_cannot_be_combined(self):
        changes = [(90, 6, 44), (112, 10, 350), (96, 9, 60), (105, 7, 69),
                   (122, 4, 5), (126, 4, 9), (47, 3, 3), (50, 3, 5),
                   (35, 6, 51), (14, 3, 1)]
        for field in changes:
            with self.subTest(field=field), self.assertRaises(ValueError):
                eew_plan([report('eew'), report('eew', [field])])

    def test_unknown_or_empty_warning_regions_are_never_dropped(self):
        for fields in ([(200, 1, 1)], [(209, 1, 1)], [(130, 80, 0)]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                eew_plan([report('eew', fields)])

    def test_unknown_hypocentre_depth_magnitude_prediction_or_advice_requires_fallback(self):
        changes = [(112, 10, 1023), (96, 9, 502), (105, 7, 0), (122, 4, 13),
                   (126, 4, 13), (47, 3, 6), (53, 9, 499)]
        for field in changes:
            with self.subTest(field=field), self.assertRaises(ValueError):
                eew_plan([report('eew', [field])])

    def test_open_upper_bounds_preserve_or_greater_meaning(self):
        plan = eew_plan([report('eew', [(126, 4, 11), (50, 3, 6)])])
        self.assertEqual(plan.headline, '予測震度 5弱程度以上')
        self.assertEqual(plan.notice_lines[0], '最大予測長周期地震動：階級 1程度以上')

    def test_reversed_or_contradictory_bounds_are_rejected(self):
        changes = [[(122, 4, 9)], [(47, 3, 5)], [(122, 4, 14)],
                   [(122, 4, 15), (126, 4, 11)], [(47, 3, 7), (50, 3, 6)]]
        for fields in changes:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                eew_plan([report('eew', fields)])

    def test_no_long_period_data_is_omitted_but_unknown_is_preserved(self):
        no_data = eew_plan([report('eew', [(47, 3, 0), (50, 3, 0)])])
        self.assertNotIn('長周期', '\n'.join(no_data.notice_lines))
        unknown = eew_plan([report('eew', [(47, 3, 7), (50, 3, 7)])])
        self.assertIn('最大予測長周期地震動：階級 不明', unknown.notice_lines)
        one_missing = eew_plan([report('eew', [(47, 3, 0)])])
        self.assertIn('下限 該当情報なし / 上限 3', one_missing.notice_lines[0])

    def test_defined_unknown_and_none_intensity_values_are_explicit(self):
        unknown = eew_plan([report('eew', [(122, 4, 15), (126, 4, 15)])])
        self.assertEqual(unknown.headline, '予測震度 不明')
        none = eew_plan([report('eew', [(122, 4, 14), (126, 4, 14)])])
        self.assertEqual(none.headline, '予測震度 なし')
        lower_unknown = eew_plan([report('eew', [(122, 4, 15)])])
        self.assertEqual(lower_unknown.headline, '予測震度 下限 不明 / 上限 6弱')

    def test_undefined_occurrence_does_not_use_the_clock(self):
        plan = eew_plan([report('eew', [(80, 5, 0), (85, 5, 31), (90, 6, 63)])])
        self.assertIn('地震発生 不明', plan.notice_lines[1])

    def test_invalid_or_future_occurrence_is_rejected(self):
        for field in ((85, 5, 1), (80, 5, 0), (85, 5, 24), (90, 6, 60)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                eew_plan([report('eew', [field])])

    def test_unknown_depth_and_magnitude_remain_explicit_without_a_marker(self):
        plan = eew_plan([report('eew', [(96, 9, 511), (105, 7, 127)])])
        self.assertIn('深さ 不明 / M不明', plan.notice_lines[1])
        self.assertTrue(all(not hasattr(n, 'position') for n in plan.notices))

    def test_empty_or_other_category_bundle_is_rejected(self):
        with self.assertRaises(ValueError):
            eew_plan([])
        with self.assertRaises(ValueError):
            eew_plan([report('marine')])


if __name__ == '__main__':
    unittest.main()
