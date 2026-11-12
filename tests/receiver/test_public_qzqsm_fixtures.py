"""Keep the public corpus pinned, complete and independently reproducible offline."""

from collections import Counter
import hashlib
import unittest
from unittest.mock import patch

from azarashi.reports import dcr

from tests.qzqsm_samples import CORPUS, decode_reports, frames, manifest, reference_time


class PublicFixtureTests(unittest.TestCase):
    def test_recordings_match_their_pinned_sources_and_checksums(self):
        for name, item in manifest()['recordings'].items():
            with self.subTest(recording=name):
                self.assertRegex(item['source_url'],
                                 r'^https://raw\.githubusercontent\.com/nbtk/azarashi/'
                                 r'[0-9a-f]{40}/tests/qzqsm_[a-z0-9_]+\.log$')
                data = (CORPUS / item['file']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
                self.assertEqual(len(frames(name)), item['frame_count'])
                self.assertEqual(len(set(frames(name))), item['frame_count'])
                self.assertTrue(item['provenance'])
                self.assertIsNotNone(reference_time(name).utcoffset())

    def test_every_frame_decodes_with_the_recorded_type_inventory(self):
        total = 0
        for name, item in manifest()['recordings'].items():
            with self.subTest(recording=name):
                reports = decode_reports(name)
                self.assertEqual(Counter(type(report).__name__ for report in reports),
                                 item['expected_types'])
                self.assertEqual([report.nmea for report in reports], frames(name))
                self.assertTrue(all(report.timestamp == reference_time(name)
                                    for report in reports))
                total += len(reports)
        self.assertEqual(total, 333)

    def test_both_dcr_recordings_include_a_complete_training_nankai_bulletin(self):
        for name in ('dcr_receiver_20260924', 'dcr_archive_20260821'):
            with self.subTest(recording=name):
                pages = [report for report in decode_reports(name)
                         if isinstance(report, dcr.NankaiTroughEarthquake)]
                self.assertEqual(len(pages), 27)
                self.assertEqual({report.page_number for report in pages}, set(range(1, 28)))
                self.assertEqual({report.total_page for report in pages}, {27})
                self.assertEqual({report.report_classification_no for report in pages}, {7})
                self.assertEqual(len({report.report_time for report in pages}), 1)

    def test_reading_a_recording_does_not_replace_another_tests_decoder_assembly(self):
        prior_pages = {99: object()}
        prior_announcement = object()
        with patch.object(dcr.NankaiTroughEarthquake, 'reports', prior_pages), \
                patch.object(dcr.NankaiTroughEarthquake, 'announcement', prior_announcement), \
                patch.object(dcr.NankaiTroughEarthquake, 'completed', False):
            decode_reports('dcr_receiver_20260924')
            self.assertIs(dcr.NankaiTroughEarthquake.reports, prior_pages)
            self.assertEqual(set(prior_pages), {99})
            self.assertIs(dcr.NankaiTroughEarthquake.announcement, prior_announcement)
            self.assertFalse(dcr.NankaiTroughEarthquake.completed)


if __name__ == '__main__':
    unittest.main()
