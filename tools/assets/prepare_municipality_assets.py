#!/usr/bin/env python3
"""Build offline municipal boundaries from the pinned JMA volcanic GIS layer.

Use azarashi==0.17.1, pyshp==2.3.1 and shapely==2.1.2 in a separate asset
environment. These GIS dependencies are not required by the image service.
"""

import argparse
from collections import Counter
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile
import zlib

from azarashi.definitions.qzss.dcr.local_government import local_government

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'https://www.data.jma.go.jp/developer/gis/20241226_AreaInformationCity_volcano_GIS.zip'
SOURCE_SHA256 = '3cd58fc7b0a5f1aebec04a1065f4a4f73633e05185f7660196f8a7cb6159ab5e'
TERMS = 'https://www.jma.go.jp/jma/kishou/info/coment.html'
DATE = '2026-11-10'
NON_SPECIFIC = frozenset(code for code in local_government if code % 100000 == 99999)
EXPECTED_CODES = frozenset(local_government) - NON_SPECIFIC
NAME_ALIASES = {1340000: '東京都のうち八丈支庁管内'}
UNASSIGNED_NAMES = (
    '市川市・船橋市境界地先の土地', '鳥島', 'ベヨネース列岩', '須美寿島', '孀婦岩',
    '荒川河口部', '名古屋港口埋立地', '鰹島', '羽島', '鷹島(甑島南方)',
    '津倉瀬(宇治群島北東方)', 'うるま市・金武町境界部地先の埋立地',
    '中央防波堤外側廃棄物処理場(中潮橋南側）',
)

NOTICE = '''JMA volcanic municipality GIS / 気象庁の火山関係市町村境界を加工

出典: 気象庁「予報区等GISデータの一覧」市町村等（火山関係）
https://www.data.jma.go.jp/developer/gis.html
https://www.data.jma.go.jp/developer/gis/20241226_AreaInformationCity_volcano_GIS.zip
2026年11月10日取得。入力SHA-256は municipalities_manifest.json に記録。
気象庁ホームページの利用規約（公共データ利用規約 第1.0版）に基づき利用。
https://www.jma.go.jp/jma/kishou/info/coment.html
気象庁の公表データを Shirasu が加工して作成。気象庁作成の画像ではない。

元のJGD2011経緯度を保持。GISのregioncodeをDCR-017の市町村コードと直接照合し、
1745の具体的な区域と名称を検証する。1340000のみGIS名「東京都のうち八丈支庁管内」と
DCR名「東京都八丈支庁」の明示的な名称対応を用いる。「その他の市町村」47コードと、
元GISでコードが空の13地物には区域を推定・割当てしない。市町村の合成や統合はしない。

Shapely2.1.2 simplify(preserve_topology=True)で許容値最大0.002度から簡略化。
元形状との面積差が1%以下になるまで許容値を半減する。簡略化でpolygon成分・穴が
失われないことを確認。残る微小不整合はmake_valid(method="structure")で修復し、
面積差・polygon数・穴数・実際の許容値をmanifestに記録する。離島の除去や座標丸めはしない。
隣接境界は個別簡略化に伴う細いずれを含み得る。代表点は最大polygon内に置く描画上の印で、
灰や噴石の観測位置ではない。境界は予報対象市町村の文脈であり、市町村全域の降灰・
噴石落下、実際の分布・範囲、未掲載市町村の安全を示さない。

再生成（指定バージョンのGIS環境で実行）:
  python tools/assets/prepare_municipality_assets.py --source /path/to/municipalities-volcano.zip
--sourceを省略すると上記の固定URLを取得する。--outputで出力先を指定できる。
データは未公開プレビュー用。描画時には外部の地図や予報を取得しない。
'''


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def municipality_code(code, region_name):
    if not isinstance(code, str) or len(code) != 7 or not code.isascii() or not code.isdigit():
        raise ValueError('Invalid official municipality code')
    number = int(code)
    if number not in EXPECTED_CODES:
        raise ValueError('Unknown or non-specific municipality code')
    expected_name = NAME_ALIASES.get(number, local_government[number])
    if region_name != expected_name:
        raise ValueError(f'Municipality code/name mismatch: {code}')
    return number


def polygon_parts(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry]
    if geometry.geom_type == 'MultiPolygon':
        return list(geometry.geoms)
    raise ValueError('Municipality geometry must be polygonal')


