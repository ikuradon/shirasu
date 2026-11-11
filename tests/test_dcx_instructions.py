"""The C10=0 instruction applies independently of renderer availability."""

from copy import deepcopy
import unittest

import azarashi

from dcx_instructions import is_evacuation_destination
from qzqsm_samples import frames, reference_time
from scripts.gallery_catalog import _field_variant


def evacuation_report(*fields):
    sentence = frames('dcr_receiver_20260924')[70]
    # Explicit synthetic foreign test, with original country and version.
    changed = _field_variant(sentence, ((129, 2, 2), (131, 2, 2), (133, 3, 3),
                                        (136, 5, 8), (141, 5, 0), *fields))
    return azarashi.decode(changed, timestamp=reference_time('dcr_receiver_20260924'))


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


if __name__ == '__main__':
    unittest.main()
