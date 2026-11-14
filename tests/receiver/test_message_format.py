"""Preserve received details while formatting supported report instructions."""

import copy
from datetime import datetime, timezone
import unittest

import azarashi
from azarashi.definitions.qzss.dcr.information_type import information_type
from azarashi.definitions.qzss.dcr.marine_forecast_region import marine_forecast_region
from azarashi.definitions.qzss.dcr.marine_warning_code import marine_warning_code
from azarashi.definitions.qzss.dcr.report_classification import report_classification
from azarashi.reports import dcr

from shirasu.reports.format import ATTRIBUTION, post_message, report_message
from shirasu.reports.publication import report_hashtags
from tests.qzqsm_samples import decode_reports, manifest
from tests.receiver.test_dcx_instructions import evacuation_report, mtinfo_report


MARINE = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'
DCR = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
DCX = '$QZQSM,55,53B0000DE000000000000000000000000000000000000000000000000E41D10*04'
RECEIVED_AT = datetime(2026, 11, 8, 3, tzinfo=timezone.utc)


class PostMessageTests(unittest.TestCase):
    def test_notice_preserves_body_whitespace_separators_and_original_frame_order(self):
        body = '通報本文\n---\n引用内の NMEA: 例\n\n'
        notice = '出典・利用条件：https://github.com/ikuradon/shirasu/blob/main/licenses/README.md'
        text = post_message(body, iter((DCX, DCR)))
        self.assertEqual(text, body + '\n\n' + notice +
                         f'\n---\nNMEA: {DCX}\nNMEA: {DCR}')

    def test_existing_final_notice_is_not_added_twice(self):
        body = '通報本文\n\n' + ATTRIBUTION
        self.assertEqual(post_message(body, (DCR,)), body + f'\n---\nNMEA: {DCR}')


