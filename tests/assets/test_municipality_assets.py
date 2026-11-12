"""Municipal geometry keeps DCR area identities, islands and provenance."""

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from azarashi.definitions.qzss.dcr.local_government import local_government
from tools.assets import prepare_municipality_assets as builder

ASSETS = Path(__file__).resolve().parents[2] / 'image_service/assets/preview'
HAS_SHAPELY = importlib.util.find_spec('shapely') is not None


class MunicipalityAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ASSETS / 'municipalities_manifest.json').read_text())
        cls.compressed = (ASSETS / 'municipalities.geojson.zlib').read_bytes()
        cls.raw = zlib.decompress(cls.compressed)
        cls.data = json.loads(cls.raw)
        cls.features = {int(feature['properties']['code']): feature for feature in cls.data['features']}

    def test_every_concrete_dcr_area_is_mapped_without_inventing_other_municipalities(self):
        expected = {code for code, name in local_government.items() if 'その他の市町村' not in name}
        self.assertEqual(len(expected), 1745)
        self.assertEqual(set(self.features), expected)
        self.assertEqual(len(self.data['features']), len(self.features), 'Duplicate codes hide features')
        self.assertEqual(len(builder.NON_SPECIFIC), 47)
        self.assertTrue(set(self.features).isdisjoint(builder.NON_SPECIFIC))
        for code, feature in self.features.items():
            self.assertEqual(feature['properties']['name'], local_government[code])
        self.assertIn(110000, self.features, 'Sapporo is one transmitted city, not its wards')
        self.assertIn(1310100, self.features, 'Tokyo special wards remain separately transmitted areas')
        self.assertEqual(self.features[1340000]['properties']['name'], '東京都八丈支庁')

    def test_source_and_artifact_hashes_and_size_limit_are_pinned(self):
        entry = self.manifest['municipalities.geojson.zlib']
        self.assertEqual(entry['source'], builder.SOURCE)
        self.assertEqual(entry['source_sha256'], builder.SOURCE_SHA256)
        self.assertEqual(entry['source_coordinate_system'], 'JGD2011')
        for name in ('municipalities.geojson.zlib', 'MUNICIPALITIES_NOTICE.txt'):
            raw = (ASSETS / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), self.manifest[name]['sha256'])
            self.assertEqual(len(raw), self.manifest[name]['bytes'])
        self.assertEqual(len(self.raw), entry['uncompressed_bytes'])
        self.assertLessEqual(len(self.raw), 64 * 1024 * 1024)
        decoder = zlib.decompressobj()
        self.assertEqual(decoder.decompress(self.compressed, 64 * 1024 * 1024 + 1), self.raw)
        self.assertTrue(decoder.eof)
        self.assertFalse(decoder.unused_data or decoder.unconsumed_tail)

    def test_simplification_preserves_components_and_holes_and_records_any_repair(self):
        statistics = self.manifest['municipalities.geojson.zlib']['geometry']
        self.assertEqual(set(map(int, statistics)), set(self.features))
        for code, feature in self.features.items():
            stats = statistics[str(code)]
            polygons = feature['geometry']['coordinates']
            self.assertEqual(feature['geometry']['type'], 'MultiPolygon')
            self.assertEqual(stats['pre_repair_components'], stats['source_components'])
            self.assertEqual(stats['pre_repair_holes'], stats['source_holes'])
            self.assertEqual(len(polygons), stats['components'])
            self.assertEqual(sum(len(polygon) - 1 for polygon in polygons), stats['holes'])
            self.assertLessEqual(stats['relative_area_change'], .01)
            self.assertGreater(stats['simplification_tolerance_degrees'], 0)
            self.assertLessEqual(stats['simplification_tolerance_degrees'], .002)
        self.assertGreater(statistics['1342100']['source_components'], 1,
                           'Ogasawara must retain disconnected islands')

    def test_coordinates_and_anchors_are_finite_geographic_positions(self):
        for feature in self.features.values():
            anchor = feature['properties']['anchor']
            self.assertEqual(len(anchor), 2)
            self.assertTrue(all(type(value) in (int, float) and math.isfinite(value) for value in anchor))
            self.assertTrue(120 < anchor[0] < 155 and 20 < anchor[1] < 47)
            for polygon in feature['geometry']['coordinates']:
                self.assertTrue(polygon)
                for ring in polygon:
                    self.assertGreaterEqual(len(ring), 4)
                    self.assertEqual(ring[0], ring[-1])
                    for lon, lat in ring:
                        self.assertTrue(math.isfinite(lon) and math.isfinite(lat))
                        self.assertTrue(120 < lon < 155 and 20 < lat < 47)

    def test_names_and_codes_require_explicit_agreement(self):
        self.assertEqual(builder.municipality_code('0110000', '北海道札幌市'), 110000)
        self.assertEqual(builder.municipality_code('1340000', '東京都のうち八丈支庁管内'), 1340000)
        self.assertEqual(builder.municipality_code('4620100', '鹿児島県鹿児島市'), 4620100)
        for code, name in (('0462010', '鹿児島県鹿児島市'), ('46201', '鹿児島県鹿児島市'),
                           ('4620100', '鹿児島県垂水市'), ('4699999', '鹿児島県のその他の市町村'),
                           ('', ''), ('９９９９９９９', ''), (4620100, '鹿児島県鹿児島市')):
            with self.subTest(code=code), self.assertRaises(ValueError):
                builder.municipality_code(code, name)

    def test_uncoded_land_is_recorded_as_unassigned_not_guessed_from_proximity(self):
        entry = self.manifest['municipalities.geojson.zlib']
        self.assertEqual(set(entry['excluded_uncoded_source_names']), set(builder.UNASSIGNED_NAMES))
        self.assertEqual(len(entry['excluded_uncoded_source_names']), 13)
        self.assertEqual(set(entry['excluded_non_specific_codes']), builder.NON_SPECIFIC)
        self.assertEqual(entry['name_aliases'], {'1340000': '東京都のうち八丈支庁管内'})

    def test_changed_source_is_rejected_before_creating_assets(self):
        versions = {'azarashi': '0.17.1', 'pyshp': '2.3.1', 'shapely': '2.1.2'}
        with tempfile.TemporaryDirectory() as directory, patch.object(builder, 'version', side_effect=versions.__getitem__):
            source, output = Path(directory) / 'changed.zip', Path(directory) / 'out'
            source.write_bytes(b'not the reviewed source')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                builder.prepare(source, output)
            self.assertFalse(output.exists())

    @unittest.skipUnless(HAS_SHAPELY, 'Optional GIS verification requires the asset-builder environment')
    def test_all_final_polygons_are_valid_and_each_anchor_is_inside_its_main_polygon(self):
        from shapely.geometry import Point, shape
        for code, feature in self.features.items():
            with self.subTest(code=code):
                polygon = shape(feature['geometry'])
                self.assertTrue(polygon.is_valid)
                self.assertFalse(polygon.is_empty)
                main = max(polygon.geoms, key=lambda component: component.area)
                self.assertTrue(main.contains(Point(feature['properties']['anchor'])))


if __name__ == '__main__':
    unittest.main()
