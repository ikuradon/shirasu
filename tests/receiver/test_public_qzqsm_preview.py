"""Public recordings exercise the same finite stream decoder as --preview."""

from collections import Counter
import io
import unittest
from unittest.mock import Mock, patch

from azarashi.reports import dcr

from shirasu import service as main
from shirasu.preview import preview_stream
from tests.qzqsm_samples import expected_nankai_text, frames, manifest, recording_path, reference_time


class PublicQzqsmPreviewTests(unittest.TestCase):
    def test_public_logs_decode_and_format_every_eligible_frame_with_fixed_date_context(self):
        for name, metadata in manifest()['recordings'].items():
            with self.subTest(recording=name):
                stdout, stderr = io.StringIO(), io.StringIO()
                formatter = Mock(side_effect=main.report_message)
                with patch.object(dcr.NankaiTroughEarthquake, 'completed', False), \
                        patch.object(dcr.NankaiTroughEarthquake, 'reports', {}), \
                        patch.object(dcr.NankaiTroughEarthquake, 'announcement', None), \
                        recording_path(name).open('rb') as stream:
                    counts = preview_stream(
                        stream, input_format='nmea', received_at=reference_time(name),
                        message_formatter=formatter, report_filter=main.is_publishable,
                        stdout=stdout, stderr=stderr, source_name=name)
                expected_types = dict(metadata['expected_types'])
                ignored = sum(expected_types.pop(kind, 0) for kind in ('NullMsg', 'Unknown'))
                self.assertEqual(counts, {'decoded': len(frames(name)),
                                          'shown': sum(expected_types.values()),
                                          'ignored': ignored, 'errors': 0})
                self.assertEqual(stderr.getvalue(), '')
                reports = [call.args[0] for call in formatter.call_args_list]
                self.assertEqual(Counter(type(report).__name__ for report in reports), expected_types)
                for report in reports:
                    self.assertEqual(report.timestamp, reference_time(name))
                    self.assertIn('NMEA: ' + report.nmea, stdout.getvalue())
                if 'NankaiTroughEarthquake' in expected_types:
                    self.assertIn(expected_nankai_text(name), stdout.getvalue())
                    self.assertIn('受信中', stdout.getvalue())
                self.assertEqual(stdout.getvalue().count('=== プレビュー'), counts['shown'])


if __name__ == '__main__':
    unittest.main()
