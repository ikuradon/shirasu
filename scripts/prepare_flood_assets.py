#!/usr/bin/env python3
"""Prepare the pinned JMA flood forecast reaches for private, offline rendering.

Use azarashi==0.17.1 and pyshp==2.3.1. Pyshp is a build-only dependency.
The generated geometry is deliberately not checked into the public repository;
download the pinned source or pass a saved ZIP with --source when building.
"""

import argparse
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile
import zlib

from azarashi.definitions.qzss.dcr.flood_forecast_region import flood_forecast_region

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://www.jmbsc.or.jp/jp/online/joho-sample/tech-sira_20260520/tech-sira_20260520_flood_risk_map_all.zip'
SOURCE_SHA256 = 'bad789ae3413787d709b74896ed74da88f2416f3f352ca9a37877e8c9130e220'
SPECIFICATION = 'https://www.data.jma.go.jp/suishin/oshirase/pdf/20260807k.pdf'
POLICY = 'https://www.jma.go.jp/jma/kishou/books/hakusho/2019/index3.html#toc-173'
DATE = '2026-11-10'
MAX_EXPANDED_BYTES = 8 * 1024 * 1024
NON_SPECIFIC = frozenset(code for code, name in flood_forecast_region.items() if 'その他の河川' in name)
EXPECTED_CODES = frozenset(flood_forecast_region) - NON_SPECIFIC

NOTICE = '''JMA flood forecast reaches / 指定河川洪水予報の予報区間

気象庁提供の流路データ（国土数値情報を基に気象庁加工）を加工して作成。
画像の加工・作成者: Shirasu。気象庁が作成した画像ではない。

出典: 気象庁「流域雨量指数計算河川及び洪水予報河川の予報区間の流路データ」
配布元: 気象業務支援センター（資料は気象庁より提供）
https://www.jmbsc.or.jp/jp/online/c-onlineGsd-oshirase.html
https://www.jmbsc.or.jp/jp/online/joho-sample/tech-sira_20260520/tech-sira_20260520_flood_risk_map_all.zip
2026年11月10日取得。固定した入力のSHA-256は flood_manifest.json に記録。
仕様 No.10704（2026年8月7日改訂）:
https://www.data.jma.go.jp/suishin/oshirase/pdf/20260807k.pdf
原典: 国土交通省 国土数値情報（河川台帳）（単位流域台帳）（流路）S52。

仕様書は流路データの地図表示への使用を想定している。気象庁の気象データ利用の
一般方針は次を参照:
https://www.jma.go.jp/jma/kishou/books/hakusho/2019/index3.html#toc-173
当該ZIP固有の利用許諾文書は確認できておらず、一般方針だけを根拠に、原典由来の
GISデータ自体の無条件な公開再配布を許諾されたものとは扱わない。
生成した flood.geojson.zlib はサーバー内部の描画用。公開リポジトリには含めず、
公開HTTP経路も設けない。公開する画像に上記の出典と加工者を表示する。

TYPE=2 の338予報区間のみを選択。FAREACODEをDCR-017の12桁予報区域コードと
完全照合し、先頭ゼロを保持する。名称は azarashi 0.17.1 のDCR表から取得する。
57の「その他の河川」に地理形状を割り当てず、TYPE=1の中小河川流路も採用しない。
全てのPolyLineパート・座標を保持してMultiLineStringへ変換する。簡略化、丸め、
区間の追加・結合・削除は行わない。元の経緯度（JGD2000）を保持する。
流路は予報対象区間の概略表示であり、河岸、浸水範囲、氾濫の発生位置ではない。
未掲載の河川・地域の安全を表さない。

再生成（azarashi 0.17.1 に加え scripts/requirements-flood-assets.txt を導入）:
  python scripts/prepare_flood_assets.py --source /path/to/flow-20260520.zip
--sourceを省略すると固定URLを取得。--outputで出力先を指定できる。
データは未公開プレビュー用。描画時には外部データを取得しない。
'''


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def forecast_code(value):
    if (not isinstance(value, str) or len(value) != 12
            or not value.isascii() or not value.isdigit() or int(value) not in EXPECTED_CODES):
        raise ValueError('Unknown, malformed or non-specific flood forecast code')
    return value


def convert(code, shape):
    """Retain the source segments exactly; never join disconnected branches."""
    code = forecast_code(code)
    if shape.shapeType != 3:  # ESRI PolyLine, as specified by JMA No.10704.
        raise ValueError('Flood forecast geometry must be PolyLine')
    starts = list(shape.parts)
    points = list(shape.points)
    if (not starts or starts[0] != 0 or any(type(index) is not int for index in starts)
            or any(a >= b for a, b in zip(starts, starts[1:])) or starts[-1] >= len(points)):
        raise ValueError('Invalid flood forecast line parts')
    coordinates = []
    for start, end in zip(starts, [*starts[1:], len(points)], strict=True):
        line = []
        for point in points[start:end]:
            if (len(point) != 2 or any(type(value) not in (int, float) or not math.isfinite(value)
                                      for value in point)
                    or not -180 <= point[0] <= 180 or not -90 < point[1] < 90):
                raise ValueError('Invalid flood forecast coordinate')
            line.append(list(point))
        if len(line) < 2 or all(point == line[0] for point in line):
            raise ValueError('Empty or degenerate flood forecast line')
        coordinates.append(line)
    feature = {'type': 'Feature', 'properties': {'code': code, 'name': flood_forecast_region[int(code)]},
               'geometry': {'type': 'MultiLineString', 'coordinates': coordinates}}
    statistics = {'source_components': len(starts), 'source_points': len(points),
                  'components': len(coordinates), 'points': sum(map(len, coordinates))}
    return feature, statistics


