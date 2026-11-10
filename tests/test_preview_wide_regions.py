"""Wide regional presentation retains scope and the values attached to it."""

from dataclasses import replace
import importlib.util
import unittest
from unittest.mock import patch


HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.colors import to_rgba
    from matplotlib.text import Text
    import numpy as np

    from image_service import render as base
    from image_service import render_v4 as published
    from image_service import preview_wide_layout as wide
    from image_service import preview_wide_regions as maps
    from image_service.eew_map import eew_plan
    from image_service.weather_map_v4 import weather_plan
    from scripts.gallery_catalog import load_cases
    from test_regional_reports import report


@unittest.skipUnless(HAS_RENDERER, 'Wide regional maps use the isolated image environment')
class WideRegionalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def draw(self, slug, function):
        records = self.cases[slug].records
        figure = wide.figure()
        self.addCleanup(figure.clear)
        plan = function(figure, base._decode(records))
        figure.canvas.draw()
        wide.validate(figure)
        return figure, plan

    def all_text(self, figure):
        return ''.join(text.get_text() for text in figure.findobj(Text)).replace('\n', '').replace(' ', '')

    def assert_plain_locator(self, locator):
        identities = {artist.get_gid() for artist in locator.patches}
        self.assertEqual(identities - {None}, {'v3-land', 'preview-main-extent'})
        self.assertFalse(locator.collections, 'Locator must not show administrative or hazard boundaries')
        self.assertFalse(locator.lines, 'Locator must not show tracks or graticules')
        self.assertFalse(locator.texts)
        self.assertFalse(locator.artists)
        frame, = [artist for artist in locator.patches if artist.get_gid() == 'preview-main-extent']
        self.assertEqual(frame.get_edgecolor(), to_rgba(wide.WHITE))
        self.assertEqual(frame.get_facecolor()[3], 0)

    def test_weather_has_only_sidebar_names_and_two_exact_outline_colours(self):
        figure, plan = self.draw('weather-many', maps.draw_weather)
        main, locator = figure.axes
        self.assertEqual(figure.canvas.get_width_height(), (1920, 1080))
        self.assertLess(locator.get_position().x1, wide.PANEL_X)
        self.assertLess(locator.get_position().y1, .4)
        self.assertFalse(main.artists)
        self.assertFalse(any(line.get_gid() == 'label-leader' for line in main.lines))
        for notice in plan.notices:
            texts = [text for text in figure.findobj(Text) if text.get_text() == notice.title]
            self.assertEqual(len(texts), 1)
            self.assertGreater(texts[0].get_position()[0], wide.PANEL_X)
        areas = [artist for artist in main.patches
                 if (artist.get_gid() or '').startswith('region-area-')]
        expected = {code for notice in plan.notices
                    for code in maps.geometry.geography._expand('weather', notice.code)}
        self.assertEqual({int(area.get_gid().removeprefix('region-area-')) for area in areas}, expected)
        self.assertTrue(all(area.get_facecolor()[3] == 0 for area in areas))
        borders = [artist for artist in main.patches
                   if (artist.get_gid() or '').startswith('weather-outline-')]
        self.assertEqual(len(borders), 3 * len(expected))
        self.assertEqual({border.get_edgecolor() for border in borders},
                         {to_rgba('#f2f5f7'), to_rgba('#0c000c'), to_rgba('#aa00aa')})
        self.assert_plain_locator(locator)
        full = np.concatenate([area.get_path().vertices for area in areas])
        self.assertGreater(full[:, 0].max(), 153, 'Tokyo includes Minamitorishima')
        self.assertLess(full[:, 1].min(), base._my(22), 'Tokyo includes its southern islands')
        self.assertGreaterEqual(full[:, 0].min(), locator.get_xlim()[0])
        self.assertLessEqual(full[:, 0].max(), locator.get_xlim()[1])
        self.assertGreaterEqual(full[:, 1].min(), locator.get_ylim()[0])
        self.assertLessEqual(full[:, 1].max(), locator.get_ylim()[1])
        self.assertGreater(main.get_ylim()[0], base._my(30),
                           'Tokyo island subdivisions must not shrink the Kanto close-up')

    def test_eew_compaction_preserves_all_16_areas_and_global_prediction(self):
        figure, plan = self.draw('eew', maps.draw_eew)
        self.assertEqual([notice.title for notice in plan.notices],
                         ['中国地方', '四国地方', '九州地方'])
        source = eew_plan(base._decode(self.cases['eew'].records))
        scope = lambda values: {code for notice in values for code in
                                maps.geometry.geography._expand('eew', notice.code)}
        self.assertEqual(scope(source.notices), scope(plan.notices))
        self.assertEqual(len(scope(plan.notices)), 16)
        text = self.all_text(figure)
        self.assertIn('通報全体の予測震度', text)
        self.assertIn('6弱程度以上', text)
        for line in source.notice_lines:
            if line.startswith(('最大予測長周期地震動：', '地震発生 ')):
                for value in line.replace('最大予測長周期地震動：', '').split(' / '):
                    self.assertIn(value.replace(' ', ''), text)
        self.assertNotIn('強い揺れに警戒してください。', text)

    def test_eew_plum_keeps_assumed_place_time_and_omits_fixed_depth_magnitude(self):
        figure = wide.figure()
        self.addCleanup(figure.clear)
        plan = maps.draw_eew(figure, [report('eew', [(96, 9, 10), (105, 7, 10)])])
        wide.validate(figure)
        text = self.all_text(figure)
        self.assertIn('仮定震源（PLUM法）', text)
        self.assertIn('仮定発生時刻', text)
        self.assertIn('地名茨城県沖', text)
        self.assertIn('最大予測長周期地震動', text)
        self.assertIn('階級1〜3', text)
        self.assertNotIn('M1.0', text)
        self.assertNotIn('10km', text)
        self.assertNotIn('強い揺れに警戒してください。', text)
        self.assertEqual(len(plan.notices), 6)

    def test_eew_does_not_promote_partial_coverage_or_hide_distinct_values(self):
        source = eew_plan(base._decode(self.cases['eew'].records))
        individual = next(notice for notice in source.notices if notice.code == 37)
        broad = next(notice for notice in source.notices if notice.code == 66)
        partial = replace(source, notices=(individual,))
        self.assertEqual([notice.code for notice in maps.compact_eew(partial).notices], [37])
        distinct = replace(source, notices=(replace(individual, lines=('別の受信値',)), broad))
        self.assertEqual([notice.code for notice in maps.compact_eew(distinct).notices], [37, 66])

    def test_different_weather_sets_keep_individual_rows_and_own_side_borders(self):
        reports = base._decode(self.cases['weather-many'].records)
        plan = weather_plan(reports)
        first = plan.notices[0]
        plan = replace(plan, notices=(replace(first, lines=first.lines[:1], styles=first.styles[:1]),
                                      *plan.notices[1:]))
        figure = wide.figure()
        self.addCleanup(figure.clear)
        with patch.object(maps, 'weather_plan', return_value=plan):
            maps.draw_weather(figure, reports)
        text = self.all_text(figure)
        self.assertNotIn('区域に共通', text)
        self.assertEqual(text.count('大雨特別警報'), 7)
        self.assertEqual(text.count('土砂災害警戒情報'), 6)
        borders = [artist for artist in figure.axes[0].patches
                   if (artist.get_gid() or '').startswith('weather-outline-')]
        self.assertTrue(borders)
        self.assertTrue(all(border.get_clip_path() is not None for border in borders),
                        'Different neighbouring categories must not paint across each other')

    def test_received_region_values_survive_right_panel_reflow(self):
        for slug, function in (('marine', maps.draw_marine),
                               ('seismic-intensity', maps.draw_seismic),
                               ('tsunami-warning', maps.draw_tsunami),
                               ('tsunami-release', maps.draw_tsunami)):
            with self.subTest(slug=slug):
                figure, plan = self.draw(slug, function)
                text = self.all_text(figure)
                for notice in plan.notices:
                    self.assertIn(notice.title.replace(' ', ''), text)
                    for line in notice.lines:
                        if slug == 'seismic-intensity':
                            for value in line.removeprefix('受信震度 ').split(' / '):
                                self.assertIn('震度' + value, text)
                        else:
                            self.assertIn(line.replace(' ', ''), text)
                references = [item for item in figure.axes[0].texts
                              if (item.get_gid() or '').startswith('region-reference-')]
                self.assertEqual(len(references), 0 if slug == 'seismic-intensity' else len(plan.notices))
                self.assertTrue(all(item.get_text().isalpha() for item in references))
                for locator in (ax for ax in figure.axes if ax.get_gid() == 'preview-locator'):
                    self.assert_plain_locator(locator)
                if slug == 'seismic-intensity':
                    for line in plan.notice_lines:
                        self.assertIn(line.replace(' ', ''), text)
                if slug == 'tsunami-release':
                    self.assertIn('【解除】', text)

    def test_wide_drawing_does_not_change_published_state_or_png(self):
        records = self.cases['weather'].records
        constants = (base.WIDTH, base.HEIGHT, base.DPI, base.SEA, base.LAND, base.COAST)
        before = published.render(records)
        self.draw('weather-many', maps.draw_weather)
        self.assertEqual(constants, (base.WIDTH, base.HEIGHT, base.DPI, base.SEA, base.LAND, base.COAST))
        self.assertEqual(before, published.render(records))


if __name__ == '__main__':
    unittest.main()
