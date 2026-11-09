#!/usr/bin/env python3
"""Pin official NWPTAC forecast points to the DCR-017 coastal region codes.

Run with azarashi==0.17.1. The public table's figure numbers are NOT DCR codes.
--source accepts a saved copy of the pinned HTML; runtime uses only the asset.
"""

import argparse
import hashlib
from html.parser import HTMLParser
from importlib.metadata import version
import json
from pathlib import Path
import re
import urllib.request

from azarashi.definitions.qzss.dcr.coastal_region import coastal_region_en

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://www.data.jma.go.jp/eqev/data/nwptac/forecastpoint.html'
SOURCE_SHA256 = '4d91a6b422ffdd19e3a20439aa6eca027289e61565cdb66fc5f4897b75202ddb'
TERMS = 'https://www.jma.go.jp/jma/kishou/info/coment.html'
DATE = '2026-11-10'
NON_SPECIFIC = {99, 100}

# Explicit DCR code -> (JMA forecast-point token, DCR point name). These are
# checked against both source tables, including the coastal block names.
# In particular, 73 is reserved; Guam/Saipan and Rabaul/Kavieng are reversed
# relative to the official HTML's figure numbering.
POINT_CODES = {
    1: ('UST_KAMCHATSK', 'Ust-Kamchatsk'),
    2: ('PETROPAVLOVSK_K', 'Petropavlovsk-K'),
    3: ('SEVERO_KURILSK', 'Severo Kurilsk'),
    4: ('URUP_IS.', 'Urup Islands'),
    5: ('BUSAN', 'Busan'),
    6: ('NOHWA', 'Nohwa'),
    7: ('SEOGWIPO', 'Seogwipo'),
    8: ('HUALIEN', 'Hualien'),
    9: ('BASCO', 'Basco'),
    10: ('PALANAN', 'Palanan'),
    11: ('LEGASPI', 'Legaspi'),
    12: ('LAOANG', 'Laoang'),
    13: ('MADRID', 'Madrid'),
    14: ('DAVAO', 'Davao'),
    15: ('BEREBERE', 'Berebere'),
    16: ('PATANI', 'Patani'),
    17: ('SORONG', 'Sorong'),
    18: ('MANOKWARI', 'Manokwari'),
    19: ('WARSA', 'Warsa'),
    20: ('JAYAPURA', 'Jayapura'),
    21: ('VANIMO', 'Vanimo'),
    22: ('WEWAK', 'Wewak'),
    23: ('MADANG', 'Madang'),
    24: ('MANUS_IS.', 'Manus Islands'),
    25: ('RABAUL', 'Rabaul'),
    26: ('KAVIENG', 'Kavieng'),
    27: ('KIMBE', 'Kimbe'),
    28: ('KIETA', 'Kieta'),
    29: ('GUAM', 'Guam'),
    30: ('SAIPAN', 'Saipan'),
    31: ('MALAKAL', 'Malakal'),
    32: ('YAP_IS.', 'Yap Island'),
    33: ('CHUUK_IS.', 'Chuuk Island'),
    34: ('POHNPEI_IS.', 'Pohnpei Island'),
    35: ('KOSRAE_IS.', 'Kosrae Island'),
    36: ('ENIWETOK', 'Eniwetok Island'),
    37: ('PANGGOE', 'Panggoe'),
    38: ('AUKI', 'Auki'),
    39: ('KIRAKIRA', 'Kirakira'),
    40: ('MUNDA', 'Munda'),
    41: ('HONIARA', 'Honiara'),
    66: ('OSTROV_KARAGINSKIY', 'Ostrov-Karaginskiy'),
    67: ('NIKOLSKOYA', 'Nikolskoya'),
    68: ('TONGYEONG', 'Tongyeong'),
    69: ('HEUKSANDO', 'Heuksando'),
    70: ('CHEJU_ISLAND', 'Cheju-Island'),
    71: ('CHILUNG', 'Chilung'),
    72: ('TAITUNG', 'Taitung'),
    74: ('HOMEL', 'Homel'),
    75: ('GEME', 'Geme'),
    76: ('ULAMONA', 'Ulamona'),
    77: ('GHATERE', 'Ghatere'),
    78: ('AMUN', 'Amun'),
    79: ('FALAMAE', 'Falamae'),
    80: ('MISIMA', 'Misima'),
    81: ('ALOTAU', 'Alotau'),
    82: ('LAE', 'Lae'),
    83: ('PORT_MORESBY', 'Port-Moresby'),
    84: ('SHANGHAI', 'Shanghai'),
    85: ('ZHOUSHAN', 'Zhoushan'),
    86: ('WENZHOU', 'Wenzhou'),
}

