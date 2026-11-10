"""Real ash-forecast images retain received municipalities, times and phenomena."""

import copy
import importlib.util
import struct
import unittest
from unittest.mock import patch

from image_protocol import make_path
import test_image_renderer as helpers
from test_image_renderer_v3 import sample, changed

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
NAME = 'ash_fall'
if HAS_RENDERER:
    from matplotlib.colors import to_rgba
    from matplotlib.offsetbox import PaddedBox
    from matplotlib.text import Text
    from matplotlib.transforms import Bbox
    from image_service import render as base
    from image_service import render_v4 as published
    from image_service import render_preview as preview
    from image_service import ash_map, volcano_map
    from image_service import preview_context as context


def forecast(*entries):
    """Encode (municipality, hours, phenomenon) tuples in a real training frame."""
    fields = [(83, 116, 0)]
    for index, (municipality, hours, phenomenon) in enumerate(entries):
        offset = 83 + index * 29
        fields.extend(((offset, 3, hours), (offset + 3, 3, phenomenon),
                       (offset + 6, 23, municipality)))
    return changed(NAME, *fields)


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for ash preview PNG tests')
class AshForecastRendererTests(unittest.TestCase):
    def capture(self, records):
        figures = []
        original = base._figure

        def make_figure():
            figure = original()
            figures.append(figure)
            figure.clear = lambda *args, **kwargs: None
            self.addCleanup(lambda: base.Figure.clear(figure))
            return figure

        with patch.object(base, '_figure', side_effect=make_figure):
            image = preview.render(records)
        return image, figures[-1]

    def texts(self, figure):
        return '\n'.join(text.get_text() for text in figure.findobj(Text))

    def points(self, figure, prefix):
        self.assertTrue(figure.axes, 'All supported forecasts must retain their map')
        return {int(artist.get_gid().removeprefix(prefix)): artist
                for artist in figure.axes[0].collections
                if (artist.get_gid() or '').startswith(prefix)}

    def badges(self, figure):
        result = {}
        for badge in figure.findobj(PaddedBox):
            texts = [text for text in badge.findobj(Text)
                     if (text.get_gid() or '').startswith('ash-hazard-')]
            if texts:
                self.assertEqual(len(texts), 1)
                result[texts[0].get_text()] = (badge, texts[0])
        return result

    def assert_complete_map(self, figure, municipalities):
        self.assertEqual(set(self.points(figure, 'municipality-anchor-')), set(municipalities))
        self.assertEqual(set(self.points(figure, 'volcano-point-')), {506})
        helpers.RendererTests.assert_clear_leaders(self, figure, figure.axes[0], len(municipalities) + 1)

    def test_training_map_preserves_every_municipality_time_and_forecast_phenomenon(self):
        records = sample(NAME)
        self.assertEqual(base._decode(records)[0].report_classification_no, 7)
        image, figure = self.capture(records)
        self.assertEqual(image.category, 'AshFall')
        self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', image.png[16:24]), (1440, 1200))
        self.assert_complete_map(figure, {4620100, 4621400, 4621800})
        texts = self.texts(figure)
        for expected in ('訓練', '降灰予報', '詳細', '桜島',
                         '鹿児島市', '垂水市', '霧島市',
                         '2026/11/10 09:10', '2026/11/10 09:00', 'JST',
                         '基点＋1時間：多量の降灰', '基点＋1時間：小さな噴石の落下',
                         '基点＋2時間：やや多量の降灰', '基点＋3時間：少量の降灰'):
            self.assertIn(expected, texts)

    def test_actual_badges_distinguish_ash_amounts_and_cinders_without_losing_borders(self):
        _, figure = self.capture(sample(NAME))
        badges = self.badges(figure)
        expected = {
            '基点＋1時間：多量の降灰': ('#ff0000', '#ffffff'),
            '基点＋2時間：やや多量の降灰': ('#ffff00', '#000000'),
            '基点＋3時間：少量の降灰': ('#d9d9d9', '#000000'),
        }
        self.assertEqual(len(badges), 4)
        for text, (background, foreground) in expected.items():
            badge, label = badges[text]
            self.assertEqual(badge.patch.get_facecolor(), to_rgba(background))
            self.assertEqual(to_rgba(label.get_color()), to_rgba(foreground))
            self.assertEqual(label.get_path_effects(), [], 'Dark map halos must not obscure badge text')
        cinders, cinders_text = badges['基点＋1時間：小さな噴石の落下']
        self.assertEqual(cinders.patch.get_facecolor()[3], 0)
        self.assertEqual(cinders.patch.get_edgecolor(), to_rgba('#ff0000'))
        self.assertEqual(to_rgba(cinders_text.get_color()), to_rgba('#ffffff'))
        # Border padding must be accounted for by the shared label placement.
        renderer = figure.canvas.get_renderer()
        for annotation in figure.axes[0].artists:
            if not hasattr(annotation, 'offsetbox'):
                continue
            outer = annotation.offsetbox.get_window_extent(renderer)
            for badge in annotation.findobj(PaddedBox):
                inner = badge.get_window_extent(renderer)
                self.assertGreaterEqual(inner.x0, outer.x0 - 1)
                self.assertGreaterEqual(inner.y0, outer.y0 - 1)
                self.assertLessEqual(inner.x1, outer.x1 + 1)
                self.assertLessEqual(inner.y1, outer.y1 + 1)

    def test_zoomed_ash_map_has_a_lower_right_locator_and_unobscured_forecast_labels(self):
        _, figure = self.capture(sample(NAME))
        self.assert_complete_map(figure, {4620100, 4621400, 4621800})
        main = figure.axes[0]
        locator, = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        self.assertLess(locator.get_position().y1, .5)
        self.assertGreater(locator.get_position().x0, .7)
        self.assertTrue(any(artist.get_gid() == 'preview-prefecture-boundaries'
                            for artist in main.collections))
        obstacle = Bbox.from_extents(*context.LOCATOR_OBSTACLE).transformed(figure.transFigure)
        for prefix in ('municipality-anchor-', 'volcano-point-'):
            for point in self.points(figure, prefix).values():
                self.assertFalse(obstacle.contains(*main.transData.transform(point.get_offsets()[0])))
        for artist in main.artists:
            if hasattr(artist, 'offsetbox'):
                self.assertFalse(artist.offsetbox.get_window_extent(figure.canvas.get_renderer()).overlaps(obstacle))
        for line in main.lines:
            if line.get_gid() == 'label-leader':
                self.assertFalse(line.get_transform().transform_path(line.get_path()).intersects_bbox(obstacle, filled=False))

    def test_locator_retains_remote_municipal_parts_outside_the_enlarged_mainland(self):
        records = forecast((4620100, 1, 2))
        catalogue = dict(ash_map._municipalities())
        feature = copy.deepcopy(catalogue[4620100])
        geometry = feature['geometry']
        if geometry['type'] == 'Polygon':
            geometry = {'type': 'MultiPolygon', 'coordinates': [geometry['coordinates']]}
            feature['geometry'] = geometry
        remote = [[145., 29.], [145.02, 29.], [145.02, 29.02], [145., 29.]]
        geometry['coordinates'].append([remote])
        catalogue[4620100] = feature
        with patch.object(ash_map, '_municipalities', return_value=catalogue):
            _, figure = self.capture(records)
        self.assert_complete_map(figure, {4620100})
        main = figure.axes[0]
        self.assertLess(main.get_xlim()[1], 145.)
        locator, = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        area, = [patch for patch in locator.patches if patch.get_gid() == 'region-area-4620100']
        self.assertGreaterEqual(area.get_path().vertices[:, 0].max(), 145.02)
        for lon, lat in remote:
            self.assertTrue(locator.bbox.contains(*locator.transData.transform((lon, float(base._my(lat))))))

    def test_official_volcano_point_and_neutral_municipalities_do_not_invent_a_plume(self):
        _, figure = self.capture(sample(NAME))
        volcano = self.points(figure, 'volcano-point-')[506]
        position = volcano.get_offsets()[0]
        self.assertAlmostEqual(float(position[0]), 130.657)
        self.assertAlmostEqual(float(position[1]), float(base._my(31.593)))
        # A triangle marks the volcano, distinct from the representative city points.
        self.assertEqual(len(volcano.get_paths()[0].vertices), 4)
        areas = {int(artist.get_gid().removeprefix('region-area-')): artist
                 for artist in figure.axes[0].patches
                 if (artist.get_gid() or '').startswith('region-area-')}
        self.assertEqual(set(areas), {4620100, 4621400, 4621800})
        for area in areas.values():
            self.assertEqual(area.get_edgecolor(), to_rgba('#8b98a5'))
            self.assertEqual(area.get_facecolor(), to_rgba('#a3b3c2', .035))
        self.assertFalse(any((artist.get_gid() or '').startswith(('plume-', 'ash-distribution-', 'wind-'))
                             for axes in figure.axes for artist in (*axes.patches, *axes.lines)))
        texts = self.texts(figure)
        self.assertIn('未掲載地域の安全', texts)

    def test_same_city_keeps_separate_times_and_simultaneous_cinders_with_clear_leaders(self):
        records = forecast((4620100, 1, 3), (4620100, 1, 4),
                           (4620100, 2, 2), (4620100, 3, 1))
        _, figure = self.capture(records + records)
        self.assert_complete_map(figure, {4620100})
        self.assertEqual(set(self.badges(figure)), {
            '基点＋1時間：多量の降灰', '基点＋1時間：小さな噴石の落下',
            '基点＋2時間：やや多量の降灰', '基点＋3時間：少量の降灰'})

    def test_cancelled_unknown_or_conflicting_forecasts_use_the_complete_summary(self):
        cases = [changed(NAME, (41, 2, 2)), changed(NAME, (69, 2, 0)),
                 changed(NAME, (71, 12, 4000)), changed(NAME, (86, 3, 7)),
                 changed(NAME, (83, 3, 7)), changed(NAME, (89, 23, 999999)),
                 changed(NAME, (53, 5, 0), (58, 5, 31), (63, 6, 63)),
                 forecast((4620100, 1, 3)) + forecast((4620100, 1, 2))]
        for records in cases:
            with self.subTest(records=records):
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertEqual(image.png, published.render(records).png)
        _, figure = self.capture(cases[0])
        self.assertIn('取消', self.texts(figure))

    def test_missing_municipality_or_volcano_and_unreadable_layout_leave_no_partial_map(self):
        records = sample(NAME)
        incomplete_cities = dict(ash_map._municipalities())
        del incomplete_cities[4621400]
        incomplete_volcanoes = dict(volcano_map._positions())
        del incomplete_volcanoes['506']
        for target, name, kwargs in (
                (ash_map, '_municipalities', {'return_value': incomplete_cities}),
                (volcano_map, '_positions', {'return_value': incomplete_volcanoes}),
                (ash_map, 'draw_map', {'side_effect': ValueError('No readable layout')})):
            with self.subTest(missing=name), patch.object(target, name, **kwargs):
                image, figure = self.capture(records)
            self.assertEqual(figure.axes, [])
            self.assertEqual(image.png, published.render(records).png)

    def test_preview_is_deterministic_and_published_urls_keep_the_original_ash_summary(self):
        records = sample(NAME)
        old = published.render(records)
        first = preview.render(records)
        self.assertEqual(first, preview.render(records))
        self.assertNotEqual(first.png, old.png)
        self.assertEqual(old, published.render(records))
        with patch.object(preview, 'render', side_effect=AssertionError('Preview is offline only')):
            for version in ('v1', 'v2', 'v3', 'v4'):
                with self.subTest(version=version):
                    response = helpers.request(make_path(records, 'test', helpers.KEY, version=version))
                    self.assertEqual(response['status'], '200 OK')
                    self.assertEqual(response['body'], old.png)

    def test_ash_rendering_does_not_change_nwp_preview_or_published_weather(self):
        nwp = sample('northwest_pacific_tsunami')
        before = preview.render(nwp)
        weather = sample('weather')
        old_weather = published.render(weather)
        preview.render(sample(NAME))
        self.assertEqual(preview.render(nwp), before)
        preview.render(weather)
        self.assertEqual(published.render(weather), old_weather)


if __name__ == '__main__':
    unittest.main()
