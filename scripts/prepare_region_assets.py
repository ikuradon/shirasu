#!/usr/bin/env python3
"""Rebuild pinned JMA regional map assets (build-time dependencies only).

Install azarashi==0.17.1, pyshp==2.3.1 and shapely==2.1.2 in a separate
environment, then run this script with a --cache directory. Network sources are
SHA-256 pinned: upstream changes require an explicit review of codes/geometry.
The runtime image service does not need GIS conversion dependencies or network.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

from azarashi.definitions.qzss.dcr.marine_forecast_region import marine_forecast_region
from azarashi.definitions.qzss.dcr.tsunami_forecast_region import tsunami_forecast_region
import shapefile
from shapely import make_valid
from shapely.geometry import LineString, MultiPolygon, mapping, shape


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATE = '2026-11-10'
TERMS = 'https://www.jma.go.jp/jma/kishou/info/coment.html'
GIS_INDEX = 'https://www.data.jma.go.jp/developer/gis.html'
TSUNAMI_GROUP_SOURCE = 'https://www.data.jma.go.jp/suishin/jyouhou/pdf/370.pdf'
SOURCES = {
    'marines.json': (
        'https://www.jma.go.jp/bosai/common/const/geojson/marines.json',
        'd90b16ed99e5b9c93cc286752a228d3fe2e487adef51771e61107998eb4d0854',
    ),
    'marine_labels.json': (
        'https://www.jma.go.jp/bosai/common/const/label_pos/marines.json',
        '6f987a1bf904f969272851e9e3b211783a9c2dfcaedcbd3377c09400f662066d',
    ),
    'marine-source.zip': (
        'https://www.data.jma.go.jp/developer/gis/20190125_AreaMarineAJ_GIS.zip',
        '1abc1e7314b940d7c518934a0117cc8b33f8a3f57534ed6ee0a419530bcf8e84',
    ),
    'tsunami.json': (
        'https://www.jma.go.jp/bosai/common/const/geojson/tsunami.json',
        '1acde2b6c6a43843fe171781e8126a8db82ef2dd42a2187db66a3a708fab1dc3',
    ),
    'prefectures-source.zip': (
        'https://www.data.jma.go.jp/developer/gis/20190125_AreaInformationPrefectureEarthquake_GIS.zip',
        '5f36a66e2a2bbdc2eb04d4c7be0dc9d710903040d4b721716a0b1368d80da235',
    ),
    'tsunami-regions.pdf': (
        TSUNAMI_GROUP_SOURCE,
        '55d99304305c8c96064fa34a95a2a012b801eadecfa56c51f9460e2ae517fce2',
    ),
}

# The base region 5000 (対馬海峡) is not an aggregate. "Other" codes have
# deliberately no geometry. Every aggregate is expanded to official base codes.
MARINE_GROUPS = {
    '1000': [1010, 1020, 1030, 1040, 1050],
    '1100': [1110, 1120, 1130, 1140, 1150],
    '2000': [2010, 2020],
    '3000': [3010, 3020],
    '3100': [3110, 3120, 3130, 3140],
    '3200': [3210, 3220, 3230],
    '4000': [4010, 4020, 4030],
    '4100': [4110, 4120, 4130],
    '5100': [5110, 5120, 5130],
    '5200': [5210, 5220, 5230],
    '6000': [6010, 6020, 6030],
}

# Transcribed from JMA 370.pdf, 津波-63..65, 別表1. Although these aggregate
# names were removed from JMA XML in 2022, IS-QZSS-DCR-017 retains them.
# Old JMA code 193 is not in DCR-017; do not introduce it as a QZQSM code.
TSUNAMI_GROUPS = {
    '191': [100, 101, 102],
    '192': [110, 111],
    '281': [200, 201, 202],
    '291': [201, 210, 220, 250],
    '292': [200, 202, 230, 240],
    '481': [310, 311, 312],
    '482': [312, 330],
    '483': [340, 341],
    '484': [360, 361],
    '485': [390, 391],
    '486': [391, 400],
    '491': [300, 310, 311, 312, 330],
    '492': [320, 321],
    '493': [340, 341, 350, 360, 361, 370],
    '494': [380, 390, 391, 400],
    '681': [520, 521, 522],
    '682': [550, 551],
    '683': [600, 601],
    '691': [522, 530, 580, 600, 610],
    '692': [500, 520, 540, 550, 551, 700],
    '693': [510, 521, 560, 570, 590, 601, 701],
    '781': [700, 701],
    '782': [710, 711, 712],
    '783': [712, 720],
    '784': [712, 730, 731],
    '785': [712, 740],
    '786': [750, 751],
    '787': [770, 771, 772, 773],
    '791': [710, 750, 751, 760, 770],
    '792': [711, 712, 720, 730, 731, 740, 773],
    '793': [771, 772],
    '891': [800, 801, 802],
}

NOTICE = """JMA regional map assets / 気象庁区域データの加工について