def convert(code, original):
    from shapely import make_valid
    from shapely.geometry import MultiPolygon, mapping
    original_parts = polygon_parts(original)
    original_holes = sum(len(part.interiors) for part in original_parts)
    if original.is_empty or original.area <= 0:
        raise ValueError('Empty municipality geometry')
    tolerance = 0.002
    while True:
        simplified = original.simplify(tolerance, preserve_topology=True)
        if abs(simplified.area - original.area) / original.area <= 0.01:
            break
        tolerance /= 2
        if tolerance < 0.000000125:
            raise ValueError(f'Cannot preserve municipality area: {code}')
    simplified = MultiPolygon(polygon_parts(simplified))
    pre_holes = sum(len(part.interiors) for part in simplified.geoms)
    if len(simplified.geoms) != len(original_parts) or pre_holes != original_holes:
        raise ValueError(f'Simplification dropped polygon components or holes: {code}')
    before_repair = simplified
    if not simplified.is_valid:
        simplified = MultiPolygon(polygon_parts(make_valid(simplified, method='structure')))
    repair_delta = abs(simplified.area - before_repair.area)
    if (not simplified.is_valid or simplified.is_empty or repair_delta / original.area >= 1e-6
            or abs(simplified.area - original.area) / original.area > 0.01):
        raise ValueError(f'Unexpected municipality topology repair: {code}')
    anchor = max(simplified.geoms, key=lambda part: part.area).representative_point()
    feature = {'type': 'Feature', 'properties': {'code': str(code), 'name': local_government[code],
               'anchor': [anchor.x, anchor.y]}, 'geometry': mapping(simplified)}
    statistics = {
        'source_components': len(original_parts), 'source_holes': original_holes,
        'components': len(simplified.geoms),
        'holes': sum(len(part.interiors) for part in simplified.geoms),
        'source_valid': original.is_valid,
        'relative_area_change': abs(simplified.area - original.area) / original.area,
        'simplification_tolerance_degrees': tolerance,
        'repair_area_change_square_degrees': repair_delta,
        'pre_repair_components': len(before_repair.geoms), 'pre_repair_holes': pre_holes,
    }
    return feature, statistics


def source_features(path):
    import shapefile
    from shapely.geometry import shape
    with tempfile.TemporaryDirectory(prefix='shirasu-municipality-gis-') as directory:
        prefix = Path(directory) / 'municipalities'
        with zipfile.ZipFile(path) as archive:
            for extension in ('shp', 'shx', 'dbf'):
                names = [name for name in archive.namelist() if name.endswith('.' + extension)]
                if len(names) != 1:
                    raise ValueError('Ambiguous source shapefile')
                # Copy only the expected files to known names; never extract
                # archive paths or load the nationwide SHP into memory at once.
                with archive.open(names[0]) as source, prefix.with_suffix('.' + extension).open('wb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
        with shapefile.Reader(str(prefix), encoding='utf-8') as reader:
            omitted = []
            for index, record in enumerate(reader.iterRecords()):
                if not record.regioncode:
                    omitted.append(record.name)
                    continue
                code = municipality_code(record.regioncode, record.regionname)
                yield code, shape(reader.shape(index).__geo_interface__)
            if Counter(omitted) != Counter(UNASSIGNED_NAMES):
                raise ValueError('Uncoded JMA land features changed')


def prepare(source, output):
    for name, expected in (('azarashi', '0.17.1'), ('pyshp', '2.3.1'), ('shapely', '2.1.2')):
        if version(name) != expected:
            raise ValueError(f'Use pinned {name}=={expected}')
    if file_hash(source) != SOURCE_SHA256:
        raise ValueError('Official municipality source changed; review before updating')
    features, statistics = {}, {}
    for code, original in source_features(source):
        if code in features:
            raise ValueError('Duplicate municipality source code')
        feature, stats = convert(code, original)
        features[code], statistics[str(code)] = feature, stats
        if len(features) % 100 == 0:
            print(f'Converted {len(features)} municipalities', flush=True)
    if set(features) != EXPECTED_CODES or len(features) != 1745:
        raise ValueError('Incomplete municipality code coverage')
    raw = (json.dumps({'type': 'FeatureCollection', 'features': [features[code] for code in sorted(features)]},
                     ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError('Municipality asset exceeds runtime expansion limit')
    compressed = zlib.compress(raw, 9)
    notice = NOTICE.encode()
    manifest = {
        'municipalities.geojson.zlib': {
            'source': SOURCE, 'source_sha256': SOURCE_SHA256, 'retrieved': DATE,
            'terms': TERMS, 'source_coordinate_system': 'JGD2011',
            'sha256': hashlib.sha256(compressed).hexdigest(), 'bytes': len(compressed),
            'uncompressed_bytes': len(raw), 'compression': 'zlib; level=9', 'feature_count': len(features),
            'excluded_non_specific_codes': sorted(NON_SPECIFIC),
            'excluded_uncoded_source_names': list(UNASSIGNED_NAMES),
            'name_aliases': {str(key): value for key, value in NAME_ALIASES.items()},
            'processing': 'Direct regioncode/name match; no invented membership. Topology-preserving adaptive simplification; no island or hole removal; maximum-polygon representative points.',
            'geometry': statistics,
        },
        'MUNICIPALITIES_NOTICE.txt': {'sha256': hashlib.sha256(notice).hexdigest(), 'bytes': len(notice)},
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'municipalities.geojson.zlib').write_bytes(compressed)
    (output / 'MUNICIPALITIES_NOTICE.txt').write_bytes(notice)
    (output / 'municipalities_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/preview')
    args = parser.parse_args()
    if args.source:
        result = prepare(args.source, args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='shirasu-municipality-source-') as directory:
            source = Path(directory) / 'source.zip'
            with urllib.request.urlopen(SOURCE, timeout=60) as response, source.open('wb') as target:
                shutil.copyfileobj(response, target, 1024 * 1024)
            result = prepare(source, args.output)
    entry = result['municipalities.geojson.zlib']
    print(f"Wrote {entry['feature_count']} municipalities, {entry['bytes']} compressed bytes")


if __name__ == '__main__':
    main()
