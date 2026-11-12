#!/usr/bin/env python3
"""Extract internal prefectural borders before simplifying the shared lines.

Build-only dependencies: pyshp==2.3.1 and shapely==2.1.2. No runtime GIS package
is required. The source is the same pinned JMA GIS used by the v2 prefectures.
"""

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'https://www.data.jma.go.jp/developer/gis/20190125_AreaInformationPrefectureEarthquake_GIS.zip'
SOURCE_SHA256 = '5f36a66e2a2bbdc2eb04d4c7be0dc9d710903040d4b721716a0b1368d80da235'
TERMS = 'https://www.jma.go.jp/jma/kishou/info/coment.html'
DATE = '2026-11-10'
TOLERANCE = .002
ASSET = 'prefecture_boundaries.geojson.zlib'
MANIFEST = 'prefecture_boundaries_manifest.json'
NOTICE_NAME = 'PREFECTURE_BOUNDARIES_NOTICE.txt'
NOTICE = '''Internal prefectural borders / 都道府県境の背景表示

出典: 気象庁 GIS「地震情報／都道府県等」（2019年1月25日版）
https://www.data.jma.go.jp/developer/gis/20190125_AreaInformationPrefectureEarthquake_GIS.zip
2026年11月10日取得。入力・加工後のSHA-256は prefecture_boundaries_manifest.json に記録。
気象庁ホームページの利用規約（公共データ利用規約 第1.0版）に基づき利用。
https://www.jma.go.jp/jma/kishou/info/coment.html

気象庁データを Shirasu が加工して作成。気象庁が作成した画像ではない。
47都道府県の元の区域から、異なる県の境界が共有する長さのある線だけを抽出する。
点で接する部分・海岸などの外周は含めない。抽出後に連続する線分を結合し、
共有線を一度だけ0.002度の許容誤差で簡略化する。個別に簡略化した県の輪郭を
重ねたり、そのずれから境界を補間したりしない。座標は原典のJGD2011経緯度を保持。

県境は地図上の位置を把握するための背景であり、洪水予報区間、浸水域、
行政上の境界確定や現在の精密な測量を示すものではない。

再生成（pyshp 2.3.1、Shapely 2.1.2 の加工用環境）:
  python tools/assets/prepare_prefecture_boundary_assets.py --source /path/to/prefectures-source.zip
--source省略時は固定した公式URLを取得。描画時の外部通信やGIS用依存は不要。
'''


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _line_parts(geometry):
    if geometry.geom_type == 'LineString':
        return [geometry] if not geometry.is_empty and geometry.length > 0 else []
    if geometry.geom_type in ('MultiLineString', 'GeometryCollection'):
        return [part for child in geometry.geoms for part in _line_parts(child)]
    return []


