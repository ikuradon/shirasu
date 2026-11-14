"""Tag only known report metadata and preserve the original received text."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

import azarashi
from azarashi.reports import dcr

from shirasu.reports.publication import (
    ATTRIBUTION, message_hashtags, post_message, report_hashtags,
)
from shirasu.reports.typhoon import typhoon_summary
from tests.qzqsm_samples import decode_reports, manifest
from tests.receiver.test_dcx_instructions import evacuation_report
from tests.receiver.test_typhoon import report as typhoon_report
from tools.preview.gallery_catalog import load_cases


class PostHashtagTests(unittest.TestCase):
    def test_footer_keeps_quoted_hashes_whitespace_and_original_nmea_order(self):
        body = '受信本文 #台風\nhttps://example.test/#地震\n---\n引用\n\n'
        sentences = ('first #洪水', 'second #津波')
        text = post_message(body, iter(sentences), hashtags=('QZSS', '海上警報'))
        self.assertEqual(text, body + '\n\n#QZSS #海上警報\n\n' + ATTRIBUTION
                         + '\n---\nNMEA: first #洪水\nNMEA: second #津波')
        self.assertEqual(message_hashtags(text), ('qzss', '海上警報'))
        self.assertEqual(message_hashtags('https://images.example.test/#火山\n\n' + text),
                         ('qzss', '海上警報'))

    def test_duplicate_case_variants_are_one_display_tag_and_one_lowercase_index(self):
        text = post_message('本文', ('record',),
                            hashtags=('qzss', 'QZSS', 'Lアラート', 'lアラート', '訓練'))
        self.assertIn('\n\n#QZSS #Lアラート #訓練\n\n', text)
        self.assertEqual(message_hashtags(text), ('qzss', 'lアラート', '訓練'))
        duplicate = text.replace('#QZSS #Lアラート', '#QZSS #QZSS #Lアラート #lアラート')
        self.assertEqual(message_hashtags(duplicate), ('qzss', 'lアラート', '訓練'))

    def test_existing_source_notice_is_kept_once_when_tags_are_added(self):
        for body in ('本文\n\n' + ATTRIBUTION, '本文\n' + ATTRIBUTION):
            text = post_message(body, ('record',), hashtags=('QZSS', '台風'))
            self.assertEqual(text, '本文\n\n#QZSS #台風\n\n' + ATTRIBUTION + '\n---\nNMEA: record')
            self.assertEqual(message_hashtags(text), ('qzss', '台風'))

    def test_legacy_posts_and_hashtags_inside_received_text_are_not_indexed(self):
        for body in ('#台風', '引用\n\n#QZSS #地震\n\n引用続き',
                     '引用\n\n#QZSS #洪水\n\n' + ATTRIBUTION + '\n---\nNMEA: quoted\n引用続き'):
            self.assertEqual(message_hashtags(post_message(body, ('record',))), ())
        self.assertEqual(message_hashtags('#QZSS #台風'), ())

    def test_malformed_or_uncontrolled_footer_is_not_hashtag_metadata(self):
        text = post_message('本文', ('record',), hashtags=('QZSS', '台風'))
        for line in ('#台風', '#QZSS #unknown', '#QZSS #台風 #地震', '#QZSS #訓練 #台風',
                     '#QZSS  #台風', '#QZSS\t#台風', '#QZSS #台風\n#津波'):
            with self.subTest(line=line):
                self.assertEqual(message_hashtags(text.replace('#QZSS #台風', line)), ())
        for changed in (text.replace(ATTRIBUTION, '任意のリンク'), text + '\n末尾への追記',
                        text.replace('NMEA: record', '本文の続き')):
            self.assertEqual(message_hashtags(changed), ())

    def test_public_recordings_cover_each_available_report_family(self):
        expected = {
            'Typhoon': '台風', 'Hypocenter': '地震', 'SeismicIntensity': '地震',
            'EarthquakeEarlyWarning': '地震', 'Tsunami': '津波',
            'Weather': '気象情報', 'Volcano': '火山', 'AshFall': '降灰',
            'Flood': '洪水', 'Marine': '海上警報', 'NankaiTroughEarthquake': '南海トラフ',
            'LAlert': 'Lアラート', 'JAlert': 'Jアラート', 'MTInfo': '自治体情報',
            'OutsideJapan': '海外災害情報',
        }
        checked = set()
        for name in manifest()['recordings']:
            for report in decode_reports(name):
                kind = type(report).__name__
                if kind in expected:
                    self.assertEqual(report_hashtags(report)[:2], ('QZSS', expected[kind]))
                    checked.add(kind)
        self.assertEqual(checked, set(expected))

    def test_northwest_pacific_forecast_uses_the_tsunami_topic(self):
        case = next(case for case in load_cases() if case.category == 'NorthwestPacificTsunami')
        sentence, received_ms = case.records[0]
        report = azarashi.decode(sentence, timestamp=datetime.fromtimestamp(received_ms / 1000, timezone.utc))
        self.assertIsInstance(report, dcr.NorthwestPacificTsunami)
        self.assertEqual(report_hashtags(report)[:2], ('QZSS', '津波'))

    def test_training_is_explicit_even_for_a_cancelled_or_unknown_dcr_family(self):
        ordinary = typhoon_report()
        cancelled_training = typhoon_report(changes=((14, 3, 7), (41, 2, 2)))
        self.assertEqual(report_hashtags(ordinary), ('QZSS', '台風'))
        self.assertEqual(report_hashtags(cancelled_training), ('QZSS', '台風', '訓練'))

        class FutureDcr(dcr.Base):
            pass

        unknown = object.__new__(FutureDcr)
        unknown.report_classification_no = 7
        self.assertEqual(report_hashtags(unknown), ('QZSS', '訓練'))
        unknown.report_classification_no = 3
        self.assertEqual(report_hashtags(unknown), ('QZSS',))
        self.assertEqual(report_hashtags(SimpleNamespace(report_classification_no=7)), ('QZSS',))

    def test_dcx_training_requires_a_reviewed_version_and_explicit_state(self):
        for version in (0, 1):
            for state in range(4):
                report = evacuation_report((214, 6, version), (24, 2, state))
                with self.subTest(version=version, state=state):
                    self.assertEqual(report_hashtags(report),
                                     ('QZSS', '海外災害情報') + (('訓練',) if state == 0 else ()))
        report = evacuation_report((214, 6, 63), (24, 2, 0))
        self.assertEqual(report_hashtags(report), ('QZSS', '海外災害情報'))

    def test_collected_typhoon_has_one_footer_for_all_original_points(self):
        reports = [typhoon_report(index=i, changes=((14, 3, 7),)) for i in (1, 0)]
        text = typhoon_summary(reports)
        self.assertEqual(text.count('#QZSS'), 1)
        self.assertEqual(message_hashtags(text), ('qzss', '台風', '訓練'))
        self.assertEqual(text.rpartition('\n---\n')[2].splitlines(),
                         ['NMEA: ' + report.nmea for report in reversed(reports)])


if __name__ == '__main__':
    unittest.main()
