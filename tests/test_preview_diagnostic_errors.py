"""Known fallback causes are classified; arbitrary exception text stays private."""

import unittest

from image_service.preview_diagnostic_errors import known_error_code
from image_service.preview_diagnostics import MESSAGES


class PreviewDiagnosticErrorTests(unittest.TestCase):
    def test_dcr_layout_constraints_are_distinct_from_received_value_errors(self):
        examples = {
            'Complete seismic badges do not fit inside their prefectures': 'layout_unavailable',
            'Dated centres do not fit outside the locator': 'layout_unavailable',
            'Complete ash time matrix does not fit': 'layout_unavailable',
            'Complete river name does not fit outside the forecast reach': 'layout_unavailable',
            'Complete tsunami values do not fit outside the locator': 'layout_unavailable',
            'Unknown or mixed typhoon position, time or identity': 'invalid_fields',
            'Invalid tsunami arrival day': 'invalid_fields',
        }
        for message, expected in examples.items():
            with self.subTest(message=message):
                self.assertEqual(known_error_code(ValueError(message)), expected)
                self.assertIn(expected, MESSAGES)

    def test_missing_catalogue_data_unknown_codes_and_ambiguous_checks_differ(self):
        examples = {
            'No official position for this volcano': 'missing_position',
            'Missing regional geometry': 'missing_boundary',
            'No geometry for this EEW warning area': 'missing_boundary',
            'Unknown forecast region': 'unknown_area',
            'Unknown volcano municipality': 'unknown_area',
            'No received weather regions': 'no_area',
            'Missing or duplicated municipality geometry': 'invalid_fields',
            'Unknown marine area or warning': 'invalid_fields',
            'Tsunami regions require coastline geometry': 'unsupported_geometry',
            'Unsupported ash report classification': 'unsupported_report',
            'DCX ellipse crosses the Mercator latitude limit': 'projection_unavailable',
            'DCX ellipse is outside the supported projection or axis range': 'invalid_fields',
        }
        for message, expected in examples.items():
            with self.subTest(message=message):
                self.assertEqual(known_error_code(ValueError(message)), expected)
                self.assertIn(expected, MESSAGES)

    def test_unknown_text_and_paths_cannot_match_by_substring_or_pattern(self):
        for message in (
            'Complete sidebar does not fit /private/receiver/secret.key',
            '/private/data: Unknown forecast region',
            'Unknown forecast region: 123456',
            'Missing regional geometry\n/private/state.sqlite',
            'Unknown forecast region ',
            'unknown forecast region',
            'Some new constraint',
        ):
            with self.subTest(message=message):
                self.assertIsNone(known_error_code(ValueError(message)))

    def test_only_a_single_literal_argument_is_classified_without_stringifying(self):
        class MisleadingValueError(ValueError):
            def __str__(self):
                raise AssertionError('Exception strings must not be evaluated')

        self.assertEqual(known_error_code(MisleadingValueError('Unknown forecast region')), 'unknown_area')
        self.assertIsNone(known_error_code(MisleadingValueError('/private/secret.key')))
        self.assertIsNone(known_error_code(ValueError('Unknown forecast region', '/private/secret.key')))
        self.assertIsNone(known_error_code(ValueError()))
        self.assertIsNone(known_error_code(ValueError(123)))

    def test_asset_runtime_errors_are_not_relabelled_as_rendering_constraints(self):
        for error in (RuntimeError('Missing regional geometry'),
                      FileNotFoundError('No official position for this volcano'),
                      TypeError('Complete sidebar does not fit'),
                      'Unknown forecast region', None):
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(known_error_code(error))


if __name__ == '__main__':
    unittest.main()
