"""Fixed map extents skip path scans without changing drawing or normal Axes."""

import importlib.util
import io
import unittest
from unittest.mock import patch

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from matplotlib.axes import Axes
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import PathPatch, Rectangle
    from matplotlib.path import Path
    from matplotlib.transforms import Affine2D
    from image_service import preview_wide_layout as wide
    from image_service.preview_scale import ScaleContext


@unittest.skipUnless(HAS_RENDERER, 'Install the isolated image renderer dependencies')
class FixedMapAxesTests(unittest.TestCase):
    def axes(self, axes_class):
        fig = Figure(figsize=(4, 3), dpi=80, facecolor=wide.SEA)
        FigureCanvasAgg(fig)
        self.addCleanup(fig.clear)
        ax = fig.add_axes((.1, .1, .8, .8), axes_class=axes_class, facecolor=wide.LAND)
        ax.set_xlim(120, 150)
        ax.set_ylim(20, 50)
        ax.set_aspect('equal')
        ax.set_axis_off()
        return fig, ax

    def complex_patch(self):
        # Includes an off-screen polygon, a hole and Bezier curves. Clipping and
        # path transforms must remain the same when data-limit scans disappear.
        outer = Path(((118, 22), (145, 22), (145, 55), (118, 55), (118, 22)),
                     (Path.MOVETO, Path.LINETO, Path.LINETO, Path.LINETO, Path.CLOSEPOLY))
        hole = Path(((125, 28), (125, 34), (132, 34), (132, 28), (125, 28)),
                    (Path.MOVETO, Path.LINETO, Path.LINETO, Path.LINETO, Path.CLOSEPOLY))
        curve = Path.unit_circle().transformed(Affine2D().scale(4).translate(139, 42))
        return PathPatch(Path.make_compound_path(outer, hole, curve),
                         facecolor='#e2a444', edgecolor='#eef7fa', linewidth=1.7, alpha=.8)

    def png(self, figure):
        stream = io.BytesIO()
        figure.savefig(stream, format='png', metadata={'Software': 'fixed-map-regression'})
        return stream.getvalue()

    def test_fixed_map_does_not_walk_patch_bezier_segments(self):
        _, fixed = self.axes(wide._FixedMapAxes)
        _, ordinary = self.axes(Axes)
        fixed_patch, ordinary_patch = self.complex_patch(), self.complex_patch()
        with patch.object(Path, 'iter_bezier', side_effect=AssertionError('expensive path scan')):
            self.assertIs(fixed.add_patch(fixed_patch), fixed_patch)
            # Positive control: an ordinary Axes still performs the old scan,
            # so this also detects an accidental global Matplotlib override.
            with self.assertRaisesRegex(AssertionError, 'expensive path scan'):
                ordinary.add_patch(ordinary_patch)

    def test_png_clipping_transforms_and_patch_lifecycle_match_ordinary_axes(self):
        fixed_figure, fixed = self.axes(wide._FixedMapAxes)
        ordinary_figure, ordinary = self.axes(Axes)
        for ax in (fixed, ordinary):
            ax.add_patch(self.complex_patch())
            ax.add_patch(Rectangle((.02, .02), .2, .15, transform=ax.transAxes,
                                   facecolor='#527bbd', edgecolor='none'))
        self.assertEqual(self.png(fixed_figure), self.png(ordinary_figure))
        for actual, expected in zip(fixed.patches, ordinary.patches):
            self.assertIs(actual.axes, fixed)
            self.assertIs(actual.get_figure(), fixed_figure)
            np.testing.assert_array_equal(actual.get_transform().get_matrix(),
                                          expected.get_transform().get_matrix())
            np.testing.assert_array_equal(actual.get_clip_box().extents,
                                          expected.get_clip_box().extents)
        land = fixed.patches[0]
        self.assertIn(land, fixed.get_children())
        land.remove()
        self.assertNotIn(land, fixed.patches)
        self.assertNotIn(land, fixed.get_children())
        self.assertIsNone(land.axes)
        with self.assertRaises(TypeError):
            fixed.add_patch('not a Patch')

    def test_enabling_either_autoscale_axis_uses_normal_patch_bounds(self):
        for axis in ('x', 'y'):
            with self.subTest(axis=axis):
                _, fixed = self.axes(wide._FixedMapAxes)
                _, ordinary = self.axes(Axes)
                for ax in (fixed, ordinary):
                    getattr(ax, f'set_autoscale{axis}_on')(True)
                    ax.add_patch(Rectangle((155, 55), 10, 20))
                    ax.autoscale_view()
                np.testing.assert_array_equal(fixed.dataLim.extents, ordinary.dataLim.extents)
                self.assertEqual(fixed.get_xlim(), ordinary.get_xlim())
                self.assertEqual(fixed.get_ylim(), ordinary.get_ylim())

    def test_relim_after_enabling_autoscale_recovers_earlier_patches(self):
        _, fixed = self.axes(wide._FixedMapAxes)
        _, ordinary = self.axes(Axes)
        for ax in (fixed, ordinary):
            ax.add_patch(self.complex_patch())
            ax.add_patch(Rectangle((152, 52), 10, 20))
            ax.set_autoscale_on(True)
            ax.relim()
            ax.autoscale_view()
        # Recovery also proves inherited add_patch retained the in-autoscale
        # marker, which Matplotlib 3.11's relim requires.
        np.testing.assert_array_equal(fixed.dataLim.extents, ordinary.dataLim.extents)
        self.assertEqual(fixed.get_xlim(), ordinary.get_xlim())
        self.assertEqual(fixed.get_ylim(), ordinary.get_ylim())

    def test_adjustable_datalim_keeps_normal_bounds_even_with_autoscale_off(self):
        fixed_figure, fixed = self.axes(wide._FixedMapAxes)
        ordinary_figure, ordinary = self.axes(Axes)
        for ax in (fixed, ordinary):
            ax.set_adjustable('datalim')
            ax.add_patch(self.complex_patch())
        np.testing.assert_array_equal(fixed.dataLim.extents, ordinary.dataLim.extents)
        self.assertEqual(self.png(fixed_figure), self.png(ordinary_figure))
        self.assertEqual(fixed.get_xlim(), ordinary.get_xlim())
        self.assertEqual(fixed.get_ylim(), ordinary.get_ylim())

    def test_main_map_and_locator_retain_geographic_extents_and_projection(self):
        figures = [wide.figure(), wide.figure()]
        for fig in figures:
            self.addCleanup(fig.clear)
        ring = np.array(((125., 25.), (146., 25.), (146., 55.), (125., 55.), (125., 25.)))
        sequences = (np.array(((137., 36.), (141., 42.))),)
        views = []
        with (patch.object(wide.terrain, '_land', return_value=(([ring], (125, 146, 25, 55)),)),
              patch.object(wide.context, 'draw_prefecture_boundaries'),
              patch.object(wide.scale, 'context_for', return_value=ScaleContext((), 4., ())),
              patch.object(wide, 'text')):
            views.append(wide.map_axes(figures[0], sequences, force_locator=True))
            with patch.object(wide, '_FixedMapAxes', Axes):
                views.append(wide.map_axes(figures[1], sequences, force_locator=True))
        fixed, ordinary = views
        self.assertIsInstance(fixed.ax, wide._FixedMapAxes)
        self.assertIsInstance(fixed.locator, wide._FixedMapAxes)
        self.assertEqual(type(ordinary.ax), Axes)
        self.assertEqual(fixed.limits, ordinary.limits)
        self.assertEqual(fixed.visible_limits, ordinary.visible_limits)
        for actual, expected in ((fixed.ax, ordinary.ax), (fixed.locator, ordinary.locator)):
            original = actual.get_xlim(), actual.get_ylim()
            actual.add_patch(Rectangle((-200, -100), 500, 200, fill=False))
            actual.apply_aspect()
            expected.apply_aspect()
            self.assertEqual((actual.get_xlim(), actual.get_ylim()), original)
            self.assertEqual(actual.get_xlim(), expected.get_xlim())
            self.assertEqual(actual.get_ylim(), expected.get_ylim())
            np.testing.assert_array_equal(actual.get_position().bounds, expected.get_position().bounds)
            np.testing.assert_array_equal(actual.transData.transform(sequences[0]),
                                          expected.transData.transform(sequences[0]))
        frame, = [p for p in fixed.locator.patches if p.get_gid() == 'preview-main-extent']
        self.assertAlmostEqual(frame.get_width(), fixed.visible_limits[1] - fixed.visible_limits[0])
        self.assertAlmostEqual(frame.get_height(), fixed.visible_limits[3] - fixed.visible_limits[2])


if __name__ == '__main__':
    unittest.main()
