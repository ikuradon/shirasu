"""Geographic context names must yield to the received forecast annotations."""

import importlib.util
from types import SimpleNamespace
import unittest

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from matplotlib.offsetbox import AnnotationBbox, TextArea
    from matplotlib.patches import Rectangle
    from matplotlib.path import Path
    from image_service import preview_place_labels as names
    from image_service import preview_wide_layout as wide
    from image_service import render as base


@unittest.skipUnless(HAS_RENDERER, 'Install the isolated image renderer dependencies')
class PlaceLabelTests(unittest.TestCase):
    def view(self, *, locator=False):
        fig = wide.figure()
        self.addCleanup(fig.clear)
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_axis_off()
        inset = fig.add_axes(wide.LOCATOR_POSITION) if locator else None
        if inset is not None:
            inset.set_gid('preview-locator')
            inset.set_axis_off()
        obstacles = wide.OBSTACLES + ((wide.LOCATOR_OBSTACLE,) if locator else ())
        return fig, wide.MapView(ax, inset, (0, 1, 0, 1), (0, wide.PANEL_X, 0, 1), obstacles)

    def place(self, *, position=(.3, .4), name='熊本県', code=43, kind='prefecture', polygon=None):
        if polygon is None:
            polygon = (np.array(((.04, .1), (.70, .1), (.70, .75), (.04, .75), (.04, .1))),)
        return SimpleNamespace(name=name, position=position, code=code, kind=kind, polygon=polygon)

    def test_names_are_sparse_deduplicated_and_do_not_add_forecast_symbols(self):
        fig, view = self.view()
        places = [self.place(position=(x, y), name=f'地域{index}', code=index)
                  for index, (x, y) in enumerate(((.12, .3), (.32, .3), (.53, .3),
                                                  (.12, .6), (.32, .6), (.53, .6)))]
        labels = names.draw_place_labels(fig, view, (places[0], *places))
        self.assertEqual(len(labels), 5)
        self.assertEqual(len({label.get_text() for label in labels}), 5)
        self.assertTrue(all(label.axes is view.ax for label in labels))
        self.assertFalse(view.ax.lines)
        self.assertFalse(view.ax.collections)
        self.assertTrue(all(label.get_bbox_patch() is None for label in labels))

    def test_nearest_core_name_is_followed_by_geographically_spread_names(self):
        fig, view = self.view()
        points = ((.25, .45), (.33, .45), (.25, .57), (.33, .57), (.41, .45),
                  (.08, .20), (.68, .65))
        places = [self.place(position=point, name=f'地域{index}', code=index)
                  for index, point in enumerate(points)]
        labels = names.draw_place_labels(fig, view, places)
        self.assertEqual(len(labels), 5)
        self.assertEqual(labels[0].get_text(), '地域0')
        self.assertTrue({'地域5', '地域6'}.issubset({label.get_text() for label in labels}),
                        'A nearby cluster must not consume all slots before the distant map context')

    def test_names_avoid_seismic_squares_and_typhoon_annotation_boxes(self):
        for kind in ('seismic', 'typhoon'):
            with self.subTest(kind=kind):
                fig, view = self.view()
                point = (.3, .4)
                if kind == 'seismic':
                    obstacle = Rectangle((point[0] - 35 / wide.WIDTH, point[1] - 35 / wide.HEIGHT),
                                         70 / wide.WIDTH, 70 / wide.HEIGHT,
                                         transform=fig.transFigure, gid='seismic-badge-43-4', zorder=7)
                    view.ax.add_patch(obstacle)
                    view.ax.text(*point, '5＋', ha='center', va='center',
                                 fontproperties=base._font(25, True), gid='seismic-intensity-43-4')
                else:
                    obstacle = AnnotationBbox(TextArea('11/10 12:00', textprops={
                        'fontproperties': base._font(20, True)}), point, xycoords='data',
                        frameon=False, box_alignment=(.5, .5), zorder=9)
                    view.ax.add_artist(obstacle)
                marker = view.ax.scatter(*point, s=100, gid='received-point')
                original = np.array(marker.get_offsets(), copy=True)
                lines_before = tuple(view.ax.lines)
                labels = names.draw_place_labels(fig, view, (self.place(),))
                self.assertEqual(len(labels), 1)
                renderer = fig.canvas.get_renderer()
                self.assertFalse(labels[0].get_window_extent(renderer).overlaps(
                    obstacle.get_window_extent(renderer)))
                np.testing.assert_array_equal(marker.get_offsets(), original)
                self.assertEqual(tuple(view.ax.lines), lines_before)
                self.assertTrue(Path(self.place().polygon[0]).contains_point(labels[0].get_position()))

    def test_names_do_not_hide_received_tracks_or_leaders(self):
        fig, view = self.view()
        line, = view.ax.plot((.1, .6), (.4, .4), linewidth=3, gid='typhoon-track')
        original = np.array(line.get_xydata(), copy=True)
        labels = names.draw_place_labels(fig, view, (self.place(),))
        self.assertEqual(len(labels), 1)
        box = labels[0].get_window_extent(fig.canvas.get_renderer()).padded(4)
        self.assertFalse(line.get_transform().transform_path(line.get_path()).intersects_bbox(box, filled=False))
        np.testing.assert_array_equal(line.get_xydata(), original)
        self.assertEqual(len(view.ax.lines), 1, 'Context names must not add callout leaders')

    def test_received_area_outline_is_clear_but_its_interior_remains_available(self):
        for point in ((.3, .4), (.2, .4)):
            with self.subTest(anchor=point):
                fig, view = self.view()
                area = Rectangle((.2, .25), .2, .3, facecolor='#586a73', edgecolor='white',
                                 linewidth=1.5, gid='region-area-43')
                view.ax.add_patch(area)
                labels = names.draw_place_labels(fig, view, (self.place(position=point),))
                self.assertEqual(len(labels), 1)
                box = labels[0].get_window_extent(fig.canvas.get_renderer()).padded(4)
                self.assertFalse(area.get_transform().transform_path(area.get_path()).intersects_bbox(
                    box, filled=False))
                if point == (.3, .4):
                    np.testing.assert_allclose(labels[0].get_position(), point)

    def test_thin_neutral_subdivision_line_does_not_exclude_a_useful_place_name(self):
        fig, view = self.view()
        area = Rectangle((.3, .25), .2, .3, facecolor='#586a73', edgecolor='#94a3ad',
                         linewidth=.45, gid='region-area-43')
        view.ax.add_patch(area)
        labels = names.draw_place_labels(fig, view, (self.place(),))
        self.assertEqual(len(labels), 1)
        np.testing.assert_allclose(labels[0].get_position(), (.3, .4))

    def test_header_panel_and_plain_locator_stay_free_of_context_names(self):
        fig, view = self.view(locator=True)
        places = [self.place(position=point, name=f'区域{index}', code=index, kind='island')
                  for index, point in enumerate(((.30, .90), (.87, .5), (.67, .2)))]
        labels = names.draw_place_labels(fig, view, places)
        self.assertEqual(labels, ())
        self.assertFalse(view.locator.texts)
        self.assertFalse(view.locator.lines)
        self.assertFalse(view.locator.collections)

    def test_blocked_prefecture_name_is_omitted_without_moving_it_to_another_region(self):
        fig, view = self.view()
        polygon = (np.array(((.29, .39), (.31, .39), (.31, .41), (.29, .41), (.29, .39))),)
        marker = view.ax.scatter(.3, .4, s=400, gid='received-point')
        labels = names.draw_place_labels(fig, view, (self.place(polygon=polygon),))
        self.assertEqual(labels, ())
        self.assertFalse(view.ax.texts)
        np.testing.assert_array_equal(marker.get_offsets(), ((.3, .4),))

    def test_tiny_island_name_can_sit_immediately_beside_its_received_marker(self):
        fig, view = self.view()
        polygon = (np.array(((.29, .39), (.31, .39), (.31, .41), (.29, .41), (.29, .39))),)
        marker = view.ax.scatter(.3, .4, s=400, gid='volcano-point')
        labels = names.draw_place_labels(fig, view, (self.place(
            name='小笠原諸島', kind='island', polygon=polygon),))
        self.assertEqual(len(labels), 1)
        self.assertFalse(Path(polygon[0]).contains_point(labels[0].get_position()))
        displacement = (view.ax.transData.transform(labels[0].get_position())
                        - view.ax.transData.transform((.3, .4)))
        self.assertLessEqual(np.linalg.norm(displacement), 72)
        np.testing.assert_array_equal(marker.get_offsets(), ((.3, .4),))
        self.assertFalse(view.ax.lines)


if __name__ == '__main__':
    unittest.main()
