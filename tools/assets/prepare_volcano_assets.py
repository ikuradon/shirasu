#!/usr/bin/env python3
"""Build v3's volcano positions from a pinned official JMA map catalogue.

Run with azarashi==0.17.1. --source accepts an already downloaded catalogue;
otherwise only the pinned public URL below is fetched. Runtime rendering never
uses the network. Review changed upstream data before publishing a new version.
"""

import argparse
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import urllib.request

from azarashi.definitions.qzss.dcr.volcano_name import volcano_name

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'https://www.jma.go.jp/bosai/volcano/const/volcano_list.json'
SOURCE_SHA256 = '0dd309cb4d9dc62dc1e9330b945c78e4a58416a55b21535bcd16e5181799eb5a'
TERMS = 'https://www.jma.go.jp/jma/kishou/info/coment.html'
DATE = '2026-11-10'
NO_POSITION = {900, 901, 902, 4000}

NOTICE = '''JMA volcano position catalogue / 気象庁火山位置データの加工について

出典: 気象庁ホームページ「あなたの街の防災情報」の火山一覧
https://www.jma.go.jp/bosai/volcano/const/volcano_list.json
2026年11月10日取得。入力のSHA-256は volcano_manifest.json に記録。
気象庁ホームページの利用規約（公共データ利用規約 第1.0版）に基づき利用。
https://www.jma.go.jp/jma/kishou/info/coment.html

気象庁データを Shirasu が加工して作成。気象庁が作成した画像ではない。
元一覧の117地点の緯度・経度を数値化して経度・緯度順に保存し、火山コードを
IS-QZSS-DCR-017 / azarashi 0.17.1 と照合。名称は通報の固定コード表に合わせ、
全角括弧のみを正規化して照合している。画面上のラベル移動量は使用しない。
全国・その他・新たな活火山（900/901/902）とその他の火山（4000）には、
位置を割り当てていない。知らない地点を他の火山の位置で代用しない。

座標は火山の代表位置であり、今回噴火した火口の観測位置、危険半径、
警戒範囲、避難対象区域ではない。市町村名を受信してもその境界は推定しない。
レベル1や「活火山であることに留意」は「安全」や通報取消を意味しない。

再生成（azarashi 0.17.1 の環境で実行）:
  python tools/assets/prepare_volcano_assets.py
既存の取得ファイルを使う場合:
  python tools/assets/prepare_volcano_assets.py --source /path/to/volcano_list.json
出力先は --output で変更できる。公開済み世代のアセットは上書きしないこと。
'''


def prepare(data, output):
    if version('azarashi') != '0.17.1':
        raise ValueError('Use the frozen azarashi 0.17.1 code table')
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise ValueError('JMA catalogue changed; review before creating a new asset version')
    result = {}
    for row in json.loads(data):
        code = int(row['code'])
        if code in NO_POSITION:
            if 'latlon' in row:
                raise ValueError('A non-specific volcano unexpectedly has a position')
            continue
        name = row['name_jp'].replace('（', '(').replace('）', ')')
        if code not in volcano_name or name != volcano_name[code] or str(code) in result:
            raise ValueError('Volcano code/name mismatch')
        lat, lon = map(float, row['latlon'])
        if not (math.isfinite(lat) and math.isfinite(lon) and -85 < lat < 85 and -180 <= lon <= 180):
            raise ValueError('Invalid official volcano position')
        result[str(code)] = {'name': volcano_name[code], 'coordinates': [lon, lat]}
    if set(map(int, result)) != set(volcano_name) - NO_POSITION or len(result) != 117:
        raise ValueError('Volcano catalogue coverage mismatch')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    asset = (json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (output / 'volcano.json').write_bytes(asset)
    notice = NOTICE.encode()
    (output / 'VOLCANO_NOTICE.txt').write_bytes(notice)
    manifest = {
        'volcano.json': {
            'source': SOURCE, 'source_sha256': SOURCE_SHA256, 'retrieved': DATE,
            'terms': TERMS, 'sha256': hashlib.sha256(asset).hexdigest(), 'bytes': len(asset),
            'feature_count': 117, 'excluded_codes': sorted(NO_POSITION),
            'processing': 'Official latlon strings converted to numeric [longitude, latitude]; no shifting or interpolation. Names checked against azarashi 0.17.1 after normalizing parentheses.',
        },
        'VOLCANO_NOTICE.txt': {'sha256': hashlib.sha256(notice).hexdigest(), 'bytes': len(notice)},
    }
    (output / 'volcano_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return len(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'image_service/assets/v3')
    args = parser.parse_args()
    if args.source:
        data = args.source.read_bytes()
    else:
        with urllib.request.urlopen(SOURCE, timeout=30) as response:
            data = response.read()
    print(f'Prepared {prepare(data, args.output)} official volcano positions')


if __name__ == '__main__':
    main()
