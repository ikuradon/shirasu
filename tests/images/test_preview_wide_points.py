"""Received point values remain faithful when the wide layout moves the inset."""

from datetime import datetime, timezone
import importlib.util
import unittest

from tests.images import test_image_renderer as helpers
from tests.images.test_image_renderer_v3 import sample, changed

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.text import Text
    import numpy as np
    from image_service import render as base
    from image_service import preview_wide_layout as wide
    from image_service import preview_wide_points as points
    from tools.preview.gallery_catalog import load_cases


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for wide point-map tests')
class WidePointTests(unittest.TestCase):
    def figure(self):
        figure = wide.figure()
        self.addCleanup(figure.clear)
        return figure

    def texts(self, figure):
        return '\n'.join(text.get_text() for text in figure.findobj(Text))

    def source(self):
        return next(case.records for case in load_cases() if case.slug == 'typhoon-track')

    def assert_map_locator(self, figure, view):
        figure.canvas.draw()
        self.assertEqual(tuple(round(value) for value in figure.get_size_inches() * figure.dpi), (1920, 1080))
        self.assertIsNotNone(view.locator)
        box = view.locator.get_position()
        self.assertGreater(box.x0, .5)
        self.assertLess(box.x1, wide.PANEL_X)
        self.assertLess(box.y1, .5)
        self.assertTrue(any(artist.get_gid() == 'preview-prefecture-boundaries' for artist in view.ax.collections))
        inset = box.transformed(figure.transFigure)
        for artist in view.ax.artists:
            if hasattr(artist, 'offsetbox'):
                self.assertFalse(artist.offsetbox.get_window_extent(figure.canvas.get_renderer()).overlaps(inset))
        for line in view.ax.lines:
            if line.get_gid() in ('label-leader', 'typhoon-track'):
                self.assertFalse(line.get_transform().transform_path(line.get_path()).intersects_bbox(inset, filled=False))
        for marker in view.ax.collections:
            if (marker.get_gid() or '').startswith(('typhoon-point-', 'hypocenter-point', 'volcano-point-')):
                self.assertFalse(inset.contains(*view.ax.transData.transform(marker.get_offsets()[0])))

    def test_recorded_track_keeps_seven_centres_and_dates_but_only_current_pressure_in_panel(self):
        reports = base._decode(self.source())
        figure = self.figure()
        view = points.draw_typhoon(figure, reports)
        self.assert_map_locator(figure, view)
        helpers.RendererTests.assert_clear_leaders(self, figure, view.ax, 7)
        markers = [artist for artist in view.ax.collections if (artist.get_gid() or '').startswith('typhoon-point-')]
        source = sorted(reports, key=lambda report: (report.reference_time, report.reference_time_type_raw, report.nmea))
        self.assertEqual(len(markers), len(source))
        for marker, report in zip(markers, source, strict=True):
            lon, lat = base._coordinates(report.coordinates_of_typhoon_raw)
            np.testing.assert_allclose(marker.get_offsets()[0], (lon, float(base._my(lat))))
            self.assertIn(base._time(report.reference_time), self.texts(figure))
        for expected in ('台風第26号', '1002', '最大風速', '最大瞬間風速', '20', '30', '09:00', '09:50'):
            self.assertIn(expected, self.texts(figure))
        self.assertNotIn('965', self.texts(figure), 'Forecast pressure must not be presented as current pressure')

    def test_newer_estimate_is_current_without_promoting_a_future_forecast(self):
        observed, forecast, *_ = self.source()
        estimate = (helpers.change_fields(observed[0], [(69, 3, 2), (63, 6, 30), (143, 11, 1000)]), observed[1])
        figure = self.figure()
        points.draw_typhoon(figure, base._decode((observed, estimate, forecast)))
        text = self.texts(figure)
        self.assertIn('09:30', text)
        self.assertIn('推定', text)
        self.assertIn('1000', text)
        self.assertNotIn('1002', text)
        self.assertNotIn('998', text)

    def test_forecast_only_bundle_is_explicit_and_retains_unknown_wind_values(self):
        sentence, received = self.source()[1]
        record = (helpers.change_fields(sentence, [(154, 7, 0), (161, 7, 0)]), received)
        figure = self.figure()
        points.draw_typhoon(figure, base._decode((record,)))
        text = self.texts(figure)
        self.assertIn('受信した予報', text)
        self.assertIn('実況・推定の受信なし', text)
        self.assertIn('最大風速 不明', text)
        self.assertIn('最大瞬間風速 不明', text)
        self.assertNotIn('0m/s', text)

    def test_year_rollover_retains_year_in_forecast_label(self):
        receipt = int(datetime(2026, 12, 31, 12, 55, tzinfo=timezone.utc).timestamp() * 1000)
        sentence = helpers.change_fields(helpers.TYPHOON[1][0], [(21, 4, 12), (25, 5, 31),
                                            (53, 5, 1), (58, 5, 12), (63, 6, 0)])
        figure = self.figure()
        view = points.draw_typhoon(figure, base._decode(((sentence, receipt),)))
        self.assert_map_locator(figure, view)
        self.assertIn('2027/01/01 21:00', self.texts(figure))

    def test_hypocenter_keeps_exact_position_and_received_earthquake_values(self):
        sentence, received = helpers.hypocenter()
        records = ((helpers.change_fields(sentence, [(53, 9, 211)]), received),)
        figure = self.figure()
        view = points.draw_hypocenter(figure, base._decode(records))
        self.assert_map_locator(figure, view)
        marker, = [artist for artist in view.ax.collections if artist.get_gid() == 'hypocenter-point']
        np.testing.assert_allclose(marker.get_offsets()[0], (139, float(base._my(35))))
        for expected in ('訓練', '震源情報', 'マグニチュード', '5.0', '深さ', '10', '津波警報', '発生時刻'):
            self.assertIn(expected, self.texts(figure))
        self.assertFalse(any((artist.get_gid() or '').startswith(('tsunami-wave-', 'intensity-area-'))
                             for artist in view.ax.patches))

    def test_volcano_keeps_activity_precision_and_all_received_municipalities(self):
        figure = self.figure()
        view = points.draw_volcano(figure, base._decode(sample('volcano')))
        self.assert_map_locator(figure, view)
        marker, = [artist for artist in view.ax.collections if artist.get_gid() == 'volcano-point-503']
        self.assertEqual(marker.get_offsets()[0, 0], 131.104)
        for expected in ('阿蘇山', '入山規制', '熊本県阿蘇市', '熊本県高森町', '熊本県南阿蘇村', '23時45分'):
            self.assertIn(expected, self.texts(figure))

    def test_invalid_point_semantics_are_rejected_before_map_creation(self):
        cases = [
            (points.draw_typhoon, changed('typhoon', (41, 2, 2))),
            (points.draw_typhoon, changed('typhoon', (103, 7, 127))),
            (points.draw_volcano, changed('volcano', (41, 2, 2))),
            (points.draw_volcano, changed('volcano', (76, 12, 4000))),
        ]
        sentence, received = helpers.hypocenter()
        cases.append((points.draw_hypocenter, ((helpers.change_fields(sentence, [(123, 7, 127)]), received),)))
        for draw, records in cases:
            with self.subTest(draw=draw.__name__):
                figure = self.figure()
                with self.assertRaises(ValueError):
                    draw(figure, base._decode(records))
                self.assertEqual(figure.axes, [])

    def test_point_previews_do_not_mutate_published_dimensions_or_palette(self):
        from image_service import render_v4 as published
        records = (helpers.hypocenter(),)
        original = published.render(records)
        before = (base.WIDTH, base.HEIGHT, base.DPI, base.SEA, base.LAND, base.COAST, base._figure, base._text)
        points.draw_hypocenter(self.figure(), base._decode(records))
        after = (base.WIDTH, base.HEIGHT, base.DPI, base.SEA, base.LAND, base.COAST, base._figure, base._text)
        self.assertEqual(after, before)
        self.assertEqual(published.render(records), original)


if __name__ == '__main__':
    unittest.main()
