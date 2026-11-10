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
    from matplotlib.text import Text
    from matplotlib.transforms import Bbox
    from image_service import render as base
    from image_service import render_v4 as published
    from image_service import render_preview as preview
    from image_service import ash_map, volcano_map
    from image_service import preview_wide_layout as wide
    from image_service import preview_wide_special as special


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

    def points(self, figure, prefix):
        self.assertTrue(figure.axes, 'All supported forecasts must retain their map')
        return {int(artist.get_gid().removeprefix(prefix)): artist
                for artist in figure.axes[0].collections
                if (artist.get_gid() or '').startswith(prefix)}

    def cells(self, figure):
        """Pair real time-cell rectangles with their separate text artists."""
        labels = {}
        for label in figure.texts:
            identity = label.get_gid() or ''
            if identity.startswith('ash-cell-'):
                cell_identity, _ = identity.rsplit('-', 1)
                self.assertNotIn(cell_identity, labels)
                labels[cell_identity] = label
        cells = {}
        for patch in figure.artists:
            identity = patch.get_gid() or ''
            if identity.startswith('ash-cell-'):
                _, _, municipality, hour, lane = identity.split('-')
                key = int(municipality), int(hour), lane
                self.assertNotIn(key, cells)
                cells[key] = patch, labels.pop(identity)
        self.assertFalse(labels, 'Every forecast value must have a visible time cell')
        self.assertTrue(cells, 'Supported forecasts must retain the time matrix')
        return cells

    def assert_complete_map(self, figure, municipalities):
        self.assertEqual(set(self.points(figure, 'municipality-anchor-')), set(municipalities))
        self.assertEqual(set(self.points(figure, 'volcano-point-')), {506})
        labels = [text for text in figure.texts
                  if (text.get_gid() or '').startswith('wide-number-municipality-anchor-')]
        self.assertEqual({text.get_text() for text in labels},
                         {str(index) for index in range(1, len(municipalities) + 1)})

    def assert_plain_locator(self, locator):
        identities = {artist.get_gid() for artist in locator.patches}
        self.assertEqual(identities - {None}, {'v3-land', 'preview-main-extent'})
        self.assertFalse(locator.collections, 'Locator must not show administrative or hazard boundaries')
        self.assertFalse(locator.lines, 'Locator must not show tracks or graticules')
        self.assertFalse(locator.texts)
        self.assertFalse(locator.artists)
        frame, = [artist for artist in locator.patches if artist.get_gid() == 'preview-main-extent']
        self.assertEqual(frame.get_edgecolor(), to_rgba(wide.WHITE))
        self.assertEqual(frame.get_facecolor()[3], 0)

    def test_training_map_preserves_every_municipality_time_and_forecast_phenomenon(self):
        records = sample(NAME)
        reports = base._decode(records)
        self.assertEqual(reports[0].report_classification_no, 7)
        image, figure = self.capture(records)
        self.assertEqual(image.category, 'AshFall')
        self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', image.png[16:24]), (1920, 1080))
        self.assert_complete_map(figure, {4620100, 4621400, 4621800})
        texts = self.texts(figure)
        for expected in ('訓練', '降灰予報', '詳細', '桜島',
                         '鹿児島市', '垂水市', '霧島市',
                         '2026/11/10 09:10', '2026/11/10 09:00', 'JST',
                         '基点からの時間', '単位：時間', '—：この時間の記載なし',
                         '多量の降灰', '小さな噴石の落下', 'やや多量の降灰', '少量の降灰'):
            self.assertIn(expected, texts)
        for hour in range(1, 7):
            self.assertIn(f'＋{hour}', texts)
        received = {(municipality, hour, 'cinders' if phenomenon == 4 else 'ash'): phenomenon
                    for report in reports
                    for municipality, hour, phenomenon in zip(
                        report.local_governments_raw, report.expected_ash_fall_times_raw,
                        report.ash_fall_warning_codes_raw, strict=True)}
        self.assertEqual(received, {(4620100, 1, 'ash'): 3, (4620100, 1, 'cinders'): 4,
                                    (4621400, 2, 'ash'): 2, (4621800, 3, 'ash'): 1})
        cells = self.cells(figure)
        self.assertEqual(len(cells), 24)
        displayed = {key: int(label.get_gid().rsplit('-', 1)[1])
                     for key, (_, label) in cells.items()
                     if not label.get_gid().endswith('-missing')}
        self.assertEqual(displayed, received)
        for key, (_, label) in cells.items():
            if key not in received:
                self.assertEqual(label.get_text(), '—')

    def test_actual_cells_distinguish_ash_amounts_and_cinders_without_losing_borders(self):
        _, figure = self.capture(sample(NAME))
        cells = self.cells(figure)
        expected = {
            (4620100, 1, 'ash'): ('多量', '#ff0000', '#ffffff'),
            (4621400, 2, 'ash'): ('やや\n多量', '#ffff00', '#000000'),
            (4621800, 3, 'ash'): ('少量', '#d9d9d9', '#000000'),
        }
        for key, (text, background, foreground) in expected.items():
            cell, label = cells[key]
            self.assertEqual(label.get_text(), text)
            self.assertEqual(cell.get_facecolor(), to_rgba(background))
            self.assertEqual(cell.get_edgecolor(), to_rgba(background))
            self.assertEqual(to_rgba(label.get_color()), to_rgba(foreground))
            self.assertEqual(label.get_path_effects(), [], 'Dark map halos must not obscure cell text')
        cinders, cinders_text = cells[(4620100, 1, 'cinders')]
        self.assertEqual(cinders_text.get_text(), '◆')
        self.assertEqual(cinders.get_facecolor(), cells[(4620100, 2, 'cinders')][0].get_facecolor())
        self.assertNotEqual(cinders.get_facecolor(), to_rgba('#ff0000'))
        self.assertEqual(cinders.get_edgecolor(), to_rgba('#ff0000'))
        self.assertEqual(to_rgba(cinders_text.get_color()), to_rgba('#ffffff'))
        self.assertEqual(cinders_text.get_path_effects(), [])
        heavy = cells[(4620100, 1, 'ash')][0]
        self.assertEqual(cinders.get_x(), heavy.get_x(), 'Simultaneous phenomena share the same hour')
        self.assertLess(cinders.get_y(), heavy.get_y(), 'Cinders have an independent lane')
        # Check the actual coloured rectangles, not the invisible text halo
        # suppressors, for clipping, overlap and complete text containment.
        renderer = figure.canvas.get_renderer()
        boxes = [patch.get_window_extent(renderer) for patch, _ in cells.values()]
        for index, box in enumerate(boxes):
            self.assertGreaterEqual(box.x0, wide.PANEL_X * wide.WIDTH)
            self.assertLessEqual(box.x1, wide.WIDTH)
            self.assertGreaterEqual(box.y0, 0)
            self.assertLessEqual(box.y1, wide.HEIGHT)
            self.assertFalse(any(box.overlaps(other) for other in boxes[index + 1:]))
        for patch, label in cells.values():
            box, text_box = patch.get_window_extent(renderer), label.get_window_extent(renderer)
            self.assertTrue(box.contains(text_box.x0, text_box.y0))
            self.assertTrue(box.contains(text_box.x1, text_box.y1))

    def test_zoomed_ash_map_has_a_lower_right_locator_and_unobscured_forecast_labels(self):
        _, figure = self.capture(sample(NAME))
        self.assert_complete_map(figure, {4620100, 4621400, 4621800})
        main = figure.axes[0]
        locator, = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        self.assertLess(locator.get_position().y1, .5)
        self.assertGreater(locator.get_position().x0, .5)
        self.assertLess(locator.get_position().x1, wide.PANEL_X)
        self.assert_plain_locator(locator)
        self.assertTrue(any(artist.get_gid() == 'preview-prefecture-boundaries'
                            for artist in main.collections))
        obstacle = Bbox.from_extents(*wide.LOCATOR_OBSTACLE).transformed(figure.transFigure)
        for prefix in ('municipality-anchor-', 'volcano-point-'):
            for point in self.points(figure, prefix).values():
                self.assertFalse(obstacle.contains(*main.transData.transform(point.get_offsets()[0])))
        for artist in main.artists:
            if hasattr(artist, 'offsetbox'):
                self.assertFalse(artist.offsetbox.get_window_extent(figure.canvas.get_renderer()).overlaps(obstacle))
        for line in main.lines:
            if (line.get_gid() or '').startswith('wide-number-leader-'):
                self.assertFalse(line.get_transform().transform_path(line.get_path()).intersects_bbox(obstacle, filled=False))

    def test_plain_locator_covers_remote_extent_and_main_geometry_retains_parts_and_holes(self):
        records = forecast((4620100, 1, 2))
        catalogue = dict(ash_map._municipalities())
        feature = copy.deepcopy(catalogue[4620100])
        geometry = feature['geometry']
        if geometry['type'] == 'Polygon':
            geometry = {'type': 'MultiPolygon', 'coordinates': [geometry['coordinates']]}
            feature['geometry'] = geometry
        remote = [[145., 29.], [145.02, 29.], [145.02, 29.02], [145., 29.02], [145., 29.]]
        hole = [[145.005, 29.005], [145.005, 29.01], [145.01, 29.01],
                [145.01, 29.005], [145.005, 29.005]]
        geometry['coordinates'].append([remote, hole])
        catalogue[4620100] = feature
        with patch.object(ash_map, '_municipalities', return_value=catalogue):
            _, figure = self.capture(records)
        self.assert_complete_map(figure, {4620100})
        main = figure.axes[0]
        self.assertLess(main.get_xlim()[1], 145.)
        locator, = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        self.assert_plain_locator(locator)
        area, = [patch for patch in main.patches if patch.get_gid() == 'region-area-4620100']
        self.assertGreaterEqual(area.get_path().vertices[:, 0].max(), 145.02)
        for lon, lat in remote:
            self.assertTrue(locator.bbox.contains(*locator.transData.transform((lon, float(base._my(lat))))))
        for lon, lat in (*remote, *hole):
            self.assertTrue(any(abs(vertex[0] - lon) < 1e-10
                                and abs(vertex[1] - float(base._my(lat))) < 1e-10
                                for vertex in area.get_path().vertices),
                            'Complete municipality parts and holes must remain in main geometry')
        vertices = area.get_path().vertices
        self.assertGreaterEqual(vertices[:, 0].min(), locator.get_xlim()[0])
        self.assertLessEqual(vertices[:, 0].max(), locator.get_xlim()[1])
        self.assertGreaterEqual(vertices[:, 1].min(), locator.get_ylim()[0])
        self.assertLessEqual(vertices[:, 1].max(), locator.get_ylim()[1])

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
            self.assertEqual(area.get_edgecolor(), to_rgba('#9ba8b4'))
            self.assertEqual(area.get_facecolor()[3], 0)
        self.assertFalse(any((artist.get_gid() or '').startswith(('plume-', 'ash-distribution-', 'wind-'))
                             for axes in figure.axes for artist in (*axes.patches, *axes.lines)))
        texts = self.texts(figure)
        self.assertIn('市町村別の予報', texts)

    def test_same_city_keeps_separate_times_and_simultaneous_cinders_with_clear_leaders(self):
        records = forecast((4620100, 1, 3), (4620100, 1, 4),
                           (4620100, 2, 2), (4620100, 3, 1))
        _, figure = self.capture(records + records)
        self.assert_complete_map(figure, {4620100})
        cells = self.cells(figure)
        self.assertEqual(len(cells), 12, 'Repeated reception must not create extra time lanes')
        displayed = {key: label.get_text() for key, (_, label) in cells.items()
                     if not label.get_gid().endswith('-missing')}
        self.assertEqual(displayed, {(4620100, 1, 'ash'): '多量',
                                    (4620100, 1, 'cinders'): '◆',
                                    (4620100, 2, 'ash'): 'やや\n多量',
                                    (4620100, 3, 'ash'): '少量'})
        for hour in (4, 5, 6):
            self.assertEqual(cells[(4620100, hour, 'ash')][1].get_text(), '—')
        for hour in (2, 3, 4, 5, 6):
            self.assertEqual(cells[(4620100, hour, 'cinders')][1].get_text(), '—')

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
                self.assertEqual(struct.unpack('>II', image.png[16:24]), (1920, 1080))
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
                (special, '_numbered_markers', {'side_effect': ValueError('No readable layout')})):
            with self.subTest(missing=name), patch.object(target, name, **kwargs):
                image, figure = self.capture(records)
            self.assertEqual(figure.axes, [])
            self.assertEqual(struct.unpack('>II', image.png[16:24]), (1920, 1080))

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
