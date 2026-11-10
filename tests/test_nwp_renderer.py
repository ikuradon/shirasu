"""Real Northwest Pacific point forecasts and isolation from published images."""

import importlib.util
import struct
import unittest
from unittest.mock import patch

from image_protocol import make_path
import test_image_renderer as helpers
from test_image_renderer_v3 import sample, changed

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
NAME = 'northwest_pacific_tsunami'
if HAS_RENDERER:
    from matplotlib.text import Text
    from matplotlib.transforms import Bbox
    from image_service import render as base
    from image_service import render_v4 as published
    from image_service import render_preview as preview
    from image_service import nwp_map
    from image_service import preview_wide_layout as wide
    from image_service import preview_wide_tsunami as tsunami


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for preview PNG tests')
class NorthwestPacificRendererTests(unittest.TestCase):
    def capture(self, records):
        figures = []
        original = wide.figure

        def make_figure():
            figure = original()
            figures.append(figure)
            clear, save = figure.clear, figure.savefig
            saved = False

            def clear_before_save(*args, **kwargs):
                if not saved:
                    clear(*args, **kwargs)

            def save_and_keep(*args, **kwargs):
                nonlocal saved
                result = save(*args, **kwargs)
                saved = True
                return result

            figure.clear = clear_before_save
            figure.savefig = save_and_keep
            self.addCleanup(lambda: base.Figure.clear(figure))
            return figure

        with patch.object(wide, 'figure', side_effect=make_figure):
            image = preview.render(records)
        return image, figures[-1]

    def texts(self, figure):
        return ''.join(text.get_text() for text in figure.findobj(Text))

    def points(self, figure):
        self.assertTrue(figure.axes, 'A complete supported bulletin should remain a point map')
        return {int(artist.get_gid().removeprefix('nwp-point-')): artist
                for artist in figure.axes[0].collections
                if (artist.get_gid() or '').startswith('nwp-point-')}

    def test_training_map_preserves_all_five_named_forecasts_and_utc_dates(self):
        records = sample(NAME)
        self.assertEqual(base._decode(records)[0].report_classification_no, 7)
        image, figure = self.capture(records)
        self.assertEqual(image.category, 'NorthwestPacificTsunami')
        self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', image.png[16:24]), (1920, 1080))
        self.assertEqual(set(self.points(figure)), {1, 20, 29, 41, 84})
        text = self.texts(figure)
        for expected in ('訓練', '北西太平洋津波情報', 'Ust-Kamchatsk', 'Shanghai',
                         'Guam', 'Jayapura', 'Honiara', '2026/11/09 23:50',
                         '予想高 0.3〜1m', '予想高 1〜3m', '予想高 3〜5m',
                         '予想高 5〜10m', '予想高 10m超',
                         '到達予想 11/10 00:20 UTC', '到達予想 11/10 01:10 UTC',
                         '到達予想 11/10 02:30 UTC', '到達予想 11/10 03:45 UTC',
                         '到達予想 11/10 05:00 UTC', '予測地点ごとの津波高'):
            self.assertIn(expected, text)
        self.assertNotIn('JST', text, 'The international bulletin must not mix time zones')
        self.assertEqual(len([label for label in figure.texts
                              if (label.get_gid() or '').startswith('wide-tsunami-value-nwp-point-')]), 5)
        self.assertFalse(any(ax.get_gid() == 'preview-locator' for ax in figure.axes),
                         'The broad Pacific overview does not need a second miniature overview')

    def test_focused_forecast_has_a_lower_right_locator_without_hiding_its_point(self):
        # One Shanghai forecast retains its transmitted height and time. Clear
        # only the four unused forecast slots, rather than supplying fake points.
        records = changed(NAME, (77, 7, 84), (84, 112, 0))
        _, figure = self.capture(records)
        points = self.points(figure)
        self.assertEqual(set(points), {84})
        main = figure.axes[0]
        locator, = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        self.assertLess(locator.get_position().y1, .5)
        self.assertGreater(locator.get_position().x0, .5)
        self.assertLess(locator.get_position().x1, wide.PANEL_X)
        self.assertTrue(any(artist.get_gid() == 'preview-prefecture-boundaries'
                            for artist in main.collections))
        frame, = [patch for patch in locator.patches if patch.get_gid() == 'preview-main-extent']
        for actual, expected in zip(frame.get_bbox().extents,
                (main.get_xlim()[0], main.get_ylim()[0],
                 main.get_xlim()[0] + wide.PANEL_X * (main.get_xlim()[1] - main.get_xlim()[0]),
                 main.get_ylim()[1]), strict=True):
            self.assertAlmostEqual(actual, expected)
        obstacle = Bbox.from_extents(*wide.LOCATOR_OBSTACLE).transformed(figure.transFigure)
        self.assertFalse(obstacle.contains(*main.transData.transform(points[84].get_offsets()[0])))
        for artist in main.artists:
            if hasattr(artist, 'offsetbox'):
                self.assertFalse(artist.offsetbox.get_window_extent(figure.canvas.get_renderer()).overlaps(obstacle))
        for line in main.lines:
            if (line.get_gid() or '').startswith('wide-tsunami-leader-'):
                self.assertFalse(line.get_transform().transform_path(line.get_path()).intersects_bbox(obstacle, filled=False))
        self.assertEqual(len([label for label in figure.texts
                              if (label.get_gid() or '').startswith('wide-tsunami-value-nwp-point-')]), 1)

    def test_markers_use_official_positions_across_the_equator_without_a_warning_footprint(self):
        _, figure = self.capture(sample(NAME))
        points = self.points(figure)
        # Independent published coordinates, including two southern-hemisphere points.
        expected = {1: (162.6, 56.1), 84: (122.3, 31.2), 29: (144.7, 13.4),
                    20: (140.8, -2.4), 41: (160.0, -9.3)}
        for code, (lon, lat) in expected.items():
            with self.subTest(code=code):
                actual = points[code].get_offsets()[0]
                self.assertAlmostEqual(float(actual[0]) % 360, lon)
                self.assertAlmostEqual(float(actual[1]), float(base._my(lat)))
                pixel = figure.axes[0].transData.transform(actual)
                self.assertTrue(figure.axes[0].bbox.contains(*pixel))
        self.assertFalse(any((artist.get_gid() or '').startswith(
            ('region-area-', 'region-coast-', 'epicentre', 'hypocenter'))
            for axes in figure.axes for artist in (*axes.patches, *axes.lines, *axes.collections)))

    def test_qualitative_height_and_ambiguous_arrival_are_not_invented_measurements(self):
        records = changed(NAME, (57, 5, 31), (62, 6, 63), (68, 9, 509),
                          (96, 9, 510), (124, 9, 511))
        _, figure = self.capture(records)
        self.assertEqual(set(self.points(figure)), {1, 20, 29, 41, 84})
        text = self.texts(figure)
        for expected in ('予想高 巨大（Huge）', '予想高 高い（High）', '予想高 不明',
                         '到達済み、または到達時刻不明'):
            self.assertIn(expected, text)
        self.assertNotIn('到達予想 11/10 00:20 UTC', text)
        self.assertNotIn('31:63', text)

    def test_unknown_point_height_time_or_potential_falls_back_as_a_complete_bulletin(self):
        fields = (((77, 7, 73),), ((77, 7, 100),), ((68, 9, 507),),
                  ((57, 5, 30),), ((53, 3, 0),), ((53, 3, 7),))
        for changes in fields:
            with self.subTest(changes=changes):
                records = changed(NAME, *changes)
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertEqual(struct.unpack('>II', image.png[16:24]), (1920, 1080))

    def test_cancellation_and_conflicting_forecasts_never_leave_an_active_point_map(self):
        cancellation = changed(NAME, (41, 2, 2))
        conflict = sample(NAME) + changed(NAME, (68, 9, 2))
        for records in (cancellation, conflict):
            with self.subTest(records=records):
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertEqual(struct.unpack('>II', image.png[16:24]), (1920, 1080))
        _, figure = self.capture(cancellation)
        self.assertIn('取消', self.texts(figure))

    def test_missing_one_position_or_unreadable_layout_rejects_the_entire_map(self):
        records = sample(NAME)
        incomplete = dict(nwp_map._positions())
        del incomplete[29]
        for target, name, kwargs in (
                (nwp_map, '_positions', {'return_value': incomplete}),
                (tsunami, '_value_markers', {'side_effect': ValueError('No readable layout')})):
            with self.subTest(missing=name), patch.object(target, name, **kwargs):
                image, figure = self.capture(records)
            self.assertEqual(figure.axes, [])
            self.assertEqual(struct.unpack('>II', image.png[16:24]), (1920, 1080))

    def test_preview_is_deterministic_and_cannot_change_the_published_nwp_summary(self):
        records = sample(NAME)
        original = published.render(records)
        first = preview.render(records)
        self.assertEqual(first, preview.render(records))
        self.assertNotEqual(first.png, original.png)
        self.assertEqual(published.render(records), original)
        # Every published generation still uses its old summary for this category.
        with patch.object(preview, 'render', side_effect=AssertionError('Preview is offline only')):
            for version in ('v1', 'v2', 'v3', 'v4'):
                with self.subTest(version=version):
                    response = helpers.request(make_path(records, 'test', helpers.KEY, version=version))
                    self.assertEqual(response['status'], '200 OK')
                    self.assertEqual(response['body'], original.png)

    def test_other_previews_do_not_change_their_published_images(self):
        for records in (sample('weather'), sample('eew'), helpers.TYPHOON):
            with self.subTest(category=type(base._decode(records)[0]).__name__):
                original = published.render(records)
                preview.render(records)
                self.assertEqual(published.render(records), original)


if __name__ == '__main__':
    unittest.main()
