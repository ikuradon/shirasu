"""Received DCX instructions apply independently of renderer availability."""

from copy import deepcopy
import unittest

import azarashi

from dcx_instructions import (MtInfoEvacuation, MtInfoEvacuationError,
                              is_evacuation_destination, mtinfo_evacuation)
from qzqsm_samples import frames, reference_time
from scripts.gallery_catalog import _field_variant


def evacuation_report(*fields):
    sentence = frames('dcr_receiver_20260924')[70]
    # Explicit synthetic foreign test, with original country and version.
    changed = _field_variant(sentence, ((129, 2, 2), (131, 2, 2), (133, 3, 3),
                                        (136, 5, 8), (141, 5, 0), *fields))
    return azarashi.decode(changed, timestamp=reference_time('dcr_receiver_20260924'))


def mtinfo_report(*fields):
    sentence = frames('dcx_archive_20260917')[163]
    # Keep the received main and additional ellipses, explicitly deriving Vn=1.
    changed = _field_variant(sentence, ((214, 6, 1), *fields))
    return azarashi.decode(changed, timestamp=reference_time('dcx_archive_20260917'))


class DcxInstructionTests(unittest.TestCase):
    def test_supported_foreign_evacuation_configuration_and_all_message_states(self):
        for state in range(4):
            with self.subTest(state=state):
                self.assertTrue(is_evacuation_destination(evacuation_report((24, 2, state))))
        for version in (0, 1, 63):
            with self.subTest(version=version):
                self.assertTrue(is_evacuation_destination(evacuation_report((214, 6, version))))
        self.assertTrue(is_evacuation_destination(evacuation_report((26, 9, 0))),
                        'Country code zero is Afghanistan, not an empty field')

    def test_neither_the_projection_nor_an_image_is_required_for_the_instruction(self):
        # Main and secondary ellipses are valid transmitted geometry at a
        # polar centre, even though the image renderer cannot show them.
        self.assertTrue(is_evacuation_destination(evacuation_report((80, 16, 65535))))
        self.assertTrue(is_evacuation_destination(evacuation_report(
            (113, 5, 31), (118, 5, 31), (131, 2, 3), (133, 3, 7))))
        self.assertTrue(is_evacuation_destination(evacuation_report((131, 15, 0))),
                        'Zero transform bits are a real quarter-size destination')

    def test_domestic_non_b3_and_other_instruction_libraries_do_not_get_the_default(self):
        changes = (((26, 9, 111),), ((129, 2, 0),), ((129, 2, 1),), ((129, 2, 3),),
                   ((66, 1, 1),), ((67, 3, 1),), ((26, 9, 511),),
                   ((141, 5, 1),), ((141, 5, 17),), ((141, 5, 30),))
        for fields in changes:
            with self.subTest(fields=fields):
                self.assertFalse(is_evacuation_destination(evacuation_report(*fields)))
        self.assertFalse(is_evacuation_destination(None))
        self.assertFalse(is_evacuation_destination(object()))

    def test_missing_boolean_out_of_range_or_inconsistent_raw_fields_do_not_authorize_default(self):
        original = evacuation_report()
        changes = (('a1', True), ('a1', 4), ('a2', None), ('a2', -1), ('a9', False),
                   ('a10', False), ('a17', None), ('a18', original.camf.a18 ^ 1),
                   ('a18', 1 << 15), ('a12', -1), ('a13', 1 << 17),
                   ('a14', 0), ('a15', True), ('a16', 64), ('c7', 4),
                   ('c8', -1), ('c9', 32), ('c10', False))
        for field, value in changes:
            with self.subTest(field=field, value=value):
                report = deepcopy(original)
                setattr(report.camf, field, value)
                self.assertFalse(is_evacuation_destination(report))
        report = deepcopy(original)
        del report.camf.c7
        self.assertFalse(is_evacuation_destination(report))

    def test_ignore_flags_must_explicitly_enable_both_geometry_fields(self):
        for field in ('ignore_a12_to_a16', 'ignore_a17_to_a18'):
            for value in (True, None, 0):
                with self.subTest(field=field, value=value):
                    report = evacuation_report()
                    setattr(report, field, value)
                    self.assertFalse(is_evacuation_destination(report))

    def test_recognition_never_changes_the_decoded_report(self):
        report = evacuation_report()
        before = deepcopy({name: value for name, value in report.get_params().items()
                           if name != 'camf'})
        before_camf = deepcopy(report.camf.get_params())
        camf = report.camf
        text = str(report)
        self.assertTrue(is_evacuation_destination(report))
        self.assertEqual({name: value for name, value in report.get_params().items()
                          if name != 'camf'}, before)
        self.assertIs(report.camf, camf)
        self.assertEqual(report.camf.get_params(), before_camf)
        self.assertEqual(str(report), text)


