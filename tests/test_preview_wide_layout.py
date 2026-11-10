"""Geometry and text must stay clear of the in-map locator and panel."""
import importlib.util
import struct
import unittest

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from image_service import render as base
    from image_service import preview_wide_layout as wide
    from image_service import render_preview as preview
    from scripts.gallery_catalog import load_cases


@unittest.skipUnless(HAS_RENDERER, 'Install the isolated image renderer dependencies')
class WideLayoutTests(unittest.TestCase):
    def figure(self):
        fig = wide.figure()
        self.addCleanup(fig.clear)
        return fig

    def test_locator_is_inside_the_map_and_avoids_all_fitted_geometry(self):
        fig = self.figure()
        vertices = np.array(((139., float(base._my(35.))), (141., float(base._my(37.)))))
        view = wide.map_axes(fig, (vertices,), full=(vertices,))
        self.assertIsNotNone(view.locator)
        self.assertLess(view.locator.get_position().x1, wide.PANEL_X)
        self.assertGreater(view.locator.get_position().x0, .5)
        self.assertLess(view.locator.get_position().y1, .4)
        fig.canvas.draw()
        point_positions = fig.transFigure.inverted().transform(view.ax.transData.transform(vertices))
        left, bottom, right, top = wide.LOCATOR_OBSTACLE
        for x, y in point_positions:
            self.assertFalse(left <= x <= right and bottom <= y <= top)
            self.assertLess(x, wide.PANEL_X)
            self.assertLess(y, .78)
        frame, = [p for p in view.locator.patches if p.get_gid() == 'preview-main-extent']
        self.assertAlmostEqual(frame.get_width(), view.visible_limits[1] - view.visible_limits[0])
        self.assertEqual((base.WIDTH, base.HEIGHT), (1440, 1200))

    def test_sidebar_rejects_an_unreadable_bundle_without_drawing_partial_rows(self):
        fig = self.figure()
        with self.assertRaises(ValueError):
            wide.sidebar(fig, '対象区域', [wide.Row(str(i), ('非常に長い受信情報' * 50,)) for i in range(16)])
        self.assertEqual(fig.texts, [])

    def test_cancellation_remains_explicit_and_has_no_map_at_wide_dimensions(self):
        case = next(c for c in load_cases() if c.slug == 'weather-cancel')
        image = preview.render(case.records)
        self.assertEqual(struct.unpack('>II', image.png[16:24]), (1920, 1080))
        fig = self.figure()
        wide.summary(fig, base._decode(case.records))
        self.assertEqual(fig.axes, [])
        self.assertIn('【取消】', ''.join(t.get_text() for t in fig.texts))
        self.assertIn('取り消されました', ''.join(t.get_text() for t in fig.texts))


if __name__ == '__main__':
    unittest.main()