def prepare(source, output):
    import shapefile
    from shapely import line_merge, make_valid, STRtree
    from shapely.geometry import MultiLineString, shape

    for package, expected in (('pyshp', '2.3.1'), ('shapely', '2.1.2')):
        if version(package) != expected:
            raise ValueError(f'Use pinned {package}=={expected}')
    if file_hash(source) != SOURCE_SHA256:
        raise ValueError('Official prefecture source changed; review before regenerating')
    borders, names, repaired = {}, {}, []
    with tempfile.TemporaryDirectory(prefix='shirasu-prefecture-source-') as directory:
        prefix = Path(directory) / 'prefectures'
        with zipfile.ZipFile(source) as archive:
            for suffix in ('shp', 'dbf', 'shx'):
                members = [name for name in archive.namelist() if name.endswith('.' + suffix)]
                if len(members) != 1:
                    raise ValueError('Missing or duplicated source shapefile component')
                with archive.open(members[0]) as origin, prefix.with_suffix('.' + suffix).open('wb') as target:
                    shutil.copyfileobj(origin, target, 1024 * 1024)
        with shapefile.Reader(str(prefix), encoding='utf-8') as reader:
            for row in reader.iterShapeRecords():
                code = int(row.record.code)
                if code not in range(1, 48) or code in borders:
                    raise ValueError('Unknown or duplicate prefecture code')
                geometry = shape(row.shape.__geo_interface__)
                if not geometry.is_valid:
                    geometry = make_valid(geometry, method='structure')
                    repaired.append(code)
                if (geometry.is_empty or not geometry.is_valid
                        or geometry.geom_type not in ('Polygon', 'MultiPolygon')):
                    raise ValueError('Invalid prefecture source polygon')
                borders[code], names[code] = geometry.boundary, row.record.name
    if set(borders) != set(range(1, 48)):
        raise ValueError('Expected all 47 prefectures')

    codes = sorted(borders)
    boundaries = [borders[code] for code in codes]
    tree = STRtree(boundaries)
    features, statistics = [], []
    for index, boundary in enumerate(boundaries):
        for other in sorted(int(value) for value in tree.query(boundary) if value > index):
            a, b = codes[index], codes[other]
            parts = _line_parts(boundary.intersection(boundaries[other]))
            if not parts:
                continue
            shared = line_merge(MultiLineString(parts))
            original_parts = _line_parts(shared)
            simplified = shared.simplify(TOLERANCE, preserve_topology=True)
            result_parts = _line_parts(simplified)
            if (len(result_parts) != len(original_parts) or not simplified.is_valid
                    or any(part.is_empty or part.length <= 0 for part in result_parts)):
                raise ValueError('Simplification lost an internal border component')
            coordinates = [list(part.coords) for part in result_parts]
            # Canonicalize direction/component order without changing geometry.
            coordinates = sorted(min(line, list(reversed(line))) for line in coordinates)
            feature = {'type': 'Feature', 'properties': {'codes': [a, b], 'names': [names[a], names[b]]},
                       'geometry': {'type': 'MultiLineString', 'coordinates': coordinates}}
            features.append(feature)
            statistics.append({'codes': [a, b], 'components': len(result_parts),
                               'source_length_degrees': shared.length,
                               'length_degrees': simplified.length,
                               'source_points': sum(len(part.coords) for part in original_parts),
                               'points': sum(len(part.coords) for part in result_parts)})
    if not any(feature['properties']['codes'] == [8, 11] for feature in features):
        raise ValueError('Original source is missing the Ibaraki–Saitama boundary')
    raw = (json.dumps({'type': 'FeatureCollection', 'features': features},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    compressed, notice = zlib.compress(raw, 9), NOTICE.encode()
    manifest = {
        ASSET: {'source': SOURCE, 'source_sha256': SOURCE_SHA256, 'retrieved': DATE,
                'terms': TERMS, 'source_coordinate_system': 'JGD2011',
                'sha256': hashlib.sha256(compressed).hexdigest(), 'bytes': len(compressed),
                'uncompressed_bytes': len(raw), 'feature_count': len(features),
                'tolerance_degrees': TOLERANCE, 'source_prefecture_count': len(codes),
                'repaired_source_codes': repaired,
                'processing': 'Positive-length intersections of distinct original prefecture boundaries; merge contiguous lines, then simplify each shared border once; exclude point contacts and external coastline; canonicalize line direction and component order.',
                'borders': statistics},
        NOTICE_NAME: {'sha256': hashlib.sha256(notice).hexdigest(), 'bytes': len(notice)},
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / ASSET).write_bytes(compressed)
    (output / NOTICE_NAME).write_bytes(notice)
    (output / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/preview')
    args = parser.parse_args()
    if args.source:
        manifest = prepare(args.source, args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='shirasu-prefecture-download-') as directory:
            source = Path(directory) / 'source.zip'
            with urllib.request.urlopen(SOURCE, timeout=60) as response, source.open('wb') as target:
                shutil.copyfileobj(response, target, 1024 * 1024)
            manifest = prepare(source, args.output)
    entry = manifest[ASSET]
    print(f"Prepared {entry['feature_count']} internal prefectural borders ({entry['bytes']} bytes)")


if __name__ == '__main__':
    main()
