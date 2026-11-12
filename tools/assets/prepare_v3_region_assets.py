#!/usr/bin/env python3
"""Rebuild pinned v3 Weather/EEW geometry; no runtime GIS dependencies.

Use a separate environment with azarashi==0.17.1, pyshp==2.3.1 and
shapely==2.1.2, then run with --cache DIRECTORY. Downloads are SHA-256
pinned. Only standard zlib-compressed GeoJSON and provenance enter the app.
"""

import argparse
import gc
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path
import urllib.request
import zipfile
import zlib

from azarashi.definitions.qzss.dcr.eew_forecast_region import eew_forecast_region
from azarashi.definitions.qzss.dcr.weather_forecast_region import weather_forecast_region
import shapefile
from shapely import make_valid, union_all
from shapely.geometry import MultiPolygon, mapping, shape


ROOT = Path(__file__).resolve().parents[2]
DATE = '2026-11-10'
TERMS = 'https://www.jma.go.jp/jma/kishou/info/coment.html'
SOURCES = {
    'weather.zip': (
        'https://www.data.jma.go.jp/developer/gis/20190125_AreaForecastLocalM_prefecture_GIS.zip',
        'f4e32e21a806120eac86c529ff9e5ed8130b13ac9e5988c2e8a4a8f4093ddce0',
    ),
    'weather-class10.zip': (
        'https://www.data.jma.go.jp/developer/gis/20190125_AreaForecastLocalM_1saibun_GIS.zip',
        '1a085be077321738f301fe0e321f66539b5b2e7d6c9865b3cb263d19dd5c7939',
    ),
    'eew.zip': (
        'https://www.data.jma.go.jp/developer/gis/20190125_AreaForecastLocalEEW_GIS.zip',
        '1101f460cdb249f4a52910d15772ae1a29f92ab26ed3b593462ff3aaf3f74eeb',
    ),
    'dcr-017.pdf': (
        'https://qzss.go.jp/en/technical/download/pdf/ps-is-qzss/is-qzss-dcr-017.pdf',
        '24bc3a2f62b22f0b46d9e01a2ed1cb61bc6c77b257567fc8588baf390a2ab84c',
    ),
    'eew-groups.pdf': (
        'https://www.data.jma.go.jp/suishin/catalogue/format/SvdEto_KinkyuJishinKeiho.2_format.pdf',
        '568666a969497c7501669a85205698cb69a673f19358eaab01d037fcd1517d11',
    ),
}

# DCR-017 table 4.1.2-41. Tokyo's four official first-order subdivisions
# preserve its entire geometry while avoiding parent/child double drawing.
# 130040 is an internal geometry only: the Weather decoder does not accept it
# as a standalone DCR region. Unknown region 500000 has no invented geometry.
WEATHER_GROUPS = {
    '12000': [12010, 12020],
    '14100': [14010, 14020],
    '15000': [15010, 15020],
    '16000': [16010, 16020, 16030],
    '16100': [16010, 16020],
    '17000': [17010, 17020],
    '130000': [130010, 130020, 130030, 130040],
    '460000': [460040, 460100],
}

# Explicit GIS code -> DCR code conversion. Both tables' names must also match;
# no fuzzy name search, geographic proximity or administrative-prefix inference.
EEW_SOURCE_CODES = (
    9011, 9012, 9013, 9014, 9020, 9030, 9040, 9050, 9060, 9070,
    9080, 9090, 9100, 9110, 9120, 9131, 9132, 9133, 9140, 9150,
    9160, 9170, 9180, 9190, 9200, 9210, 9220, 9230, 9240, 9250,
    9260, 9270, 9280, 9290, 9300, 9310, 9320, 9330, 9340, 9350,
    9360, 9370, 9380, 9390, 9400, 9410, 9420, 9430, 9440, 9450,
    9461, 9462, 9471, 9472, 9473, 9474,
)
EEW_CODES = {source: target for target, source in enumerate(EEW_SOURCE_CODES, 1)}

