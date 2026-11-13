"""DCX scope follows raw selectors, not incidental decoded place names."""

from copy import deepcopy
import importlib.util
import unittest
from unittest.mock import Mock, patch

HAS_DECODER = importlib.util.find_spec('azarashi') is not None
if HAS_DECODER:
    import azarashi
    from image_service import preview_dcx_reports as dcx_reports
    from image_service.preview_dcx_reports import Area, Ellipse, Point, SecondaryEllipse, dcx_plan
    from image_service.preview_diagnostics import reason
    from shirasu.reports.format import ATTRIBUTION, report_message
    from tests.qzqsm_samples import frames, reference_time
    from tools.preview.gallery_catalog import _field_variant

HAS_RENDERER = HAS_DECODER and all(importlib.util.find_spec(name) for name in ('numpy', 'matplotlib'))
if HAS_RENDERER:
    from image_service import preview_wide_dcx as dcx_images
    from image_service import preview_dcx_areas as dcx_areas
    from image_service.preview_diagnostics import PreviewFallback, error_reasons


@unittest.skipUnless(HAS_DECODER, 'DCX plans need the pinned azarashi dependency')
class DcxReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = frames('dcx_archive_20260917')
        cls.when = reference_time('dcx_archive_20260917')

    def report(self, index=70, *fields, version=1):
        # Explicitly synthetic: preserve raw recordings and recalculate CRC.
        changed = _field_variant(self.source[index], ((214, 6, version), *fields))
        return azarashi.decode(changed, timestamp=self.when)

    def plan(self, index=70, *fields, version=1):
        return dcx_plan((self.report(index, *fields, version=version),))

    def secondary_report(self, *fields):
        # Explicit foreign test variant: international library #1, C7=2,
        # C8=3 (scale 1), C9=16 (180 degrees), C10=5 (keep phone charged).
        return self.report(70, (26, 9, 100), (66, 1, 0), (67, 3, 0), (129, 2, 2),
                           (131, 2, 2), (133, 3, 3), (136, 5, 16), (141, 5, 5), *fields)

    def earthquake_report(self, *fields, version=0):
        # Synthetic foreign earthquake with a zero-code (.25 axis) vector.
        return self.report(70, (26, 9, 100), (40, 7, 36), (66, 1, 0), (67, 3, 0), (129, 2, 3),
                           (131, 4, 5), (135, 3, 3), (138, 4, 0), (142, 4, 0),
                           *fields, version=version)

    def assert_topic(self, plan):
        self.assertEqual(plan.scope_kind, 'topic')
        self.assertEqual(plan.geometries, ())
        self.assertEqual(plan.areas, ())

    def test_public_domestic_version_zero_is_not_assumed_compatible(self):
        for index in (0, 115, 163):
            with self.subTest(index=index):
                original = azarashi.decode(self.source[index], timestamp=self.when)
                self.assertEqual(original.camf.vn, 0)
                plan = dcx_plan((original,))
                self.assert_topic(plan)
                self.assertEqual(plan.reasons, (reason('unsupported_version', 'selection', '国内DCX Vn=0'),))
        self.assert_topic(self.plan(version=63))

    def test_lalert_ex1_uses_exact_defined_jis_codes(self):
        plan = self.plan(0, (146, 16, 1202))
        self.assertEqual(plan.scope_kind, 'area')
        self.assertEqual(plan.areas, (Area('municipalities', 120200, '北海道函館市'),))
        self.assertEqual(plan.geometries, ())
        self.assertEqual([(item.code, item.stage) for item in plan.reasons], [('no_geometry', 'geometry')])
        prefecture = self.plan(0, (146, 16, 13000))
        self.assertEqual(prefecture.areas, (Area('prefectures', 13, '東京都'),))
        for code in (0, 13400, 65535):
            with self.subTest(code=code):
                fallback = self.plan(0, (146, 16, code))
                self.assert_topic(fallback)
                self.assertEqual(fallback.reasons[-1].code, 'unknown_area' if code else 'no_area')
                self.assertEqual(fallback.reasons[-1].stage, 'area')

    def test_ward_code_is_not_silently_replaced_with_its_city(self):
        # Asset availability is the renderer's responsibility. Preserve the
        # exact defined ward code so it can reject the *whole* candidate.
        plan = self.plan(0)
        self.assertEqual(plan.areas, (Area('municipalities', 110100, '札幌市中央区'),))
        self.assertNotEqual(plan.areas[0].code, 110000)

    def test_current_and_retired_ward_codes_keep_their_distinct_identity(self):
        # Old and current wards may share a name. The semantic layer must not
        # rewrite one code to another to obtain an available boundary.
        bits = (27101 << 48) | (27127 << 32) | (1101 << 16) | 1102
        plan = self.plan(115, (146, 1, 1), (147, 64, bits))
        self.assertEqual(plan.areas, (
            Area('municipalities', 2710100, '大阪市北区'),
            Area('municipalities', 2712700, '大阪市北区'),
            Area('municipalities', 110100, '札幌市中央区'),
            Area('municipalities', 110200, '札幌市北区'),
        ))

    def test_jalert_prefecture_mask_keeps_every_selected_prefecture(self):
        plan = self.plan(115, (146, 1, 0), (147, 64, 7 << 17))
        self.assertEqual(plan.scope_kind, 'area')
        self.assertEqual(tuple(area.code for area in plan.areas), (1, 2, 3))
        self.assertEqual(tuple(area.name for area in plan.areas), ('北海道', '青森県', '岩手県'))
        self.assertEqual(plan.geometries, ())
        all_prefectures = self.plan(115)
        self.assertEqual(tuple(area.code for area in all_prefectures.areas), tuple(range(1, 48)))
        self.assert_topic(self.plan(115, (147, 64, (7 << 17) | 1)))
        self.assert_topic(self.plan(115, (147, 64, 0)))

    def test_jalert_municipal_list_is_all_or_nothing(self):
        bits = (1202 << 48) | (13101 << 32) | (47201 << 16)
        plan = self.plan(115, (146, 1, 1), (147, 64, bits))
        self.assertEqual(tuple(area.code for area in plan.areas), (120200, 1310100, 4720100))
        for invalid in (13400, 65535, 13000):
            with self.subTest(invalid=invalid):
                self.assert_topic(self.plan(115, (146, 1, 1), (147, 64, bits | invalid)))
        duplicate = self.plan(115, (146, 1, 1), (147, 64, bits | 1202))
        self.assertEqual(duplicate.areas, plan.areas)

    def test_mtinfo_refined_main_ellipse_does_not_use_ex1_as_target_area(self):
        plan = self.plan(163, (162, 52, 0))
        self.assertEqual(plan.scope_kind, 'geometry')
        self.assertEqual(plan.geometries,
                         (Ellipse(140.717397, 41.765507, 3.413, 2.524, 39.375),))
        self.assertEqual(plan.areas, (), 'EX1 is not a target when the main ellipse is present')
        self.assertEqual(plan.topic, '洪水')
        self.assertEqual(plan.headline, '洪水情報')
        # The semantic plan retains a lower-priority candidate's limitation;
        # draw_dcx removes this irrelevant reason when geometry succeeds.
        self.assertEqual([(item.code, item.stage) for item in plan.reasons], [('no_area', 'area')])

    def test_raw_zero_axes_are_small_ellipses_and_never_fabricated_point_pins(self):
        plan = self.plan(70, (113, 5, 0), (118, 5, 0), (131, 15, 0))
        self.assertEqual(len(plan.geometries), 1)
        self.assertIsInstance(plan.geometries[0], Ellipse)
        self.assertEqual(plan.geometries[0].semi_major_km, .216)
        smallest = self.plan(70, (113, 5, 0), (118, 5, 0),
                             (131, 15, 0), (137, 3, 7), (140, 3, 7))
        self.assertEqual(smallest.geometries[0].semi_minor_km, .027)

    def test_b2_keeps_hazard_point_separate_from_main_ellipse(self):
        report = self.report(70, (26, 9, 100), (129, 2, 1), (131, 7, 64), (138, 7, 65))
        plan = dcx_plan((report,))
        self.assertEqual(len(plan.geometries), 2)
        ellipse, point = plan.geometries
        self.assertIsInstance(ellipse, Ellipse)
        self.assertIsInstance(point, Point)
        self.assertEqual(point.role, 'hazard_center')
        self.assertNotEqual((ellipse.lon, ellipse.lat), (point.lon, point.lat))
        self.assertEqual(point.lon, report.c6_longitude_of_centre_of_hazard)
        self.assertEqual(point.lat, report.c5_latitude_of_centre_of_hazard)
        report.c5_latitude_of_centre_of_hazard = 91
        self.assert_topic(dcx_plan((report,)))

    def test_unsupported_extra_geometry_is_not_partially_drawn(self):
        plan = self.plan(70, (26, 9, 100), (129, 2, 3), (40, 7, 36))
        self.assert_topic(plan)
        self.assertEqual(plan.reasons[0].code, 'unsupported_geometry')
        self.assertEqual(plan.reasons[0].stage, 'geometry')
        self.assertTrue(plan.reasons[0].detail.startswith('B4'))
        scalar = self.plan(70, (26, 9, 100), (129, 2, 3), (40, 7, 68))
        self.assertEqual(scalar.scope_kind, 'geometry')

    def test_mtinfo_target_and_additional_ellipse_have_independent_centres_and_roles(self):
        for direction, role in ((0, 'evacuate_from'), (1, 'evacuate_to')):
            for state, status in ((0, '訓練'), (1, '発表'), (2, '更新'), (3, '解除')):
                with self.subTest(direction=direction, state=state):
                    report = self.report(163, (162, 1, direction), (24, 2, state))
                    before = str(report), deepcopy(report.camf.get_params())
                    plan = dcx_plan((report,))
                    self.assertEqual((plan.scope_kind, plan.status), ('geometry', status))
                    self.assertEqual(plan.geometries,
                                     (Ellipse(140.717397, 41.765507, 3.413, 2.524, 39.375),))
                    self.assertEqual(plan.additional_ellipse,
                                     Ellipse(140.701261, 41.757292, .535, .535, -37.96875, role))
                    self.assertEqual(plan.areas, (), 'EX1 must not expand an existing primary ellipse')
                    self.assertEqual((str(report), report.camf.get_params()), before)
        absent = self.plan(163, (162, 52, 0))
        self.assertEqual(absent.scope_kind, 'geometry')
        self.assertIsNone(absent.additional_ellipse)

    def test_mtinfo_ex2_only_is_a_real_unprojectable_ellipse_not_an_absent_instruction(self):
        report = self.report(163, (162, 52, 0), (162, 1, 1))
        self.assertIs(report.ignore_ex2_to_ex7, True, 'The pinned decoder ignores EX2 for its sentinel')
        plan = dcx_plan((report,))
        self.assertEqual(plan.scope_kind, 'geometry')
        self.assertEqual(plan.additional_ellipse,
                         Ellipse(45., -90., .216, .216, -90., 'evacuate_to'))
        self.assertEqual(len(plan.geometries), 1, 'Projection failure belongs to the whole renderer candidate')

    def test_mtinfo_additional_longitude_beyond_dateline_is_normalized_without_clipping(self):
        plan = self.plan(163, (180, 17, 131071))
        self.assertEqual(plan.scope_kind, 'geometry')
        self.assertEqual(plan.additional_ellipse.lon, -135.)

    def test_invalid_mtinfo_additional_fields_cannot_leave_a_primary_map_or_area(self):
        for fields in ((), ((80, 49, 0), (131, 15, 0))):
            for target, name, value in (
                    ('raw', 'ex3', -1), ('raw', 'ex2', True),
                    ('report', 'ex3_additional_ellipse_centre_latitude', float('nan')),
                    ('report', 'ignore_ex2_to_ex7', True)):
                with self.subTest(fields=fields, field=name):
                    report = self.report(163, *fields)
                    setattr(report.camf if target == 'raw' else report, name, value)
                    plan = dcx_plan((report,))
                    self.assert_topic(plan)
                    self.assertIsNone(plan.additional_ellipse)
                    self.assertEqual((plan.reasons[0].code, plan.reasons[0].stage),
                                     ('invalid_fields', 'geometry'))
        for library in (((66, 1, 0),), ((67, 3, 1),)):
            plan = self.plan(163, *library)
            self.assert_topic(plan)
            self.assertEqual(plan.reasons[0].code, 'unsupported_geometry')

    def test_mtinfo_additional_ellipse_cannot_replace_an_unknown_primary_target(self):
        for code in (0, 65535):
            with self.subTest(code=code):
                plan = self.plan(163, (80, 49, 0), (131, 15, 0), (146, 16, code))
                self.assert_topic(plan)
                self.assertIsNotNone(plan.additional_ellipse)

    def test_domestic_lalert_and_mtinfo_only_allow_b1_geometry(self):
        for index in (70, 163):
            for setting in (1, 2, 3):
                with self.subTest(index=index, setting=setting):
                    plan = self.plan(index, (162, 52, 0), (129, 2, setting))
                    self.assert_topic(plan)
                    self.assertEqual((plan.reasons[0].code, plan.reasons[0].stage),
                                     ('unsupported_geometry', 'geometry'))
                    self.assertIn('国内', plan.reasons[0].detail)

    def assert_earthquake_unconfirmed(self, report):
        plan = dcx_plan((report,))
        self.assert_topic(plan)
        self.assertEqual(plan.reasons[0], reason('unsupported_geometry', 'geometry',
                         'B4地震: 震央方位D3の基準方向・地点変換が未確認'))
        return plan

    def test_b4_earthquake_known_magnitudes_and_reserved_values_have_distinct_reasons(self):
        for value in range(16):
            with self.subTest(magnitude=value):
                report = self.earthquake_report((131, 4, value))
                if value <= 8:
                    self.assert_earthquake_unconfirmed(report)
                else:
                    plan = dcx_plan((report,))
                    self.assert_topic(plan)
                    self.assertEqual(plan.reasons[0], reason('unsupported_geometry', 'geometry',
                                     f'B4地震: 予約済みの規模 D1={value}'))

    def test_b4_earthquake_all_vector_codes_are_present_but_cannot_locate_a_point(self):
        factors = (.25, .5, .75, 1, 2, 3, 5, 10, 20, 30, 40, 50, 70, 100, 150, 200)
        for value in range(8):
            with self.subTest(coefficient=value):
                self.assert_earthquake_unconfirmed(self.earthquake_report((135, 3, value)))
        for value in range(16):
            with self.subTest(azimuth=value):
                report = self.earthquake_report((138, 4, value))
                self.assertEqual(report.d3_azimuth_from_centre_of_main_ellipse_to_epicentre, value * 22.5)
                self.assert_earthquake_unconfirmed(report)
            with self.subTest(distance=value):
                report = self.earthquake_report((142, 4, value))
                self.assertEqual(report.d4_vector_length_between_centre_of_main_ellipse_and_epicentre,
                                 factors[value])
                self.assert_earthquake_unconfirmed(report)

    def test_b4_earthquake_status_and_foreign_version_do_not_authorize_a_guessed_map(self):
        for state, status in ((0, '訓練'), (1, '発表'), (2, '更新'), (3, '解除')):
            for version in (0, 1, 63):
                with self.subTest(state=state, version=version):
                    report = self.earthquake_report((24, 2, state), version=version)
                    before = str(report), deepcopy(report.camf.get_params()), report.nmea
                    plan = self.assert_earthquake_unconfirmed(report)
                    self.assertEqual((plan.status, plan.topic, plan.headline), (status, '地震', '地震情報'))
                    self.assertEqual(report_message(report), f'{before[0]}\n\n{ATTRIBUTION}\n---\nNMEA: {before[2]}')
                    self.assertEqual((str(report), report.camf.get_params(), report.nmea), before)

    def test_b4_earthquake_requires_the_reviewed_international_library(self):
        for library, version in ((1, 0), (0, 1), (0, 7), (1, 7)):
            with self.subTest(library=library, version=version):
                report = self.earthquake_report((66, 1, library), (67, 3, version))
                plan = dcx_plan((report,))
                self.assert_topic(plan)
                self.assertEqual((plan.reasons[0].code, plan.reasons[0].stage),
                                 ('unsupported_geometry', 'geometry'))
                self.assertIn('未対応のライブラリ', plan.reasons[0].detail)
                self.assertIn(f'A9={library}', plan.reasons[0].detail)
                self.assertIn(f'A10={version}', plan.reasons[0].detail)
        for field, width in (('a9', 1), ('a10', 3)):
            for value in (None, False, -1, 1 << width, float('nan')):
                with self.subTest(field=field, value=value):
                    report = self.earthquake_report()
                    setattr(report.camf, field, value)
                    plan = dcx_plan((report,))
                    self.assert_topic(plan)
                    self.assertEqual(plan.reasons[0], reason('invalid_fields', 'geometry', field))

    def test_b4_earthquake_cannot_bypass_the_domestic_b1_requirement(self):
        for index in (70, 163):
            with self.subTest(index=index):
                report = self.report(index, (40, 7, 36), (129, 2, 3),
                                     (131, 15, (5 << 11) | (3 << 8)), (162, 52, 0))
                plan = dcx_plan((report,))
                self.assert_topic(plan)
                self.assertEqual(plan.reasons[0], reason('unsupported_geometry', 'geometry',
                                 'B4: 国内通報はB1固定'))

    def test_b4_earthquake_malformed_raw_fields_and_inconsistent_packing_are_invalid(self):
        original = self.earthquake_report()
        for field, width in (('a18', 15), ('d1', 4), ('d2', 3), ('d3', 4), ('d4', 4)):
            for value in (None, True, -1, 1 << width, float('nan'), float('inf')):
                with self.subTest(field=field, value=value):
                    report = deepcopy(original)
                    setattr(report.camf, field, value)
                    plan = dcx_plan((report,))
                    self.assert_topic(plan)
                    self.assertEqual(plan.reasons[0], reason('invalid_fields', 'geometry', field))
            with self.subTest(missing=field):
                report = deepcopy(original)
                delattr(report.camf, field)
                self.assertEqual(dcx_plan((report,)).reasons[0], reason('invalid_fields', 'geometry', field))
        report = deepcopy(original)
        report.camf.a18 ^= 1
        plan = dcx_plan((report,))
        self.assert_topic(plan)
        self.assertEqual(plan.reasons[0], reason('invalid_fields', 'geometry',
                         'B4地震: A18とD1〜D4が不一致'))

    def test_b4_earthquake_decoded_fields_must_match_the_transmitted_values(self):
        original = self.earthquake_report()
        for field in ('d1_magnitude_on_richter_scale', 'd2_seismic_coefficient',
                      'd3_azimuth_from_centre_of_main_ellipse_to_epicentre',
                      'd4_vector_length_between_centre_of_main_ellipse_and_epicentre'):
            for value in (None, True, 'unrecognized', float('nan'), float('inf'), -1):
                with self.subTest(field=field, value=value):
                    report = deepcopy(original)
                    setattr(report, field, value)
                    plan = dcx_plan((report,))
                    self.assert_topic(plan)
                    self.assertEqual(plan.reasons[0], reason('invalid_fields', 'geometry', field))

    def test_scalar_b4_retains_its_existing_main_ellipse(self):
        # Non-earthquake B4 fields must not acquire the D3/D4 requirement.
        expected = dcx_plan((self.earthquake_report((129, 2, 1)),)).geometries[0]
        for hazard in (44, 68, 82):
            with self.subTest(hazard=hazard):
                report = self.earthquake_report((40, 7, hazard), (131, 15, 0))
                plan = dcx_plan((report,))
                self.assertEqual(plan.scope_kind, 'geometry')
                self.assertEqual(plan.geometries, (expected,))
                self.assertFalse(any(item.stage == 'geometry' for item in plan.reasons))
                self.assertEqual(report_message(report), f'{report}\n\n{ATTRIBUTION}\n---\nNMEA: {report.nmea}')

    def test_secondary_ellipse_keeps_main_frame_and_exact_transmitted_parameters(self):
        plan = dcx_plan((self.secondary_report(),))
        self.assertEqual(plan.scope_kind, 'geometry')
        main = Ellipse(141.297617, 43.040970, 10.933, 8.085, 53.4375)
        self.assertEqual(plan.geometries, (main, SecondaryEllipse(main, 2, 1., 180., 5)))
        self.assertEqual(plan.areas, ())
        self.assertFalse(any(isinstance(item, Point) for item in plan.geometries),
                         'Neither ellipse centre is a transmitted disaster location')

    def test_zero_secondary_geometry_codes_are_a_real_quarter_size_ellipse(self):
        plan = dcx_plan((self.secondary_report((131, 15, 1)),))
        self.assertEqual(plan.scope_kind, 'geometry')
        main, secondary = plan.geometries
        self.assertEqual(secondary, SecondaryEllipse(main, 0, .25, 0., 1))
        maximum = dcx_plan((self.secondary_report((131, 2, 3), (133, 3, 7), (136, 5, 31)),))
        main, secondary = maximum.geometries
        self.assertEqual(secondary, SecondaryEllipse(main, 3, 2., 348.75, 5))

    def test_secondary_instruction_codes_require_the_reviewed_international_library(self):
        for code in (*range(17), 30, 31):
            with self.subTest(code=code):
                plan = dcx_plan((self.secondary_report((141, 5, code)),))
                self.assertEqual(plan.scope_kind, 'geometry')
                self.assertEqual(plan.geometries[1].instruction_code, code)
                self.assertEqual(plan.geometries[1].role,
                                 'evacuation_destination' if code == 0 else 'secondary_target')
        for fields in (((66, 1, 1),), ((67, 3, 1),), ((67, 3, 7),)):
            with self.subTest(fields=fields):
                plan = dcx_plan((self.secondary_report(*fields),))
                self.assert_topic(plan)
                self.assertEqual(plan.reasons[0].code, 'unsupported_geometry')
                self.assertIn('ライブラリ', plan.reasons[0].detail)

    def test_evacuation_destination_is_not_an_additional_hazard_target(self):
        for state, expected_status in ((0, '訓練'), (1, '発表'), (2, '更新'), (3, '解除')):
            with self.subTest(state=state):
                report = self.secondary_report((24, 2, state), (141, 5, 0))
                before = (str(report), deepcopy(report.camf.get_params()))
                plan = dcx_plan((report,))
                self.assertEqual(plan.scope_kind, 'geometry')
                main, destination = plan.geometries
                self.assertEqual(destination,
                                 SecondaryEllipse(main, 2, 1., 180., 0, role='evacuation_destination'))
                self.assertEqual(plan.status, expected_status)
                self.assertEqual(plan.areas, ())
                self.assertEqual((str(report), report.camf.get_params()), before)

    def test_all_zero_b3_fields_keep_the_destination_inside_the_main_scope(self):
        plan = dcx_plan((self.secondary_report((131, 15, 0)),))
        self.assertEqual(plan.scope_kind, 'geometry')
        main, destination = plan.geometries
        self.assertEqual(destination,
                         SecondaryEllipse(main, 0, .25, 0., 0, role='evacuation_destination'))
        self.assertEqual(len(plan.geometries), 2, 'The main scope must still take priority in the overlap')

    def test_evacuation_destination_requires_the_same_library_and_version_as_its_text(self):
        for fields in (((66, 1, 1),), ((67, 3, 1),), ((67, 3, 7),)):
            with self.subTest(fields=fields):
                plan = dcx_plan((self.secondary_report((141, 5, 0), *fields),))
                self.assert_topic(plan)
                self.assertEqual(plan.reasons[0].code, 'unsupported_geometry')
                self.assertIn('ライブラリ', plan.reasons[0].detail)
        for version in (0, 1, 31, 63):
            with self.subTest(version=version):
                plan = dcx_plan((self.secondary_report((141, 5, 0), (214, 6, version)),))
                self.assertEqual(plan.scope_kind, 'geometry')
                self.assertEqual(plan.geometries[1].role, 'evacuation_destination')

    def test_evacuation_map_declines_a_descriptor_that_cannot_receive_default_post_guidance(self):
        report = self.secondary_report((141, 5, 0))
        # A stale decoded C10=0 cannot override a contradictory raw A18.
        # Both the post formatter and map must use the same qualification.
        report.camf.a18 ^= 1
        plan = dcx_plan((report,))
        self.assert_topic(plan)
        self.assertEqual((plan.reasons[0].code, plan.reasons[0].stage), ('invalid_fields', 'geometry'))

    def test_reserved_secondary_instructions_decline_the_whole_geometry(self):
        for code in range(17, 30):
            with self.subTest(code=code):
                plan = dcx_plan((self.secondary_report((141, 5, code)),))
                self.assert_topic(plan)
                self.assertEqual((plan.reasons[0].code, plan.reasons[0].stage),
                                 ('unsupported_geometry', 'geometry'))
                self.assertIn(f'C10={code}', plan.reasons[0].detail)
                self.assertIn('予約', plan.reasons[0].detail)

    def test_invalid_or_inconsistent_secondary_fields_do_not_leave_the_main_ellipse(self):
        changes = (('camf', 'c7', 4), ('camf', 'c8', -1), ('camf', 'c9', 32),
                   ('camf', 'c10', None), ('camf', 'c7', True),
                   ('report', 'c7_shift_of_second_ellipse_centre', 1),
                   ('report', 'c8_homothetic_factor_of_second_ellipse', float('nan')),
                   ('report', 'c8_homothetic_factor_of_second_ellipse', None),
                   ('report', 'c9_bearing_angle_of_second_ellipse', 181.))
        for target, field, value in changes:
            with self.subTest(field=field, value=value):
                report = self.secondary_report()
                setattr(report.camf if target == 'camf' else report, field, value)
                plan = dcx_plan((report,))
                self.assert_topic(plan)
                self.assertEqual((plan.reasons[0].code, plan.reasons[0].stage),
                                 ('invalid_fields', 'geometry'))

    def test_mtinfo_area_candidate_keeps_the_additional_instruction(self):
        plan = self.plan(163, (80, 49, 0))
        self.assertEqual(plan.scope_kind, 'area')
        self.assertEqual(plan.geometries, ())
        self.assertEqual(plan.areas, (Area('municipalities', 120200, '北海道函館市'),))
        self.assertEqual(plan.additional_ellipse,
                         Ellipse(140.701261, 41.757292, .535, .535, -37.96875, 'evacuate_from'))

    def test_decoder_ignore_flags_are_authoritative(self):
        for index, field in ((70, 'ignore_a12_to_a16'), (70, 'ignore_a17_to_a18'),
                             (0, 'ignore_ex1'), (115, 'ignore_ex8_to_ex9')):
            with self.subTest(field=field):
                report = self.report(index)
                setattr(report, field, True)
                self.assert_topic(dcx_plan((report,)))

    def test_overseas_common_camf_geometry_does_not_inherit_domestic_version_rule(self):
        for version in (0, 1, 31, 63):
            with self.subTest(version=version):
                plan = self.plan(70, (26, 9, 100), version=version)
                self.assertEqual(plan.scope_kind, 'geometry')
                self.assertEqual(plan.areas, ())
        # A dateline longitude remains a physical longitude, not clipped into
        # Japan. The renderer subsequently unwraps the complete ellipse.
        plan = self.plan(70, (26, 9, 100), (96, 17, 131000), (131, 15, 0), version=9)
        self.assertGreater(plan.geometries[0].lon, 179)

    def test_nonfinite_invalid_and_incomplete_geometry_declines_whole_candidate(self):
        source = self.report()
        for field, value in (('c1_refined_latitude_of_centre_of_main_ellipse', float('nan')),
                             ('c2_refined_longitude_of_centre_of_main_ellipse', 180.1),
                             ('c3_refined_length_of_semi_major_axis', 0),
                             ('c4_refined_length_of_semi_minor_axis', 2500),
                             ('a16_ellipse_azimuth', None)):
            with self.subTest(field=field):
                report = deepcopy(source)
                setattr(report, field, value)
                plan = dcx_plan((report,))
                self.assert_topic(plan)
                self.assertEqual((plan.reasons[0].code, plan.reasons[0].stage),
                                 ('invalid_fields', 'geometry'))

    def test_release_status_and_training_are_not_warning_levels_or_cancellation(self):
        for raw, expected in ((0, '訓練'), (1, '発表'), (2, '更新'), (3, '解除')):
            with self.subTest(raw=raw):
                plan = self.plan(70, (24, 2, raw))
                self.assertEqual(plan.status, expected)
                self.assertEqual(plan.scope_kind, 'geometry')
                self.assertNotIn('警報', plan.headline)
        unknown_hazard = self.plan(70, (40, 7, 127))
        self.assert_topic(unknown_hazard)
        self.assertEqual(unknown_hazard.topic, '防災情報')
        self.assertEqual(unknown_hazard.reasons, (reason('unknown_hazard', 'selection', 'A4=127'),))

    def test_invalid_raw_fields_and_unknown_exceptions_keep_safe_distinct_reasons(self):
        report = self.report()
        report.camf.a1 = None
        plan = dcx_plan((report,))
        self.assert_topic(plan)
        self.assertEqual(plan.reasons, (reason('invalid_fields', 'selection', 'a1'),))
        sensitive = 'unknown failure containing /private/example-secret'
        with patch.object(dcx_reports, '_geometries', side_effect=ValueError(sensitive)):
            plan = self.plan()
        self.assert_topic(plan)
        self.assertEqual(plan.reasons[0], reason('unclassified_fallback', 'geometry'))
        self.assertNotIn(sensitive, repr(plan))

    def test_no_mixed_bundle_or_unknown_provider_is_accepted(self):
        report = self.report()
        for reports in ((), (report, report), (self.report(70, (35, 5, 31)),)):
            with self.subTest(count=len(reports)):
                with self.assertRaises(ValueError):
                    dcx_plan(reports)


