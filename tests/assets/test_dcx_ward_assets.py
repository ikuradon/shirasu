"""DCX wards retain exact administrative identities and every polygon component."""

from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from azarashi.definitions.qzss.dcx.ex1_target_area_code import ex1_target_area_code_ja as NAMES
from tools.assets import prepare_dcx_ward_assets as builder

ASSETS = Path(__file__).resolve().parents[2] / 'image_service/assets/preview'
HAS_SHAPELY = importlib.util.find_spec('shapely') is not None
HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('matplotlib', 'numpy'))
HISTORICAL = frozenset((22131, 22132, 22133, 22134, 22135, 22136, 22137,
                        27101, 27105, 27110, 27112, 28103, 28104, 40102, 40104))


class DcxWardAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ASSETS / 'dcx_wards_manifest.json').read_text())
        cls.compressed = (ASSETS / 'dcx_wards.geojson.zlib').read_bytes()
        cls.raw = zlib.decompress(cls.compressed)
        cls.collection = json.loads(cls.raw)
        cls.features = {int(f['properties']['code']): f for f in cls.collection['features']}

    def test_all_171_current_wards_have_exact_codes_and_names(self):
        expected = {code * 100 for code, name in NAMES.items()
                    if '市' in name and name.endswith('区') and code not in HISTORICAL}
        self.assertEqual(len(expected), 171)
        self.assertEqual(set(self.features), expected)
        self.assertEqual(len(self.collection['features']), len(expected))
        for code, feature in self.features.items():
            raw_code = code // 100
            self.assertEqual(feature['properties']['name'], NAMES[raw_code])
            expected_name = NAMES[raw_code // 1000 * 1000] + NAMES[raw_code].replace('市', '市のうち', 1)
            self.assertEqual(feature['properties']['source_name'], expected_name)
            self.assertEqual(builder.ward_code(f'{code:07d}', expected_name), raw_code)
        self.assertEqual(len({name.split('市', 1)[0] for code, name in NAMES.items()
                              if code * 100 in self.features}), 20)

    def test_supplement_is_disjoint_from_existing_dcr_municipalities(self):
        original = json.loads(zlib.decompress((ASSETS / 'municipalities.geojson.zlib').read_bytes()))
        existing = {int(f['properties']['code']) for f in original['features']}
        self.assertEqual(len(existing), 1745)
        self.assertTrue(existing.isdisjoint(self.features))
        self.assertIn(110000, existing, 'Sapporo city remains distinct from any of its wards')
        self.assertIn(110100, self.features)
        missing = {code for code in NAMES if code % 1000 and code * 100 not in existing | self.features.keys()}
        self.assertEqual(missing, HISTORICAL | {1696, 1697, 1698, 1699, 1700})

    def test_same_named_historical_wards_are_not_aliased_to_current_boundaries(self):
        for old, current in ((27101, 27127), (22137, 22140)):
            with self.subTest(old=old, current=current):
                self.assertEqual(NAMES[old], NAMES[current])
                self.assertNotIn(old * 100, self.features)
                self.assertIn(current * 100, self.features)
                with self.assertRaises(ValueError):
                    builder.ward_code(f'{old * 100:07d}', self.features[current * 100]['properties']['source_name'])
        excluded = self.manifest['dcx_wards.geojson.zlib']['excluded_historical_codes']
        self.assertEqual(excluded, {str(c * 100): NAMES[c] for c in HISTORICAL})

    def test_source_artifact_hashes_and_size_limit_are_pinned(self):
        entry = self.manifest['dcx_wards.geojson.zlib']
        self.assertEqual(entry['source'],
                         'https://www.data.jma.go.jp/developer/gis/20241128_AreaInformationCity_quake_GIS.zip')
        self.assertEqual(entry['source_sha256'],
                         '56188bee85d4865f9d6da16e3c535a94bb9c93d0729d490e0121015de804ac65')
        self.assertEqual(entry['source_coordinate_system'], 'JGD2011')
        self.assertEqual(entry['terms'], 'https://www.jma.go.jp/jma/kishou/info/coment.html')
        self.assertEqual(entry['retrieved'], '2026-11-11')
        self.assertEqual(entry['feature_count'], 171)
        self.assertEqual(entry['codes'], sorted(self.features))
        for name in ('dcx_wards.geojson.zlib', 'DCX_WARDS_NOTICE.txt'):
            raw = (ASSETS / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), self.manifest[name]['sha256'])
            self.assertEqual(len(raw), self.manifest[name]['bytes'])
        self.assertEqual(len(self.raw), entry['uncompressed_bytes'])
        self.assertLessEqual(len(self.raw), 8 * 1024 * 1024)
        decoder = zlib.decompressobj()
        self.assertEqual(decoder.decompress(self.compressed, 8 * 1024 * 1024 + 1), self.raw)
        self.assertTrue(decoder.eof)
        self.assertFalse(decoder.unused_data or decoder.unconsumed_tail)

    def test_all_source_components_and_holes_survive_simplification(self):
        stats = self.manifest['dcx_wards.geojson.zlib']['geometry']
        self.assertEqual(set(map(int, stats)), set(self.features))
        # Independently audited in the pinned original SHP, before this builder ran.
        self.assertEqual(sum(s['source_components'] for s in stats.values()), 1638)
        self.assertEqual(sum(s['source_holes'] for s in stats.values()), 6)
        self.assertEqual(sum(s['source_components'] > 1 for s in stats.values()), 50)
        for code, feature in self.features.items():
            with self.subTest(code=code):
                stat = stats[str(code)]
                polygons = feature['geometry']['coordinates']
                self.assertEqual(feature['geometry']['type'], 'MultiPolygon')
                self.assertEqual(len(polygons), stat['source_components'])
                self.assertEqual(sum(len(p) - 1 for p in polygons), stat['source_holes'])
                self.assertEqual(stat['components'], stat['source_components'])
                self.assertEqual(stat['holes'], stat['source_holes'])
                self.assertTrue(stat['source_valid'])
                self.assertLessEqual(stat['relative_area_change'], .01)
                self.assertGreater(stat['simplification_tolerance_degrees'], 0)
                self.assertLessEqual(stat['simplification_tolerance_degrees'], .002)
                for polygon in polygons:
                    self.assertTrue(polygon)
                    for ring in polygon:
                        self.assertGreaterEqual(len(ring), 4)
                        self.assertEqual(ring[0], ring[-1])
                        for lon, lat in ring:
                            self.assertTrue(math.isfinite(lon) and math.isfinite(lat))
                            self.assertTrue(120 < lon < 155 and 20 < lat < 47)

    def test_code_and_name_must_agree_without_inference(self):
        self.assertEqual(builder.ward_code('0110100', '北海道札幌市のうち中央区'), 1101)
        for code, name in (('0110100', '北海道札幌市のうち北区'),
                           ('0110101', '北海道札幌市のうち中央区'),
                           ('0110000', '北海道札幌市'),
                           ('0110100', '札幌市中央区'),
                           ('110100', '北海道札幌市のうち中央区'),
                           ('０１１０１００', '北海道札幌市のうち中央区'),
                           ('', ''), (110100, '北海道札幌市のうち中央区')):
            with self.subTest(code=code, name=name), self.assertRaises(ValueError):
                builder.ward_code(code, name)

    def test_source_change_is_rejected_before_writing_anything(self):
        versions = {'azarashi': '0.17.1', 'pyshp': '2.3.1', 'shapely': '2.1.2'}
        with tempfile.TemporaryDirectory() as directory, patch.object(builder, 'version', side_effect=versions.__getitem__):
            source, output = Path(directory) / 'changed.zip', Path(directory) / 'out'
            source.write_bytes(b'not the reviewed official GIS')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                builder.prepare(source, output)
            self.assertFalse(output.exists())

    @unittest.skipUnless(HAS_SHAPELY, 'Optional GIS verification requires the asset environment')
    def test_all_output_geometries_are_valid_and_nonempty(self):
        from shapely.geometry import shape
        for code, feature in self.features.items():
            with self.subTest(code=code):
                geometry = shape(feature['geometry'])
                self.assertTrue(geometry.is_valid)
                self.assertFalse(geometry.is_empty)
                self.assertGreater(geometry.area, 0)