# JMA's 緊急地震速報（警報）電文フォーマット別表4 (printed 地震-54..58).
# Kanto excludes Izu/Ogasawara, Mie belongs to Tokai, Yamaguchi to Chugoku,
# and Amami is a separate aggregate rather than a child of Kyushu.
EEW_GROUPS = {
    '57': [1, 2, 3, 4],
    '58': [5, 6, 7, 8, 9, 10],
    '59': [11, 12, 13, 14, 15, 16, 19],
    '60': [17], '61': [18],
    '62': [20, 21, 22, 23],
    '63': [24, 25],
    '64': [26, 27, 28, 29],
    '65': [30, 31, 32, 33, 34, 35],
    '66': [36, 37, 38, 39, 40],
    '67': [41, 42, 43, 44],
    '68': [45, 46, 47, 48, 49, 50, 51],
    '69': [52], '70': [53, 54, 55, 56],
}

NOTICE = """JMA v3 regional assets / 気象庁の区域データを加工して作成

出典: 気象庁「予報区等GISデータの一覧」（2026年11月10日取得）。
https://www.data.jma.go.jp/developer/gis.html
利用規約: https://www.jma.go.jp/jma/kishou/info/coment.html
気象庁の公表データを Shirasu が加工して作成。気象庁作成の画像ではない。
再利用・表示する場合も出典および加工した旨を明記すること。

weather.geojson.zlib: 府県予報区等（2019年1月25日版）の64区域から東京都を除き、
同日版の一次細分区域の東京地方・伊豆諸島北部・伊豆諸島南部・小笠原諸島を追加。
東京都全域は4区域の集約として保持。小笠原諸島130040は内部形状コードであり、
QZQSM Weather の単独地域としては使用しない。北海道の複合区域と鹿児島県全体も
明示した子区域で表す。未定義/その他の区域には位置や境界を割り当てない。

eew.geojson.zlib: 緊急地震速報／府県予報区（2019年1月25日版）の56区域。
GISコードとIS-QZSS-DCR-017コードを明示表で対応させ、全名称も照合する。
地方集約14区域の対応は気象庁「緊急地震速報（警報）電文フォーマット」別表4
（地震-54〜58頁）に基づく。北海道の4分割、東京地方／伊豆／小笠原、鹿児島／奄美、
沖縄4区域を保持する。地図は予測震度の面的分布や地震動の到達範囲を示さない。

元のJGD2011経緯度を保持し、Shapely 2.1.2 simplify(..., preserve_topology=True)
で各区域を簡略化。許容値は最大0.002度、面積差が1%を超える小区域は許容値を半減し
1%以内に収める。各区域の実際の許容値をmanifestへ記録する。簡略化前後で全polygon
成分・穴の数を検証し保持する。残る不整合は
make_valid(method="structure") で微小な重複だけ修復し、その前後の成分・穴・面積差を
regional_manifest.jsonに記録する。非polygon出力または相対面積差1e-6以上は拒否する。
座標丸めや離島の除外は行わない。隣接境界は個別簡略化の誤差による細いずれを含み得る。
代表点は最大polygon内の点であり、観測点や災害発生地点ではない。

JSONはUTF-8、zlib(level=9)で圧縮。ソースURL・SHA-256・検証結果はregional_manifest.json、
再生成手順はtools/assets/prepare_v3_region_assets.pyを参照。
"""


def fetch(cache, name):
    url, expected = SOURCES[name]
    path = cache / name
    if not path.exists():
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'Upstream content changed; review before updating: {url}')
        path.write_bytes(data)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f'Cached content differs from pinned source: {name}')
    return path


def rows(path, selected=None):
    with zipfile.ZipFile(path) as archive:
        data = {ext: io.BytesIO(archive.read(next(n for n in archive.namelist()
                                                  if n.endswith('.' + ext))))
                for ext in ('shp', 'shx', 'dbf')}
        reader = shapefile.Reader(**data, encoding='utf-8')
        for index, record in enumerate(reader.iterRecords()):
            if not record.code and selected is not None:
                continue  # The first-order source also has an uncoded outline.
            code = int(record.code)
            if selected is None or code in selected:
                yield code, record.name, shape(reader.shape(index).__geo_interface__)


def normalize_name(value):
    return value.replace('（', '(').replace('）', ')')


def parts(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry]
    if geometry.geom_type == 'MultiPolygon':
        return list(geometry.geoms)
    raise ValueError('Source contains non-polygon geometry')


