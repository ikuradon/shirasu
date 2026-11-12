"""Ash forecast meaning from CRC-valid, explicitly training QZQSM frames."""

import copy
from datetime import datetime, timezone
import unittest

from image_service.ash_reports import ash_plan
from tests.images.test_regional_reports import report


def received(*fields):
    return report('ash_fall', fields)


def entries(*rows, extra=()):
    fields = [(83, 116, 0), *extra]
    for index, (municipality, hours, phenomenon) in enumerate(rows):
        offset = 83 + index * 29
        fields.extend(((offset, 3, hours), (offset + 3, 3, phenomenon),
                       (offset + 6, 23, municipality)))
    return received(*fields)


class AshReportTests(unittest.TestCase):
    def test_training_example_keeps_all_cities_and_simultaneous_ash_and_small_cinders(self):
        value = received()
        original = copy.deepcopy(vars(value))
        plan = ash_plan([value, value])
        self.assertEqual(value.report_classification_no, 7)
        self.assertEqual(plan.layer, 'ash_fall')
        self.assertEqual((plan.volcano_code, plan.volcano_name, plan.warning_type), (506, '桜島', 2))
        self.assertEqual(plan.title, '降灰予報（詳細）')
        self.assertEqual({notice.code for notice in plan.notices}, {4620100, 4621400, 4621800})
        kagoshima = next(notice for notice in plan.notices if notice.code == 4620100)
        self.assertEqual(kagoshima.title, '鹿児島県鹿児島市')
        self.assertEqual(kagoshima.lines,
                         ('基点＋1時間：多量の降灰', '基点＋1時間：小さな噴石の落下'))
        self.assertEqual(plan.notice_lines, ('火山：桜島 / 基点時刻：2026/11/10 09:00 JST',))
        self.assertIn('市町村全域の降灰分布や噴石の落下範囲', plan.note)
        self.assertIn('未掲載地域の安全', plan.note)
        self.assertTrue(all(notice.color == '#ffffff' for notice in plan.notices))
        self.assertEqual(vars(value), original)

    def test_all_four_phenomena_keep_their_names_and_distinct_label_styles(self):
        names = ('少量の降灰', 'やや多量の降灰', '多量の降灰', '小さな噴石の落下')
        backgrounds = ('#d9d9d9', '#ffff00', '#ff0000', 'none')
        for hazard, (name, background) in enumerate(zip(names, backgrounds), 1):
            with self.subTest(hazard=hazard):
                notice, = ash_plan([entries((4620100, 1, hazard))]).notices
                self.assertEqual(notice.lines, ('基点＋1時間：' + name,))
                self.assertEqual(notice.styles[0].background, background)
                self.assertNotIn('mm', notice.lines[0], 'The report supplies a category, not a measured amount')
        stones, = ash_plan([entries((4620100, 1, 4))]).notices
        self.assertEqual(stones.styles[0].edge, '#ff0000')
        self.assertEqual(stones.styles[0].foreground, '#ffffff')

    def test_hours_remain_relative_forecast_values_without_invented_onset_or_intervals(self):
        for hours in range(1, 7):
            with self.subTest(hours=hours):
                notice, = ash_plan([entries((4620100, hours, 1))]).notices
                self.assertEqual(notice.lines, (f'基点＋{hours}時間：少量の降灰',))
                self.assertNotIn('開始', notice.lines[0])
                self.assertNotIn('〜', notice.lines[0])

    def test_multiple_hours_and_exact_duplicates_do_not_drop_information(self):
        one = entries((4620100, 3, 2), (4620100, 1, 3), (4620100, 1, 4))
        two = entries((4620100, 3, 2), (4620100, 2, 1))
        notice, = ash_plan([one, two, one]).notices
        self.assertEqual(notice.lines, ('基点＋1時間：多量の降灰', '基点＋1時間：小さな噴石の落下',
                                      '基点＋2時間：少量の降灰', '基点＋3時間：やや多量の降灰'))
        self.assertEqual(tuple(style.label for style in notice.styles),
                         ('多量の降灰', '小さな噴石の落下', '少量の降灰', 'やや多量の降灰'))
        self.assertEqual(ash_plan([one, two]), ash_plan([two, one]))

    def test_conflicting_ash_amounts_for_one_city_and_time_reject_the_whole_map(self):
        for first, second in ((1, 2), (2, 3), (3, 1)):
            with self.subTest(first=first, second=second), self.assertRaises(ValueError):
                ash_plan([entries((4620100, 1, first)), entries((4620100, 1, second))])
        with self.assertRaises(ValueError):
            ash_plan([entries((4620100, 1, 1), (4620100, 1, 3))])

    def test_same_time_different_cities_and_same_city_different_times_are_independent(self):
        plan = ash_plan([entries((4620100, 1, 1), (4621400, 1, 3), (4620100, 2, 3))])
        self.assertEqual(len(plan.notices), 2)
        self.assertEqual(next(n.lines for n in plan.notices if n.code == 4620100),
                         ('基点＋1時間：少量の降灰', '基点＋2時間：多量の降灰'))

    def test_priority_training_preliminary_detailed_and_corrections_are_supported(self):
        for classification in (2, 7):
            for kind, name in ((1, '速報'), (2, '詳細')):
                for information in (0, 1):
                    with self.subTest(classification=classification, kind=kind, information=information):
                        plan = ash_plan([received((14, 3, classification), (69, 2, kind), (41, 2, information))])
                        self.assertEqual(plan.title, f'降灰予報（{name}）')
                        self.assertEqual(plan.warning_type, kind)

    def test_unsupported_classifications_and_cancellations_cannot_look_like_forecasts(self):
        for classification in (0, 1, 3, 4, 5, 6):
            with self.subTest(classification=classification), self.assertRaises(ValueError):
                ash_plan([received((14, 3, classification))])
        for information in (2, 3):
            with self.subTest(information=information), self.assertRaises(ValueError):
                ash_plan([received((41, 2, information))])
        for kind in (0, 3):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                ash_plan([received((69, 2, kind))])

    def test_unknown_values_never_get_discarded_to_leave_a_plausible_partial_map(self):
        fields = ([(89, 23, 0)], [(89, 23, 4699999)], [(89, 23, 4712345)],
                  [(83, 3, 0)], [(83, 3, 7)], [(86, 3, 0)], [(86, 3, 5)],
                  [(86, 3, 6)], [(86, 3, 7)], [(71, 12, 900)], [(71, 12, 901)],
                  [(71, 12, 902)], [(71, 12, 4000)], [(71, 12, 100)])
        for changed in fields:
            with self.subTest(fields=changed), self.assertRaises(ValueError):
                ash_plan([received(), received(*changed)])

    def test_base_time_is_utc_in_the_frame_but_displayed_with_full_japanese_date(self):
        value = received((53, 5, 9), (58, 5, 23), (63, 6, 50))
        plan = ash_plan([value])
        self.assertEqual(plan.base_time, datetime(2026, 11, 9, 23, 50, tzinfo=timezone.utc))
        self.assertEqual(plan.notice_lines, ('火山：桜島 / 基点時刻：2026/11/10 08:50 JST',))
        self.assertNotEqual(plan.base_time, value.report_time)

    def test_previous_month_and_year_are_preserved_without_using_current_wall_clock(self):
        month = ash_plan([received((25, 5, 1), (53, 5, 31), (58, 5, 23), (63, 6, 50))])
        self.assertEqual(month.base_time, datetime(2026, 10, 31, 23, 50, tzinfo=timezone.utc))
        self.assertIn('2026/11/01 08:50 JST', month.notice_lines[0])
        year = ash_plan([received((21, 4, 1), (25, 5, 1), (53, 5, 31), (58, 5, 23), (63, 6, 50))])
        self.assertEqual(year.base_time, datetime(2026, 12, 31, 23, 50, tzinfo=timezone.utc))
        self.assertIn('2027/01/01 08:50 JST', year.notice_lines[0])

    def test_invalid_unknown_future_or_inconsistent_activity_time_requires_fallback(self):
        for fields in (((53, 5, 0),), ((58, 5, 24),), ((63, 6, 60),),
                       ((53, 5, 0), (58, 5, 31), (63, 6, 63)), ((58, 5, 1),)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ash_plan([received(*fields)])
        for value in (None, datetime(2026, 11, 10),
                      datetime(2026, 11, 9, tzinfo=timezone.utc),
                      datetime(2026, 11, 10, 0, 0, 1, tzinfo=timezone.utc)):
            changed = received()
            changed.activity_time = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                ash_plan([changed])

    def test_malformed_raw_times_and_numeric_lookalikes_are_not_coerced(self):
        for raw in ({'day': 10, 'hour': False, 'minute': 0}, {'day': 10, 'hour': 0},
                    {'day': 10, 'hour': '0', 'minute': 0}):
            value = received()
            value.activity_time_raw = raw
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ash_plan([value])
        for field, changed in (('expected_ash_fall_times_raw', True),
                               ('ash_fall_warning_codes_raw', '3'), ('local_governments_raw', 4620100.0)):
            value = received()
            getattr(value, field)[0] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                ash_plan([value])

    def test_mixed_volcano_base_time_forecast_type_or_bulletin_rejects_whole_bundle(self):
        for fields in (((71, 12, 503),), ((69, 2, 1),), ((63, 6, 5),),
                       ((41, 2, 1),), ((35, 6, 11),), ((14, 3, 2),)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ash_plan([received(), received(*fields)])
        with self.assertRaises(ValueError):
            ash_plan([received(), report('volcano')])

    def test_missing_entries_or_short_arrays_never_silently_drop_forecasts(self):
        with self.assertRaises(ValueError):
            ash_plan([entries()])
        with self.assertRaises(ValueError):
            ash_plan([])
        for field in ('expected_ash_fall_times_raw', 'ash_fall_warning_codes_raw', 'local_governments_raw'):
            value = received()
            getattr(value, field).pop()
            with self.subTest(field=field), self.assertRaises(ValueError):
                ash_plan([value])


if __name__ == '__main__':
    unittest.main()
