#!/usr/bin/env python3
"""Build DCX-only ward boundaries from pinned official JMA earthquake GIS.

Use azarashi==0.17.1, pyshp==2.3.1 and shapely==2.1.2 in an asset environment.
The existing volcanic municipality asset and published renderers stay frozen.
"""

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from azarashi.definitions.qzss.dcx.ex1_target_area_code import ex1_target_area_code_ja as NAMES
from tools.assets.prepare_municipality_assets import file_hash, polygon_parts

SOURCE = 'https://www.data.jma.go.jp/developer/gis/20241128_AreaInformationCity_quake_GIS.zip'
SOURCE_SHA256 = '56188bee85d4865f9d6da16e3c535a94bb9c93d0729d490e0121015de804ac65'
TERMS = 'https://www.jma.go.jp/jma/kishou/info/coment.html'
DATE = '2026-11-11'
# Retired codes must not borrow the current city's or a same-named ward's shape.
HISTORICAL_CODES = frozenset((22131, 22132, 22133, 22134, 22135, 22136, 22137,
                            27101, 27105, 27110, 27112, 28103, 28104, 40102, 40104))
WARD_CODES = frozenset(code for code, name in NAMES.items() if '市' in name and name.endswith('区'))
EXPECTED_CODES = WARD_CODES - HISTORICAL_CODES
SOURCE_CODES = frozenset(f'{code * 100:07d}' for code in EXPECTED_CODES)
MAX_BYTES = 8 * 1024 * 1024

NOTICE = '''JMA earthquake/tsunami municipality GIS / 気象庁の政令市区境界を加工

出典: 気象庁「予報区等GISデータの一覧」市町村等（地震津波関係）
https://www.data.jma.go.jp/developer/gis.html
https://www.data.jma.go.jp/developer/gis/20241128_AreaInformationCity_quake_GIS.zip
2026年11月11日取得。入力SHA-256は dcx_wards_manifest.json に記録。
気象庁ホームページの利用規約（公共データ利用規約 第1.0版）に基づき利用。
https://www.jma.go.jp/jma/kishou/info/coment.html
気象庁の公表データを Shirasu が加工して作成。気象庁作成の画像ではない。

配布ページに明記されたJGD2011経緯度を保持。regioncodeをDCXのJISコード×100と
直接照合し、regionnameを「都道府県名＋市名＋のうち＋区名」と照合する。
現行20政令市171区のみを収録。DCX定義に残る旧15区は未収録としてmanifestに明記し、
現行区や市全域への代替・合成を行わない。同名の旧大阪市北区・旧浜松市天竜区も区別する。
他の市町村・コードのない地物は採用しない。DCR用の既存市町村アセットは変更しない。

Shapely2.1.2 simplify(preserve_topology=True)、最大許容値0.002度で簡略化。
面積差1%以下まで許容値を半減し、polygon成分・穴の個数を保存する。
必要な微小修復も成分・穴を保存し、処理統計をmanifestへ記録。離島の除去・座標丸めなし。
隣接区の境界は個別簡略化による細いずれを含み得る。区域は受信した通報の対象であり、
被害範囲や区全域への均一な影響を示さない。未指定区域の安全も示さない。

再生成（指定バージョンのGIS環境）:
  python tools/assets/prepare_dcx_ward_assets.py --source /path/to/municipalities-quake.zip
--source省略時は固定URLを取得する。--outputで出力先を指定できる。
未公開DCXプレビュー専用。描画時の外部データ取得は行わない。
'''


