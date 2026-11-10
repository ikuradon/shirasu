"""Regional previews add orientation without losing warnings or remote areas."""

from dataclasses import replace
import importlib.util
import unittest
from unittest.mock import patch

import test_image_renderer as helpers
from test_image_renderer_v3 import sample

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.colors import to_rgba
    from matplotlib.offsetbox import PaddedBox
    from matplotlib.text import Text
    from matplotlib.transforms import Bbox
    import numpy as np
    from image_service import map_v3 as geography
    from image_service import preview_context as context
    from image_service import preview_regions as maps
    from image_service import region_map as layout
    from image_service import render as base
    from image_service import render_v4 as published
    from image_service.eew_map import eew_plan
    from image_service.marine_regions import marine_map
    from image_service.regional_reports import RegionNotice, seismic_intensity_map, tsunami_map
    from image_service.weather_map_v4 import weather_plan

    ADAPTERS = {'weather': weather_plan, 'eew': eew_plan, 'marine': marine_map,
                'seismic_intensity': seismic_intensity_map, 'tsunami': tsunami_map}


@unittest.skipUnless(HAS_RENDERER, 'Regional preview tests use the isolated image environment')
class RegionalContextTests(unittest.TestCase):
    def draw(self, plan):
        figure = base._figure()
        self.addCleanup(figure.clear)
        ax = maps.draw_map(figure, plan)
        figure.canvas.draw()
        return figure, ax

    def assert_locator_clear(self, figure, ax):
        locator = next((axes for axes in figure.axes if axes.get_gid() == 'preview-locator'), None)
        self.assertIsNotNone(locator)
        self.assertLess(locator.get_position().y1, .5, 'The locator belongs in the lower right')
        self.assertGreater(locator.get_position().x0, .7)
        obstacle = Bbox.from_extents(*context.LOCATOR_OBSTACLE).transformed(figure.transFigure)
        for annotation in ax.artists:
            if hasattr(annotation, 'offsetbox'):
                self.assertFalse(annotation.offsetbox.get_window_extent(
                    figure.canvas.get_renderer()).overlaps(obstacle))
        for line in ax.lines:
            if line.get_gid() == 'label-leader':
                self.assertFalse(line.get_transform().transform_path(line.get_path()).intersects_bbox(
                    obstacle, filled=False))
        frame = next(patch for patch in locator.patches if patch.get_gid() == 'preview-main-extent')
        left, right = ax.get_xlim()
        bottom, top = ax.get_ylim()
        np.testing.assert_allclose(frame.get_bbox().extents,
                                   (left, bottom, right, top),
                                   rtol=0, atol=1e-10)
        return locator

    def test_all_five_received_regional_types_keep_labels_areas_and_context(self):
        for name, adapter in ADAPTERS.items():
            with self.subTest(category=name):
                plan = adapter(base._decode(sample(name)))
                # One actual received marine area gives a local sea forecast;
                # the broad seven-area sample is covered by its existing suite.
                if name == 'marine':
                    plan = replace(plan, notices=plan.notices[:1])
                figure, ax = self.draw(plan)
                self.assertTrue(any(artist.get_gid() == 'preview-prefecture-boundaries'
                                    for artist in ax.collections))
                self.assertEqual({artist.get_gid() for artist in ax.collections
                                  if (artist.get_gid() or '').startswith('region-anchor-')},
                                 {f'region-anchor-{notice.code}' for notice in plan.notices})
                labels = '\n'.join(text.get_text() for text in figure.findobj(Text)).replace('\n', '')
                for notice in plan.notices:
                    self.assertIn(notice.title.replace('\n', ''), labels)
                    for detail in notice.lines:
                        self.assertIn(detail.replace('\n', ''), labels)
                locator = self.assert_locator_clear(figure, ax)
                if plan.layer in ('marine', 'tsunami'):
                    self.assertFalse(any((artist.get_gid() or '').startswith(('region-area-', 'region-coast-'))
                                         for artist in (*locator.patches, *locator.lines)),
                                     'A local map needs only the locator frame, without duplicate warning symbols')
                helpers.RendererTests.assert_clear_leaders(self, figure, ax, len(plan.notices))
                source = geography if plan.layer in ('weather', 'eew') else layout
                prefix = 'region-coast-' if plan.layer == 'tsunami' else 'region-area-'
                self.assertEqual({artist.get_gid() for artist in (*ax.patches, *ax.lines)
                                  if (artist.get_gid() or '').startswith(prefix)},
                                 {f'{prefix}{code}' for notice in plan.notices
                                  for code in source._expand(plan.layer, notice.code)})
                if name == 'weather':
                    badges = {}
                    for badge in figure.findobj(PaddedBox):
                        texts = [text for text in badge.findobj(Text)
                                 if (text.get_gid() or '').startswith('weather-hazard-')]
                        if texts:
                            badges[texts[0].get_gid()] = badge
                    for notice in plan.notices:
                        for index, palette in enumerate(notice.styles):
                            badge = badges[f'weather-hazard-{notice.code}-{index}']
                            self.assertEqual(badge.patch.get_facecolor(), to_rgba(palette.background))
                            self.assertEqual(badge.patch.get_edgecolor(), to_rgba(palette.edge))

    def test_tokyo_remote_islands_stay_in_the_single_location_map(self):
        original = seismic_intensity_map(base._decode(sample('seismic_intensity')))
        plan = replace(original, notices=tuple(n for n in original.notices if n.code == 13))
        figure, ax = self.draw(plan)
        locator = self.assert_locator_clear(figure, ax)
        self.assertEqual(len(figure.axes), 2, 'Do not stack a second island inset over the locator')
        self.assertFalse(any(axes.get_gid() == 'region-overview' for axes in figure.axes))
        area = next(patch for patch in locator.patches if patch.get_gid() == 'region-area-13')
        expected = np.concatenate([ring for feature in layout._layer('prefectures')[13]
                                   for polygon in layout._geometry(feature, 'prefectures', 139.)
                                   for ring in polygon])
        actual = area.get_path().vertices
        self.assertEqual(len(actual), len(expected), 'Every island ring and hole must remain present')
        np.testing.assert_allclose(actual.min(axis=0), expected.min(axis=0), rtol=0, atol=0)
        np.testing.assert_allclose(actual.max(axis=0), expected.max(axis=0), rtol=0, atol=0)
        self.assertGreater(expected[:, 0].max(), 153, 'The fixture must include Minamitorishima')
        self.assertLess(expected[:, 1].min(), base._my(22), 'The fixture must include southern islands')
        self.assertTrue(layout._inside_limits([expected], (*locator.get_xlim(), *locator.get_ylim())))
        self.assertLess(ax.get_xlim()[1], expected[:, 0].max(), 'The main map remains a useful close-up')

    def test_unknown_and_conflicting_regions_reject_before_any_map_is_drawn(self):
        original = seismic_intensity_map(base._decode(sample('seismic_intensity')))
        first = original.notices[0]
        for notices in ((first, RegionNotice(99999, '未知', (), first.color)),
                        (first, replace(first, color='#000000'))):
            figure = base._figure()
            self.addCleanup(figure.clear)
            with self.assertRaises(ValueError):
                maps.draw_map(figure, replace(original, notices=notices))
            self.assertEqual(figure.axes, [])

    def test_island_inside_main_body_but_under_locator_is_preserved_in_locator(self):
        original = seismic_intensity_map(base._decode(sample('seismic_intensity')))
        plan = replace(original, notices=tuple(n for n in original.notices if n.code == 13))
        principal = [[139., 35.], [140., 35.], [140., 36.], [139., 36.], [139., 35.]]
        projected = np.asarray(principal)
        projected[:, 1] = base._my(projected[:, 1])
        limits, locator = context.limits([projected, np.asarray([[139.5, base._my(35.5)]])])
        self.assertTrue(locator)
        x = float(limits[0] + .85 * (limits[1] - limits[0]))
        y = float(limits[2] + .3 * (limits[3] - limits[2]))
        latitude = float(np.degrees(np.arctan(np.sinh(np.radians(y)))))
        island = [[x, latitude], [x + .01, latitude], [x + .01, latitude + .01],
                  [x, latitude + .01], [x, latitude]]
        feature = {'type': 'Feature', 'properties': {'code': '13', 'anchor': [139.5, 35.5]},
                   'geometry': {'type': 'MultiPolygon', 'coordinates': [[principal], [island]]}}
        with patch.object(layout, '_layer', return_value={13: [feature]}), \
                patch.object(layout, '_expand', return_value=(13,)):
            figure, main = self.draw(plan)
        position = figure.transFigure.inverted().transform(main.transData.transform((x, y)))
        self.assertTrue(context.LOCATOR_OBSTACLE[0] < position[0] < context.LOCATOR_OBSTACLE[2])
        self.assertTrue(context.LOCATOR_OBSTACLE[1] < position[1] < context.LOCATOR_OBSTACLE[3])
        self.assertTrue(layout.LABEL_BOTTOM < position[1] < layout.LABEL_TOP)
        locator = self.assert_locator_clear(figure, main)
        area = next(patch for patch in locator.patches if patch.get_gid() == 'region-area-13')
        self.assertEqual(len(area.get_path().vertices), len(principal) + len(island))
        self.assertGreaterEqual(area.get_path().vertices[:, 0].max(), x + .01)

    def test_preview_drawing_does_not_change_published_weather_png(self):
        records = sample('weather')
        before = published.render(records)
        self.draw(weather_plan(base._decode(records)))
        self.assertEqual(before, published.render(records))


if __name__ == '__main__':
    unittest.main()