class MtInfoInstructionTests(unittest.TestCase):
    def test_official_additional_ellipse_example_matches_literal_specification_values(self):
        # DCX-004 sections 4.2.4.1 and 5.3.1 provide these transmitted fields
        # and physical values independently of the decoder's implementation.
        report = mtinfo_report((163, 17, 91522), (180, 17, 68950),
                               (197, 5, 13), (202, 5, 11), (207, 7, 96))
        self.assertEqual(mtinfo_evacuation(report),
                         MtInfoEvacuation(0, 139.689138, 35.687299, 10.933, 5.979, 45.))

    def test_received_additional_ellipse_is_independent_and_has_both_directions(self):
        for direction in (0, 1):
            with self.subTest(direction=direction):
                result = mtinfo_evacuation(mtinfo_report((162, 1, direction)))
                self.assertEqual(result, MtInfoEvacuation(direction, 140.701261, 41.757292,
                                                        .535, .535, -37.96875))
        for state in range(4):
            with self.subTest(state=state):
                self.assertIsNotNone(mtinfo_evacuation(mtinfo_report((24, 2, state))))

    def test_only_all_six_zero_fields_mean_no_instruction(self):
        none = mtinfo_report((162, 52, 0))
        self.assertTrue(none.ignore_ex2_to_ex7)
        self.assertIsNone(mtinfo_evacuation(none))
        head = mtinfo_report((162, 52, 1 << 51))
        self.assertTrue(head.ignore_ex2_to_ex7, 'The pinned decoder ignores EX2 in its sentinel')
        self.assertIsNone(head.ex2_evacuate_direction_type)
        self.assertEqual(mtinfo_evacuation(head), MtInfoEvacuation(1, 45., -90., .216, .216, -90.))
        head.ex3_additional_ellipse_centre_latitude = 0
        with self.assertRaises(MtInfoEvacuationError) as raised:
            mtinfo_evacuation(head)
        self.assertEqual(raised.exception.field, 'ex3_additional_ellipse_centre_latitude')

    def test_projection_limits_do_not_erase_received_instructions(self):
        for latitude, expected in ((0, -90.), (131071, 90.)):
            with self.subTest(latitude=latitude):
                self.assertEqual(mtinfo_evacuation(mtinfo_report((163, 17, latitude))).lat, expected)
        for code, expected in ((0, .216), (31, 2500.)):
            with self.subTest(axis=code):
                value = mtinfo_evacuation(mtinfo_report((197, 5, code), (202, 5, code)))
                self.assertEqual((value.semi_major_km, value.semi_minor_km), (expected, expected))
        value = mtinfo_evacuation(mtinfo_report((180, 17, 131071), (207, 7, 127)))
        self.assertEqual((value.lon, value.azimuth), (-135., 88.59375))

    def test_unrelated_reports_are_not_assigned_a_local_government_instruction(self):
        for report in (None, object(), evacuation_report()):
            with self.subTest(report_type=type(report)):
                self.assertIsNone(mtinfo_evacuation(report))

    def test_unsupported_domestic_versions_libraries_and_settings_are_distinct(self):
        for field, offset, width, value in (('vn', 214, 6, 0), ('vn', 214, 6, 63),
                                           ('a9', 66, 1, 0), ('a10', 67, 3, 7),
                                           ('a17', 129, 2, 1), ('a17', 129, 2, 3)):
            with self.subTest(field=field, value=value):
                with self.assertRaises(MtInfoEvacuationError) as raised:
                    mtinfo_evacuation(mtinfo_report((offset, width, value)))
                self.assertEqual(raised.exception.field, field)
                self.assertTrue(raised.exception.unsupported)

    def test_malformed_raw_fields_and_wrong_provider_are_not_instructions(self):
        original = mtinfo_report()
        widths = (('a1', 2), ('a2', 9), ('a3', 5), ('a9', 1), ('a10', 3), ('a17', 2), ('vn', 6),
                  ('ex2', 1), ('ex3', 17), ('ex4', 17), ('ex5', 5), ('ex6', 5), ('ex7', 7))
        for field, width in widths:
            for value in (None, True, -1, 1 << width, float('nan'), float('inf')):
                with self.subTest(field=field, value=value):
                    report = deepcopy(original)
                    setattr(report.camf, field, value)
                    with self.assertRaises(MtInfoEvacuationError) as raised:
                        mtinfo_evacuation(report)
                    self.assertEqual(raised.exception.field, field)
                    self.assertFalse(raised.exception.unsupported)
        for field, value in (('a2', 100), ('a3', 1)):
            with self.subTest(field=field):
                report = deepcopy(original)
                setattr(report.camf, field, value)
                with self.assertRaises(MtInfoEvacuationError) as raised:
                    mtinfo_evacuation(report)
                self.assertEqual(raised.exception.field, field)
                self.assertFalse(raised.exception.unsupported)
        with self.assertRaises(MtInfoEvacuationError) as raised:
            mtinfo_evacuation(mtinfo_report((197, 5, 0), (202, 5, 1)))
        self.assertEqual(raised.exception.field, 'ex5/ex6')

    def test_ignore_flags_and_decoded_values_must_match_received_fields(self):
        original = mtinfo_report()
        for ignored in (True, None, 0):
            with self.subTest(ignored=ignored):
                report = deepcopy(original)
                report.ignore_ex2_to_ex7 = ignored
                with self.assertRaises(MtInfoEvacuationError) as raised:
                    mtinfo_evacuation(report)
                self.assertEqual(raised.exception.field, 'ignore_ex2_to_ex7')
        empty = mtinfo_report((162, 52, 0))
        empty.ignore_ex2_to_ex7 = False
        with self.assertRaises(MtInfoEvacuationError):
            mtinfo_evacuation(empty)
        for field in ('ex2_evacuate_direction_type', 'ex3_additional_ellipse_centre_latitude',
                      'ex4_additional_ellipse_centre_longitude', 'ex5_additional_ellipse_semi_major_axis',
                      'ex6_additional_ellipse_semi_minor_axis', 'ex7_additional_ellipse_azimuth'):
            for value in (None, True, float('nan'), float('inf'), 'wrong', 17):
                with self.subTest(field=field, value=value):
                    report = deepcopy(original)
                    setattr(report, field, value)
                    with self.assertRaises(MtInfoEvacuationError) as raised:
                        mtinfo_evacuation(report)
                    self.assertEqual(raised.exception.field, field)
                    self.assertFalse(raised.exception.unsupported)

    def test_raw_recovery_and_longitude_normalization_never_mutate_the_report(self):
        for fields in ((), ((162, 52, 1 << 51),), ((180, 17, 131071),)):
            with self.subTest(fields=fields):
                report = mtinfo_report(*fields)
                before = deepcopy({k: v for k, v in report.get_params().items() if k != 'camf'})
                before_camf = deepcopy(report.camf.get_params())
                original_text = str(report)
                camf = report.camf
                self.assertIsNotNone(mtinfo_evacuation(report))
                self.assertEqual({k: v for k, v in report.get_params().items() if k != 'camf'}, before)
                self.assertEqual(report.camf.get_params(), before_camf)
                self.assertIs(report.camf, camf)
                self.assertEqual(str(report), original_text)


if __name__ == '__main__':
    unittest.main()
