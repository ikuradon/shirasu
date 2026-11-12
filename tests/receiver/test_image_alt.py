"""ALT describes received metadata without claiming a particular rendering."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import azarashi
from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.flood_forecast_region import flood_forecast_region

from shirasu.images.alt import DEFAULT_IMAGE_ALT, JST, image_alt_text
from tests.qzqsm_samples import decode_reports
from tools.preview.gallery_catalog import _field_variant, load_cases


def decode(record):
    sentence, milliseconds = record
    return azarashi.decode(sentence, timestamp=datetime.fromtimestamp(milliseconds / 1000, timezone.utc))


def changed(record, fields):
    return _field_variant(record[0], fields), record[1]


class ImageAltTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def record(self, slug):
        return self.cases[slug].records[0]

    def test_all_catalog_types_have_short_specific_one_paragraph_alt(self):
        labels = {
            'Typhoon': '台風26号', 'Hypocenter': '震源情報', 'Weather': '気象警報等',
            'EarthquakeEarlyWarning': '緊急地震速報', 'SeismicIntensity': '震度情報',
            'Tsunami': '津波情報', 'Volcano': '火山情報', 'AshFall': '降灰予報',
            'Flood': '指定河川洪水予報', 'NorthwestPacificTsunami': '北西太平洋津波情報',
            'Marine': '海上警報', 'LAlert': 'Lアラート（空襲）',
            'JAlert': 'Jアラート（安全に関する警告）', 'MTInfo': '自治体情報（洪水）',
            'OutsideJapan': '海外向け防災情報',
            'NankaiTroughEarthquake': '南海トラフ地震',
        }
        self.assertEqual({case.category for case in self.cases.values()}, set(labels))
        for case in self.cases.values():
            with self.subTest(slug=case.slug):
                text = image_alt_text(case.records)
                self.assertLessEqual(len(text), 300)
                self.assertNotIn('\n', text)
                self.assertIn('詳細は投稿本文を参照。', text)
                self.assertNotIn('地図', text)
                self.assertNotIn('NMEA', text)
                self.assertNotIn('https:', text)
                label = '台風32号' if case.slug == 'typhoon-cancel-reference' else labels[case.category]
                self.assertIn(label, text)
                self.assertEqual('【訓練・試験】' in text, case.classification == 'training')

    def test_dcr_uses_issued_time_in_jst_not_receipt_or_occurrence(self):
        text = image_alt_text((self.record('marine'),))
        self.assertIn('発表日時：2026年09月24日05時35分（JST）。', text)
        self.assertNotIn('受信基準日時', text)
        sentence, ms = self.record('marine')
        self.assertEqual(text, image_alt_text(((sentence, ms + 3_600_000),)))

    def test_dcx_labels_receipt_instead_of_hazard_onset_as_issue_time(self):
        record = self.record('dcx-mtinfo')
        text = image_alt_text((record,))
        expected = datetime.fromtimestamp(record[1] / 1000, timezone.utc).astimezone(
            JST).strftime('%Y年%m月%d日%H時%M分')
        self.assertIn('受信基準日時：' + expected + '（JST）', text)
        self.assertNotIn('発表日時', text)
        self.assertNotIn('発生日時', text)
        for code, marker in ((2, '【更新】'), (3, '【解除】')):
            with self.subTest(state=code):
                variant = changed(record, ((24, 2, code),))
                variant_text = image_alt_text((variant,))
                self.assertIn(marker, variant_text)
                self.assertNotIn('【取消】', variant_text)

    def test_typhoon_aggregation_is_short_order_independent_and_keeps_number(self):
        records = self.cases['typhoon-cancel-reference'].cancel_reference
        text = image_alt_text(records)
        self.assertEqual(text, image_alt_text(tuple(reversed(records))))
        self.assertIn('台風32号', text)
        self.assertLess(len(text), 150)
        corrections = tuple(changed(record, ((41, 2, 1),)) for record in records)
        self.assertIn('【訂正】', image_alt_text(corrections))

    def test_cancel_keeps_training_marker_and_never_reactivates_release_fields(self):
        record = changed(self.record('weather'), ((14, 3, 7), (41, 2, 2), (53, 3, 2)))
        text = image_alt_text((record,))
        self.assertIn('【訓練・試験】【取消】', text)
        self.assertIn('この情報は取り消されました。', text)
        self.assertNotIn('解除', text)
        self.assertNotIn('警戒範囲', text)
        flood = changed(self.record('flood-release'), ((41, 2, 2),))
        self.assertNotIn('解除', image_alt_text((flood,)))

    def test_weather_tsunami_flood_and_marine_release_are_explicit(self):
        weather = changed(self.record('weather'), ((53, 3, 2),))
        marine = self.record('marine')
        count = len(decode(marine).marine_warning_codes_raw)
        marine = changed(marine, tuple((53 + index * 19, 5, 0) for index in range(count)))
        for record in (weather, marine, self.record('tsunami-release'), self.record('flood-release')):
            with self.subTest(kind=type(decode(record)).__name__):
                text = image_alt_text((record,))
                self.assertIn('【解除】', text)
                self.assertNotIn('【取消】', text)
        self.assertIn('注意情報への切替を含みます', image_alt_text((self.record('flood-release'),)))

    def test_mixed_marine_and_flood_rows_do_not_claim_complete_release(self):
        for slug, width, stride in (('marine', 5, 19), ('flood-issue', 4, 44)):
            record = self.record(slug)
            release, active = (0, 21) if slug == 'marine' else (1, 2)
            fields = [(53, width, release), (53 + stride, width, active)]
            if slug == 'flood-issue':
                first_code = decode(record).flood_forecast_regions_raw[0]
                second_code = next(code for code in flood_forecast_region if code != first_code)
                fields.append((53 + stride + width, 40, second_code))
            variant = changed(record, fields)
            with self.subTest(kind=slug):
                text = image_alt_text((variant,))
                self.assertIn('【解除情報を含む】', text)
                self.assertNotIn('【解除】', text)

    def test_low_volcano_and_no_tsunami_potential_do_not_invent_release(self):
        volcano = changed(self.record('volcano'), ((69, 7, 11),))
        nwp = decode(self.record('nwp'))
        nwp.tsunamigenic_potential_raw = 0
        with patch('shirasu.images.alt.azarashi.decode', return_value=nwp):
            text = image_alt_text((self.record('nwp'),))
        self.assertNotIn('解除', text)
        self.assertNotIn('解除', image_alt_text((volcano,)))

    def test_mixed_types_dates_and_states_are_not_merged_into_one_bulletin(self):
        weather = self.record('weather')
        cancel = changed(weather, ((41, 2, 2),))
        text = image_alt_text((weather, cancel))
        self.assertIn('【状態混在】', text)
        self.assertIn('発表・取消', text)
        self.assertNotIn('この情報は取り消されました', text)
        later = changed(weather, ((35, 6, 47),))
        self.assertIn('発表日時が複数あります', image_alt_text((weather, later)))
        dcx_record = self.record('dcx-mtinfo')
        text = image_alt_text((weather, (dcx_record[0], weather[1])))
        self.assertIn('複数種類', text)
        self.assertIn('発表日時と受信基準日時が混在', text)
        self.assertIn('【訓練・試験を含む】', text)
        self.assertLessEqual(len(text), 300)

    def test_unknown_metadata_and_time_are_explicit_not_ordinary_issue(self):
        record = changed(self.record('typhoon-track'), ((14, 3, 6), (41, 2, 3), (87, 7, 0)))
        text = image_alt_text((record,))
        self.assertIn('通報区分は確認できません', text)
        self.assertIn('情報形態は確認できません', text)
        self.assertIn('号数は確認できません', text)
        self.assertNotIn('【取消】', text)
        weather = changed(self.record('weather'), ((53, 3, 0),))
        self.assertIn('解除区分は確認できません', image_alt_text((weather,)))
        report = decode(self.record('weather'))
        for value in (None, datetime(2026, 11, 1)):
            report.report_time = value
            with patch('shirasu.images.alt.azarashi.decode', return_value=report):
                text = image_alt_text((self.record('weather'),))
            self.assertIn('発表日時を確認できません', text)
            self.assertNotIn('受信基準日時', text)

    def test_unknown_dcx_version_does_not_interpret_layout_or_onset(self):
        report = decode(self.record('dcx-mtinfo'))
        report.camf.vn = report.dcx_version = 3
        with patch('shirasu.images.alt.azarashi.decode', return_value=report):
            text = image_alt_text((self.record('dcx-mtinfo'),))
        self.assertIn('未対応のDCXバージョン', text)
        self.assertIn('受信基準日時', text)
        self.assertNotIn('洪水', text)
        self.assertNotIn('【訓練', text)
        self.assertLessEqual(len(text), 300)

    def test_unknown_dcx_library_keeps_status_but_does_not_guess_hazard(self):
        report = decode(self.record('dcx-mtinfo'))
        report.camf.a10 = 1
        with patch('shirasu.images.alt.azarashi.decode', return_value=report):
            text = image_alt_text((self.record('dcx-mtinfo'),))
        self.assertIn('【訓練・試験】', text)
        self.assertIn('受信基準日時', text)
        self.assertIn('災害種別は確認できません', text)
        self.assertNotIn('洪水', text)

    def test_long_mixed_metadata_preserves_critical_markers_within_300_characters(self):
        records = tuple(self.record(slug) for slug in
                        ('weather', 'typhoon-track', 'flood-issue', 'dcx-mtinfo'))
        reports = [decode(record) for record in records]
        reports[0].report_classification_no = 7
        reports[0].information_type_no = 2
        reports[1].report_classification_no = 6
        reports[1].information_type_no = 3
        reports[1].typhoon_number_raw = 0
        reports[1].report_time = None
        reports[2].flood_warning_levels_raw = [1, 2]
        reports[2].flood_forecast_regions_raw = [1, 2]
        reports[3].camf.vn = reports[3].dcx_version = 3
        records = tuple((sentence, records[0][1]) for sentence, _ in records)
        with patch('shirasu.images.alt.azarashi.decode', side_effect=reports):
            text = image_alt_text(records)
        self.assertLessEqual(len(text), 300)
        self.assertNotIn('\n', text)
        for marker in ('訓練・試験を含む', '状態混在', '取消', '解除情報を含む', '確認できません'):
            self.assertIn(marker, text)

    def test_nankai_alt_uses_raw_headers_without_changing_shared_state(self):
        report = next(item for item in decode_reports('dcr_receiver_20260924')
                      if isinstance(item, dcr.NankaiTroughEarthquake))
        owner = dcr.NankaiTroughEarthquake
        saved = (owner.completed, owner.reports, owner.announcement)
        record = (report.nmea, round(report.timestamp.timestamp() * 1000))
        with patch('shirasu.images.alt.azarashi.decode') as decoder:
            text = image_alt_text((record,))
            decoder.assert_not_called()
        self.assertIn('【訓練・試験】南海トラフ地震臨時情報（巨大地震注意）', text)
        self.assertIn('発表日時：2026年09月24日10時35分（JST）', text)
        self.assertNotIn('受信完了', text)
        self.assertIs(owner.completed, saved[0])
        self.assertIs(owner.reports, saved[1])
        self.assertIs(owner.announcement, saved[2])

    def test_nankai_ended_is_not_all_clear_and_cancel_hides_old_hazard_keyword(self):
        report = next(item for item in decode_reports('dcr_receiver_20260924')
                      if isinstance(item, dcr.NankaiTroughEarthquake))
        record = (report.nmea, round(report.timestamp.timestamp() * 1000))
        with patch('shirasu.images.alt.azarashi.decode', side_effect=AssertionError('No Nankai decode')):
            ended = image_alt_text((changed(record, ((53, 4, 6),)),))
            cancelled = image_alt_text((changed(record, ((41, 2, 2),)),))
            corrected = image_alt_text((changed(record, ((41, 2, 1),)),))
        self.assertIn('調査終了', ended)
        self.assertNotIn('解除', ended)
        self.assertIn('【訓練・試験】【取消】南海トラフ地震関連情報', cancelled)
        self.assertIn('取り消されました', cancelled)
        self.assertNotIn('巨大地震注意', cancelled)
        self.assertIn('【訓練・試験】【訂正】', corrected)

    def test_validation_and_no_input_mutation_or_external_access(self):
        record = self.record('weather')
        for bad in (None, (), ((record[0], True),), (('invalid', record[1]),),
                    ((record[0], -1),), (record, record), ((record[0],),)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    image_alt_text(bad)
        records = [list(record)]
        before = deepcopy(records)
        with patch('socket.socket', side_effect=AssertionError('network')), \
                patch.object(Path, 'read_text', side_effect=AssertionError('filesystem')), \
                patch.object(Path, 'write_text', side_effect=AssertionError('filesystem')):
            self.assertIn('気象警報等', image_alt_text(records))
        self.assertEqual(records, before)
        self.assertEqual(DEFAULT_IMAGE_ALT, 'みちびき受信通報の図')


if __name__ == '__main__':
    unittest.main()