出典: 気象庁ホームページ「地理情報システム（GIS）データ」および
「あなたの街の防災情報」の表示用区域データ（2026年11月10日取得）。
https://www.data.jma.go.jp/developer/gis.html
https://www.jma.go.jp/bosai/map.html

気象庁ホームページの利用規約（公共データ利用規約 第1.0版）に基づき利用。
https://www.jma.go.jp/jma/kishou/info/coment.html
気象庁区域データを Shirasu が加工して作成。気象庁が作成した画像ではない。
加工データを再利用・表示する場合も出典と加工した旨を表示すること。

marine.geojson: 気象庁 GIS「地方海上予報区」（2019年1月25日版）の37基本区域。
島を示す穴を保持し、都道府県データと同じ方法・許容値で簡略化。元GISの海岸に
ある自己接触・微小な重複が簡略化後も残った場合、Shapely make_valid の
structure 方法で修復する。修復前後の成分・穴の数と面積変化はmanifestに記録。
非ポリゴンの生成や区域面積の百万分の一以上の変化があれば生成を中止する。
ラベル代表点
は気象庁の表示用 gravity 座標。名称・コードを QZQSM と照合。公式ウェブ表示用
GeoJSONは島の穴を独立polygonとして平坦化しているため、境界の元データには
採用せず、正式GISのpolygon/hole構造を使用した。
tsunami.geojson: 気象庁の表示用津波予報区66区域。海岸線座標は取得元のまま。
名称は IS-QZSS-DCR-017（azarashi 0.17.1）、代表点は最長線分の中点から算出。
groups.json: 受信コードを基本区域へ展開する表。津波集約区域の構成は次の
気象庁資料の津波-63～65頁、別表1で確認（2022年にJMA XMLからは廃止された
集約区域も QZQSM に残るため収録）。未知・その他の区域には形状を割り当てない。
https://www.data.jma.go.jp/suishin/jyouhou/pdf/370.pdf
https://xml.kishou.go.jp/20220621_jmaxml_info.pdf

prefectures.geojson: 気象庁 GIS「地震情報／都道府県等」（2019年1月25日版）
の47区域をコード01～47から1～47に変換。元の JGD2011 の経緯度を保持し、
各区域を Shapely 2.1.2 の topology-preserving simplify（許容値0.002度）で
簡略化。元の全ポリゴン成分と穴の個数を検証し保持。座標の丸め・離島の除外・
別区域への付け替えは行っていない。隣接区域を一体に簡略化していないため、
簡略化の誤差による細い隙間や重なりが生じることがある。代表点は最大面積の
ポリゴン内の点で、観測点や震央ではない。県内の一様な震度を意味しない。

