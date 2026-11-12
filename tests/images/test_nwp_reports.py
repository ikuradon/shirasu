"""NWP forecast semantics from protocol-valid training frames, without maps."""

import copy
from datetime import datetime, timedelta, timezone
import unittest

from image_service.nwp_reports import nwp_plan
from tests.images.test_regional_reports import report


def received(*fields):
    return report('northwest_pacific_tsunami', fields)


def first_notice(value):
    return next(n for n in nwp_plan([value]).notices if n.code == 1)


class NorthwestPacificReportTests(unittest.TestCase):
    def test_training_example_keeps_every_forecast_point_and_bulletin_potential(self):
        value = received()
        before = copy.deepcopy(vars(value))
        plan = nwp_plan([value, value])
        self.assertEqual(value.report_classification_no, 7)
        self.assertEqual(plan.layer, 'northwest_pacific_tsunami')
        self.assertEqual({n.code for n in plan.notices}, {1, 20, 29, 41, 84})
        self.assertEqual(first_notice(value).title, 'Ust-Kamchatsk')
        self.assertEqual(first_notice(value).lines, ('予想高 3〜5m', '到達予想 11/10 00:20 UTC'))
        self.assertEqual(plan.notice_lines,
                         ('There is a Possibility of a Destructive Ocean-Wide Tsunami.',))
        self.assertIn('浸水範囲や国内の津波警報区分', plan.note)
        self.assertIn('未掲載地域の安全', plan.note)
        self.assertEqual(len({n.color for n in plan.notices}), 1,
                         'Height ranges must not become domestic warning classes')
        self.assertEqual(plan.legend, ())
        self.assertEqual(vars(value), before)

    def test_every_height_range_and_qualitative_unknown_value_is_preserved(self):
        expected = {1: '0.3〜1m', 2: '1〜3m', 3: '3〜5m', 4: '5〜10m',
                    508: '10m超', 509: '巨大（Huge）', 510: '高い（High）', 511: '不明'}
        for code, label in expected.items():
            with self.subTest(code=code):
                notice = first_notice(received((68, 9, code)))
                self.assertEqual(notice.lines[0], '予想高 ' + label)
                self.assertNotIn('警報', '\n'.join(notice.lines))

    def test_arrived_or_unknown_stays_ambiguous_for_both_day_flags(self):
        for day in (0, 1):
            with self.subTest(day=day):
                value = received((56, 1, day), (57, 5, 31), (62, 6, 63))
                self.assertIsNone(value.expected_tsunami_arrival_times[0])
                line = first_notice(value).lines[1]
                self.assertEqual(line, '到達済み、または到達時刻不明')
                self.assertNotIn('UTC', line)

    def test_arrival_day_is_relative_to_utc_not_the_japanese_calendar_day(self):
        value = received()
        self.assertEqual(value.report_time, datetime(2026, 11, 9, 23, 50, tzinfo=timezone.utc))
        self.assertEqual(value.report_time.astimezone(timezone(timedelta(hours=9))).day, 10)
        self.assertEqual(first_notice(value).lines[1], '到達予想 11/10 00:20 UTC')
        same_day = received((56, 1, 0))
        self.assertEqual(first_notice(same_day).lines[1], '到達予想 11/09 00:20 UTC')

    def test_day_carry_handles_month_and_year_boundaries(self):
        month_end = received((25, 5, 30))
        self.assertEqual(month_end.expected_tsunami_arrival_times[0],
                         datetime(2026, 12, 1, 0, 20, tzinfo=timezone.utc))
        self.assertEqual(first_notice(month_end).lines[1], '到達予想 12/01 00:20 UTC')
        year_end = received((21, 4, 12), (25, 5, 31))
        self.assertEqual(year_end.expected_tsunami_arrival_times[0],
                         datetime(2027, 1, 1, 0, 20, tzinfo=timezone.utc))
        self.assertEqual(first_notice(year_end).lines[1], '到達予想 01/01 00:20 UTC')

    def test_actual_midnight_is_not_the_domestic_release_placeholder(self):
        value = received((57, 5, 0), (62, 6, 0))
        self.assertEqual(first_notice(value).lines[1], '到達予想 11/10 00:00 UTC')

    def test_partially_invalid_times_and_inconsistent_resolved_dates_are_rejected(self):
        for hour, minute in ((24, 0), (31, 0), (0, 60), (23, 63)):
            with self.subTest(hour=hour, minute=minute), self.assertRaises(ValueError):
                nwp_plan([received((57, 5, hour), (62, 6, minute))])
        for value in (None, datetime(2026, 11, 10, 0, 20),
                      datetime(2026, 11, 11, 0, 20, tzinfo=timezone.utc)):
            inconsistent = received()
            inconsistent.expected_tsunami_arrival_times[0] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                nwp_plan([inconsistent])
        inconsistent = received((57, 5, 31), (62, 6, 63))
        inconsistent.expected_tsunami_arrival_times[0] = received().expected_tsunami_arrival_times[0]
        with self.assertRaises(ValueError):
            nwp_plan([inconsistent])

    def test_malformed_raw_date_fields_do_not_get_silently_coerced(self):
        for raw in ({'day': True, 'hour': 0, 'minute': 20},
                    {'day': 2, 'hour': 0, 'minute': 20},
                    {'day': 1, 'hour': 0}, {'day': 1, 'hour': '0', 'minute': 20}):
            value = received()
            value.expected_tsunami_arrival_times_raw[0] = raw
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                nwp_plan([value])

    def test_reserved_unknown_and_unmapped_regions_reject_the_whole_bundle(self):
        for code in (0, 42, 65, 73, 87, 99, 100, 127):
            with self.subTest(code=code), self.assertRaises(ValueError):
                nwp_plan([received((77, 7, code))])
        for code in (0, 5, 507):
            with self.subTest(height=code), self.assertRaises(ValueError):
                nwp_plan([received((68, 9, code))])

    def test_valid_added_forecast_codes_are_not_confused_with_table_row_numbers(self):
        for code in (66, 67, 68, 69, 70, 71, 72, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 85, 86):
            with self.subTest(code=code):
                plan = nwp_plan([received((77, 7, code))])
                self.assertIn(code, {notice.code for notice in plan.notices})

    def test_duplicate_point_requires_the_same_height_and_arrival(self):
        original = received()
        self.assertEqual(nwp_plan([original, original]), nwp_plan([original]))
        for fields in (((68, 9, 4),), ((62, 6, 25),), ((56, 1, 0),)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                nwp_plan([original, received(*fields)])

    def test_bulletin_potential_is_preserved_and_mixed_bulletins_are_rejected(self):
        for code, text in ((1, 'Ocean-Wide'), (2, 'Regional'), (3, 'Near the Epicenter'),
                           (4, 'Very Small Possibility')):
            with self.subTest(code=code):
                self.assertIn(text, nwp_plan([received((53, 3, code))]).notice_lines[0])
        for fields in (((53, 3, 2),), ((35, 6, 51),), ((14, 3, 2),)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                nwp_plan([received(), received(*fields)])

    def test_nonforecast_potential_messages_keep_the_full_text_fallback(self):
        for potential in (0, 5, 6, 7):
            with self.subTest(potential=potential), self.assertRaises(ValueError):
                nwp_plan([received((53, 3, potential))])

    def test_only_priority_or_training_issues_use_the_forecast_map(self):
        self.assertTrue(nwp_plan([received((14, 3, 2))]).notices)
        for classification in (0, 1, 3, 4, 5, 6):
            with self.subTest(classification=classification), self.assertRaises(ValueError):
                nwp_plan([received((14, 3, classification))])
        for information_type in (1, 2, 3):
            with self.subTest(information_type=information_type), self.assertRaises(ValueError):
                nwp_plan([received((41, 2, information_type))])

    def test_empty_or_inconsistent_arrays_cannot_drop_received_values(self):
        with self.assertRaises(ValueError):
            nwp_plan([received((56, 140, 0))])
        for name in ('coastal_regions_raw', 'tsunami_heights_raw',
                     'expected_tsunami_arrival_times_raw', 'expected_tsunami_arrival_times'):
            value = received()
            getattr(value, name).pop()
            with self.subTest(name=name), self.assertRaises(ValueError):
                nwp_plan([value])
        for reports in ([], [received(), report('weather')]):
            with self.assertRaises(ValueError):
                nwp_plan(reports)


if __name__ == '__main__':
    unittest.main()
