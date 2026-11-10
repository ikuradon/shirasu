"""Forecast reach assets preserve exact DCR identities and disconnected lines."""

from collections import Counter, defaultdict
import hashlib
import importlib.util
from itertools import combinations
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from azarashi.definitions.qzss.dcr.flood_forecast_region import flood_forecast_region
from scripts import prepare_flood_assets as builder

ASSETS = Path(__file__).resolve().parents[1] / 'image_service/assets/preview'
HAS_SHAPELY = importlib.util.find_spec('shapely') is not None

# The reviewed source contains short overlaps between these nominal reaches.
# Keep this audit explicit: two warning colours cannot faithfully occupy the
# same line, and a later source revision may need more than an exact-edge guard.
SHARED_REACH_SEGMENTS = {
    ('820209000101', '820209000102'): 18,  # 雄物川上流 / 下流
    ('830303000102', '830303000103'): 11,  # 利根川下流部 / 中流部
    ('850509000102', '850509000103'): 2,   # 木曽川下流 / 中流
    ('850509000402', '850509000403'): 13,  # 長良川下流 / 中流
    ('870705000101', '870705000102'): 38,  # 江の川上流 / 下流
}


class FloodAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ASSETS / 'flood_manifest.json').read_text())
        cls.compressed = (ASSETS / 'flood.geojson.zlib').read_bytes()
        cls.raw = zlib.decompress(cls.compressed)
        cls.data = json.loads(cls.raw)
        cls.features = {feature['properties']['code']: feature for feature in cls.data['features']}

    def test_all_specific_forecast_reaches_match_without_assigning_other_rivers(self):
        expected = {f'{code:012d}' for code, name in flood_forecast_region.items() if 'その他の河川' not in name}
        self.assertEqual(len(expected), 338)
        self.assertEqual(set(self.features), expected)
        self.assertEqual(len(self.data['features']), len(self.features), 'Duplicate codes hide reaches')
        self.assertEqual(self.data['type'], 'FeatureCollection')
        excluded = self.manifest['flood.geojson.zlib']['excluded_non_specific_codes']
        self.assertEqual(len(excluded), 57)
        self.assertEqual(set(excluded), {f'{code:012d}' for code in builder.NON_SPECIFIC})
        self.assertTrue(set(self.features).isdisjoint(excluded))
        for code, feature in self.features.items():
            self.assertEqual(feature['properties']['name'], flood_forecast_region[int(code)])
        self.assertEqual(self.manifest['flood.geojson.zlib']['excluded_type_1_features'], 23820)

    def test_leading_zeros_survive_and_codes_remain_forecast_regions(self):
        leading = [code for code in self.features if code.startswith('0')]
        self.assertEqual(len(leading), 7)
        self.assertIn('010175000100', self.features)
        self.assertEqual(self.features['010175000100']['properties']['name'], '新川(北海道道央)')
        for code in self.features:
            self.assertEqual(len(code), 12)
            self.assertTrue(code.isascii() and code.isdigit())

    def test_source_and_artifact_hashes_and_expansion_limit_are_pinned(self):
        entry = self.manifest['flood.geojson.zlib']
        self.assertEqual(entry['source'], builder.SOURCE)
        self.assertEqual(entry['source_sha256'], builder.SOURCE_SHA256)
        self.assertEqual(entry['source_coordinate_system'], 'JGD2000')
        for name in ('flood.geojson.zlib', 'FLOOD_NOTICE.txt'):
            raw = (ASSETS / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), self.manifest[name]['sha256'])
            self.assertEqual(len(raw), self.manifest[name]['bytes'])
        self.assertEqual(len(self.raw), entry['uncompressed_bytes'])
        self.assertLessEqual(len(self.raw), builder.MAX_EXPANDED_BYTES)
        decoder = zlib.decompressobj()
        self.assertEqual(decoder.decompress(self.compressed, builder.MAX_EXPANDED_BYTES + 1), self.raw)
        self.assertTrue(decoder.eof)
        self.assertFalse(decoder.unused_data or decoder.unconsumed_tail)

    def test_every_original_component_and_vertex_is_retained(self):
        statistics = self.manifest['flood.geojson.zlib']['geometry']
        self.assertEqual(set(statistics), set(self.features))
        for code, feature in self.features.items():
            lines, stats = feature['geometry']['coordinates'], statistics[code]
            self.assertEqual(feature['geometry']['type'], 'MultiLineString')
            self.assertEqual(len(lines), stats['source_components'])
            self.assertEqual(sum(map(len, lines)), stats['source_points'])
            self.assertEqual(stats['components'], stats['source_components'])
            self.assertEqual(stats['points'], stats['source_points'])
        self.assertEqual(statistics['810101000100']['source_components'], 4,
                         '天塩川 has separate source line parts; do not connect them')
        self.assertGreater(sum(stats['components'] > 1 for stats in statistics.values()), 0)

    def test_all_reaches_have_finite_nonempty_geographic_lines(self):
        for feature in self.features.values():
            self.assertTrue(feature['geometry']['coordinates'])
            for line in feature['geometry']['coordinates']:
                self.assertGreaterEqual(len(line), 2)
                self.assertTrue(any(point != line[0] for point in line))
                for point in line:
                    self.assertEqual(len(point), 2)
                    lon, lat = point
                    self.assertTrue(math.isfinite(lon) and math.isfinite(lat))
                    self.assertTrue(120 < lon < 155 and 20 < lat < 47)

    def test_known_overlapping_reaches_are_detectable_by_exact_source_edges(self):
        owners = defaultdict(set)
        for code, feature in self.features.items():
            for line in feature['geometry']['coordinates']:
                for start, end in zip(line, line[1:]):
                    if start != end:
                        edge = tuple(sorted((tuple(start), tuple(end))))
                        owners[edge].add(code)
        shared = Counter()
        for codes in owners.values():
            shared.update(combinations(sorted(codes), 2))
        self.assertEqual(shared, SHARED_REACH_SEGMENTS)

    @unittest.skipUnless(HAS_SHAPELY, 'Optional GIS audit requires Shapely in the asset-builder environment')
    def test_geometric_line_overlaps_have_no_pairs_missed_by_exact_edges(self):
        from shapely import STRtree
        from shapely.geometry import shape
        codes = sorted(self.features)
        lines = [shape(self.features[code]['geometry']) for code in codes]
        tree = STRtree(lines)
        overlaps = set()
        for first, line in enumerate(lines):
            for second in tree.query(line):
                if second <= first:
                    continue
                # Points where rivers merely meet/cross have zero length.
                # Positive line length also catches partially shared segments
                # whose vertices differ, independently of the cheap edge audit.
                if line.intersection(lines[second]).length > 0:
                    overlaps.add((codes[first], codes[second]))
        self.assertEqual(overlaps, set(SHARED_REACH_SEGMENTS))

    def test_conversion_keeps_short_and_disconnected_parts_without_a_join(self):
        shape = SimpleNamespace(shapeType=3, parts=[0, 3],
            points=[(140.1, 35.2), (140.10001, 35.20001), (140.10002, 35.20002),
                    (141.1, 36.2), (141.2, 36.3)])
        feature, stats = builder.convert('010175000100', shape)
        self.assertEqual(feature['geometry']['coordinates'], [
            [[140.1, 35.2], [140.10001, 35.20001], [140.10002, 35.20002]],
            [[141.1, 36.2], [141.2, 36.3]]])
        self.assertEqual(stats, {'source_components': 2, 'source_points': 5, 'components': 2, 'points': 5})

    def test_unknown_or_non_specific_code_never_gets_a_location(self):
        self.assertEqual(builder.forecast_code('010175000100'), '010175000100')
        for code in ('10175000100', '019999999999', '999999999999', '', '０１０１７５０００１００', 10175000100):
            with self.subTest(code=code), self.assertRaises(ValueError):
                builder.forecast_code(code)

    def test_invalid_line_shapes_are_rejected(self):
        cases = [
            (5, [0], [(140, 35), (141, 36)]),
            (3, [], []),
            (3, [1], [(140, 35), (141, 36)]),
            (3, [0, 0], [(140, 35), (141, 36)]),
            (3, [0, 1], [(140, 35), (141, 36)]),
            (3, [0, 2], [(140, 35), (141, 36)]),
            (3, [0], [(140, 35), (140, 35)]),
            (3, [0], [(140, 35), (float('nan'), 36)]),
            (3, [0], [(140, 35), (141, float('inf'))]),
            (3, [0], [(140, 35), (181, 36)]),
            (3, [0], [(140, 35), (141, 90)]),
            (3, [0], [(140, 35), (141, 36, 0)]),
        ]
        for kind, parts, points in cases:
            with self.subTest(kind=kind, parts=parts, points=points), self.assertRaises(ValueError):
                builder.convert('010175000100', SimpleNamespace(shapeType=kind, parts=parts, points=points))

    def test_changed_source_is_rejected_before_creating_assets(self):
        versions = {'azarashi': '0.17.1', 'pyshp': '2.3.1'}
        with tempfile.TemporaryDirectory() as directory, patch.object(builder, 'version', side_effect=versions.__getitem__):
            source, output = Path(directory) / 'changed.zip', Path(directory) / 'out'
            source.write_bytes(b'not the reviewed source')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                builder.prepare(source, output)
            self.assertFalse(output.exists())

    def test_incomplete_and_duplicate_coverage_are_rejected_before_writing(self):
        shape = SimpleNamespace(shapeType=3, parts=[0], points=[(140, 35), (141, 36)])
        versions = {'azarashi': '0.17.1', 'pyshp': '2.3.1'}
        for rows, message in (([('010175000100', shape)], 'Incomplete'),
                              ([('010175000100', shape)] * 2, 'Duplicate')):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory, \
                    patch.object(builder, 'version', side_effect=versions.__getitem__), \
                    patch.object(builder, 'file_hash', return_value=builder.SOURCE_SHA256), \
                    patch.object(builder, 'source_features', return_value=iter(rows)):
                output = Path(directory) / 'out'
                with self.assertRaisesRegex(ValueError, message):
                    builder.prepare('unused.zip', output)
                self.assertFalse(output.exists())

    @unittest.skipUnless(HAS_SHAPELY, 'Optional GIS audit requires Shapely in the asset-builder environment')
    def test_all_final_multilines_are_valid(self):
        from shapely.geometry import shape
        for code, feature in self.features.items():
            with self.subTest(code=code):
                geometry = shape(feature['geometry'])
                self.assertTrue(geometry.is_valid)
                self.assertFalse(geometry.is_empty)


if __name__ == '__main__':
    unittest.main()
