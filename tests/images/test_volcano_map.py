"""Volcano rendering keeps received meanings and maps only official positions."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from azarashi.definitions.qzss.dcr.local_government import local_government
from azarashi.definitions.qzss.dcr.volcanic_warning_code import volcanic_warning_code
from matplotlib.offsetbox import AnnotationBbox
from matplotlib.patches import Circle
from matplotlib.text import Text

from image_service import render as base
from image_service import volcano_map as volcano
from tests.images.test_regional_reports import report


class VolcanoMapTests(unittest.TestCase):
    def fixture(self, *fields):
        return report('volcano', fields)

    def figure(self):
        figure = base._figure()
        self.addCleanup(figure.clear)
        return figure

    def test_pinned_catalogue_maps_all_specific_dcr_volcanoes(self):
        self.assertTrue(volcano.verify_assets())
        positions = volcano._positions()
        self.assertEqual(len(positions), 117)
        self.assertEqual(positions['503']['coordinates'], [131.104, 32.884])
        self.assertNotEqual(positions['350']['coordinates'], positions['351']['coordinates'])
        self.assertNotEqual(positions['550']['coordinates'], positions['551']['coordinates'])
        for code in (900, 901, 902, 4000):
            self.assertNotIn(str(code), positions)

    def test_training_plan_keeps_level_time_and_every_received_municipality(self):
        received = self.fixture()
        before = copy.deepcopy(vars(received))
        plan = volcano.volcano_plan([received])
        self.assertEqual(received.report_classification_no, 7)
        self.assertEqual(plan.title, '火山情報')
        self.assertEqual(plan.notices[0].title, '阿蘇山')
        self.assertEqual(plan.notices[0].lines, (
            'レベル3(入山規制)', '活動時刻：2026年11月9日23時45分 JST',
            '対象市町村（受信分）', '熊本県阿蘇市', '熊本県高森町', '熊本県南阿蘇村'))
        self.assertIn('代表位置', plan.note)
        self.assertIn('避難対象区域を示すものではありません', plan.note)
        self.assertEqual(vars(received), before)

    def test_levels_and_eruption_states_are_not_inferred_from_other_codes(self):
        for code in volcano.COLORS:
            with self.subTest(code=code):
                plan = volcano.volcano_plan([self.fixture((69, 7, code))])
                self.assertEqual(plan.notices[0].lines[0], volcanic_warning_code[code])
                self.assertNotIn('解除', '\n'.join(plan.notices[0].lines))
        confirmed = volcano.volcano_plan([self.fixture((69, 7, 52))])
        possible = volcano.volcano_plan([self.fixture((69, 7, 62))])
        self.assertEqual(confirmed.notices[0].lines[0], '噴火')
        self.assertEqual(possible.notices[0].lines[0], '噴火したもよう')

    def test_approximate_times_do_not_gain_false_precision(self):
        for ambiguity in (1, 2, 3):
            plan = volcano.volcano_plan([self.fixture((50, 3, ambiguity))])
            self.assertIn('45分頃 JST', plan.notices[0].lines[1])
        hour = volcano.volcano_plan([self.fixture((50, 3, 4))]).notices[0].lines[1]
        self.assertIn('23時頃 JST', hour)
        self.assertNotIn('分', hour)
        day = volcano.volcano_plan([self.fixture((50, 3, 5))]).notices[0].lines[1]
        self.assertEqual(day, '活動時刻：2026年11月9日頃 UTC')
        for ambiguity, unit in ((6, '月'), (7, '年')):
            plan = volcano.volcano_plan([self.fixture((50, 3, ambiguity))])
            self.assertEqual(plan.notices[0].lines[1], f'活動時刻：{unit}単位の概数（日時は特定不可）')

    def test_undefined_activity_time_is_visible_without_inventing_a_date(self):
        plan = volcano.volcano_plan([self.fixture((53, 5, 0), (58, 5, 31), (63, 6, 63))])
        self.assertEqual(plan.notices[0].lines[1], '活動時刻：不明')

    def test_distinct_municipalities_are_combined_without_repeating_identical_values(self):
        first = self.fixture()
        other = self.fixture((88, 23, 4310000))
        plan = volcano.volcano_plan([first, other, first])
        self.assertEqual(plan.notices[0].lines.count('熊本県阿蘇市'), 1)
        self.assertEqual(plan.notices[0].lines.count('熊本県高森町'), 1)
        self.assertIn('熊本県熊本市', plan.notices[0].lines)

    def test_no_municipality_list_does_not_mean_no_hazard(self):
        plan = volcano.volcano_plan([self.fixture((88, 115, 0))])
        self.assertEqual(plan.notices[0].lines[-1], '対象市町村：本通報に記載なし')

    def test_unknown_positions_warnings_or_cities_require_whole_report_fallback(self):
        fields = [[(76, 12, code)] for code in (900, 901, 902, 4000, 4095)]
        fields += [[(69, 7, code)] for code in (0, 63, 127)]
        fields += [[(88, 23, 8388607)]]
        for changes in fields:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                volcano.volcano_plan([self.fixture(*changes)])

    def test_mixed_activity_or_cancelled_messages_do_not_become_one_active_map(self):
        for changes in ([(76, 12, 506)], [(69, 7, 15)], [(63, 6, 44)],
                        [(50, 3, 4)], [(14, 3, 1)], [(41, 2, 1)]):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                volcano.volcano_plan([self.fixture(), self.fixture(*changes)])
        with self.assertRaises(ValueError):
            volcano.volcano_plan([self.fixture((41, 2, 2))])
        with self.assertRaises(ValueError):
            volcano.volcano_plan([])

    def test_draw_uses_one_official_point_with_complete_label_and_no_hazard_circle(self):
        figure = self.figure()
        plan = volcano.draw_volcano(figure, [self.fixture()])
        self.assertEqual(len(figure.axes), 1)
        axes = figure.axes[0]
        markers = [artist for artist in axes.collections if artist.get_gid() == 'volcano-point-503']
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].get_offsets()[0, 0], 131.104)
        self.assertAlmostEqual(markers[0].get_offsets()[0, 1], base._my(32.884))
        self.assertFalse(any(isinstance(artist, Circle) for artist in axes.patches))
        self.assertFalse(any((artist.get_gid() or '').startswith('region-area-') for artist in axes.patches))
        labels = [artist for artist in axes.artists if isinstance(artist, AnnotationBbox)]
        self.assertEqual(len(labels), 1)
        text = '\n'.join(artist.get_text() for artist in figure.findobj(Text))
        for expected in ('阿蘇山', '入山規制', '熊本県阿蘇市', '熊本県高森町', '熊本県南阿蘇村', '23時45分'):
            self.assertIn(expected, text)
        self.assertEqual(plan.layer, 'volcano')

    def test_overfull_city_list_is_rejected_instead_of_truncated(self):
        received = self.fixture()
        codes = list(local_government)[:50]
        received.local_governments_raw = codes
        received.local_governments = [local_government[code] for code in codes]
        with self.assertRaises(ValueError):
            volcano.draw_volcano(self.figure(), [received])

    def test_asset_hash_mismatch_is_a_server_error(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for name in ('volcano.json', 'volcano_manifest.json', 'VOLCANO_NOTICE.txt'):
                (destination / name).write_bytes((volcano.ASSETS / name).read_bytes())
            (destination / 'volcano.json').write_text('{}')
            volcano.verify_assets.cache_clear()
            try:
                with patch.object(volcano, 'ASSETS', destination), self.assertRaises(RuntimeError):
                    volcano.verify_assets()
            finally:
                volcano.verify_assets.cache_clear()


if __name__ == '__main__':
    unittest.main()