@unittest.skipUnless(HAS_RENDERER, 'DCX rendering decisions need isolated image dependencies')
class DcxDiagnosticRenderingTests(unittest.TestCase):
    """Exercise the fallback flow without drawing maps or allocating images."""

    @classmethod
    def setUpClass(cls):
        cls.source = frames('dcx_archive_20260917')
        cls.when = reference_time('dcx_archive_20260917')

    report = DcxReportTests.report

    def test_geometry_success_omits_the_unused_administrative_fallback_reason(self):
        report, fig = self.report(), Mock()
        with patch.object(dcx_images, '_geometry'), patch.object(dcx_images.wide, 'validate'):
            plan = dcx_images.draw_dcx(fig, (report,))
        self.assertEqual(plan.scope_kind, 'geometry')
        self.assertEqual(plan.reasons, ())
        fig.clear.assert_not_called()

    def test_area_success_preserves_only_why_geometry_was_unavailable(self):
        report, fig = self.report(0, (146, 16, 1202)), Mock()
        with patch.object(dcx_images, '_area'), patch.object(dcx_images.wide, 'validate'):
            plan = dcx_images.draw_dcx(fig, (report,))
        self.assertEqual(plan.scope_kind, 'area')
        self.assertEqual([(item.code, item.stage) for item in plan.reasons], [('no_geometry', 'geometry')])
        fig.clear.assert_not_called()

    def test_missing_boundary_retains_exact_code_and_falls_back_as_a_whole(self):
        report, fig = self.report(0), Mock()
        with patch.object(dcx_areas, 'municipality', return_value=None), \
                patch.object(dcx_images, '_topic'), patch.object(dcx_images.wide, 'validate'):
            plan = dcx_images.draw_dcx(fig, (report,))
        self.assertEqual(plan.scope_kind, 'topic')
        self.assertEqual([item.code for item in plan.reasons], ['no_geometry', 'missing_boundary'])
        self.assertEqual(plan.reasons[-1].detail, 'municipalities: code=110100')
        fig.clear.assert_called_once()

    def test_unknown_validation_error_is_not_guessed_to_be_layout_failure_or_exposed(self):
        report, fig = self.report(), Mock()
        sensitive = 'unrecognized layout failure containing /private/example-secret'
        with patch.object(dcx_images, '_geometry'), patch.object(dcx_images, '_topic'), \
                patch.object(dcx_images.wide, 'validate', side_effect=[ValueError(sensitive), None]):
            plan = dcx_images.draw_dcx(fig, (report,))
        self.assertEqual(plan.scope_kind, 'topic')
        self.assertEqual([(item.code, item.stage) for item in plan.reasons],
                         [('unclassified_fallback', 'layout'), ('no_area', 'area')])
        self.assertNotIn(sensitive, repr(plan))
        fig.clear.assert_called_once()

    def test_outer_topic_fallback_merges_and_deduplicates_authored_reasons(self):
        report, fig = self.report(), Mock()
        failure = reason('layout_unavailable', 'layout', '画像外の文字')
        with patch.object(dcx_images, '_topic'):
            plan = dcx_images.draw_topic(fig, (report,), reasons=(failure, failure))
        self.assertEqual(plan.scope_kind, 'topic')
        self.assertEqual([item.code for item in plan.reasons], ['no_area', 'layout_unavailable'])
        self.assertEqual(plan.reasons[-1], failure)

    def test_transient_topic_failure_retains_prior_candidate_failures_for_outer_retry(self):
        report, fig = self.report(), Mock()
        geometry_failure = PreviewFallback('projection_unavailable', stage='geometry',
                                           detail='災害中心位置')
        sensitive = 'transient topic failure containing /private/example-secret'
        with patch.object(dcx_images, '_geometry', side_effect=geometry_failure), \
                patch.object(dcx_images, '_topic', side_effect=[ValueError(sensitive), None]), \
                patch.object(dcx_images.wide, 'validate'):
            with self.assertRaises(PreviewFallback) as caught:
                dcx_images.draw_dcx(fig, (report,))
            reasons = error_reasons(caught.exception, stage='render')
            self.assertEqual([(item.code, item.stage) for item in reasons],
                             [('projection_unavailable', 'geometry'), ('no_area', 'area'),
                              ('unclassified_fallback', 'render')])
            plan = dcx_images.draw_topic(fig, (report,), reasons=reasons)
        self.assertEqual(plan.scope_kind, 'topic')
        self.assertEqual(set(plan.reasons), set(reasons))
        self.assertEqual(len(plan.reasons), 3)
        self.assertNotIn(sensitive, repr(plan))


if __name__ == '__main__':
    unittest.main()
