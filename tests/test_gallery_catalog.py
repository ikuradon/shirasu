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

from message_format import report_message
from scripts.gallery_catalog import _dcx_training_variant, load_cases


ROOT = Path(__file__).resolve().parents[1]


def decoded(case):
    return [azarashi.decode(sentence, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
            for sentence, ms in case.records]


class GalleryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.cases = {case.slug: case for case in load_cases()}

    def test_catalog_covers_image_categories_with_stable_unique_output_names(self):
        self.assertEqual(len(self.cases), 30)
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

    def test_synthetic_dcx_preserves_source_provenance_and_only_changes_declared_fields(self):
        variants = (
            ('dcx-municipality', 'dcx-lalert', ((146, 16),)),
            ('dcx-ward', 'dcx-lalert', ()),
            ('dcx-wards', 'dcx-jalert', ((146, 1), (147, 64))),
            ('dcx-prefectures', 'dcx-jalert', ((147, 64),)),
            ('dcx-ellipse', 'dcx-mtinfo', ((162, 52),)),
        )
        for slug, original_slug, changed_fields in variants:
            with self.subTest(case=slug):
                case, original = self.cases[slug], self.cases[original_slug]
                self.assertEqual((case.source_kind, case.classification, case.state),
                                 ('synthetic', 'training', 'issue'))
                self.assertIsNone(case.source_url)
                self.assertEqual(case.source_file, original.source_file)
                self.assertEqual(case.source_sha256, original.source_sha256)
                self.assertIn('合成訓練例', case.source_label)
                self.assertIn('合成訓練例', case.description)
                self.assertIn('Vn=1', case.description)
                self.assertIn('A1=0', case.description)
                self.assertNotEqual(case.records[0][0], original.records[0][0])
                self.assertEqual(case.records[0][1], original.records[0][1])
                self.assertEqual(decoded(original)[0].camf.vn, 0)
                # The independent decoder verifies CRC-24Q and the NMEA checksum.
                report = decoded(case)[0]
                self.assertEqual((report.camf.vn, report.camf.a1), (1, 0))
                self.assertEqual((report.camf.sdmt, report.camf.sdm),
                                 (decoded(original)[0].camf.sdmt, decoded(original)[0].camf.sdm))
                # The satellite mask, original hazard and all unlisted fields stay intact.
                payloads = [int(item.records[0][0].split('*')[0].split(',')[2] + '0', 16)
                            for item in (case, original)]
                allowed = 0
                for offset, width in (*changed_fields, (24, 2), (214, 6), (226, 24)):
                    allowed |= ((1 << width) - 1) << (256 - offset - width)
                self.assertEqual((payloads[0] ^ payloads[1]) & ~allowed, 0)

    def test_synthetic_dcx_selects_exact_areas_and_preserves_the_basic_ellipse(self):
        municipality = decoded(self.cases['dcx-municipality'])[0]
        self.assertEqual(municipality.camf.ex1, 1202)
        self.assertEqual(municipality.ex1_target_area_ja, '北海道函館市')
        self.assertFalse(municipality.ignore_ex1)
        self.assertTrue(municipality.ignore_a12_to_a16)
        ward = decoded(self.cases['dcx-ward'])[0]
        self.assertEqual(ward.camf.ex1, 1101)
        self.assertEqual(ward.ex1_target_area_ja, '札幌市中央区')
        self.assertFalse(ward.ignore_ex1)
        self.assertTrue(ward.ignore_a12_to_a16)
        wards = decoded(self.cases['dcx-wards'])[0]
        self.assertEqual(wards.camf.ex8, 1)
        self.assertEqual(wards.camf.ex9, (1101 << 48) | (1102 << 32))
        self.assertEqual(wards.ex9_target_area_list_ja, ['札幌市中央区', '札幌市北区'])
        self.assertFalse(wards.ignore_ex8_to_ex9)
        prefectures = decoded(self.cases['dcx-prefectures'])[0]
        self.assertEqual(prefectures.camf.ex8, 0)
        self.assertEqual(prefectures.camf.ex9, 7 << 17)
        self.assertEqual(prefectures.ex9_target_area_list_ja, ['北海道', '青森県', '岩手県'])
        ellipse = decoded(self.cases['dcx-ellipse'])[0]
        original = decoded(self.cases['dcx-mtinfo'])[0]
        self.assertTrue(ellipse.ignore_ex2_to_ex7)
        self.assertFalse(ellipse.ignore_a12_to_a16)
        self.assertEqual([getattr(ellipse.camf, f'ex{index}') for index in range(2, 8)], [0] * 6)
        for field in ('a12', 'a13', 'a14', 'a15', 'a16', 'a17', 'a18'):
            self.assertEqual(getattr(ellipse.camf, field), getattr(original.camf, field))

    def test_secondary_ellipse_samples_preserve_the_foreign_source_and_main_geometry(self):
        original = self.cases['dcx-outside-japan']
        source_report = decoded(original)[0]
        variants = (
            ('dcx-secondary-ellipse', (2, 3, 8, 30)),
            ('dcx-secondary-overlap', (0, 7, 0, 30)),
            ('dcx-secondary-evacuation', (2, 3, 8, 0)),
            ('dcx-evacuation-overlap', (0, 0, 0, 0)),
        )
        for slug, expected in variants:
            with self.subTest(case=slug):
                case = self.cases[slug]
                self.assertEqual((case.source_kind, case.classification, case.state),
                                 ('synthetic', 'training', 'issue'))
                self.assertEqual(case.category, 'OutsideJapan')
                self.assertIsNone(case.source_url)
                self.assertEqual(case.source_file, original.source_file)
                self.assertEqual(case.source_sha256, original.source_sha256)
                self.assertIn('公開受信ログ', case.source_label)
                for text in (case.title, case.source_label, case.description):
                    self.assertIn('合成訓練例', text)
                self.assertIn('国外Vn=0', case.description)
                self.assertIn('ビット129〜145', case.description)
                self.assertNotEqual(case.records[0][0], original.records[0][0])
                self.assertEqual(case.records[0][1], original.records[0][1])
                # Decoding independently checks both the payload CRC and NMEA checksum.
                report = decoded(case)[0]
                self.assertEqual((report.camf.a1, report.camf.a2, report.camf.vn), (0, 103, 0))
                self.assertEqual((report.camf.a9, report.camf.a10, report.camf.a17), (0, 0, 2))
                self.assertEqual(tuple(getattr(report.camf, f'c{i}') for i in range(7, 11)), expected)
                self.assertEqual(report.c7_shift_of_second_ellipse_centre, expected[0])
                self.assertEqual(report.c8_homothetic_factor_of_second_ellipse, (expected[1] + 1) / 4)
                self.assertEqual(report.c9_bearing_angle_of_second_ellipse, expected[2] * 11.25)
                for field in ('a12', 'a13', 'a14', 'a15', 'a16'):
                    self.assertEqual(getattr(report.camf, field), getattr(source_report.camf, field))
                payloads = [int(item.records[0][0].split('*')[0].split(',')[2] + '0', 16)
                            for item in (case, original)]
                allowed = 0
                for offset, width in ((24, 2), (129, 17), (226, 24)):
                    allowed |= ((1 << width) - 1) << (256 - offset - width)
                self.assertEqual((payloads[0] ^ payloads[1]) & ~allowed, 0,
                                 'Only B3 settings, the forced training flag and CRC may change')
        evacuation = self.cases['dcx-secondary-evacuation']
        self.assertIn('既定指示', evacuation.description)
        self.assertIn('投稿本文', evacuation.description)
        self.assertNotIn('未対応', evacuation.description)
        self.assertNotIn('未対応', evacuation.tags)
        overlap = self.cases['dcx-evacuation-overlap']
        self.assertIn('0.25倍', overlap.description)
        self.assertIn('同心', overlap.description)
        self.assertIn('主範囲の指示が優先', overlap.description)

    def test_earthquake_vector_sample_preserves_source_geometry_and_original_post_text(self):
        case = self.cases['dcx-earthquake-vector']
        original = self.cases['dcx-outside-japan']
        source_report = decoded(original)[0]
        self.assertEqual((case.source_kind, case.classification, case.state, case.category),
                         ('synthetic', 'training', 'issue', 'OutsideJapan'))
        self.assertEqual((case.source_file, case.source_sha256),
                         (original.source_file, original.source_sha256))
        self.assertIsNone(case.source_url)
        for text in (case.title, case.source_label, case.description):
            self.assertIn('合成訓練例', text)
        for text in ('フレーム70', '国外Vn=0', 'ビット40〜46', '129〜145', 'CRC', 'NMEA',
                     '0°', '0.25倍', '方位基準', '未確認', '主楕円だけを描かずトピック表示'):
            self.assertIn(text, case.description)
        self.assertNotEqual(case.records[0][0], original.records[0][0])
        self.assertEqual(case.records[0][1], original.records[0][1])
        # Decode the changed frame independently, checking CRC and NMEA checksum.
        report = decoded(case)[0]
        self.assertEqual((report.camf.a1, report.camf.a2, report.camf.vn), (0, 103, 0))
        self.assertEqual((report.camf.a4, report.camf.a17), (36, 3))
        self.assertEqual(tuple(getattr(report.camf, f'd{i}') for i in range(1, 5)), (5, 3, 0, 0))
        self.assertEqual(report.d1_magnitude_on_richter_scale, '6.0-6.9 - Strong')
        self.assertEqual(report.d2_seismic_coefficient, '5 weak')
        self.assertEqual(report.d3_azimuth_from_centre_of_main_ellipse_to_epicentre, 0.)
        self.assertEqual(report.d4_vector_length_between_centre_of_main_ellipse_and_epicentre, .25)
        for field in ('a6', 'a7', 'a12', 'a13', 'a14', 'a15', 'a16'):
            self.assertEqual(getattr(report.camf, field), getattr(source_report.camf, field))
        self.assertEqual(report_message(report), f'{report}\n---\nNMEA: {report.nmea}')
        payloads = [int(item.records[0][0].split('*')[0].split(',')[2] + '0', 16)
                    for item in (case, original)]
        allowed = 0
        for offset, width in ((24, 2), (40, 7), (129, 17), (226, 24)):
            allowed |= ((1 << width) - 1) << (256 - offset - width)
        self.assertEqual((payloads[0] ^ payloads[1]) & ~allowed, 0,
                         'Only the hazard, B4 settings, forced training flag and CRC may change')

    def test_dcx_derivation_always_forces_the_test_flag_without_altering_satellite_designation(self):
        original = self.cases['dcx-lalert']
        sentence, ms = original.records[0]
        # Even an explicit attempt to select an ordinary message cannot make a synthetic alert.
        changed = _dcx_training_variant(sentence, ((24, 2, 1), (214, 6, 1)))
        report = azarashi.decode(changed, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
        self.assertEqual(report.camf.a1, 0)
        self.assertEqual((report.camf.sdmt, report.camf.sdm),
                         (decoded(original)[0].camf.sdmt, decoded(original)[0].camf.sdm))

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
