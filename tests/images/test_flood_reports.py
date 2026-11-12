"""Flood meaning and whole-bulletin fallback from CRC-valid training frames."""

import copy
from datetime import datetime
import unittest

from image_service.flood_reports import flood_plan, NON_SPECIFIC_CODES, REGION_CODES
from tests.images.test_regional_reports import report


def received(*fields):
    return report('flood', fields)


def entries(*rows, extra=()):
    fields = [(53, 132, 0), *extra]
    for index, (region, level) in enumerate(rows):
        fields.extend(((53 + index * 44, 4, level), (57 + index * 44, 40, region)))
    return received(*fields)


class FloodReportTests(unittest.TestCase):
    def test_training_example_keeps_each_reach_and_its_received_warning(self):
        value = received()
        before = copy.deepcopy(vars(value))
        plan = flood_plan([value, value])
        self.assertEqual(value.report_classification_no, 7)
        self.assertEqual(plan.layer, 'flood')
        self.assertEqual(plan.title, '指定河川洪水予報')
        self.assertEqual([(n.code, n.warning_level, n.lines) for n in plan.notices], [
            (830304000100, 2, ('氾濫警戒情報',)),
            (830304007100, 3, ('氾濫危険情報',)),
            (830305000100, 4, ('氾濫発生情報',)),
        ])
        self.assertEqual(plan.notices[0].title, '荒川(埼玉県・東京都)')
        self.assertIn('洪水予報の対象区間', plan.note)
        self.assertIn('浸水範囲や未掲載地域の安全', plan.note)
        self.assertEqual(vars(value), before)

    def test_colour_follows_the_named_product_not_the_four_bit_code_number(self):
        cases = ((2, '氾濫警戒情報', '#ff2800'),
                 (3, '氾濫危険情報', '#aa00aa'),
                 (4, '氾濫発生情報', '#0c000c'))
        for level, label, colour in cases:
            with self.subTest(level=level):
                notice, = flood_plan([entries((830304000100, level))]).notices
                self.assertEqual(notice.lines, (label,))
                self.assertEqual(notice.color, colour)
                self.assertEqual(notice.styles[0].background, colour)
                self.assertEqual(notice.styles[0].foreground, '#ffffff')
                self.assertNotIn('レベル', '\n'.join(notice.lines))
        occurrence, = flood_plan([entries((830304000100, 4))]).notices
        self.assertEqual(occurrence.styles[0].edge, '#ffffff')

    def test_release_is_neutral_and_does_not_claim_all_advisories_have_ended(self):
        plan = flood_plan([entries((830304000100, 1), (830305000100, 3))])
        release = next(notice for notice in plan.notices if notice.warning_level == 1)
        self.assertEqual(release.lines, ('警報解除',))
        self.assertEqual(release.color, '#c8c8cb')
        self.assertEqual(release.styles[0].background, 'none')
        self.assertIn('氾濫注意情報への切替', plan.notice_lines[0])
        self.assertIn('安全を示すものではありません', plan.notice_lines[0])
        self.assertEqual(flood_plan([received()]).notice_lines, ())
        with self.assertRaises(ValueError):
            flood_plan([entries((830304000100, 1), extra=((41, 2, 2),))])

    def test_priority_regular_training_issues_and_corrections_are_supported(self):
        for classification in (2, 3, 7):
            for information in (0, 1):
                with self.subTest(classification=classification, information=information):
                    self.assertTrue(flood_plan([received((14, 3, classification),
                                                        (41, 2, information))]).notices)
        for classification in (0, 1, 4, 5, 6):
            with self.subTest(classification=classification), self.assertRaises(ValueError):
                flood_plan([received((14, 3, classification))])
        for information in (2, 3):
            with self.subTest(information=information), self.assertRaises(ValueError):
                flood_plan([received((41, 2, information))])

    def test_all_other_river_codes_and_unknown_codes_reject_the_complete_map(self):
        self.assertEqual(len(NON_SPECIFIC_CODES), 57)
        self.assertEqual(len(REGION_CODES), 338)
        for code in (*sorted(NON_SPECIFIC_CODES), 0, 123456789012):
            with self.subTest(code=code), self.assertRaises(ValueError):
                flood_plan([received(), entries((code, 2))])
        for level in (0, *range(5, 16)):
            with self.subTest(level=level), self.assertRaises(ValueError):
                flood_plan([received(), entries((830304000100, level))])

    def test_leading_zero_region_codes_are_preserved_as_numeric_dcr_codes(self):
        for code in sorted(REGION_CODES):
            if code < 100_000_000_000:
                with self.subTest(code=code):
                    notice, = flood_plan([entries((code, 2))]).notices
                    self.assertEqual(notice.code, code)
                    self.assertEqual(len(f'{notice.code:012d}'), 12)
        notice, = flood_plan([entries((10175000100, 2))]).notices
        self.assertEqual(notice.title, '新川(北海道道央)')

    def test_exact_duplicates_merge_but_conflicting_warnings_cannot_be_summarized_as_maximum(self):
        first = entries((830304000100, 2), (830305000100, 3))
        second = entries((830305000100, 3), (830304007100, 4))
        plan = flood_plan([first, second, first])
        self.assertEqual(len(plan.notices), 3)
        self.assertEqual(plan, flood_plan([second, first]))
        for first_level, second_level in ((1, 4), (2, 3), (4, 3)):
            with self.subTest(levels=(first_level, second_level)), self.assertRaises(ValueError):
                flood_plan([entries((830304000100, first_level)),
                            entries((830304000100, second_level))])
        with self.assertRaises(ValueError):
            flood_plan([entries((830304000100, 2), (830304000100, 3))])

    def test_different_issue_times_classifications_and_information_types_do_not_merge(self):
        for fields in (((35, 6, 11),), ((14, 3, 2),), ((41, 2, 1),)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                flood_plan([received(), received(*fields)])
        with self.assertRaises(ValueError):
            flood_plan([received(), report('weather')])

    def test_missing_entries_or_mismatched_arrays_do_not_silently_drop_values(self):
        for reports in ([], [entries()]):
            with self.assertRaises(ValueError):
                flood_plan(reports)
        for field in ('flood_forecast_regions_raw', 'flood_warning_levels_raw'):
            for replacement in ([], None, [830304000100] * 4):
                value = received()
                setattr(value, field, replacement)
                with self.subTest(field=field, replacement=replacement), self.assertRaises(ValueError):
                    flood_plan([value])
            value = received()
            getattr(value, field).pop()
            with self.subTest(field=field), self.assertRaises(ValueError):
                flood_plan([value])

    def test_missing_issue_time_and_numeric_lookalikes_are_rejected(self):
        for timestamp in (None, datetime(2026, 11, 10)):
            value = received()
            value.report_time = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                flood_plan([value])
        for field, replacement in (('flood_forecast_regions_raw', 830304000100.0),
                                   ('flood_forecast_regions_raw', '830304000100'),
                                   ('flood_warning_levels_raw', True),
                                   ('flood_warning_levels_raw', '2')):
            value = received()
            getattr(value, field)[0] = replacement
            with self.subTest(field=field, replacement=replacement), self.assertRaises(ValueError):
                flood_plan([value])


if __name__ == '__main__':
    unittest.main()