NOTICE = '''NWPTAC forecast points / 北西太平洋津波予測地点の加工について

出典: 気象庁 Northwest Pacific Tsunami Advisory Center
NWPTA FORECAST POINTS AND COASTAL BLOCKS
https://www.data.jma.go.jp/eqev/data/nwptac/forecastpoint.html
2026年11月10日取得。入力のSHA-256は nwp_manifest.json に記録。
気象庁ホームページの利用規約（公共データ利用規約 第1.0版）に基づき利用。
https://www.jma.go.jp/jma/kishou/info/coment.html

気象庁データを Shirasu が加工して作成。気象庁が作成した画像ではない。
元表の61地点の緯度・経度を数値化し、南緯を負値に変換。地点の移動や補間は行わない。
公式表の図中番号とIS-QZSS-DCR-017の沿岸地域コードは別物であり、
scripts/prepare_nwp_assets.py の明示的な対応表で名称と沿岸ブロックを照合する。
DCRコード1〜41および66〜86（予約の73を除く）の61地点に対応。
不明（99）、その他（100）、予約コードには位置を割り当てない。
nameは公式の地点名、decoder_nameはazarashi 0.17.1の通報名称。
figure_numberは元表の図中番号であり、通報コードではない。

座標は津波予測の代表地点であり、津波の観測点、浸水域、沿岸全体の
危険度や未掲載地域の安全を示すものではない。

再生成（azarashi 0.17.1 の環境で実行）:
  python scripts/prepare_nwp_assets.py
保存した公式HTMLを使う場合:
  python scripts/prepare_nwp_assets.py --source /path/to/forecastpoint.html
出力先は --output で変更できる。このデータは公開前のプレビュー用。
'''


class _TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag == 'td' and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.row is not None and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = self.cell = None


def coordinate(text, axis):
    matched = re.fullmatch(r'(\d+(?:\.\d+)?)([NSEW])', text)
    if matched is None:
        raise ValueError('Invalid coordinate in JMA forecast-point table')
    value, hemisphere = float(matched[1]), matched[2]
    limit, allowed = (90, 'NS') if axis == 'latitude' else (180, 'EW')
    if axis not in ('latitude', 'longitude') or hemisphere not in allowed or value > limit:
        raise ValueError('Invalid coordinate axis, hemisphere or bounds')
    return -value if hemisphere in 'SW' else value


def parse_points(data):
    """Validate every official point by name; never map by row or figure order."""
    parser = _TableRows()
    parser.feed(data.decode('utf-8-sig'))
    by_name, block = {}, None
    for cells in parser.rows:
        if len(cells) not in (4, 5) or not cells[-1].isdigit():
            continue
        if len(cells) == 5:
            block, *cells = cells
        name, lat, lon, number = cells
        if not block or name in by_name:
            raise ValueError('Missing coastal block or duplicated forecast point')
        by_name[name] = {'name': name, 'coastal_block': block,
                         'latitude': coordinate(lat, 'latitude'),
                         'longitude': coordinate(lon, 'longitude'), 'figure_number': int(number)}
    if (set(by_name) != {names[0] for names in POINT_CODES.values()}
            or len(by_name) != 61
            or {point['figure_number'] for point in by_name.values()} != set(range(1, 62))):
        raise ValueError('Official forecast-point coverage mismatch')
    if set(POINT_CODES) != set(coastal_region_en) - NON_SPECIFIC:
        raise ValueError('DCR coastal-code coverage mismatch')
    result = []
    for code, (official_name, decoder_name) in sorted(POINT_CODES.items()):
        point = by_name[official_name]
        expected = f"{decoder_name} ({point['coastal_block'].title()})"
        if coastal_region_en[code].casefold() != expected.casefold():
            raise ValueError(f'DCR forecast-point name or coastal-block mismatch: {code}')
        result.append({'code': code, **point, 'decoder_name': coastal_region_en[code]})
    return {'points': result}


def prepare(data, output):
    if version('azarashi') != '0.17.1':
        raise ValueError('Use the pinned azarashi 0.17.1 code table')
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise ValueError('Official source changed; review before regenerating the asset')
    result = parse_points(data)
    asset = (json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    notice = NOTICE.encode()
    manifest = {
        'nwp_points.json': {
            'source': SOURCE, 'source_sha256': SOURCE_SHA256, 'retrieved': DATE,
            'terms': TERMS, 'sha256': hashlib.sha256(asset).hexdigest(), 'bytes': len(asset),
            'feature_count': len(result['points']), 'excluded_non_specific_codes': sorted(NON_SPECIFIC),
            'reserved_codes_in_mapped_range': [*range(42, 66), 73],
            'processing': 'Explicit DCR-017 code/name mapping checked against azarashi 0.17.1 and JMA coastal-block names; N/E positive, S/W negative; no interpolation, shifting or inferred positions. Figure numbers retained separately from DCR codes.',
        },
        'NWP_NOTICE.txt': {'sha256': hashlib.sha256(notice).hexdigest(), 'bytes': len(notice)},
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'nwp_points.json').write_bytes(asset)
    (output / 'NWP_NOTICE.txt').write_bytes(notice)
    (output / 'nwp_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return len(result['points'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/preview')
    args = parser.parse_args()
    if args.source:
        data = args.source.read_bytes()
    else:
        with urllib.request.urlopen(SOURCE, timeout=30) as response:
            data = response.read()
    print(f'Prepared {prepare(data, args.output)} official NWP forecast points')


if __name__ == '__main__':
    main()