def source_features(path):
    import shapefile
    with tempfile.TemporaryDirectory(prefix='shirasu-flood-gis-') as directory:
        prefix = Path(directory) / 'flood'
        with zipfile.ZipFile(path) as archive:
            for extension in ('shp', 'shx', 'dbf'):
                name = 'flood_risk_map_all.' + extension
                if archive.namelist().count(name) != 1:
                    raise ValueError('Missing or duplicated flood source shapefile')
                with archive.open(name) as source, prefix.with_suffix('.' + extension).open('wb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
        with shapefile.Reader(str(prefix), encoding='ascii') as reader:
            if reader.shapeType != shapefile.POLYLINE:
                raise ValueError('Unexpected flood source geometry type')
            expected_fields = [('TYPE', 'C', 1, 0), ('RIVERCODE', 'C', 8, 0), ('FLOODRISK', 'C', 1, 0),
                               ('FAREACODE', 'C', 12, 0), ('FLOODFCST', 'C', 1, 0)]
            if [tuple(field) for field in reader.fields[1:]] != expected_fields:
                raise ValueError('Unexpected flood source fields')
            other_count = 0
            for index, record in enumerate(reader.iterRecords()):
                if record.TYPE == '1':
                    other_count += 1
                    continue
                if record.TYPE != '2':
                    raise ValueError('Unexpected source river type')
                yield forecast_code(record.FAREACODE), reader.shape(index)
            if other_count != 23820:
                raise ValueError('Unexpected non-forecast river coverage')


def prepare(source, output):
    for name, expected in (('azarashi', '0.17.1'), ('pyshp', '2.3.1')):
        if version(name) != expected:
            raise ValueError(f'Use pinned {name}=={expected}')
    if file_hash(source) != SOURCE_SHA256:
        raise ValueError('Official flood source changed; review before updating')
    features, statistics = {}, {}
    for code, shape in source_features(source):
        if code in features:
            raise ValueError('Duplicate flood forecast source code')
        features[code], statistics[code] = convert(code, shape)
    if ({int(code) for code in features} != EXPECTED_CODES or len(features) != 338
            or len(NON_SPECIFIC) != 57):
        raise ValueError('Incomplete flood forecast code coverage')
    raw = (json.dumps({'type': 'FeatureCollection', 'features': [features[code] for code in sorted(features)]},
                      ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False) + '\n').encode()
    if len(raw) > MAX_EXPANDED_BYTES:
        raise ValueError('Flood asset exceeds runtime expansion limit')
    compressed, notice = zlib.compress(raw, 9), NOTICE.encode()
    manifest = {
        'flood.geojson.zlib': {
            'source': SOURCE, 'source_sha256': SOURCE_SHA256, 'retrieved': DATE,
            'specification': SPECIFICATION, 'general_data_policy': POLICY,
            'individual_redistribution_license': 'Not established; geometry is private renderer input, not a public data download.',
            'source_coordinate_system': 'JGD2000',
            'sha256': hashlib.sha256(compressed).hexdigest(), 'bytes': len(compressed),
            'uncompressed_bytes': len(raw), 'compression': 'zlib; level=9', 'feature_count': len(features),
            'excluded_non_specific_codes': [f'{code:012d}' for code in sorted(NON_SPECIFIC)],
            'excluded_type_1_features': 23820,
            'processing': 'TYPE=2 only; exact 12-digit FAREACODE/DCR-017 match; preserve leading zeros and all PolyLine parts and coordinates; no simplification, joining or inferred regions.',
            'geometry': statistics,
        },
        'FLOOD_NOTICE.txt': {'sha256': hashlib.sha256(notice).hexdigest(), 'bytes': len(notice)},
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'flood.geojson.zlib').write_bytes(compressed)
    (output / 'FLOOD_NOTICE.txt').write_bytes(notice)
    (output / 'flood_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/preview')
    args = parser.parse_args()
    if args.source:
        result = prepare(args.source, args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='shirasu-flood-source-') as directory:
            source = Path(directory) / 'source.zip'
            with urllib.request.urlopen(SOURCE, timeout=60) as response, source.open('wb') as target:
                shutil.copyfileobj(response, target, 1024 * 1024)
            result = prepare(source, args.output)
    entry = result['flood.geojson.zlib']
    print(f"Wrote {entry['feature_count']} flood forecast reaches, {entry['bytes']} compressed bytes")


if __name__ == '__main__':
    main()
