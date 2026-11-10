"""The offline gallery keeps provenance, message meaning and dates intact."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import azarashi
from azarashi.reports import dcr

from scripts.gallery_catalog import load_cases


ROOT = Path(__file__).resolve().parents[1]


def decoded(case):
    return [azarashi.decode(sentence, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
            for sentence, ms in case.records]


class GalleryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.cases = {case.slug: case for case in load_cases()}

    def test_catalog_covers_image_categories_with_stable_unique_output_names(self):
        self.assertEqual(len(self.cases), 20)
        self.assertEqual({case.category for case in self.cases.values()}, {
            'Typhoon', 'Hypocenter', 'Weather', 'EarthquakeEarlyWarning', 'SeismicIntensity',
            'Tsunami', 'Volcano', 'AshFall', 'Flood', 'NorthwestPacificTsunami', 'Marine',
            'LAlert', 'JAlert', 'MTInfo', 'OutsideJapan',
        })
        self.assertEqual(tuple(self.cases.values()), load_cases())
        for case in self.cases.values():
            with self.subTest(case=case.slug):
                self.assertRegex(case.slug, r'^[a-z][a-z0-9-]*$')
                self.assertTrue(case.title and case.description and case.source_label)
                self.assertEqual({type(report).__name__ for report in decoded(case)}, {case.category})
                self.assertEqual(len(case.records), len(set(case.records)))

    def test_recorded_selections_preserve_original_frames_and_fixed_date_anchors(self):
        manifest = json.loads((ROOT / 'tests/fixtures/qzqsm/manifest.json').read_text())
        for case in self.cases.values():
            with self.subTest(case=case.slug):
                raw = (ROOT / case.source_file).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), case.source_sha256)
                if case.source_kind == 'synthetic':
                    self.assertIsNone(case.source_url)
                    continue
                self.assertRegex(case.source_url, r'/[0-9a-f]{40}/tests/qzqsm_[a-z0-9_]+\.log$')
                source = next(item for item in manifest['recordings'].values()
                              if item['source_url'] == case.source_url)
                reference_ms = round(datetime.fromisoformat(source['reference_time']).timestamp() * 1000)
                original = raw.decode().splitlines()
                for sentence, ms in case.records:
                    self.assertIn(sentence, original)
                    self.assertEqual(ms, reference_ms)

    def test_batches_keep_all_forecasts_without_combining_different_issuances(self):
        typhoon = decoded(self.cases['typhoon-track'])
        self.assertEqual(len(typhoon), 7)
        self.assertEqual({report.typhoon_number_raw for report in typhoon}, {26})
        self.assertEqual([report.elapsed_time_from_reference_time_raw for report in typhoon],
                         [0, 12, 24, 48, 72, 96, 120])
        ash = decoded(self.cases['ash-detail'])
        expected = json.loads((ROOT / 'tests/fixtures/qzqsm/official_scenarios.json').read_text())['ash_detail']
        self.assertEqual(len(ash), expected['frame_count'])
        entries = [entry for report in ash for entry in zip(report.expected_ash_fall_times_raw,
                   report.ash_fall_warning_codes_raw, report.local_governments_raw, strict=True)]
        self.assertCountEqual(entries, [(row['hour'], row['warning_code'], row['municipality_code'])
                                       for row in expected['entries']])
        weather = decoded(self.cases['weather-many'])
        self.assertEqual(len(weather), 3)
        self.assertEqual(len({code for report in weather for code in report.weather_forecast_regions_raw}), 7)
        for reports in (typhoon, ash, weather):
            self.assertEqual(len({(report.report_time, report.report_classification_no,
                                   report.information_type_no) for report in reports}), 1)

    def test_training_origin_and_cancellation_or_release_are_separate_properties(self):
        for name in ('nwp', 'weather-cancel', 'seismic-islands'):
            case = self.cases[name]
            self.assertEqual((case.source_kind, case.classification), ('synthetic', 'training'))
            self.assertEqual({report.report_classification_no for report in decoded(case)}, {7})
        self.assertEqual(decoded(self.cases['weather-cancel'])[0].information_type_no, 2)
        self.assertEqual(self.cases['weather-cancel'].state, 'cancel')
        self.assertEqual(decoded(self.cases['seismic-islands'])[0].prefectures_raw, [13])
        for name in ('tsunami-release', 'flood-release'):
            case = self.cases[name]
            self.assertEqual((case.source_kind, case.classification, case.state),
                             ('receiver', 'training', 'release'))
            self.assertEqual(decoded(case)[0].information_type_no, 0)
        self.assertEqual(decoded(self.cases['tsunami-release'])[0].tsunami_warning_code_raw, 2)
        self.assertEqual(decoded(self.cases['flood-release'])[0].flood_warning_levels_raw, [1])
        self.assertEqual((self.cases['weather'].source_kind, self.cases['weather'].classification),
                         ('receiver', 'ordinary'))
        self.assertEqual((self.cases['eew'].source_kind, self.cases['eew'].classification),
                         ('receiver', 'training'))

    def test_modified_public_source_is_rejected_before_selecting_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / 'tests/fixtures/qzqsm'
            shutil.copytree(ROOT / 'tests/fixtures/qzqsm', corpus)
            with (corpus / 'qzqsm_260924.log').open('ab') as recording:
                recording.write(b'\n# modified\n')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                load_cases(root)

    def test_catalog_reading_does_not_touch_nankai_decoder_state(self):
        pages, announcement = {99: object()}, object()
        with patch.object(dcr.NankaiTroughEarthquake, 'reports', pages), \
                patch.object(dcr.NankaiTroughEarthquake, 'announcement', announcement), \
                patch.object(dcr.NankaiTroughEarthquake, 'completed', False):
            load_cases()
            self.assertIs(dcr.NankaiTroughEarthquake.reports, pages)
            self.assertEqual(set(pages), {99})
            self.assertIs(dcr.NankaiTroughEarthquake.announcement, announcement)
            self.assertFalse(dcr.NankaiTroughEarthquake.completed)


if __name__ == '__main__':
    unittest.main()