いずれも概況画像用の簡略地図であり、正確な境界、避難対象区域、津波浸水域、
津波の到達位置を示すデータではない。個別の入力URL・SHA-256・変換条件は
manifest.json、再生成手順は scripts/prepare_region_assets.py を参照。
"""


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')


def fetch(cache, filename):
    url, expected = SOURCES[filename]
    path = cache / filename
    if path.exists():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f'Upstream content changed; review source before updating: {url}')
    if not path.exists():
        path.write_bytes(data)
    return data


def source_metadata(*filenames):
    return [{'url': SOURCES[name][0], 'sha256': SOURCES[name][1],
             'retrieved': SOURCE_DATE} for name in filenames]


def gis_regions(data, expected_codes, anchors=None, *, repair=False):
    features, statistics = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        reader = shapefile.Reader(
            shp=io.BytesIO(archive.read(next(n for n in archive.namelist() if n.endswith('.shp')))),
            dbf=io.BytesIO(archive.read(next(n for n in archive.namelist() if n.endswith('.dbf')))),
            encoding='utf-8',
        )
        for row in reader.iterShapeRecords():
            code = str(int(row.record.code))
            if int(code) not in expected_codes:
                continue
            original = shape(row.shape.__geo_interface__)
            if not original.is_valid and not repair:
                raise ValueError(f'Invalid source geometry: {code}')
            simplified = original.simplify(0.002, preserve_topology=True)
            if simplified.geom_type == 'Polygon':
                simplified = MultiPolygon([simplified])
            original_parts = list(original.geoms) if original.geom_type == 'MultiPolygon' else [original]
            old_holes = sum(len(part.interiors) for part in original_parts)
            new_holes = sum(len(part.interiors) for part in simplified.geoms)
            if ((not simplified.is_valid and not repair) or simplified.is_empty
                    or len(original_parts) != len(simplified.geoms) or old_holes != new_holes):
                raise ValueError(f'Geometry/component/hole mismatch: {code}')
            before_repair = simplified
            if repair and not simplified.is_valid:
                simplified = make_valid(simplified, method='structure')
                if simplified.geom_type == 'Polygon':
                    simplified = MultiPolygon([simplified])
            repair_delta = abs(simplified.area - before_repair.area)
            if (simplified.geom_type != 'MultiPolygon' or not simplified.is_valid
                    or simplified.is_empty or repair_delta / before_repair.area >= 1e-6):
                raise ValueError(f'Unexpected geometry repair: {code}')
            point = max(simplified.geoms, key=lambda part: part.area).representative_point()
            features.append({'type': 'Feature', 'properties': {
                'code': code, 'name': row.record.name,
                'anchor': anchors[code]['gravity'] if anchors else [point.x, point.y],
            }, 'geometry': mapping(simplified)})
            statistics.append({
                'code': code, 'components': len(simplified.geoms),
                'holes': sum(len(part.interiors) for part in simplified.geoms),
                'relative_area_change': abs(simplified.area - original.area) / original.area,
            })
            if repair:
                statistics[-1].update({
                    'source_valid': original.is_valid,
                    'pre_repair_components': len(before_repair.geoms),
                    'pre_repair_holes': new_holes,
                    'repair_area_change_square_degrees': repair_delta,
                })
            print(f'Converted area {code}', flush=True)
    if {int(feature['properties']['code']) for feature in features} != expected_codes:
        raise ValueError('GIS region code mismatch')
    return {'type': 'FeatureCollection', 'features': features}, statistics


def build(cache, output):
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    source = {name: fetch(cache, name) for name in SOURCES}
    marine_codes = {int(feature['properties']['code'])
                    for feature in json.loads(source['marines.json'])['features']}
    labels = json.loads(source['marine_labels.json'])
    marine, marine_stats = gis_regions(source['marine-source.zip'], marine_codes, labels, repair=True)
    for feature in marine['features']:
        code = str(int(feature['properties']['code']))
        if feature['properties']['name'] != marine_forecast_region[int(code)]:
            raise ValueError(f'Marine name mismatch: {code}')
        feature['properties'] = {'code': code, 'name': marine_forecast_region[int(code)],
                                 'anchor': labels[code]['gravity']}
    tsunami = json.loads(source['tsunami.json'])
    for feature in tsunami['features']:
        code = str(int(feature['properties']['code']))
        longest = max((LineString(line) for line in feature['geometry']['coordinates']),
                      key=lambda line: line.length)
        point = longest.interpolate(0.5, normalized=True)
        feature['properties'] = {'code': code, 'name': tsunami_forecast_region[int(code)],
                                 'anchor': [point.x, point.y]}
    groups = {'marine': MARINE_GROUPS, 'tsunami': TSUNAMI_GROUPS}
    for layer, data, table, unknown in (
            ('marine', marine, marine_forecast_region, 10000),
            ('tsunami', tsunami, tsunami_forecast_region, 1000)):
        codes = {int(feature['properties']['code']) for feature in data['features']}
        if len(codes) != len(data['features']):
            raise ValueError(f'Duplicate {layer} feature code')
        if codes | {int(code) for code in groups[layer]} | {unknown} != set(table):
            raise ValueError(f'{layer} decoder code coverage mismatch')
        for code, children in groups[layer].items():
            if int(code) in codes or not children or not set(children) <= codes or len(set(children)) != len(children):
                raise ValueError(f'Invalid {layer} aggregate: {code}')
    dump(output / 'marine.geojson', marine)
    dump(output / 'tsunami.geojson', tsunami)
    dump(output / 'groups.json', groups)
    pref, stats = gis_regions(source['prefectures-source.zip'], set(range(1, 48)))
    dump(output / 'prefectures.geojson', pref)
    finish_manifest(output, stats, marine_stats)


def finish_manifest(output, stats, marine_stats):
    (output / 'JMA_NOTICE.txt').write_text(NOTICE)
    manifest = {
        'marine.geojson': {
            'source': source_metadata('marine-source.zip', 'marines.json', 'marine_labels.json'),
            'feature_count': 37,
            'coordinate_reference': 'Source JGD2011 longitude/latitude retained; display map only.',
            'processing': 'Official GIS geometry; Shapely 2.1.2 simplify(0.002, preserve_topology=True) preserves every source component/hole before repair; residual invalid geometry repaired using make_valid(method="structure"). Repair counts and area changes recorded below; non-polygon output or relative area change >= 1e-6 rejected. No rounding. Shared boundaries may differ within tolerance. Web geometry is not used because island holes are flattened into independent polygon shells.',
            'anchor': 'Official web display gravity coordinates.',
            'simplification_tolerance_degrees': 0.002,
            'validation': marine_stats,
        },
        'tsunami.geojson': {
            'source': source_metadata('tsunami.json'), 'feature_count': 66,
            'processing': 'Official web display coastline unchanged; names from IS-QZSS-DCR-017 via azarashi 0.17.1; anchor at half of longest line.',
        },
        'prefectures.geojson': {
            'source': source_metadata('prefectures-source.zip'), 'feature_count': 47,
            'coordinate_reference': 'Source JGD2011 longitude/latitude retained; display map only.',
            'processing': 'Shapely 2.1.2 simplify(0.002, preserve_topology=True) per prefecture; every component/hole retained; no rounding. Shared boundaries may differ within simplification tolerance.',
            'anchor': 'Interior representative point of largest polygon; not an observation point.',
            'simplification_tolerance_degrees': 0.002,
            'validation': stats,
        },
        'groups.json': {
            'source': source_metadata('tsunami-regions.pdf'),
            'processing': 'Explicit JMA aggregate membership; 11 marine and 32 tsunami aggregates; checked against azarashi 0.17.1 DCR-017 tables. Tsunami source pages 津波-63..65, 別表1.',
        },
        'JMA_NOTICE.txt': {'source': [{'url': TERMS, 'retrieved': SOURCE_DATE}]},
    }
    for filename, metadata in manifest.items():
        data = (output / filename).read_bytes()
        metadata['sha256'] = hashlib.sha256(data).hexdigest()
        metadata['bytes'] = len(data)
        metadata['terms'] = TERMS
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/v2')
    args = parser.parse_args()
    build(args.cache, args.output)


if __name__ == '__main__':
    main()
