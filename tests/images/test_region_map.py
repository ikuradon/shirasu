"""Render real polygon holes, coastlines and complete regional labels offline."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('image_service', 'azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from image_service import region_map as region
    from image_service import render as base
    from image_service.regional_reports import RegionMap, RegionNotice
    from matplotlib.colors import to_rgb
    from matplotlib.lines import Line2D
    from matplotlib.path import Path as ShapePath
    from matplotlib.transforms import Bbox
    import numpy as np


def feature(code, kind, coordinates, anchor):
    return {'type': 'Feature', 'properties': {'code': code, 'name': f'Region {code}', 'anchor': anchor},
            'geometry': {'type': kind, 'coordinates': coordinates}}


def polygon(left=135, bottom=30, width=4, height=4):
    return [[left, bottom], [left + width, bottom], [left + width, bottom + height],
            [left, bottom + height], [left, bottom]]


@unittest.skipUnless(HAS_RENDERER, 'Regional maps run in the isolated image service')
class RegionMapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-regions-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.features = {
            'marine': [feature(1010, 'MultiPolygon', [[polygon(), polygon(136, 31, 2, 2)]], [135.5, 30.5]),
                       feature(1020, 'MultiPolygon', [[polygon(144, 24, 1, 1)]], [144.5, 24.5])],
            'tsunami': [feature(100, 'MultiLineString', [[[135, 30], [136, 31], [137, 30]],
                                                       [[144, 24], [145, 24.5]]], [136, 31])],
            'prefectures': [feature(1, 'MultiPolygon', [[polygon()]], [137, 32])],
        }
        self.groups = {'marine': {'1000': [1010, 1020]}, 'tsunami': {}}
        self.enterContext(patch.object(region, 'ASSETS', self.root))
        self.enterContext(patch.object(base, '_land', return_value=[]))
        self.write_assets()
        self.addCleanup(self.clear_caches)

    def clear_caches(self):
        region.verify_assets.cache_clear()
        region._layer.cache_clear()
        region._groups.cache_clear()

    def write_assets(self):
        manifest = {}
        for layer, features in self.features.items():
            (self.root / f'{layer}.geojson').write_text(json.dumps({'type': 'FeatureCollection', 'features': features}))
        (self.root / 'groups.json').write_text(json.dumps(self.groups))
        for name in region.ASSET_NAMES:
            manifest[name] = {'sha256': hashlib.sha256((self.root / name).read_bytes()).hexdigest()}
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        self.clear_caches()

    def plan(self, layer='marine', code=1010, **kwargs):
        return RegionMap(layer, 'Test regions', 'Test subtitle',
                         (RegionNotice(code, kwargs.get('title', '対象地域'), kwargs.get('lines', ('受信情報',)),
                                       kwargs.get('color', '#ffce6c')),), 'Test note')

    def draw(self, plan):
        figure = base._figure()
        self.addCleanup(figure.clear)
        axes = region.draw_map(figure, plan)
        figure.canvas.draw()
        return figure, axes

    def test_parent_expands_all_children_and_remote_island_sets_extent(self):
        figure, axes = self.draw(self.plan(code=1000))
        self.assertEqual({patch.get_gid() for patch in axes.patches}, {'region-area-1010', 'region-area-1020'})
        self.assertEqual(len(axes.artists), 1)  # One complete label for the parent notice.
        point = figure.transFigure.inverted().transform(axes.transData.transform((145, base._my(24))))
        self.assertTrue(.03 <= point[0] <= .97)
        self.assertTrue(.18 <= point[1] <= .80)

    def test_marine_polygon_hole_remains_unfilled_for_same_ring_winding(self):
        # Both fixture rings are counterclockwise; the painter must normalize
        # holes rather than depend on source winding or flatten all rings.
        with patch.object(region, '_annotations', return_value=[]):
            figure, axes = self.draw(self.plan())
        pixels = np.asarray(figure.canvas.buffer_rgba())
        def pixel(coordinate):
            x, y = axes.transData.transform((coordinate[0], base._my(coordinate[1])))
            return pixels[base.HEIGHT - 1 - round(y), round(x), :3]
        sea = np.rint(np.asarray(to_rgb(base.SEA)) * 255).astype(int)
        self.assertTrue(np.array_equal(pixel((137.1, 32.1)), sea))
        self.assertGreater(np.linalg.norm(pixel((135.5, 32.1)).astype(int) - sea), 20)

    def test_remote_tiny_islands_are_kept_and_batched_in_one_region_artist(self):
        self.features['marine'][0]['geometry']['coordinates'].append([polygon(155, 20, 1e-6, 1e-6)])
        self.write_assets()
        with patch.object(region, '_annotations', return_value=[]):
            _, axes = self.draw(self.plan())
        self.assertEqual(len(axes.patches), 1)
        self.assertGreater(axes.patches[0].get_path().vertices[:, 0].max(), 155)
        self.assertGreater(axes.get_xlim()[1], 155)

    def test_tsunami_draws_every_coastline_segment_without_any_filled_region(self):
        _, axes = self.draw(self.plan('tsunami', 100))
        self.assertEqual(list(axes.patches), [])
        coasts = [line for line in axes.lines if line.get_gid() == 'region-coast-100']
        self.assertEqual(len(coasts), 2)
        self.assertTrue(all(line.get_linewidth() > 4 for line in coasts))
        self.assertEqual(sum(len(line.get_xdata()) for line in coasts), 5)

    def test_prefecture_context_is_neutral_and_value_stays_on_marker(self):
        figure, axes = self.draw(self.plan('prefectures', 1, color='#ff0000'))
        self.assertEqual(len(figure.axes), 1, 'No overview is needed when every component fits')
        area = axes.patches[0]
        self.assertLess(area.get_facecolor()[3], .04)
        self.assertNotEqual(area.get_facecolor()[:3], to_rgb('#ff0000'))
        self.assertEqual(len(axes.collections), 1)
        self.assertEqual(axes.collections[0].get_gid(), 'region-anchor-1')

    def test_prefecture_remote_islands_use_full_overview_and_keep_main_map_readable(self):
        self.features['prefectures'][0]['geometry']['coordinates'].append([polygon(154, 20, .2, .2)])
        self.write_assets()
        figure, main = self.draw(self.plan('prefectures', 1))
        self.assertEqual(len(figure.axes), 2)
        overview = figure.axes[1]
        self.assertEqual(overview.get_gid(), 'region-overview')
        self.assertLess(main.get_xlim()[1], 154)
        self.assertGreater(overview.get_xlim()[1], 154.2)
        area = next(part for part in overview.patches if part.get_gid() == 'region-area-1')
        self.assertGreaterEqual(area.get_path().vertices[:, 0].max(), 154.2)
        extent = next(part for part in overview.patches if part.get_gid() == 'region-main-extent')
        left, right = main.get_xlim()
        bottom, top = main.get_ylim()
        self.assertEqual(extent.get_bbox().bounds, (left, bottom, right - left, top - bottom))
        self.assertLessEqual(overview.get_xlim()[0], left)
        self.assertGreaterEqual(overview.get_xlim()[1], right)
        self.assertLessEqual(overview.get_ylim()[0], bottom)
        self.assertGreaterEqual(overview.get_ylim()[1], top)
        x0, y0, x1, y1 = region.OVERVIEW_OBSTACLE
        obstacle = Bbox.from_extents(x0 * base.WIDTH, y0 * base.HEIGHT, x1 * base.WIDTH, y1 * base.HEIGHT)
        for artist in main.artists:
            self.assertFalse(artist.offsetbox.get_window_extent(figure.canvas.get_renderer()).overlaps(obstacle))
        for line in main.lines:
            if line.get_gid() == 'label-leader':
                self.assertFalse(line.get_transform().transform_path(line.get_path()).intersects_bbox(obstacle, filled=False))

    def test_coastal_strokes_do_not_run_through_regional_label_text(self):
        figure = base._figure()
        self.addCleanup(figure.clear)
        axes = figure.add_axes([0, 0, 1, 1])
        axes.set_xlim(0, 1)
        axes.set_ylim(0, 1)
        coast = Line2D([.1, .9], [.45, .45], linewidth=4.6, gid='region-coast-100')
        axes.add_line(coast)
        notices = [RegionNotice(100, '沿岸の対象地域', ('長い津波到達時刻の説明',), '#ffce6c')]
        region._annotations(figure, axes, [(.5, .45)], notices)
        figure.canvas.draw()
        text = axes.artists[0].offsetbox.get_window_extent(figure.canvas.get_renderer())
        boundary = coast.get_transform().transform_path(coast.get_path())
        self.assertFalse(boundary.intersects_bbox(text, filled=False))

    def test_boundary_index_keeps_stroke_width_without_filling_polygon_interiors(self):
        boundary = ShapePath([(400, 400), (800, 400), (800, 800), (400, 800), (400, 400)],
                             [ShapePath.MOVETO, ShapePath.LINETO, ShapePath.LINETO,
                              ShapePath.LINETO, ShapePath.CLOSEPOLY])
        mask = region._boundary_mask([(boundary, 12)])
        def box(x0, y0, x1, y1):
            return x0 / base.WIDTH, y0 / base.HEIGHT, x1 / base.WIDTH, y1 / base.HEIGHT
        self.assertTrue(region._hits_boundary(mask, box(411, 500, 430, 530)))
        self.assertFalse(region._hits_boundary(mask, box(500, 500, 530, 530)))
        self.assertFalse(region._hits_boundary(mask, box(900, 900, 930, 930)))

    def test_prefecture_islands_in_footer_are_clipped_and_preserved_in_overview(self):
        self.features['prefectures'][0]['geometry']['coordinates'].append([polygon(136, 29, .5, .25)])
        self.write_assets()
        figure, main = self.draw(self.plan('prefectures', 1))
        self.assertEqual(len(figure.axes), 2, 'An island inside the full axes but below the body still needs an overview')
        area = next(part for part in main.patches if part.get_gid() == 'region-area-1')
        clip = area.get_clip_box()
        self.assertAlmostEqual(clip.y0, region.LABEL_BOTTOM * base.HEIGHT)
        self.assertAlmostEqual(clip.y1, region.LABEL_TOP * base.HEIGHT)
        island = (136.25, base._my(29.125))
        x, y = main.transData.transform(island)
        self.assertGreater(y, 0)
        self.assertLess(y, region.LABEL_BOTTOM * base.HEIGHT)
        pixels = np.asarray(figure.canvas.buffer_rgba())
        sea = np.rint(np.asarray(to_rgb(base.SEA)) * 255).astype(int)
        self.assertTrue(np.array_equal(pixels[base.HEIGHT - 1 - round(y), round(x), :3], sea))
        overview_area = next(part for part in figure.axes[1].patches if part.get_gid() == 'region-area-1')
        self.assertLessEqual(overview_area.get_path().vertices[:, 1].min(), base._my(29))

    def test_unknown_member_rejects_complete_plan_before_drawing(self):
        valid = self.plan()
        plan = RegionMap(valid.layer, valid.title, valid.subtitle,
                         valid.notices + (RegionNotice(9999, '未知', ('情報',), '#ffce6c'),), valid.note)
        figure = base._figure()
        self.addCleanup(figure.clear)
        with self.assertRaises(ValueError):
            region.draw_map(figure, plan)
        self.assertEqual(figure.axes, [])

    def test_missing_group_member_and_cycles_reject_the_whole_map(self):
        for groups in ({'marine': {'1000': [1010, 9999]}},
                       {'marine': {'1000': [2000], '2000': [1000]}}):
            self.groups = groups
            self.write_assets()
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                self.draw(self.plan(code=1000))

    def test_overlapping_parent_and_child_with_different_colors_is_not_silently_overpainted(self):
        parent = self.plan(code=1000)
        plan = RegionMap(parent.layer, parent.title, parent.subtitle,
                         parent.notices + (RegionNotice(1010, '別の警報', ('情報',), '#ff0000'),), parent.note)
        with self.assertRaises(ValueError):
            self.draw(plan)

    def test_geometry_types_missing_anchors_and_poles_are_rejected(self):
        bad = [feature(1010, 'Point', [135, 30], [135, 30]),
               feature(1010, 'MultiPolygon', [[polygon()]], None),
               feature(1010, 'MultiPolygon', [[polygon(bottom=86)]], [135, 84]),
               feature(1010, 'MultiPolygon', [[polygon()[:-1]]], [135, 30]),
               feature(1010, 'MultiPolygon', [[polygon()]], [float('nan'), 30])]
        for sample in bad:
            self.features['marine'] = [sample]
            self.write_assets()
            with self.subTest(geometry=sample['geometry']['type']), self.assertRaises(ValueError):
                self.draw(self.plan())

    def test_asset_digest_mismatch_is_not_ignored(self):
        with (self.root / 'marine.geojson').open('a') as file:
            file.write(' ')
        with self.assertRaises(RuntimeError):
            region.verify_assets()

    def test_regional_render_is_deterministic(self):
        figures = [self.draw(self.plan(code=1000))[0] for _ in range(2)]
        images = []
        for figure in figures:
            output = io.BytesIO()
            figure.savefig(output, format='png', dpi=base.DPI)
            images.append(output.getvalue())
        self.assertEqual(images[0], images[1])

    def test_dense_region_labels_reserve_heading_footer_and_avoid_leader_crossings(self):
        figure = base._figure()
        self.addCleanup(figure.clear)
        axes = figure.add_axes([0, 0, 1, 1])
        axes.set_xlim(0, 1)
        axes.set_ylim(0, 1)
        anchors = [(0.44, 0.44), (0.46, 0.46), (0.48, 0.44), (0.50, 0.46), (0.52, 0.44), (0.54, 0.46)]
        notices = [RegionNotice(index + 1, f'対象地域 {index + 1}', ('受信した警報',), '#ffce6c') for index in range(6)]
        region._annotations(figure, axes, anchors, notices)
        figure.canvas.draw()
        boxes = [artist.offsetbox.get_window_extent(figure.canvas.get_renderer()) for artist in axes.artists]
        lines = [line.get_transform().transform_path(line.get_path()) for line in axes.lines if line.get_gid() == 'label-leader']
        self.assertEqual(len(lines), 6)
        for index, box in enumerate(boxes):
            self.assertGreaterEqual(box.y0, region.LABEL_BOTTOM * base.HEIGHT)
            self.assertLessEqual(box.y1, region.LABEL_TOP * base.HEIGHT)
            for other in range(index + 1, len(boxes)):
                self.assertFalse(box.overlaps(boxes[other]))
            for other, line in enumerate(lines):
                if index != other:
                    self.assertFalse(line.intersects_bbox(box, filled=False))
        for index, line in enumerate(lines):
            for other in lines[index + 1:]:
                self.assertFalse(line.intersects_path(other, filled=False))

    def test_text_is_never_truncated_to_force_it_into_a_map(self):
        with self.assertRaises(ValueError):
            self.draw(self.plan(lines=tuple('長い防災情報を省略しない' * 10 for _ in range(20))))


if __name__ == '__main__':
    unittest.main()
