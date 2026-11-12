"""The new geography must remain independent of the published regional layer."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from image_service import map_v3 as maps
from image_service import region_map as old
from image_service import render as base
from image_service.regional_reports import RegionMap, RegionNotice


def feature(code, x):
    return {'type': 'Feature', 'properties': {'code': str(code), 'name': str(code), 'anchor': [x, 35]},
            'geometry': {'type': 'Polygon', 'coordinates': [[[x-.3, 34.7], [x+.3, 34.7],
                [x+.3, 35.3], [x-.3, 35.3], [x-.3, 34.7]]]}}


class NewRegionMapTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.assets = Path(self.directory)
        self.enterContext(patch.object(maps, 'ASSETS', self.assets))
        self.clear()
        self.addCleanup(self.clear)
        for layer in maps.LAYERS:
            data = {'type': 'FeatureCollection', 'features': [feature(1, 139), feature(2, 141)]}
            (self.assets / (layer+'.geojson.zlib')).write_bytes(zlib.compress(json.dumps(data).encode()))
            (self.assets / (layer+'_groups.json')).write_text(json.dumps({'10': [1, 2]}))
        self.manifest()

    def clear(self):
        for function in (maps.verify_assets, maps._layer, maps._groups):
            function.cache_clear()

    def manifest(self):
        data = {name: {'sha256': hashlib.sha256((self.assets/name).read_bytes()).hexdigest()}
                for name in maps.ASSET_NAMES}
        (self.assets/'regional_manifest.json').write_text(json.dumps(data))

    def plan(self, *notices):
        return RegionMap('weather', '気象情報', '', tuple(notices), '')

    def test_group_keeps_every_region_without_mutating_old_assets(self):
        frozen = old.ASSETS
        fig = base._figure()
        self.addCleanup(fig.clear)
        ax = maps.draw_map(fig, self.plan(RegionNotice(10, '全域', ('試験',), '#ffce6c')))
        ids = {patch.get_gid() for patch in ax.patches}
        self.assertTrue({'region-area-1', 'region-area-2'} <= ids)
        self.assertIs(old.ASSETS, frozen)
        self.assertEqual(old.LAYERS, ('marine', 'tsunami', 'prefectures'))

    def test_single_missing_region_rejects_the_whole_plan_before_drawing(self):
        fig = base._figure()
        self.addCleanup(fig.clear)
        with self.assertRaises(ValueError):
            maps.draw_map(fig, self.plan(RegionNotice(1, '既知', (), '#ffce6c'),
                                         RegionNotice(999, '未知', (), '#ffce6c')))
        self.assertFalse(fig.axes)

    def test_parent_child_with_conflicting_colours_does_not_hide_one_warning(self):
        fig = base._figure()
        self.addCleanup(fig.clear)
        with self.assertRaises(ValueError):
            maps.draw_map(fig, self.plan(RegionNotice(10, '親', (), '#ffce6c'),
                                         RegionNotice(1, '子', (), '#6ce2ec')))
        self.assertFalse(fig.axes)

    def test_corrupted_asset_is_a_service_error(self):
        path = self.assets/'weather.geojson.zlib'
        path.write_bytes(path.read_bytes()+b'bad')
        with self.assertRaises(RuntimeError):
            maps.verify_assets()

    def test_compressed_asset_rejects_trailing_content(self):
        path = self.assets/'weather.geojson.zlib'
        path.write_bytes(path.read_bytes()+b'bad')
        self.manifest()
        with self.assertRaises(RuntimeError):
            maps._layer('weather')

    def test_cyclic_groups_cannot_be_expanded(self):
        (self.assets/'weather_groups.json').write_text('{"10":[11],"11":[10]}')
        self.manifest()
        with self.assertRaises(ValueError):
            maps._expand('weather', 10)


if __name__ == '__main__':
    unittest.main()