class MessageFormatTests(unittest.TestCase):
    def setUp(self):
        self.report = azarashi.decode(MARINE, timestamp=RECEIVED_AT)

    def metadata(self, classification=3, information=0):
        self.report.report_classification_no = classification
        self.report.report_classification = report_classification[classification]
        self.report.information_type_no = information
        self.report.information_type = information_type[information]

    def entries(self, codes, regions, labels=None):
        self.report.marine_warning_codes_raw = list(codes)
        self.report.marine_warning_codes = (list(labels) if labels is not None
                                            else [marine_warning_code[code] for code in codes])
        self.report.marine_forecast_regions_raw = list(regions)
        self.report.marine_forecast_regions = [marine_forecast_region[region] for region in regions]

    def test_real_marine_sample_matches_the_proposed_post_exactly(self):
        expected = (
            '【海上警報】発表（通常）\n'
            '発表時刻：11月8日 17:35 JST\n'
            '\n■ 海上着氷警報\n'
            '・サハリン西方海上\n'
            '・北海道西方海上\n'
            '\n■ 海上風警報\n'
            '・宗谷海峡\n'
            '・北海道西方海上\n'
            '・檜山津軽沖\n'
            '・関東海域南部\n'
            '・沿海州南部沖\n'
            '・能登沖\n'
            f'\n#QZSS #海上警報\n\n{ATTRIBUTION}\n---\nNMEA: {MARINE}'
        )
        self.assertEqual(report_message(self.report), expected)

    def test_groups_keep_first_warning_order_and_all_repeated_region_entries(self):
        self.entries([20, 10, 20, 10, 20], [1040, 1020, 1050, 1050, 1040])
        text = report_message(self.report)
        self.assertIn('■ 海上風警報\n・宗谷海峡\n・北海道西方海上\n・宗谷海峡', text)
        self.assertIn('■ 海上着氷警報\n・サハリン西方海上\n・北海道西方海上', text)
        self.assertLess(text.index('■ 海上風警報'), text.index('■ 海上着氷警報'))
        self.assertEqual(sum(line.startswith('・') for line in text.splitlines()), 5)

    def test_warning_code_and_label_both_remain_part_of_group_identity(self):
        self.entries([7, 8, 7], [1020, 1040, 1050], labels=['未定義', '未定義', '異なる表示'])
        text = report_message(self.report)
        self.assertEqual(text.count('■ 未定義\n'), 2)
        self.assertIn('■ 異なる表示\n・北海道西方海上', text)
        self.assertEqual(sum(line.startswith('・') for line in text.splitlines()), 3)

    def test_priority_and_correction_labels_are_retained(self):
        for priority, label in ((1, '最優先'), (2, '優先'), (3, '通常')):
            with self.subTest(priority=priority):
                self.metadata(priority, 1)
                text = report_message(self.report)
                self.assertEqual(text.splitlines()[0], f'【海上警報】訂正（{label}）')
                self.assertIn('■ 海上着氷警報', text)

    def test_training_is_prominent_and_preserves_information_type(self):
        for information, label in ((0, '発表'), (1, '訂正')):
            with self.subTest(information=information):
                self.metadata(7, information)
                self.assertEqual(report_message(self.report).splitlines()[0],
                                 f'【訓練・試験】海上警報（{label}）')

    def test_cancellation_is_prominent_and_keeps_priority(self):
        for priority, label in ((1, '最優先'), (2, '優先'), (3, '通常')):
            with self.subTest(priority=priority):
                self.metadata(priority, 2)
                text = report_message(self.report)
                self.assertEqual(text.splitlines()[0], f'【取消】海上警報（{label}）')
                self.assertNotIn('発表されました', text)
                self.assertIn('■ 海上風警報', text)

    def test_training_and_cancellation_are_both_shown(self):
        self.metadata(7, 2)
        text = report_message(self.report)
        self.assertEqual(text.splitlines()[0], '【訓練・試験】【取消】海上警報')
        self.assertIn('■ 海上着氷警報', text)
        self.assertTrue(text.endswith(f'\n---\nNMEA: {MARINE}'))

    def test_undefined_codes_remain_visible(self):
        self.metadata(4, 3)
        self.entries([7, 8], [999, 1000])
        text = report_message(self.report)
        for label in (information_type[3], report_classification[4],
                      marine_warning_code[7], marine_warning_code[8],
                      marine_forecast_region[999], marine_forecast_region[1000]):
            self.assertIn(label, text)

    def test_warning_lifted_is_not_confused_with_cancellation_of_a_report(self):
        self.entries([0, 20], [1020, 1050])
        text = report_message(self.report)
        self.assertEqual(text.splitlines()[0], '【海上警報】発表（通常）')
        self.assertIn('■ 海上警報解除\n・サハリン西方海上', text)
        self.assertNotIn('【取消】', text)

    def test_empty_warning_arrays_do_not_invent_regions_or_a_lifted_warning(self):
        self.entries([], [])
        self.assertEqual(report_message(self.report),
                         '【海上警報】発表（通常）\n'
                         '発表時刻：11月8日 17:35 JST\n'
                         f'\n#QZSS #海上警報\n\n{ATTRIBUTION}\n---\nNMEA: {MARINE}')

    def test_jst_conversion_handles_a_utc_date_boundary(self):
        self.report.report_time = datetime(2026, 12, 31, 16, 5, tzinfo=timezone.utc)
        self.assertIn('発表時刻：1月1日 01:05 JST', report_message(self.report))

    def test_formatting_does_not_mutate_the_report_or_its_lists(self):
        before = copy.deepcopy(self.report.__dict__)
        report_message(self.report)
        self.assertEqual(self.report.__dict__, before)

    def test_mismatched_arrays_fail_instead_of_silently_losing_items(self):
        names = ('marine_warning_codes', 'marine_warning_codes_raw',
                 'marine_forecast_regions', 'marine_forecast_regions_raw')
        for name in names:
            with self.subTest(name=name):
                report = copy.deepcopy(self.report)
                getattr(report, name).append(getattr(report, name)[0])
                with self.assertRaisesRegex(ValueError, 'equal lengths'):
                    report_message(report)

    def test_missing_or_naive_report_time_is_rejected(self):
        for value in (None, datetime(2026, 11, 8, 8, 35)):
            with self.subTest(value=value):
                self.report.report_time = value
                with self.assertRaisesRegex(ValueError, 'timezone-aware'):
                    report_message(self.report)

    def test_other_report_types_keep_their_existing_text_before_attribution(self):
        for sentence in (DCR, DCX):
            with self.subTest(sentence=sentence):
                report = azarashi.decode(sentence, timestamp=RECEIVED_AT)
                self.assertEqual(report_message(report), post_message(
                    str(report), (report.nmea,), hashtags=report_hashtags(report)))


