"""Official forecast positions cannot be confused with table figure numbers."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from azarashi.definitions.qzss.dcr.coastal_region import coastal_region_en
from tools.assets.prepare_nwp_assets import coordinate, parse_points, prepare

ASSETS = Path(__file__).resolve().parents[2] / 'image_service/assets/preview'


def table(points):
    """A complete source-shaped table, including the official figure numbers."""
    def position(value, positive, negative):
        return f'{abs(value):.1f}{positive if value >= 0 else negative}'
    rows = []
    for point in points:
        cells = (point['coastal_block'], point['name'],
                 position(point['latitude'], 'N', 'S'),
                 position(point['longitude'], 'E', 'W'), str(point['figure_number']))
        rows.append('<tr>' + ''.join('<td>' + value + '</td>' for value in cells) + '</tr>')
    return ('<table>' + ''.join(rows) + '</table>').encode()


class NWPAssetTests(unittest.TestCase):
    def setUp(self):
        self.asset = json.loads((ASSETS / 'nwp_points.json').read_text())
        self.points = self.asset['points']
        self.by_code = {point['code']: point for point in self.points}

    def test_all_specific_dcr_locations_have_one_hash_verified_official_point(self):
        manifest = json.loads((ASSETS / 'nwp_manifest.json').read_text())
        for name in ('nwp_points.json', 'NWP_NOTICE.txt'):
            raw = (ASSETS / name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest[name]['sha256'])
            self.assertEqual(len(raw), manifest[name]['bytes'])
        self.assertEqual(len(self.points), 61)
        self.assertEqual(len(self.by_code), 61)
        self.assertEqual(set(self.by_code), set(coastal_region_en) - {99, 100})
        self.assertEqual({point['figure_number'] for point in self.points}, set(range(1, 62)))
        self.assertEqual(len({point['name'] for point in self.points}), 61)
        for code in (0, 42, 65, 73, 87, 96, 99, 100):
            self.assertNotIn(code, self.by_code)
        for point in self.points:
            self.assertEqual(point['decoder_name'], coastal_region_en[point['code']])
            self.assertTrue(-90 < point['latitude'] < 90)
            self.assertTrue(-180 <= point['longitude'] <= 180)

    def test_dcr_codes_are_not_the_official_figure_numbers(self):
        expected = {
            1: ('UST_KAMCHATSK', 2, 162.6, 56.1),
            66: ('OSTROV_KARAGINSKIY', 1, 164.5, 58.8),
            25: ('RABAUL', 38, 152.3, -4.2),
            26: ('KAVIENG', 37, 150.7, -2.5),
            29: ('GUAM', 43, 144.7, 13.4),
            30: ('SAIPAN', 42, 145.8, 15.3),
            74: ('HOMEL', 19, 120.4, 24.2),
            83: ('PORT_MORESBY', 61, 147.2, -9.5),
            84: ('SHANGHAI', 13, 122.3, 31.2),
        }
        for code, values in expected.items():
            with self.subTest(code=code):
                point = self.by_code[code]
                self.assertEqual((point['name'], point['figure_number'],
                                  point['longitude'], point['latitude']), values)

    def test_southern_hemisphere_points_keep_negative_latitudes(self):
        self.assertEqual(self.by_code[17]['latitude'], -0.8)  # Sorong, south of equator.
        self.assertEqual(self.by_code[16]['latitude'], 0.4)   # Patani, north of equator.
        self.assertEqual(self.by_code[80]['latitude'], -10.6)
        self.assertEqual(coordinate('00.8S', 'latitude'), -0.8)
        self.assertEqual(coordinate('147.2E', 'longitude'), 147.2)
        self.assertEqual(coordinate('147.2W', 'longitude'), -147.2)
        for value, axis in (('90.1N', 'latitude'), ('180.1E', 'longitude'),
                            ('12.0E', 'latitude'), ('12.0N', 'longitude'),
                            ('-12.0S', 'latitude'), ('NaNN', 'latitude'),
                            ('12.0Q', 'latitude'), ('12.0N', 'altitude')):
            with self.subTest(value=value, axis=axis), self.assertRaises(ValueError):
                coordinate(value, axis)

    def test_table_reordering_does_not_reassign_any_forecast_point(self):
        # Code order is already different from table order. Reversing it also
        # protects against implementations that enumerate rows as DCR codes.
        self.assertEqual(parse_points(table(reversed(self.points))), self.asset)

    def test_coastal_block_rowspans_are_carried_to_following_points(self):
        # The real HTML only names the block on its first row. Removing the
        # repeated cells must not lose or substitute the later point's block.
        rows = []
        previous = None
        for point in sorted(self.points, key=lambda row: row['figure_number']):
            row = table([point]).decode().removeprefix('<table>').removesuffix('</table>')
            block = point['coastal_block']
            if block == previous:
                row = row.replace('<td>' + block + '</td>', '', 1)
            previous = block
            rows.append(row)
        self.assertEqual(parse_points(('<table>' + ''.join(rows) + '</table>').encode()), self.asset)

    def test_changed_missing_duplicated_or_wrong_block_points_are_rejected(self):
        wrong_block = copy.deepcopy(self.points)
        wrong_block[0]['coastal_block'] = 'TAIWAN'
        wrong_name = copy.deepcopy(self.points)
        wrong_name[0]['name'] = 'UNKNOWN_POSITION'
        same_number = copy.deepcopy(self.points)
        same_number[0]['figure_number'] = same_number[1]['figure_number']
        for points in (self.points[:-1], self.points + [self.points[0]],
                       wrong_block, wrong_name, same_number):
            with self.subTest(points=points[0]['name']), self.assertRaises(ValueError):
                parse_points(table(points))

    def test_unreviewed_source_is_rejected_before_any_files_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'preview'
            with self.assertRaisesRegex(ValueError, 'Official source changed'):
                prepare(table(self.points), target)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
