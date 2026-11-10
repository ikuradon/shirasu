"""Flood previews highlight complete forecast reaches without inventing areas."""

import copy
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import test_image_renderer as helpers
from test_image_renderer_v3 import sample, changed

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
NAME = 'flood'
if HAS_RENDERER:
    from matplotlib.colors import to_rgba
    from matplotlib.offsetbox import PaddedBox
    from matplotlib.text import Text
    import numpy as np
    from image_service import flood_map
    from image_service import render as base
    from image_service import render_preview as preview
    from image_service import render_v4 as published
    from image_service.flood_reports import flood_plan


def forecast(*rows):
    """Encode up to three (forecast-region code, warning code) pairs."""
    fields = [(53, 132, 0)]
    for index, (region, level) in enumerate(rows):
        fields.extend(((53 + index * 44, 4, level), (57 + index * 44, 40, region)))
    return changed(NAME, *fields)


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for flood preview PNG tests')
class FloodRendererTests(unittest.TestCase):
    def capture(self, records):
        figures = []
        original = base._figure

        def make_figure():
            figure = original()
            figures.append(figure)
            figure.clear = lambda *args, **kwargs: None
            self.addCleanup(lambda: base.Figure.clear(figure))
            return figure

        with patch.object(base, '_figure', side_effect=make_figure):
            image = preview.render(records)
        return image, figures[-1]

    def texts(self, figure):
        return '\n'.join(text.get_text() for text in figure.findobj(Text))

    def reaches(self, figure):
        self.assertTrue(figure.axes, 'Every supported forecast must retain its complete map')
        return {line.get_gid(): line for line in figure.axes[0].lines
                if (line.get_gid() or '').startswith('flood-reach-')}

    def test_training_png_keeps_every_forecast_and_every_source_line_component(self):
        records = sample(NAME)
        plan = flood_plan(base._decode(records))
        self.assertEqual(len(plan.notices), 3)
        image, figure = self.capture(records)
        self.assertEqual(image.category, 'Flood')
        self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', image.png[16:24]), (1440, 1200))
        lines = self.reaches(figure)
        expected = {}
        for notice in plan.notices:
            parts = flood_map._parts(flood_map._rivers()[notice.code])
            expected.update({f'flood-reach-{notice.code}-{index}': part
                             for index, part in enumerate(parts)})
        self.assertEqual(set(lines), set(expected))
        for identity, points in expected.items():
            np.testing.assert_allclose(lines[identity].get_xydata(), points, rtol=0, atol=0)
            displayed = figure.transFigure.inverted().transform(
                figure.axes[0].transData.transform(points))
            self.assertTrue(np.all((.18 <= displayed[:, 1]) & (displayed[:, 1] <= .79)))
            self.assertTrue(np.all((0 <= displayed[:, 0]) & (displayed[:, 0] <= 1)))
        helpers.RendererTests.assert_clear_leaders(self, figure, figure.axes[0], 3)
        for annotation in figure.axes[0].artists:
            if not hasattr(annotation, 'offsetbox'):
                continue
            box = annotation.offsetbox.get_window_extent(figure.canvas.get_renderer())
            for line in lines.values():
                path = line.get_transform().transform_path(line.get_path())
                self.assertFalse(path.intersects_bbox(box, filled=False),
                                 'A warning label must not cover another part of a river reach')
        texts = self.texts(figure)
        for expected in ('訓練', '指定河川洪水予報', 'JST', '国土数値情報'):
            self.assertIn(expected, texts)
        for notice in plan.notices:
            self.assertIn(notice.title, texts)
            for line in notice.lines:
                self.assertIn(line, texts)
        self.assertFalse(any((artist.get_gid() or '').startswith(('region-area-', 'inundation-'))
                             for artist in figure.axes[0].patches))

    def test_hazard_badges_and_black_reaches_remain_legible(self):
        records = sample(NAME)
        plan = flood_plan(base._decode(records))
        _, figure = self.capture(records)
        reaches = self.reaches(figure)
        self.assertIn(4, {notice.warning_level for notice in plan.notices})
        lines = {line.get_gid(): line for line in figure.axes[0].lines}
        for notice in plan.notices:
            for identity, line in reaches.items():
                if identity.startswith(f'flood-reach-{notice.code}-'):
                    self.assertEqual(to_rgba(line.get_color()), to_rgba(notice.color))
                    casing = lines[identity.replace('flood-reach-', 'flood-casing-')]
                    self.assertGreater(casing.get_linewidth(), line.get_linewidth())
                    self.assertEqual(to_rgba(casing.get_color()), to_rgba(flood_map.CASING))
                    self.assertNotEqual(to_rgba(casing.get_color()), to_rgba(line.get_color()))
        badges = {}
        for badge in figure.findobj(PaddedBox):
            texts = [text for text in badge.findobj(Text)
                     if (text.get_gid() or '').startswith('flood-hazard-')]
            if texts:
                self.assertEqual(len(texts), 1)
                badges[texts[0].get_text()] = (badge, texts[0])
        for notice in plan.notices:
            for line, style in zip(notice.lines, notice.styles, strict=True):
                badge, label = badges[line]
                self.assertEqual(badge.patch.get_facecolor(), to_rgba(style.background))
                self.assertEqual(badge.patch.get_edgecolor(), to_rgba(style.edge))
                self.assertEqual(to_rgba(label.get_color()), to_rgba(style.foreground))
                self.assertEqual(label.get_path_effects(), [])

    def test_release_is_neutral_dashed_and_distinct_from_cancellation(self):
        records = changed(NAME, (53, 4, 1))
        code = base._decode(records)[0].flood_forecast_regions_raw[0]
        _, figure = self.capture(records)
        lines = [line for identity, line in self.reaches(figure).items()
                 if identity.startswith(f'flood-reach-{code}-')]
        self.assertTrue(lines)
        self.assertTrue(all(line.is_dashed() for line in lines))
        self.assertIn('警報解除', self.texts(figure))
        self.assertIn('氾濫注意情報', self.texts(figure))
        self.assertNotIn('取消', self.texts(figure))

    def test_disconnected_parts_remain_in_view_and_anchor_is_on_longest_part(self):
        records = sample(NAME)
        plan = flood_plan(base._decode(records))
        code = plan.notices[0].code
        source = dict(flood_map._rivers())
        source[code] = {'type': 'Feature', 'properties': {'code': f'{code:012d}'},
                        'geometry': {'type': 'MultiLineString', 'coordinates': [
                            [[139.0, 35.0], [139.0, 35.2], [139.2, 35.2]],
                            [[140.0, 36.0], [140.01, 36.01]],
                        ]}}
        with patch.object(flood_map, '_rivers', return_value=source):
            _, figure = self.capture(records)
        lines = self.reaches(figure)
        for index in (0, 1):
            points = lines[f'flood-reach-{code}-{index}'].get_xydata()
            displayed = figure.transFigure.inverted().transform(
                figure.axes[0].transData.transform(points))
            self.assertTrue(np.all((.18 <= displayed[:, 1]) & (displayed[:, 1] <= .79)))
        anchor = next(artist for artist in figure.axes[0].collections
                      if artist.get_gid() == f'flood-anchor-{code}').get_offsets()[0]
        first = lines[f'flood-reach-{code}-0'].get_xydata()
        # The anchor is on one of the two legs, never the off-river centroid.
        self.assertTrue(abs(anchor[0] - first[0, 0]) < 1e-9
                        or abs(anchor[1] - first[-1, 1]) < 1e-9)
        helpers.RendererTests.assert_clear_leaders(self, figure, figure.axes[0], 3)

    def test_arclength_anchor_ignores_repeated_vertices_and_never_bridges_parts(self):
        parts = (np.asarray([[0., 0.], [0., 0.], [0., 2.], [4., 2.]]),
                 np.asarray([[20., 20.], [20., 21.]]))
        np.testing.assert_allclose(flood_map._anchor(parts), (1., 2.))
        with self.assertRaises(ValueError):
            flood_map._parts({'geometry': {'type': 'MultiLineString',
                              'coordinates': [[[139, 35], [139, 35]]]}})

    def test_overlapping_gonokawa_warning_and_release_preserve_both_as_summary(self):
        upper, lower = 870705000101, 870705000102
        for rows in (((upper, 4), (lower, 1)), ((lower, 1), (upper, 4))):
            records = forecast(*rows)
            with self.subTest(rows=rows):
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertEqual(image.png, published.render(records).png)
                for text in ('江の川上流', '江の川下流', '氾濫発生情報', '警報解除'):
                    self.assertIn(text, self.texts(figure))
        # Reverse the actual downstream vertices: direction must not permit an
        # overlapping release stroke to conceal the upstream occurrence colour.
        source = dict(flood_map._rivers())
        source[lower] = copy.deepcopy(source[lower])
        for line in source[lower]['geometry']['coordinates']:
            line.reverse()
        records = forecast((upper, 4), (lower, 1))
        with patch.object(flood_map, '_rivers', return_value=source):
            image, figure = self.capture(records)
        self.assertEqual(figure.axes, [])
        self.assertEqual(image.png, published.render(records).png)

    def test_every_reviewed_overlapping_pair_rejects_different_warning_colours(self):
        pairs = ((870705000101, 870705000102), (820209000101, 820209000102),
                 (850509000402, 850509000403), (830303000102, 830303000103),
                 (850509000102, 850509000103))
        for first, second in pairs:
            figure = base._figure()
            try:
                plan = flood_plan(base._decode(forecast((first, 4), (second, 2))))
                with self.subTest(regions=(first, second)), self.assertRaisesRegex(ValueError, 'Overlapping'):
                    flood_map.draw_map(figure, plan)
                self.assertEqual(figure.axes, [], 'Reject ambiguity before drawing any map')
            finally:
                figure.clear()

    def test_same_level_shared_reaches_keep_both_labels_and_all_components(self):
        codes = (870705000101, 870705000102)
        records = forecast(*((code, 3) for code in codes))
        _, figure = self.capture(records)
        lines = self.reaches(figure)
        self.assertEqual(set(lines), {
            f'flood-reach-{code}-{index}' for code in codes
            for index, _ in enumerate(flood_map._parts(flood_map._rivers()[code]))})
        helpers.RendererTests.assert_clear_leaders(self, figure, figure.axes[0], 2)

    def test_touching_endpoints_and_repeated_vertices_do_not_hide_different_forecasts(self):
        codes = (870705000101, 870705000102)
        source = dict(flood_map._rivers())
        coordinates = ([[139, 35], [139, 35], [139.15, 35.1]],
                       [[139.15, 35.1], [139.15, 35.1], [139.3, 35.2]])
        for code, line in zip(codes, coordinates, strict=True):
            source[code] = {'type': 'Feature', 'properties': {'code': f'{code:012d}'},
                            'geometry': {'type': 'LineString', 'coordinates': line}}
        records = forecast((codes[0], 4), (codes[1], 1))
        with patch.object(flood_map, '_rivers', return_value=source):
            _, figure = self.capture(records)
        self.assertEqual(set(self.reaches(figure)), {f'flood-reach-{code}-0' for code in codes})
        helpers.RendererTests.assert_clear_leaders(self, figure, figure.axes[0], 2)

    def test_unknown_cancelled_missing_or_unreadable_maps_keep_the_complete_summary(self):
        for records in (changed(NAME, (41, 2, 2)), changed(NAME, (53, 4, 15)),
                        changed(NAME, (57, 40, 0))):
            with self.subTest(records=records):
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertEqual(image.png, published.render(records).png)
        records = sample(NAME)
        incomplete = dict(flood_map._rivers())
        del incomplete[base._decode(records)[0].flood_forecast_regions_raw[0]]
        for target, kwargs in (('_rivers', {'return_value': incomplete}),
                               ('draw_labels', {'side_effect': ValueError('No readable layout')})):
            with self.subTest(target=target), patch.object(flood_map, target, **kwargs):
                image, figure = self.capture(records)
            self.assertEqual(figure.axes, [])
            self.assertEqual(image.png, published.render(records).png)

    def test_corrupt_private_asset_cannot_be_used_for_a_map(self):
        flood_map.verify_assets.cache_clear()
        self.addCleanup(flood_map.verify_assets.cache_clear)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for name in ('flood_manifest.json', 'FLOOD_NOTICE.txt'):
                (destination / name).write_bytes((flood_map.ASSETS / name).read_bytes())
            (destination / 'flood.geojson.zlib').write_bytes(b'wrong-source')
            with patch.object(flood_map, 'ASSETS', destination), self.assertRaisesRegex(
                    RuntimeError, 'reviewed source'):
                flood_map.verify_assets()

    def test_preview_is_deterministic_and_preserves_published_flood_output(self):
        records = sample(NAME)
        old = published.render(records)
        first = preview.render(records)
        self.assertEqual(first, preview.render(records))
        self.assertNotEqual(first.png, old.png)
        self.assertEqual(old, published.render(records))


if __name__ == '__main__':
    unittest.main()
