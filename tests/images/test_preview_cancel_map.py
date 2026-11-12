"""Cancellation backgrounds keep geography and discard every forecast layer."""

from dataclasses import replace
import importlib.util
import unittest
from unittest.mock import patch


HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from matplotlib.patches import Polygon
    from image_service import preview_cancel_map as backgrounds
    from image_service import preview_wide_layout as wide
    from image_service import preview_scale as scale
    from image_service import render as base


@unittest.skipUnless(HAS_RENDERER, 'Cancellation maps use the isolated image renderer')
class CancellationMapTests(unittest.TestCase):
    def setUp(self):
        # Small neutral geography keeps these renderer-contract tests independent
        # of GIS asset sizes. The real context/boundary draw paths still run.
        land = np.array(((125., 20.), (155., 20.), (155., 70.), (125., 70.), (125., 20.)))
        boundary = np.array(((138., 38.), (141., 42.)))
        self.enterContext(patch.object(wide.terrain, '_land', return_value=(
            ((land,), (125., 155., 20., 70.)),)))
        self.enterContext(patch.object(backgrounds.context, '_prefecture_lines', return_value=(
            (boundary, (138., 141., 38., 42.)),)))
        self.enterContext(patch.object(scale, 'context_for', return_value=scale.ScaleContext((), 4., ())))

    def figure(self):
        fig = wide.figure()
        self.addCleanup(fig.clear)
        return fig

    def source(self, fig, reports):
        self.assertEqual(reports, ('original received bundle',))
        sequence = np.array(((138., float(base._my(34.))), (140., float(base._my(36.)))))
        view = wide.map_axes(fig, (sequence,), full=(sequence,))
        view.ax.add_patch(Polygon(sequence, facecolor='red', gid='warning-fill'))
        view.ax.plot(*sequence.T, color='red', gid='forecast-track')
        view.ax.scatter(139., float(base._my(35.)), marker='x', color='magenta', gid='earthquake-marker')
        view.ax.text(139., float(base._my(35.)), '予想高 3m', fontproperties=base._font(18),
                     gid='forecast-caption')
        wide.text(fig, .1, .9, '元通報の警報', gid='wide-header')
        return view

    def assert_plain(self, fig, view):
        self.assertEqual({patch.get_gid() for patch in view.ax.patches}, {'v3-land'})
        self.assertEqual({collection.get_gid() for collection in view.ax.collections},
                         {'preview-prefecture-boundaries'})
        self.assertFalse(view.ax.texts)
        self.assertTrue(all(line.get_gid() is None for line in view.ax.lines))
        self.assertFalse(fig.artists)
        if view.locator is not None:
            identities = {patch.get_gid() for patch in view.locator.patches}
            self.assertIn(identities, (
                {'v3-land', 'preview-locator-frame', 'preview-main-extent'},
                {'v3-land', None, 'preview-main-extent'},
            ))
            self.assertEqual(len(view.locator.patches), 3)
            self.assertFalse(view.locator.texts)
            self.assertFalse(view.locator.lines)
            self.assertFalse(view.locator.collections)
        self.assertNotIn('元通報', ''.join(label.get_text() for label in fig.texts))
        self.assertNotIn('予想', ''.join(label.get_text() for label in fig.texts))

    def assert_right_locator(self, fig, view, former_position):
        left, bottom, width, height = view.locator.get_position(original=True).bounds
        np.testing.assert_allclose((bottom, width, height), former_position[1:], rtol=0, atol=1e-12)
        self.assertAlmostEqual(left + width, .96)
        self.assertGreater(left, .75, 'The plain locator belongs at the full canvas bottom-right')
        expected_obstacle = (left - .01, bottom - .01, left + width + .01, bottom + height + .04)
        self.assertTrue(any(np.allclose(value, expected_obstacle, rtol=0, atol=1e-12)
                            for value in view.obstacles))
        labels = {label.get_text(): label for label in fig.texts if label.get_gid() == 'wide-locator'}
        self.assertEqual(set(labels), {'表示位置', '枠は主図範囲'})
        np.testing.assert_allclose(labels['表示位置'].get_position(), (left, bottom + height + .015),
                                   rtol=0, atol=1e-12)
        np.testing.assert_allclose(labels['枠は主図範囲'].get_position(), (.96, bottom + height + .015),
                                   rtol=0, atol=1e-12)

    def test_capture_and_rebuild_preserve_exact_frame_without_original_forecast(self):
        originals = []

        def draw(fig, reports):
            view = self.source(fig, reports)
            originals.append((fig, backgrounds.frame_from_view(view)))

        frame = backgrounds.capture_frame(draw, ('original received bundle',))
        original, expected = originals[0]
        self.assertEqual(frame, expected)
        self.assertFalse(original.axes)
        self.assertFalse(original.texts)
        fig = self.figure()
        obstacle = (.27, .34, .74, .72)
        view = backgrounds.draw_frame(fig, frame, obstacles=(obstacle,))
        fig.canvas.draw()
        self.assertEqual(tuple(view.ax.get_xlim()) + tuple(view.ax.get_ylim()), frame.limits)
        self.assertEqual(tuple(view.ax.get_position(original=True).bounds), frame.position)
        self.assertEqual(view.visible_limits, frame.limits,
                         'The cancellation card has no panel hiding the right side of the map')
        self.assertEqual(tuple(view.locator.get_xlim()) + tuple(view.locator.get_ylim()), frame.locator_limits)
        self.assert_right_locator(fig, view, frame.locator_position)
        self.assertIn(obstacle, view.obstacles)
        extent, = [p for p in view.locator.patches if p.get_gid() == 'preview-main-extent']
        self.assertEqual(extent.get_xy(), (frame.limits[0], frame.limits[2]))
        self.assertEqual(extent.get_width(), frame.limits[1] - frame.limits[0])
        self.assertEqual(extent.get_height(), frame.limits[3] - frame.limits[2])
        self.assertTrue(all(label.get_zorder() < 20 for label in fig.texts))
        self.assert_plain(fig, view)

    def test_full_background_extent_expands_only_locator_coverage_when_needed(self):
        frame = backgrounds.MapFrame(
            (120., 160., 20., 42.5), (120., 150.2, 20., 42.5),
            locator_limits=(118., 152., 15., 48.), locator_position=wide.LOCATOR_POSITION)
        fig = self.figure()
        view = backgrounds.draw_frame(fig, frame)
        fig.canvas.draw()
        self.assertEqual(tuple(view.ax.get_xlim()) + tuple(view.ax.get_ylim()), frame.limits)
        self.assertEqual(tuple(view.ax.get_position(original=True).bounds), frame.position)
        self.assert_right_locator(fig, view, frame.locator_position)
        self.assertEqual(view.visible_limits, frame.limits)
        extent, = [p for p in view.locator.patches if p.get_gid() == 'preview-main-extent']
        self.assertEqual(extent.get_xy(), (120., 20.))
        self.assertEqual(extent.get_width(), 40.)
        self.assertLess(view.locator.get_xlim()[0], 118.)
        self.assertGreater(view.locator.get_xlim()[1], 160.)
        self.assertLess(view.locator.get_ylim()[0], 15.)
        self.assertGreater(view.locator.get_ylim()[1], 48.)
        self.assert_plain(fig, view)

    def test_reference_without_a_locator_stays_without_a_locator(self):
        frame = backgrounds.MapFrame((100., 170., 5., 45.), (100., 152.85, 5., 45.))
        fig = self.figure()
        view = backgrounds.draw_frame(fig, frame)
        self.assertIsNone(view.locator)
        self.assertEqual(len(fig.axes), 1)
        self.assertEqual(tuple(view.ax.get_xlim()) + tuple(view.ax.get_ylim()), frame.limits)
        self.assert_plain(fig, view)

    def test_missing_ambiguous_and_failed_reference_maps_are_rejected_and_cleared(self):
        figures = []

        def absent(fig, reports):
            figures.append(fig)
            wide.text(fig, .5, .5, '取消')

        def ambiguous(fig, reports):
            figures.append(fig)
            self.source(fig, reports)
            self.source(fig, reports)

        def failed(fig, reports):
            figures.append(fig)
            self.source(fig, reports)
            raise ValueError('Original forecast unavailable')

        for drawer in (absent, ambiguous, failed):
            with self.subTest(drawer=drawer.__name__), self.assertRaises(ValueError):
                backgrounds.capture_frame(drawer, ('original received bundle',))
            self.assertFalse(figures[-1].axes)
            self.assertFalse(figures[-1].texts)

    def test_invalid_frames_fail_before_drawing_any_map(self):
        frame = backgrounds.MapFrame((130., 145., 30., 45.), (130., 141., 30., 45.))
        variants = (None, replace(frame, limits=(float('nan'), 145., 30., 45.)),
                    replace(frame, limits=(145., 130., 30., 45.)),
                    replace(frame, limits=(True, 145., 30., 45.)),
                    replace(frame, visible_limits=(120., 141., 30., 45.)),
                    replace(frame, position=(0., 0., 2., 1.)),
                    replace(frame, locator_limits=(120., 150., 20., 50.)),
                    replace(frame, locator_limits=(150., 120., 20., 50.),
                            locator_position=wide.LOCATOR_POSITION))
        for value in variants:
            fig = self.figure()
            with self.subTest(frame=value), self.assertRaises(ValueError):
                backgrounds.draw_frame(fig, value)
            self.assertFalse(fig.axes)

    def test_scope_fits_every_supplied_component_without_painting_the_scope(self):
        source = np.array(((130., 35.), (131., 36.)))
        remote = np.array(((149., 52.), (150., 53.)))
        fig = self.figure()
        obstacle = (.25, .34, .75, .72)
        view = backgrounds.draw_scope(fig, (source,), full=(remote,), obstacles=(obstacle,))
        fig.canvas.draw()
        for points in (source, remote):
            displayed = fig.transFigure.inverted().transform(view.ax.transData.transform(points))
            self.assertTrue(np.all((displayed[:, 0] >= 0) & (displayed[:, 0] < wide.PANEL_X)))
            self.assertTrue(np.all((displayed[:, 1] >= 0) & (displayed[:, 1] <= 1)))
        self.assertIn(obstacle, view.obstacles)
        self.assertIs(view.ax._wide_map_view, view)
        self.assertEqual(view.visible_limits, tuple(view.ax.get_xlim()) + tuple(view.ax.get_ylim()))
        if view.locator is not None:
            self.assert_right_locator(fig, view, wide.LOCATOR_POSITION)
            extent, = [p for p in view.locator.patches if p.get_gid() == 'preview-main-extent']
            self.assertEqual(extent.get_xy(), (view.visible_limits[0], view.visible_limits[2]))
            self.assertEqual(extent.get_width(), view.visible_limits[1] - view.visible_limits[0])
            self.assertEqual(extent.get_height(), view.visible_limits[3] - view.visible_limits[2])
        self.assert_plain(fig, view)
        self.assertTrue(all(label.get_zorder() < 20 for label in fig.texts))

    def test_empty_scope_and_invalid_card_obstacles_fail_before_drawing(self):
        fig = self.figure()
        with self.assertRaises(ValueError):
            backgrounds.draw_scope(fig, ())
        with self.assertRaises(ValueError):
            backgrounds.draw_scope(fig, (np.array(((130., 35.), (131., 36.))),),
                                   obstacles=((.8, .2, .3, .7),))
        self.assertFalse(fig.axes)


if __name__ == '__main__':
    unittest.main()
