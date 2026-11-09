#!/usr/bin/env python3
"""Freeze Natural Earth's unsimplified 1:10m land for v3 maps.

Requires pyshp==2.3.1 and shapely==2.1.2 in an asset-generation environment.
Runtime rendering uses only the compressed, checked-in GeoJSON. --source
accepts the official ZIP; otherwise the pinned publisher download is fetched.
"""

import argparse
import hashlib
from importlib.metadata import version
from io import BytesIO
import json
from pathlib import Path
import urllib.request
from zipfile import ZipFile
import zlib

import shapefile
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip'
SOURCE_PAGE = 'https://www.naturalearthdata.com/downloads/10m-physical-vectors/10m-land/'
SOURCE_SHA256 = 'e547d749445eaa0964aba76738090ec88f5e63c4585122170f98c67a7ea922dc'
TERMS = 'https://www.naturalearthdata.com/about/terms-of-use/'
DATE = '2026-11-10'
PROJECTION = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",'
              '6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",'
              '0.0174532925199433]]')

NOTICE = '''Natural Earth 1:10m land / v3 専用底図

Natural Earth のパブリックドメイン地形データを Shirasu が形式変換したもの。
1:10m land polygons, version 5.1.1. 2026年11月10日取得。
https://www.naturalearthdata.com/downloads/10m-physical-vectors/10m-land/
https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip
利用条件: https://www.naturalearthdata.com/about/terms-of-use/

元データの WGS 84 経緯度、全ポリゴン、島、穴を保持して GeoJSON 化。
簡略化、座標の丸め、補間、陸地の追加・除去、区域による切り取りは行わない。
地図の表示範囲による通常の描画クリップは行う。海岸線は測量図ではなく、
避難経路や浸水範囲の判定に使うためのデータではない。
公開済み v1 / v2 の底図には変更を加えていない。

入力・出力の SHA-256、座標数、変換条件は land_manifest.json に記録。
再生成環境: pyshp==2.3.1, shapely==2.1.2
  python scripts/prepare_v3_land_assets.py --source /path/to/ne_10m_land.zip
--source を省略すると上記公式 URL から取得する。--output で出力先を変更可能。
公開済み世代を上書きしないこと。
'''


def prepare(data, output):
    if version('pyshp') != '2.3.1' or version('shapely') != '2.1.2':
        raise ValueError('Use the frozen pyshp 2.3.1 and shapely 2.1.2 converters')
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise ValueError('Natural Earth data changed; review before publishing a new version')
    with ZipFile(BytesIO(data)) as archive:
        if archive.read('ne_10m_land.VERSION.txt').decode().strip() != '5.1.1':
            raise ValueError('Unexpected Natural Earth version')
        if archive.read('ne_10m_land.prj').decode().strip() != PROJECTION:
            raise ValueError('Unexpected Natural Earth coordinate reference system')
        reader = shapefile.Reader(
            shp=BytesIO(archive.read('ne_10m_land.shp')),
            shx=BytesIO(archive.read('ne_10m_land.shx')),
            dbf=BytesIO(archive.read('ne_10m_land.dbf')))
        features, polygon_count, hole_count, vertex_count = [], 0, 0, 0
        for source_shape in reader.shapes():
            geometry = source_shape.__geo_interface__
            checked = shape(geometry)
            if checked.geom_type not in ('Polygon', 'MultiPolygon') or not checked.is_valid:
                raise ValueError('Invalid source land geometry; do not repair it silently')
            polygons = list(checked.geoms) if checked.geom_type == 'MultiPolygon' else [checked]
            for polygon in polygons:
                polygon_count += 1
                hole_count += len(polygon.interiors)
                vertex_count += len(polygon.exterior.coords) + sum(len(hole.coords) for hole in polygon.interiors)
            # Shapely validates/counts only. Serialize pyshp's original coordinates,
            # preserving their original precision, winding, components and holes.
            features.append({'type': 'Feature', 'properties': {}, 'geometry': geometry})
    if len(features) != 11 or polygon_count != 6837 or vertex_count != 446175:
        raise ValueError('Unexpected land coverage or coordinate count')
    raw = (json.dumps({'type': 'FeatureCollection', 'features': features},
                      ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()
    asset = zlib.compress(raw, 9)
    notice = NOTICE.encode()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'land.geojson.zlib').write_bytes(asset)
    (output / 'LAND_NOTICE.txt').write_bytes(notice)
    manifest = {
        'land.geojson.zlib': {
            'source': SOURCE, 'source_page': SOURCE_PAGE,
            'source_sha256': SOURCE_SHA256, 'source_bytes': len(data),
            'source_version': '5.1.1', 'retrieved': DATE, 'terms': TERMS,
            'sha256': hashlib.sha256(asset).hexdigest(), 'bytes': len(asset),
            'uncompressed_sha256': hashlib.sha256(raw).hexdigest(),
            'uncompressed_bytes': len(raw), 'feature_count': len(features),
            'polygon_count': polygon_count, 'hole_count': hole_count,
            'vertex_count': vertex_count, 'crs': 'WGS 84 longitude/latitude',
            'processing': 'pyshp 2.3.1 Shapefile-to-GeoJSON conversion; Shapely 2.1.2 checks source validity only. All original coordinates, polygon components and holes retained, with no simplification, rounding, clipping, repair, interpolation or merging. UTF-8 compact JSON with final newline; zlib level 9.',
        },
        'LAND_NOTICE.txt': {'sha256': hashlib.sha256(notice).hexdigest(), 'bytes': len(notice)},
    }
    (output / 'land_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest['land.geojson.zlib']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/v3')
    args = parser.parse_args()
    data = args.source.read_bytes() if args.source else urllib.request.urlopen(SOURCE, timeout=90).read()
    result = prepare(data, args.output)
    print(f"{result['polygon_count']} polygons, {result['vertex_count']} coordinates, {result['bytes']} bytes")


if __name__ == '__main__':
    main()
