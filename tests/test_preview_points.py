"""Point-map previews preserve received details around the shared locator."""

import importlib.util
import unittest
from unittest.mock import patch

import test_image_renderer as helpers
from test_image_renderer_v3 import sample, changed

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.text import Text
    from matplotlib.transforms import Bbox
    import numpy as np
    from image_service import preview_context as context
    from image_service import preview_points as points
    from image_service import render as base
    from image_service import render_v4 as published


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for point preview tests')
class PreviewPointTests(unittest.TestCase):
    def figure(self):
        figure = base._figure()
        self.addCleanup(figure.clear)
        return figure

    def texts(self, figure):
        return '\n'.join(text.get_text() for text in figure.findobj(Text))

    def assert_context_clear(self, figure, count):
        figure.canvas.draw()
        main = figure.axes[0]
        locator, = [axis for axis in figure.axes if axis.get_gid() == 'preview-locator']
        self.assertLess(locator.get_position().y1, .5, 'The locator belongs at lower right')
        self.assertGreater(locator.get_position().x0, .7)
        self.assertTrue(any(artist.get_gid() == 'preview-prefecture-boundaries'
                            for artist in main.collections))
        obstacle = Bbox.from_extents(*context.LOCATOR_OBSTACLE).transformed(figure.transFigure)
        for artist in main.artists:
            if hasattr(artist, 'offsetbox'):
                self.assertFalse(artist.offsetbox.get_window_extent(figure.canvas.get_renderer()).overlaps(obstacle))
        for line in main.lines:
            if line.get_gid() in ('label-leader', 'typhoon-track'):
                path = line.get_transform().transform_path(line.get_path())
                self.assertFalse(path.intersects_bbox(obstacle, filled=False))
        for artist in main.collections:
            if (artist.get_gid() or '').startswith(('typhoon-point-', 'hypocenter-point', 'volcano-point-')):
                location = main.transData.transform(artist.get_offsets()[0])
                self.assertFalse(obstacle.contains(*location))
        helpers.RendererTests.assert_clear_leaders(self, figure, main, count)

    def test_all_six_typhoon_centres_and_received_pressure_keep_clear_track_labels(self):
        reports = base._decode(helpers.TYPHOON)
        figure = self.figure()
        points.draw_typhoon(figure, reports)
        self.assert_context_clear(figure, 6)
        marks = [artist for artist in figure.axes[0].collections
                 if (artist.get_gid() or '').startswith('typhoon-point-')]
        self.assertEqual(len(marks), len(reports))
        source = sorted(reports, key=lambda report: (report.reference_time, report.reference_time_type_raw, report.nmea))
        for mark, report in zip(marks, source, strict=True):
            lon, lat = base._coordinates(report.coordinates_of_typhoon_raw)
            np.testing.assert_allclose(mark.get_offsets()[0], (lon, float(base._my(lat))))
            self.assertIn(report.central_pressure.replace('hPa', ' hPa'), self.texts(figure))
            self.assertIn(base._time(report.reference_time), self.texts(figure))
        self.assertIn('受信した6時点', self.texts(figure))
        self.assertIn('最大風速', self.texts(figure))

    def test_hypocenter_keeps_exact_position_magnitude_depth_and_tsunami_notice(self):
        sentence, received = helpers.hypocenter()
        records = [(helpers.change_fields(sentence, [(53, 9, 211)]), received)]
        report, = base._decode(records)
        figure = self.figure()
        points.draw_hypocenter(figure, [report])
        self.assert_context_clear(figure, 1)
        marker, = [artist for artist in figure.axes[0].collections if artist.get_gid() == 'hypocenter-point']
        np.testing.assert_allclose(marker.get_offsets()[0], (139, float(base._my(35))))
        for expected in ('訓練', '震源情報', 'M 5.0', '10km', '津波警報', '発生'):
            self.assertIn(expected, self.texts(figure))
        self.assertFalse(any((artist.get_gid() or '').startswith(('tsunami-wave-', 'intensity-area-'))
                             for artist in figure.axes[0].patches))

    def test_volcano_keeps_official_location_and_all_received_municipalities(self):
        figure = self.figure()
        plan = points.draw_volcano(figure, base._decode(sample('volcano')))
        self.assert_context_clear(figure, 1)
        self.assertEqual(plan.layer, 'volcano')
        marker, = [artist for artist in figure.axes[0].collections if artist.get_gid() == 'volcano-point-503']
        self.assertEqual(marker.get_offsets()[0, 0], 131.104)
        for expected in ('阿蘇山', '入山規制', '熊本県阿蘇市', '熊本県高森町', '熊本県南阿蘇村', '23時45分'):
            self.assertIn(expected, self.texts(figure))

    def test_cancelled_unknown_and_unreadable_reports_are_rejected_for_complete_fallback(self):
        cases = [
            (points.draw_typhoon, changed('typhoon', (41, 2, 2))),
            (points.draw_typhoon, changed('typhoon', (103, 7, 127))),
            (points.draw_volcano, changed('volcano', (41, 2, 2))),
            (points.draw_volcano, changed('volcano', (76, 12, 4000))),
        ]
        sentence, received = helpers.hypocenter()
        cases.extend((points.draw_hypocenter, [(helpers.change_fields(sentence, [field]), received)])
                     for field in ((41, 2, 2), (123, 7, 127)))
        for draw, records in cases:
            figure = self.figure()
            with self.subTest(draw=draw.__name__, records=records), self.assertRaises(ValueError):
                draw(figure, base._decode(records))
            self.assertEqual(figure.axes, [], 'Reject unsupported point semantics before any map')
        with patch.object(points, '_track_annotations', side_effect=ValueError('No readable layout')):
            with self.assertRaises(ValueError):
                points.draw_typhoon(self.figure(), base._decode(helpers.TYPHOON))

    def test_typhoon_year_rollover_keeps_the_received_year(self):
        from datetime import datetime, timezone
        receipt = int(datetime(2026, 12, 31, 12, 55, tzinfo=timezone.utc).timestamp() * 1000)
        sentence = helpers.change_fields(helpers.TYPHOON[1][0], [(21, 4, 12), (25, 5, 31),
                                            (53, 5, 1), (58, 5, 12), (63, 6, 0)])
        figure = self.figure()
        points.draw_typhoon(figure, base._decode([(sentence, receipt)]))
        self.assert_context_clear(figure, 1)
        self.assertIn('2027/01/01 21:00', self.texts(figure))

    def test_published_point_renderers_keep_identical_output_after_preview_draws(self):
        for records, draw in ((helpers.TYPHOON, points.draw_typhoon),
                              ((helpers.hypocenter(),), points.draw_hypocenter),
                              (sample('volcano'), points.draw_volcano)):
            with self.subTest(draw=draw.__name__):
                original = published.render(records)
                draw(self.figure(), base._decode(records))
                self.assertEqual(published.render(records), original)


if __name__ == '__main__':
    unittest.main()