class DcxEvacuationMessageTests(unittest.TestCase):
    def test_new_and_updated_alerts_explain_the_default_for_people_in_the_main_area(self):
        for state, marker in ((1, '【発表】'), (2, '【更新】')):
            with self.subTest(state=state):
                report = evacuation_report((24, 2, state))
                text = report_message(report)
                self.assertTrue(text.startswith(marker + 'DCX／避難先の指定\n'))
                self.assertIn('主範囲内の人への既定指示（IC-A-03）：危険区域を直ちに離れ、', text)
                self.assertIn('受信した第2楕円で指定された避難先へ移動し、', text)
                self.assertIn('ラジオなどで案内・情報を確認する、という内容です。', text)
                self.assertIn('その範囲内への別の指示がないことと、主範囲内の人への既定指示がないことは異なります。', text)
                self.assertIn('重なる区域では、主範囲の指示が優先します。', text)
                supplement = text.split('受信値の復号テキスト', 1)[0]
                for image_assumption in ('黄色', '添付', '画像', 'あなたは'):
                    self.assertNotIn(image_assumption, supplement)

    def test_training_is_labelled_before_any_instruction(self):
        text = report_message(evacuation_report((24, 2, 0)))
        self.assertTrue(text.startswith('【訓練・試験】DCX／避難先の指定\n'
                                        '以下は訓練電文に含まれる指示の説明です。\n'))
        self.assertIn('*** This is a test message ***', text)

    def test_release_explains_the_setting_without_a_fresh_evacuation_instruction(self):
        text = report_message(evacuation_report((24, 2, 3)))
        self.assertTrue(text.startswith('【解除】DCX／避難先の指定\n'))
        supplement = text.split('受信値の復号テキスト', 1)[0]
        self.assertIn('既定指示（IC-A-03）の設定が含まれています。', supplement)
        self.assertIn('解除の通知であり、新たな避難行動を促すものとして扱いません。', supplement)
        self.assertNotIn('直ちに', supplement)
        self.assertNotIn('移動し、', supplement)

    def test_explicit_a11_instructions_and_received_frame_are_preserved_verbatim(self):
        # Nonempty A and B instructions are not replaced by the C10 default.
        report = evacuation_report((24, 2, 1), (70, 5, 3), (75, 5, 7))
        original_text = str(report)
        original_params = copy.deepcopy({name: value for name, value in report.get_params().items()
                                         if name != 'camf'})
        original_camf = copy.deepcopy(report.camf.get_params())
        camf = report.camf
        text = report_message(report)
        self.assertTrue(text.endswith(post_message(
            original_text, (report.nmea,), hashtags=report_hashtags(report))))
        for attribute in ('a11_international_library_a', 'a11_international_library_b'):
            instruction = getattr(report, attribute)
            self.assertTrue(instruction)
            self.assertIn(instruction, text)
        self.assertEqual(str(report), original_text)
        self.assertEqual({name: value for name, value in report.get_params().items()
                          if name != 'camf'}, original_params)
        self.assertIs(report.camf, camf)
        self.assertEqual(report.camf.get_params(), original_camf)

    def test_instruction_survives_undrawable_geometry_and_foreign_versions(self):
        for fields in (((80, 16, 65535),), ((214, 6, 63),),
                       ((113, 5, 31), (118, 5, 31), (131, 2, 3), (133, 3, 7))):
            with self.subTest(fields=fields):
                report = evacuation_report(*fields)
                self.assertIn('主範囲内の人への既定指示（IC-A-03）', report_message(report))

    def test_unreviewed_libraries_and_other_secondary_configurations_preserve_received_text(self):
        for fields in (((66, 1, 1),), ((67, 3, 1),), ((141, 5, 1),),
                       ((141, 5, 17),), ((141, 5, 31),), ((129, 2, 3),)):
            with self.subTest(fields=fields):
                report = evacuation_report(*fields)
                self.assertEqual(report_message(report), post_message(
                    str(report), (report.nmea,), hashtags=report_hashtags(report)))

    def test_public_recording_preserves_all_other_received_text(self):
        checked = 0
        for recording in manifest()['recordings']:
            for report in decode_reports(recording):
                if isinstance(report, (dcr.Marine, dcr.NankaiTroughEarthquake)):
                    continue  # These already have separate formatters.
                with self.subTest(recording=recording, nmea=report.nmea):
                    self.assertEqual(report_message(report), post_message(
                        str(report), (report.nmea,), hashtags=report_hashtags(report)))
                    checked += 1
        self.assertGreater(checked, 200)