def convert(code, name, original, *, source_code):
    before_parts = parts(original)
    source_holes = sum(len(part.interiors) for part in before_parts)
    tolerance = 0.002
    while True:
        simplified = original.simplify(tolerance, preserve_topology=True)
        if abs(simplified.area - original.area) / original.area <= 0.01:
            break
        tolerance /= 2
        if tolerance < 0.00003125:
            raise ValueError(f'Unable to retain regional area: {code}')
    simplified = MultiPolygon(parts(simplified))
    before_repair = simplified
    before_holes = sum(len(part.interiors) for part in simplified.geoms)
    if (len(before_parts) != len(simplified.geoms) or source_holes != before_holes
            or simplified.is_empty):
        raise ValueError(f'Simplification lost a component or hole: {code}')
    if not simplified.is_valid:
        simplified = make_valid(simplified, method='structure')
        simplified = MultiPolygon(parts(simplified))
    repair_delta = abs(simplified.area - before_repair.area)
    if (not simplified.is_valid or simplified.is_empty
            or repair_delta / before_repair.area >= 1e-6):
        raise ValueError(f'Unexpected geometry repair: {code}')
    anchor = max(simplified.geoms, key=lambda part: part.area).representative_point()
    feature = {'type': 'Feature', 'properties': {
        'code': str(code), 'name': name, 'anchor': [anchor.x, anchor.y],
    }, 'geometry': mapping(simplified)}
    stats = {
        'code': code, 'source_code': source_code, 'source_valid': original.is_valid,
        'source_components': len(before_parts), 'source_holes': source_holes,
        'components': len(simplified.geoms),
        'holes': sum(len(part.interiors) for part in simplified.geoms),
        'relative_area_change': abs(simplified.area - original.area) / original.area,
        'simplification_tolerance_degrees': tolerance,
        'repair_area_change_square_degrees': repair_delta,
        'pre_repair_components': len(before_repair.geoms), 'pre_repair_holes': before_holes,
    }
    print(f'Converted {code}: {name}', flush=True)
    return feature, stats


def save_json(path, value, *, compressed=False):
    raw = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    data = zlib.compress(raw, 9) if compressed else raw
    path.write_bytes(data)
    return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            **({'uncompressed_bytes': len(raw), 'compression': 'zlib; level=9'} if compressed else {})}


def source_metadata(*names):
    return [{'url': SOURCES[name][0], 'sha256': SOURCES[name][1], 'retrieved': DATE}
            for name in names]


def check_codes(features, groups, expected, *, internal=()):
    codes = {int(feature['properties']['code']) for feature in features}
    if len(codes) != len(features) or codes & {int(code) for code in groups}:
        raise ValueError('Duplicate or parent geometry')
    if (codes | {int(code) for code in groups}) - set(internal) != set(expected):
        raise ValueError('Regional code coverage mismatch')
    for parent, children in groups.items():
        if not children or len(set(children)) != len(children) or not set(children) <= codes:
            raise ValueError(f'Invalid aggregate {parent}')


