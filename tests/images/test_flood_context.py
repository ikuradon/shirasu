"""Background geography and a locator must not conceal received flood reaches."""

import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from tests.images import test_flood_renderer as flood_helpers
from tests.images.test_image_renderer_v3 import sample

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.collections import LineCollection
    from matplotlib.text import Text
    from matplotlib.transforms import Bbox
    import numpy as np
    from image_service import flood_map
    from image_service import preview_context as context
    from image_service import preview_wide_layout as wide
    from image_service import render as base
    from image_service.flood_reports import flood_plan


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for flood context tests')
class FloodContextTests(unittest.TestCase):
    capture = flood_helpers.FloodRendererTests.capture
    reaches = flood_helpers.FloodRendererTests.reaches

    def test_internal_prefecture_borders_are_faint_unfilled_and_beneath_warning_lines(self):
        _, figure = self.capture(sample('flood'))
        ax = figure.axes[0]
        outlines = [artist for artist in ax.collections
                    if artist.get_gid() == 'preview-prefecture-boundaries']
        self.assertEqual(len(outlines), 1)
        outline, = outlines
        self.assertIsInstance(outline, LineCollection)
        self.assertTrue(outline.get_segments())
        self.assertEqual(len(outline.get_facecolors()), 0,
                         'Prefectures provide context, not a filled hazard footprint')
        colors = outline.get_colors()
        self.assertEqual(len(colors), 1, 'Every prefecture must have the same neutral style')
        self.assertTrue(0 < colors[0, 3] < .75, 'Boundaries must remain subdued')
        self.assertLess(max(colors[0, :3]) - min(colors[0, :3]), .2)
        for line in self.reaches(figure).values():
            self.assertLess(outline.get_zorder(), line.get_zorder())
            self.assertLess(max(outline.get_linewidths()), line.get_linewidth())

        # A dedicated asset contains only lines shared by two prefectures.
        # Drawing complete polygons would duplicate the terrain's coastlines.
        assets = Path(__file__).resolve().parents[2] / 'image_service/assets/preview'
        manifest = json.loads((assets / 'prefecture_boundaries_manifest.json').read_text())
        name = 'prefecture_boundaries.geojson.zlib'
        for filename in (name, 'PREFECTURE_BOUNDARIES_NOTICE.txt'):
            data = (assets / filename).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest[filename]['sha256'])
            self.assertEqual(len(data), manifest[filename]['bytes'])
        self.assertEqual(manifest[name]['source_sha256'],
                         '5f36a66e2a2bbdc2eb04d4c7be0dc9d710903040d4b721716a0b1368d80da235')
        collection = json.loads(zlib.decompress((assets / name).read_bytes()))
        self.assertEqual(collection['type'], 'FeatureCollection')
        features = collection['features']
        self.assertEqual(len(features), manifest[name]['feature_count'])
        pairs = {tuple(feature['properties']['codes']) for feature in features}
        # Extract shared edges before simplifying. In particular, otherwise
        # independently simplified Saitama/Ibaraki outlines lose their border.
        self.assertTrue({(8, 11), (8, 12), (11, 12), (11, 13), (13, 14)} <= pairs)
        for pair in pairs:
            self.assertEqual(len(pair), 2)
            self.assertTrue(1 <= pair[0] < pair[1] <= 47)
        vertices = set()
        for feature in features:
            geometry = feature['geometry']
            self.assertIn(geometry['type'], ('LineString', 'MultiLineString'))
            lines = ([geometry['coordinates']] if geometry['type'] == 'LineString'
                     else geometry['coordinates'])
            for line in lines:
                vertices.update((round(lon, 8), round(float(base._my(lat)), 8)) for lon, lat in line)
        for line in outline.get_segments():
            self.assertTrue(all((round(x, 8), round(y, 8)) in vertices for x, y in line))

    def test_corrupted_border_geometry_is_rejected_before_it_can_supply_context(self):
        context.verify_assets.cache_clear()
        self.addCleanup(context.verify_assets.cache_clear)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for name in ('prefecture_boundaries_manifest.json', 'PREFECTURE_BOUNDARIES_NOTICE.txt'):
                (destination / name).write_bytes((context.ASSETS / name).read_bytes())
            (destination / 'prefecture_boundaries.geojson.zlib').write_bytes(b'wrong-boundaries')
            with patch.object(context, 'ASSETS', destination), self.assertRaises(RuntimeError):
                context.verify_assets()

    def test_locator_marks_the_actual_main_view_and_keeps_national_context(self):
        _, figure = self.capture(sample('flood'))
        main = figure.axes[0]
        locators = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        self.assertEqual(len(locators), 1)
        locator, = locators
        # The main axes continue underneath the information overlay. The inset
        # belongs to the lower right of the *visible map*, left of that overlay.
        main_box, locator_box = main.get_position(), locator.get_position()
        self.assertGreater(locator_box.x0, (main_box.x0 + wide.PANEL_X) / 2)
        self.assertLess(locator_box.x1, wide.PANEL_X)
        self.assertGreater(locator_box.y0, main_box.y0)
        self.assertLess(locator_box.y1, (main_box.y0 + main_box.y1) / 2)
        frames = [patch for patch in locator.patches if patch.get_gid() == 'preview-main-extent']
        self.assertEqual(len(frames), 1)
        frame, = frames
        left, right = main.get_xlim()
        bottom, top = main.get_ylim()
        right = main.transData.inverted().transform(
            figure.transFigure.transform((wide.PANEL_X, 0)))[0]
        np.testing.assert_allclose(frame.get_bbox().extents, (left, bottom, right, top), rtol=0, atol=1e-10)
        west, east = locator.get_xlim()
        south, north = locator.get_ylim()
        self.assertLess(west, left)
        self.assertGreater(east, right)
        self.assertLess(south, bottom)
        self.assertGreater(north, top)
        for lon, lat in ((127.7, 26.2), (130.4, 33.6), (141.4, 43.1)):
            self.assertLess(west, lon)
            self.assertGreater(east, lon)
            self.assertLess(south, float(base._my(lat)))
            self.assertGreater(north, float(base._my(lat)))
        texts = '\n'.join(text.get_text() for text in figure.findobj(Text))
        self.assertIn('表示位置', texts)
        self.assertIn('枠は主図範囲', texts)

    def test_inset_does_not_hide_any_river_label_or_leader(self):
        # Multiple reaches use numbered references; a single reach uses its
        # name and a leader. Exercise both current presentations, not the old
        # AnnotationBbox labels that the wide renderer no longer creates.
        multiple = sample('flood')
        notice = flood_plan(base._decode(multiple)).notices[0]
        single = flood_helpers.forecast((notice.code, notice.warning_level))
        for records in (multiple, single):
            with self.subTest(reaches=len(flood_plan(base._decode(records)).notices)):
                self.assert_inset_clear(records)

    def assert_inset_clear(self, records):
        plan = flood_plan(base._decode(records))
        _, figure = self.capture(records)
        main = figure.axes[0]
        renderer = figure.canvas.get_renderer()
        locator, = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        caption_boxes = [text.get_window_extent(renderer) for text in figure.texts
                         if text.get_gid() == 'wide-locator']
        self.assertTrue(caption_boxes)
        obstacle = Bbox.union([locator.get_window_extent(renderer), *caption_boxes])
        # The shared map body excludes the header/footer and reserves a column
        # for the locator, rather than using the old portrait layout's Y band.
        safe_map = Bbox.from_extents(*wide.LOCATOR_BODY)
        lines = self.reaches(figure)
        expected = {}
        for notice in plan.notices:
            expected.update({f'flood-reach-{notice.code}-{index}': points
                             for index, points in enumerate(flood_map._parts(flood_map._rivers()[notice.code]))})
        self.assertEqual(set(lines), set(expected))
        for identity, line in lines.items():
            np.testing.assert_allclose(line.get_xydata(), expected[identity], rtol=0, atol=0)
            path = line.get_transform().transform_path(line.get_path())
            self.assertFalse(path.intersects_bbox(obstacle, filled=False),
                             'A locator must not cover a received river segment')
            positions = figure.transFigure.inverted().transform(main.transData.transform(line.get_xydata()))
            self.assertTrue(np.all((safe_map.x0 - 1e-9 <= positions[:, 0])
                                   & (positions[:, 0] <= safe_map.x1 + 1e-9)))
            self.assertTrue(np.all((safe_map.y0 - 1e-9 <= positions[:, 1])
                                   & (positions[:, 1] <= safe_map.y1 + 1e-9)))
        labels = [text for text in figure.texts if (text.get_gid() or '').startswith(
            ('wide-number-flood-anchor-', 'flood-map-name-'))]
        self.assertEqual(len(labels), len(plan.notices))
        boxes = {text.get_gid().rsplit('-', 1)[-1]:
                 text.get_bbox_patch().get_window_extent(renderer) for text in labels}
        for index, box in enumerate(boxes.values()):
            self.assertFalse(box.overlaps(obstacle), 'A forecast label must stay outside the locator')
            for other in list(boxes.values())[index + 1:]:
                self.assertFalse(box.overlaps(other), 'Forecast references must remain distinguishable')
        leaders = [line for line in main.lines if (line.get_gid() or '').startswith(
            ('wide-number-leader-flood-anchor-', 'flood-name-leader-'))]
        if len(plan.notices) == 1:
            self.assertEqual(len(leaders), 1, 'The named river reference must exercise a real leader')
        for line in leaders:
            path = line.get_transform().transform_path(line.get_path())
            self.assertFalse(path.intersects_bbox(obstacle, filled=False),
                             'A forecast leader must stay outside the locator')
            own_code = line.get_gid().rsplit('-', 1)[-1]
            for code, box in boxes.items():
                if code != own_code:
                    self.assertFalse(path.intersects_bbox(box, filled=False),
                                     'A leader must not run through another forecast reference')


if __name__ == '__main__':
    unittest.main()