class MtInfoEvacuationMessageTests(unittest.TestCase):
    def test_both_directions_include_the_received_range_without_assuming_an_image(self):
        for direction, action in ((0, '追加楕円の範囲から離れる'), (1, '追加楕円の範囲へ向かう')):
            for state, marker in ((1, '【発表】'), (2, '【更新】')):
                with self.subTest(direction=direction, state=state):
                    report = mtinfo_report((162, 1, direction), (24, 2, state))
                    text = report_message(report)
                    self.assertTrue(text.startswith(marker + 'DCX／自治体の追加避難指示\n'))
                    self.assertIn(f'通報対象者への追加指示：{action}、という内容です。', text)
                    self.assertIn('中心 北緯41.757292°・東経140.701261°', text)
                    self.assertIn('半長軸0.535km・半短軸0.535km、方位-37.96875°（東0°・北向き正）', text)
                    self.assertIn('A11の指示も送られている場合は、追加指示と併せて参照します。', text)
                    supplement = text.split('受信値の復号テキスト', 1)[0]
                    for invented in ('IC-A-03', '優先', '画像', '添付', '安全区域', '安全な場所', '経路', 'あなた'):
                        self.assertNotIn(invented, supplement)

    def test_training_and_release_are_explained_before_any_received_instruction(self):
        training = report_message(mtinfo_report())
        self.assertTrue(training.startswith('【訓練・試験】DCX／自治体の追加避難指示\n'
                                            '以下は訓練電文に含まれる指示の説明です。\n'))
        for direction in (0, 1):
            with self.subTest(direction=direction):
                text = report_message(mtinfo_report((24, 2, 3), (162, 1, direction)))
                self.assertTrue(text.startswith('【解除】DCX／自治体の追加避難指示\n'))
                supplement = text.split('受信値の復号テキスト', 1)[0]
                self.assertIn('追加指示の設定が含まれています。', supplement)
                self.assertIn('解除の通知であり、新たな避難行動を促すものとして扱いません。', supplement)
                self.assertNotIn('通報対象者への追加指示：', supplement)

    def test_a11_original_text_frame_and_report_are_preserved(self):
        report = mtinfo_report((70, 10, (2 << 8) | 4))
        original_text = str(report)
        before = copy.deepcopy({k: v for k, v in report.get_params().items() if k != 'camf'})
        before_camf = copy.deepcopy(report.camf.get_params())
        text = report_message(report)
        self.assertTrue(text.endswith(post_message(
            original_text, (report.nmea,), hashtags=report_hashtags(report))))
        self.assertTrue(report.a11_japanese_library)
        self.assertIn(report.a11_japanese_library, text)
        self.assertEqual({k: v for k, v in report.get_params().items() if k != 'camf'}, before)
        self.assertEqual(report.camf.get_params(), before_camf)
        self.assertEqual(str(report), original_text)

    def test_decoder_zero_exception_and_unprojectable_ranges_retain_instruction_and_parameters(self):
        report = mtinfo_report((162, 52, 1 << 51))
        self.assertTrue(report.ignore_ex2_to_ex7)
        self.assertNotIn('EX2 -', str(report))
        text = report_message(report)
        self.assertIn('追加楕円の範囲へ向かう', text)
        self.assertIn('中心 南緯90.000000°・東経45.000000°', text)
        self.assertIn('半長軸0.216km・半短軸0.216km、方位-90°', text)
        self.assertTrue(text.endswith(post_message(
            str(report), (report.nmea,), hashtags=report_hashtags(report))))
        extreme = report_message(mtinfo_report((163, 17, 131071), (180, 17, 131071),
                                               (197, 5, 31), (202, 5, 31), (207, 7, 127)))
        self.assertIn('中心 北緯90.000000°・西経135.000000°', extreme)
        self.assertIn('半長軸2500km・半短軸2500km、方位88.59375°', extreme)

    def test_no_additional_instruction_or_unreviewed_fields_keep_existing_text(self):
        for fields in (((162, 52, 0),), ((214, 6, 0),), ((66, 1, 0),), ((67, 3, 1),),
                       ((129, 2, 2),), ((197, 5, 0), (202, 5, 1))):
            with self.subTest(fields=fields):
                report = mtinfo_report(*fields)
                self.assertEqual(report_message(report), post_message(
                    str(report), (report.nmea,), hashtags=report_hashtags(report)))
        report = mtinfo_report()
        report.ex3_additional_ellipse_centre_latitude = float('nan')
        self.assertEqual(report_message(report), post_message(
            str(report), (report.nmea,), hashtags=report_hashtags(report)))


if __name__ == '__main__':
    unittest.main()