def ward_code(code, region_name):
    if not isinstance(code, str) or len(code) != 7 or not code.isascii() or not code.isdigit():
        raise ValueError('Invalid official ward code')
    number = int(code)
    raw_code, suffix = divmod(number, 100)
    if suffix or raw_code not in EXPECTED_CODES:
        raise ValueError('Unknown or historical ward code')
    expected = NAMES[raw_code // 1000 * 1000] + NAMES[raw_code].replace('市', '市のうち', 1)
    if region_name != expected:
        raise ValueError(f'Ward code/name mismatch: {code}')
    return raw_code


def source_features(path):
    import shapefile
    from shapely.geometry import shape
    with tempfile.TemporaryDirectory(prefix='shirasu-dcx-wards-') as directory:
        prefix = Path(directory) / 'wards'
        with zipfile.ZipFile(path) as archive:
            for extension in ('shp', 'shx', 'dbf'):
                names = [name for name in archive.namelist() if name.endswith('.' + extension)]
                if len(names) != 1:
                    raise ValueError('Ambiguous ward source shapefile')
                with archive.open(names[0]) as source, prefix.with_suffix('.' + extension).open('wb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
        with shapefile.Reader(str(prefix), encoding='utf-8') as reader:
            for index, record in enumerate(reader.iterRecords()):
                # Select only reviewed exact codes; never infer membership by proximity or name.
                if record.regioncode not in SOURCE_CODES:
                    continue
                code = ward_code(record.regioncode, record.regionname)
                yield code, record.regionname, shape(reader.shape(index).__geo_interface__)


def convert(code, source_name, original):
    from shapely import make_valid
    from shapely.geometry import MultiPolygon, mapping
    parts = polygon_parts(original)
    holes = sum(len(part.interiors) for part in parts)
    if original.is_empty or original.area <= 0:
        raise ValueError('Empty ward geometry')
    tolerance = .002
    while True:
        simplified = original.simplify(tolerance, preserve_topology=True)
        if abs(simplified.area - original.area) / original.area <= .01:
            break
        tolerance /= 2
        if tolerance < .000000125:
            raise ValueError(f'Cannot preserve ward area: {code}')
    simplified = MultiPolygon(polygon_parts(simplified))
    before_repair = simplified
    if not simplified.is_valid:
        simplified = MultiPolygon(polygon_parts(make_valid(simplified, method='structure')))
    repair_delta = abs(simplified.area - before_repair.area)
    for geometry in (before_repair, simplified):
        if len(geometry.geoms) != len(parts) or sum(len(p.interiors) for p in geometry.geoms) != holes:
            raise ValueError(f'Ward processing changed components or holes: {code}')
    if (not simplified.is_valid or simplified.is_empty or repair_delta / original.area >= 1e-6
            or abs(simplified.area - original.area) / original.area > .01):
        raise ValueError(f'Unexpected ward topology repair: {code}')
    feature = {'type': 'Feature', 'properties': {'code': str(code * 100), 'name': NAMES[code],
               'source_name': source_name}, 'geometry': mapping(simplified)}
    stats = {'source_components': len(parts), 'source_holes': holes,
             'components': len(simplified.geoms), 'holes': holes, 'source_valid': original.is_valid,
             'relative_area_change': abs(simplified.area - original.area) / original.area,
             'simplification_tolerance_degrees': tolerance,
             'repair_area_change_square_degrees': repair_delta}
    return feature, stats


def prepare(source, output):
    for name, expected in (('azarashi', '0.17.1'), ('pyshp', '2.3.1'), ('shapely', '2.1.2')):
        if version(name) != expected:
            raise ValueError(f'Use pinned {name}=={expected}')
    if file_hash(source) != SOURCE_SHA256:
        raise ValueError('Official ward source changed; review before updating')
    if len(EXPECTED_CODES) != 171 or len(WARD_CODES) != 186:
        raise ValueError('DCX ward definitions changed; review before updating')
    features, statistics = {}, {}
    for code, source_name, geometry in source_features(source):
        if code in features:
            raise ValueError('Duplicate ward source code')
        features[code], statistics[str(code * 100)] = convert(code, source_name, geometry)
    if set(features) != EXPECTED_CODES:
        raise ValueError('Incomplete ward code coverage')
    raw = (json.dumps({'type': 'FeatureCollection', 'features': [features[c] for c in sorted(features)]},
                     ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('Ward asset exceeds runtime expansion limit')
    compressed, notice = zlib.compress(raw, 9), NOTICE.encode()
    manifest = {
        'dcx_wards.geojson.zlib': {
            'source': SOURCE, 'source_sha256': SOURCE_SHA256, 'retrieved': DATE,
            'terms': TERMS, 'source_coordinate_system': 'JGD2011',
            'sha256': hashlib.sha256(compressed).hexdigest(), 'bytes': len(compressed),
            'uncompressed_bytes': len(raw), 'compression': 'zlib; level=9',
            'feature_count': len(features), 'codes': sorted(code * 100 for code in features),
            'excluded_historical_codes': {str(c * 100): NAMES[c] for c in sorted(HISTORICAL_CODES)},
            'processing': 'Exact regioncode/name match; no city substitution. Adaptive topology-preserving simplification; all components and holes retained.',
            'geometry': statistics,
        },
        'DCX_WARDS_NOTICE.txt': {'sha256': hashlib.sha256(notice).hexdigest(), 'bytes': len(notice)},
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'dcx_wards.geojson.zlib').write_bytes(compressed)
    (output / 'DCX_WARDS_NOTICE.txt').write_bytes(notice)
    (output / 'dcx_wards_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/preview')
    args = parser.parse_args()
    if args.source:
        result = prepare(args.source, args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='shirasu-dcx-wards-source-') as directory:
            source = Path(directory) / 'source.zip'
            with urllib.request.urlopen(SOURCE, timeout=60) as response, source.open('wb') as target:
                shutil.copyfileobj(response, target, 1024 * 1024)
            result = prepare(source, args.output)
    entry = result['dcx_wards.geojson.zlib']
    print(f"Wrote {entry['feature_count']} wards, {entry['bytes']} compressed bytes")


if __name__ == '__main__':
    main()