@unittest.skipUnless(HAS_RENDERER, 'Runtime imports use the isolated image renderer')
class DcxWardRuntimeTests(unittest.TestCase):
    def setUp(self):
        from image_service import preview_dcx_areas
        self.areas = preview_dcx_areas
        self.areas._wards.cache_clear()
        self.areas.verify_assets.cache_clear()
        self.addCleanup(self.areas._wards.cache_clear)
        self.addCleanup(self.areas.verify_assets.cache_clear)

    @contextmanager
    def modified_asset(self, compressed, *, update_hash=True):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            manifest = json.loads((ASSETS / 'dcx_wards_manifest.json').read_text())
            entry = manifest['dcx_wards.geojson.zlib']
            if update_hash:
                entry.update(sha256=hashlib.sha256(compressed).hexdigest(), bytes=len(compressed))
            (directory / 'dcx_wards.geojson.zlib').write_bytes(compressed)
            (directory / 'DCX_WARDS_NOTICE.txt').write_bytes((ASSETS / 'DCX_WARDS_NOTICE.txt').read_bytes())
            (directory / 'dcx_wards_manifest.json').write_text(json.dumps(manifest))
            with patch.object(self.areas, 'ASSETS', directory):
                yield

    def test_exact_lookup_keeps_city_current_ward_and_old_ward_distinct(self):
        original = self.areas.ash_map._municipalities()
        self.assertIs(self.areas.municipality(110000), original[110000])
        self.assertEqual(self.areas.municipality(110100)['properties']['name'], '札幌市中央区')
        for old, current in ((2710100, 2712700), (2213700, 2214000)):
            self.assertIsNone(self.areas.municipality(old))
            self.assertIsNotNone(self.areas.municipality(current))
        self.assertIsNone(self.areas.municipality(9999999))

    def test_asset_corruption_is_a_hard_failure(self):
        compressed = (ASSETS / 'dcx_wards.geojson.zlib').read_bytes()
        with self.modified_asset(compressed + b'corruption', update_hash=False):
            with self.assertRaisesRegex(RuntimeError, 'reviewed source'):
                self.areas._wards()

    def test_decompression_is_bounded_and_rejects_truncated_or_extra_streams(self):
        compressed = (ASSETS / 'dcx_wards.geojson.zlib').read_bytes()
        cases = (('over limit', zlib.compress(b'x' * 1025), 1024),
                 ('truncated', compressed[:-2], 8 * 1024 * 1024),
                 ('extra stream', compressed + zlib.compress(b'{}'), 8 * 1024 * 1024),
                 ('trailing data', compressed + b'trailing', 8 * 1024 * 1024))
        for name, content, limit in cases:
            with self.subTest(name=name):
                self.areas._wards.cache_clear()
                self.areas.verify_assets.cache_clear()
                with self.modified_asset(content), patch.object(self.areas, 'MAX_BYTES', limit):
                    with self.assertRaisesRegex(RuntimeError, 'compressed DCX ward geometry'):
                        self.areas._wards()

    def test_duplicate_features_are_rejected(self):
        collection = json.loads(zlib.decompress((ASSETS / 'dcx_wards.geojson.zlib').read_bytes()))
        collection['features'].append(copy.deepcopy(collection['features'][0]))
        compressed = zlib.compress(json.dumps(collection).encode())
        with self.modified_asset(compressed):
            with self.assertRaisesRegex(RuntimeError, 'Duplicate or invalid'):
                self.areas._wards()

    def test_malformed_asset_data_never_becomes_a_received_area_fallback(self):
        collection = json.loads(zlib.decompress((ASSETS / 'dcx_wards.geojson.zlib').read_bytes()))
        invalid_code = copy.deepcopy(collection)
        invalid_code['features'][0]['properties']['code'] = '0110100x'
        missing_properties = copy.deepcopy(collection)
        del missing_properties['features'][0]['properties']
        cases = (
            ('invalid zlib', b'not a zlib stream'),
            ('invalid json', zlib.compress(b'{"type":')),
            ('invalid UTF-8', zlib.compress(b'\xff')),
            ('noninteger code', zlib.compress(json.dumps(invalid_code).encode())),
            ('missing properties', zlib.compress(json.dumps(missing_properties).encode())),
            ('missing features', zlib.compress(b'{"type":"FeatureCollection"}')),
            ('invalid features type', zlib.compress(b'{"type":"FeatureCollection","features":null}')),
        )
        for name, compressed in cases:
            with self.subTest(name=name):
                self.areas._wards.cache_clear()
                self.areas.verify_assets.cache_clear()
                with self.modified_asset(compressed):
                    # draw_dcx catches ValueError to try a less detailed view;
                    # invalid installed assets must escape that path instead.
                    with self.assertRaisesRegex(RuntimeError, 'asset structure'):
                        self.areas._wards()

    def test_overlap_with_dcr_assets_is_rejected(self):
        with patch.object(self.areas.ash_map, '_municipalities', return_value={110100: None}):
            with self.assertRaisesRegex(RuntimeError, 'overlapping DCX ward coverage'):
                self.areas._wards()


if __name__ == '__main__':
    unittest.main()
