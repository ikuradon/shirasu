"""Value labels preserve coastal and point forecast meanings and placement."""

import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.images.test_image_renderer_v3 import changed, sample

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.text import Text
    from matplotlib.transforms import Bbox
    from image_service import preview_wide_layout as wide
    from image_service import preview_wide_tsunami as maps
    from image_service import render as base
    from tools.preview.gallery_catalog import load_cases
    from tests.images.test_regional_reports import report


@unittest.skipUnless(HAS_RENDERER, 'Wide tsunami maps use the isolated image environment')
class WideTsunamiTests(unittest.TestCase):
    def figure(self):
        figure = wide.figure()
        self.addCleanup(figure.clear)
        return figure

    def texts(self, figure):
        return ''.join(label.get_text() for label in figure.findobj(Text))

    def values(self, figure):
        return [label for label in figure.texts
                if (label.get_gid() or '').startswith('wide-tsunami-value-')]

    def assert_clear_labels(self, figure):
        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        labels = self.values(figure)
        boxes = [label.get_bbox_patch().get_window_extent(renderer) for label in labels]
        locators = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        obstacles = [Bbox.from_extents(*box).transformed(figure.transFigure)
                     for box in (*wide.OBSTACLES, *((wide.LOCATOR_OBSTACLE,) if locators else ()))]
        for index, box in enumerate(boxes):
            self.assertGreaterEqual(box.x0, 0)
            self.assertGreaterEqual(box.y0, 0)
            self.assertLessEqual(box.x1, wide.PANEL_X * wide.WIDTH)
            self.assertLessEqual(box.y1, wide.HEIGHT)
            self.assertFalse(any(box.overlaps(other) for other in (*boxes[index + 1:], *obstacles)))
        for locator in locators:
            self.assertFalse(locator.collections)
            self.assertFalse(locator.lines)
            self.assertFalse(locator.texts)
        wide.validate(figure)

    def test_actual_warning_displays_heights_and_groups_only_equal_pairs(self):
        case = next(case for case in load_cases() if case.slug == 'tsunami-warning')
        figure = self.figure()
        plan = maps.draw_tsunami(figure, base._decode(case.records))
        self.assertEqual([label.get_text() for label in self.values(figure)], ['3m'] * 3)
        text = self.texts(figure)
        self.assertEqual(text.count('到達予想 09/24 10:05 JST'), 1)
        for notice in plan.notices:
            self.assertIn(notice.title, text)
        self.assert_clear_labels(figure)

    def test_same_area_alternatives_do_not_pair_maximum_height_with_earliest_arrival(self):
        figure = self.figure()
        plan = maps.draw_tsunami(figure, [report('tsunami'),
            report('tsunami', [(96, 4, 4), (90, 6, 40)])])
        text = self.texts(figure)
        for notice in plan.notices:
            for line in notice.lines:
                self.assertIn(line, text)
        label, = [label for label in self.values(figure) if label.get_gid().endswith('-300')]
        self.assertEqual(label.get_text(), '3m\n5m')
        self.assert_clear_labels(figure)

    def test_domestic_unknown_height_and_arrived_state_remain_explicit(self):
        figure = self.figure()
        maps.draw_tsunami(figure, [report('tsunami',
            [(84, 1, 0), (85, 5, 30), (90, 6, 62), (96, 4, 14)])])
        text = self.texts(figure)
        self.assertIn('不明', [label.get_text() for label in self.values(figure)])
        self.assertIn('到達予想：該当情報なし', text)
        self.assertIn('到達状況：津波到達中と推測', text)
        self.assert_clear_labels(figure)

    def test_release_retains_received_values_without_displaying_active_height_markers(self):
        figure = self.figure()
        plan = maps.draw_tsunami(figure, [report('tsunami',
            [(80, 4, 2), (84, 1, 0), (85, 5, 0), (90, 6, 0)])])
        self.assertEqual({label.get_text() for label in self.values(figure)}, {'解除'})
        text = self.texts(figure)
        self.assertIn('【解除】', text)
        self.assertIn('解除通報に含まれる受信値', text)
        for notice in plan.notices:
            for line in notice.lines:
                self.assertIn(line, text)
        self.assertIn('到達予想：該当情報なし', text)
        self.assert_clear_labels(figure)

    def test_international_ranges_at_exact_points_retain_utc_arrival_dates(self):
        figure = self.figure()
        plan = maps.draw_nwp(figure, base._decode(sample('northwest_pacific_tsunami')))
        self.assertEqual({label.get_text() for label in self.values(figure)},
                         {'3〜5m', '5〜10m', '1〜3m', '10m超', '0.3〜1m'})
        text = self.texts(figure)
        for notice in plan.notices:
            self.assertIn(notice.title, text)
            for line in notice.lines:
                self.assertIn(line, text)
        self.assertNotIn('JST', text)
        points = {int(artist.get_gid().removeprefix('nwp-point-')): artist.get_offsets()[0]
                  for artist in figure.axes[0].collections
                  if (artist.get_gid() or '').startswith('nwp-point-')}
        for code, (lon, lat) in {1: (162.6, 56.1), 84: (122.3, 31.2), 29: (144.7, 13.4),
                                 20: (140.8, -2.4), 41: (160., -9.3)}.items():
            self.assertAlmostEqual(points[code][0] % 360, lon)
            self.assertAlmostEqual(points[code][1], base._my(lat))
        self.assert_clear_labels(figure)

    def test_international_qualitative_values_and_ambiguous_arrival_are_not_numeric(self):
        figure = self.figure()
        records = changed('northwest_pacific_tsunami', (57, 5, 31), (62, 6, 63),
                          (68, 9, 509), (96, 9, 510), (124, 9, 511))
        maps.draw_nwp(figure, base._decode(records))
        values = {label.get_text() for label in self.values(figure)}
        self.assertTrue({'巨大（Huge）', '高い（High）', '不明'}.issubset(values))
        self.assertIn('到達済み、または到達時刻不明', self.texts(figure))
        self.assertNotIn('到達予想 11/10 00:20 UTC', self.texts(figure))
        self.assert_clear_labels(figure)

    def test_single_international_point_keeps_its_plain_locator_clear(self):
        figure = self.figure()
        maps.draw_nwp(figure, base._decode(changed('northwest_pacific_tsunami',
                                                  (77, 7, 84), (84, 112, 0))))
        self.assertEqual(len(self.values(figure)), 1)
        self.assertTrue(any(ax.get_gid() == 'preview-locator' for ax in figure.axes))
        self.assert_clear_labels(figure)

    def test_crowded_labels_move_but_source_points_remain_fixed(self):
        figure = self.figure()
        ax = figure.add_axes((0, 0, 1, 1))
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_axis_off()
        view = wide.MapView(ax, None, (0, 1, 0, 1), (0, .755, 0, 1), wide.OBSTACLES)
        points = ((.3, .4), (.3, .4), (.301, .403))
        notices = [SimpleNamespace(code=index, color=wide.WHITE) for index in range(3)]
        maps._value_markers(figure, view, points, notices, ['3m', '10m超', '不明'], prefix='test')
        for artist, point in zip(ax.collections, points, strict=True):
            self.assertEqual(tuple(artist.get_offsets()[0]), point)
        self.assert_clear_labels(figure)

    def test_close_future_anchor_cannot_be_mistaken_for_another_heights_point(self):
        figure = self.figure()
        ax = figure.add_axes((0, 0, 1, 1))
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_axis_off()
        view = wide.MapView(ax, None, (0, 1, 0, 1), (0, .755, 0, 1), wide.OBSTACLES)
        # The first attempted upward leader runs from 6 to 14 pixels above
        # its own anchor. A future anchor at +12 px must block that candidate.
        points = ((.3, .4), (.3, .4 + 12 / wide.HEIGHT))
        notices = [SimpleNamespace(code=index, color=wide.WHITE) for index in range(2)]
        maps._value_markers(figure, view, points, notices, ['3m', '10m超'], prefix='nearby')
        self.assert_clear_labels(figure)
        renderer = figure.canvas.get_renderer()
        marker_boxes = [Bbox.from_bounds(x * wide.WIDTH - 7, y * wide.HEIGHT - 7, 14, 14)
                        for x, y in points]
        for label in self.values(figure):
            label_box = label.get_bbox_patch().get_window_extent(renderer)
            self.assertFalse(any(label_box.overlaps(box) for box in marker_boxes))
        for line in ax.lines:
            own = int(line.get_gid().rsplit('-', 1)[-1])
            actual = line.get_transform().transform_path(line.get_path())
            self.assertFalse(any(actual.intersects_bbox(box, filled=False)
                                 for index, box in enumerate(marker_boxes) if index != own))
        for artist, point in zip(ax.collections, points, strict=True):
            self.assertEqual(tuple(artist.get_offsets()[0]), point)

    def test_missing_point_rejects_before_any_map_is_drawn(self):
        figure = self.figure()
        positions = dict(maps.nwp_map._positions())
        del positions[29]
        with patch.object(maps.nwp_map, '_positions', return_value=positions), self.assertRaises(ValueError):
            maps.draw_nwp(figure, base._decode(sample('northwest_pacific_tsunami')))
        self.assertEqual(figure.axes, [])
        self.assertEqual(figure.texts, [])


if __name__ == '__main__':
    unittest.main()
