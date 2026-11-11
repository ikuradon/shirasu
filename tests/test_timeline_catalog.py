"""Timeline examples keep synthetic timing separate from their source frames."""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import azarashi
from azarashi.reports import dcr

from scripts.gallery_catalog import load_cases
from scripts.timeline_catalog import load_scenarios


ROOT = Path(__file__).resolve().parents[1]


def _decoded(records):
    with patch.object(dcr.NankaiTroughEarthquake, 'completed', False), \
            patch.object(dcr.NankaiTroughEarthquake, 'reports', {}), \
            patch.object(dcr.NankaiTroughEarthquake, 'announcement', None):
        return tuple(azarashi.decode(sentence, timestamp=datetime.fromtimestamp(
            milliseconds / 1000, timezone.utc)) for sentence, milliseconds in records)


class TimelineCatalogTests(unittest.TestCase):
    def setUp(self):
        self.scenarios = {scenario.slug: scenario for scenario in load_scenarios()}
        self.cases = {case.slug: case for case in load_cases()}

    def test_all_scenarios_have_ordered_synthetic_times_and_valid_training_frames(self):
        self.assertEqual(len(self.scenarios), 4)
        self.assertEqual(tuple(self.scenarios.values()), load_scenarios())
        for scenario in self.scenarios.values():
            with self.subTest(scenario=scenario.slug):
                self.assertRegex(scenario.slug, r'^[a-z][a-z0-9-]*$')
                self.assertTrue(scenario.title and scenario.description)
                self.assertEqual(len(scenario.records), len(scenario.arrival_seconds))
                self.assertEqual(len(scenario.records), len(scenario.receipt_labels))
                self.assertTrue(all(scenario.receipt_labels))
                self.assertEqual(scenario.arrival_seconds[0], 0)
                self.assertEqual(tuple(sorted(scenario.arrival_seconds)), scenario.arrival_seconds)
                self.assertGreaterEqual(scenario.until_seconds,
                                        scenario.arrival_seconds[-1] + 180)
                self.assertEqual({report.report_classification_no for report in _decoded(scenario.records)}, {7})
                for source in scenario.sources:
                    self.assertEqual(source['kind'], 'synthetic')
                    self.assertTrue(source['transformation'])
                    self.assertEqual(hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest(),
                                     source['sha256'])

    def test_late_pages_preserve_all_original_records_and_decoder_timestamps(self):
        scenario = self.scenarios['nankai-late']
        self.assertEqual(scenario.records, self.cases['nankai-advisory'].records)
        self.assertEqual(scenario.arrival_seconds, (0, 240, 250, 260, 270))
        self.assertEqual([report.page_number for report in _decoded(scenario.records)], list(range(1, 6)))

    def test_correction_changes_only_information_type_and_retains_late_old_page(self):
        scenario = self.scenarios['nankai-correction']
        pages = self.cases['nankai-advisory'].records
        self.assertEqual(scenario.records[:2], pages[:2])
        self.assertEqual(scenario.records[-1], pages[2])
        original, corrected = _decoded(pages), _decoded(scenario.records[2:7])
        for before, after, source, changed in zip(original, corrected, pages, scenario.records[2:7], strict=True):
            self.assertEqual(before.information_type_no, 0)
            self.assertEqual(after.information_type_no, 1)
            self.assertEqual(before.text_information, after.text_information)
            self.assertEqual(before.page_number, after.page_number)
            self.assertEqual(before.report_time, after.report_time)
            self.assertEqual(source[1], changed[1])
        self.assertLess(scenario.arrival_seconds[6], scenario.arrival_seconds[2] + 180)
        self.assertGreater(scenario.arrival_seconds[-1], scenario.arrival_seconds[6])

    def test_cancellation_is_a_partial_message_but_arrives_before_timeout(self):
        scenario = self.scenarios['nankai-cancellation']
        reports = _decoded(scenario.records)
        self.assertEqual([report.information_type_no for report in reports], [0, 0, 2, 0])
        self.assertEqual(scenario.arrival_seconds, (0, 20, 60, 120))
        self.assertEqual(reports[2].page_number, 1)
        self.assertEqual(scenario.records[-1], self.cases['nankai-advisory'].records[2])

    def test_typhoon_additional_frames_keep_a_single_issuance_and_full_forecast_order(self):
        scenario = self.scenarios['typhoon-additional']
        self.assertEqual(scenario.records, self.cases['typhoon-cancel-reference'].cancel_reference)
        reports = _decoded(scenario.records)
        self.assertEqual({type(report) for report in reports}, {dcr.Typhoon})
        self.assertEqual({report.information_type_no for report in reports}, {0})
        self.assertEqual(len({(report.report_time, report.typhoon_number_raw) for report in reports}), 1)
        self.assertEqual([report.elapsed_time_from_reference_time_raw for report in reports],
                         [0, 12, 24, 48, 72, 96])
        self.assertLess(scenario.arrival_seconds[1], 180)
        self.assertGreater(scenario.arrival_seconds[2], 180)
        self.assertLess(scenario.arrival_seconds[-1], scenario.arrival_seconds[2] + 180)


if __name__ == '__main__':
    unittest.main()