def build(cache, output):
    for package, expected in (('azarashi', '0.17.1'), ('pyshp', '2.3.1'), ('shapely', '2.1.2')):
        if version(package) != expected:
            raise ValueError(f'Regeneration requires {package}=={expected}')
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    sources = {name: fetch(cache, name) for name in SOURCES}
    manifest = {}
    features, statistics = [], []
    tokyo = None
    for code, name, original in rows(sources['weather.zip']):
        if code == 130000:
            tokyo = original
            continue
        if normalize_name(name) != weather_forecast_region[code]:
            raise ValueError(f'Weather code/name mismatch: {code}')
        feature, stats = convert(code, weather_forecast_region[code], original, source_code=code)
        features.append(feature)
        statistics.append(stats)
    tokyo_parts = []
    for code, name, original in rows(sources['weather-class10.zip'], set(WEATHER_GROUPS['130000'])):
        if code != 130040 and name != weather_forecast_region[code]:
            raise ValueError(f'Tokyo code/name mismatch: {code}')
        tokyo_parts.append(original)
        feature, stats = convert(code, name, original, source_code=code)
        features.append(feature)
        statistics.append(stats)
    if tokyo is None or len(tokyo_parts) != 4:
        raise ValueError('Tokyo component sources missing')
    combined_tokyo = union_all(tokyo_parts)
    tokyo_difference = combined_tokyo.symmetric_difference(tokyo).area
    if tokyo_difference / tokyo.area >= 1e-10:
        raise ValueError(f'Tokyo subdivision union differs from prefecture: {tokyo_difference}')
    check_codes(features, WEATHER_GROUPS, set(weather_forecast_region) - {500000}, internal={130040})
    features.sort(key=lambda feature: int(feature['properties']['code']))
    manifest['weather.geojson.zlib'] = {
        **save_json(output / 'weather.geojson.zlib', {'type': 'FeatureCollection', 'features': features}, compressed=True),
        'source': source_metadata('weather.zip', 'weather-class10.zip', 'dcr-017.pdf'),
        'feature_count': len(features), 'internal_geometry_codes': [130040],
        'tokyo_union_difference_square_degrees': tokyo_difference,
        'validation': statistics,
    }
    del features, statistics, tokyo_parts, tokyo, combined_tokyo
    gc.collect()
    features, statistics = [], []
    seen = set()
    for source_code, name, original in rows(sources['eew.zip']):
        if source_code not in EEW_CODES:
            raise ValueError(f'Unknown EEW GIS source code: {source_code}')
        code = EEW_CODES[source_code]
        if name != eew_forecast_region[code]:
            raise ValueError(f'EEW code/name mismatch: {source_code}, {code}, {name}')
        seen.add(source_code)
        feature, stats = convert(code, name, original, source_code=source_code)
        features.append(feature)
        statistics.append(stats)
    if seen != set(EEW_CODES):
        raise ValueError('EEW source table incomplete')
    check_codes(features, EEW_GROUPS, set(eew_forecast_region) - {80})
    features.sort(key=lambda feature: int(feature['properties']['code']))
    manifest['eew.geojson.zlib'] = {
        **save_json(output / 'eew.geojson.zlib', {'type': 'FeatureCollection', 'features': features}, compressed=True),
        'source': source_metadata('eew.zip', 'dcr-017.pdf'),
        'feature_count': len(features), 'validation': statistics,
    }
    for name, groups, provenance, description in (
        ('weather_groups.json', WEATHER_GROUPS, ('dcr-017.pdf', 'weather.zip', 'weather-class10.zip'),
         'DCR-017 table 4.1.2-41 named aggregates; Tokyo source union verified before simplification.'),
        ('eew_groups.json', EEW_GROUPS, ('eew-groups.pdf', 'dcr-017.pdf'),
         'JMA EEW format appendix 4 (printed 地震-54..58), explicit DCR parent/member correspondence.'),
    ):
        manifest[name] = {**save_json(output / name, groups),
                          'source': source_metadata(*provenance), 'processing': description}
    notice = NOTICE.encode()
    (output / 'REGIONAL_NOTICE.txt').write_bytes(notice)
    manifest['REGIONAL_NOTICE.txt'] = {'sha256': hashlib.sha256(notice).hexdigest(), 'bytes': len(notice),
                                      'source': [{'url': TERMS, 'retrieved': DATE}]}
    for name, metadata in manifest.items():
        metadata['terms'] = TERMS
        if name.endswith('.geojson.zlib'):
            metadata.update({
                'coordinate_reference': 'Source JGD2011 longitude/latitude retained.',
                'processing': 'Shapely 2.1.2 simplify(tolerance, preserve_topology=True); tolerance starts at 0.002 degrees and is halved until relative area change <=1%, with actual tolerance recorded per feature. All source polygon components and holes retained before any residual repair. If needed make_valid(method="structure"); non-polygon result or repair relative area change >=1e-6 rejected. No rounding or removed islands; shared boundaries may differ within simplification tolerance.',
                'anchor': 'Interior representative point of largest polygon; not an observation/disaster location.',
                'simplification_tolerance_degrees': 0.002,
            })
    (output / 'regional_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/v3')
    args = parser.parse_args()
    build(args.cache, args.output)


if __name__ == '__main__':
    main()
