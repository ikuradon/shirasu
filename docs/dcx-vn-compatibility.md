# 国内DCX Vn=0の互換性調査

調査日: 2026-11-10。対象コード: `4e8762b`、固定デコーダー: `azarashi==0.17.1`。

## 結論と対応方針

**内容を持つ国内DCX通報のVn=0を、正式な旧仕様として地図表示へ追加する根拠は確認できなかった。**
公開仕様001〜004は初版から国内通報のVnを`000001`に固定している。
一方、2026-09-17の公式L1Sアーカイブには非NULLのVn=0試験通報が実在する。
手元のログの変換や、デコーダーのビット位置の誤りではない。

この結果から、国内Vn=0の一般対応は有効にせず、現在のトピック表示を維持する。
今回完了したのは互換性の調査であり、対応実装ではない。政令市の区境界、追加楕円、
自治体の追加範囲など、Vn=1で仕様が確認できる拡張は独立して進められる。

NULLメッセージでは拡張部が全0になるため、Vn=0自体をすべて誤りとは扱わない。
今回の209電文はA3が1〜4で、仕様のNULL条件に該当しない。
また、国外のVnは発信機関独自の管理であり、この結論を国外通報には適用しない。

## 公開仕様と試験資料

国内の根拠は各版の§4.2.4.1.8（Lアラート・自治体情報）と
§4.2.4.2.3（Jアラート）。国外は§4.2.4.3.2、NULLは表4.3-1を参照した。
PDFページは表紙を1ページ目とする。

| 公式仕様 | 国内Vn | 本文ページ / PDFページ（Lアラート等、Jアラート） |
| --- | --- | --- |
| [001](https://qzss.go.jp/en/technical/download/pdf/ps-is-qzss/is-qzss-dcx-001.pdf) | `000001`固定 | 35 / 41、40 / 46 |
| [002](https://qzss.go.jp/en/technical/download/pdf/ps-is-qzss/is-qzss-dcx-002.pdf) | `000001`固定 | 40 / 46、45 / 51 |
| [003](https://qzss.go.jp/en/technical/download/pdf/ps-is-qzss/is-qzss-dcx-003.pdf) | `000001`固定 | 40 / 46、45 / 51 |
| [004](https://qzss.go.jp/en/technical/download/pdf/ps-is-qzss/is-qzss-dcx-004.pdf) | `000001`固定 | 42 / 49、47 / 54 |

仕様書の版番号001〜004、CAMFのA10（ライブラリ版）、電文のVnは別の番号である。
A10ではコード0がライブラリ第1版を表すが、国内Vnの0を1と読み替える規定はない。

[2024年6月の公式試験配信資料](https://qzss.go.jp/info/information/rnhmsi0000003ltx-att/dcx_test_data_details_202406.pdf)
もVn=1を指定している（表2 / PDF6、表6 / PDF11）。初期の定期試験から同じ指定だった。

2026年9月の資料は、公式URLで取得できなかったため、
[公式文書の第三者保管コピー](https://s-taka.org/xfer/dcx202609.pdf)を照合に使った。
表紙は「2026年9月1日、9月17日配信予定」、2026年8月作成。
表2（PDF6）、表6（PDF11）、表12（PDF18）などもVn=1を指定している。
第三者保管であることを区別し、この資料だけを互換性の根拠にはしない。

## 固定ログとデコーダーの独立検証

対象は [固定fixture](../tests/fixtures/qzqsm/qzqsm_dcx_260917.log)。
SHA-256: `ff8887da0c458d72749b5d3e2526e695fcfa1e665c7a812cd67f00c0dc9e7726`。
同一電文の初出だけを残した211電文で、全件A1=0（試験）、A2=111（日本）。
通常運用でのVnの頻度を示すデータではない。

| 種別 | Vn=0 | Vn=1 |
| --- | ---: | ---: |
| Lアラート | 113 | 2 |
| Jアラート | 48 | 0 |
| 自治体情報 | 48 | 0 |
| 合計 | 209 | 2 |

004の図4.2-1、図4.2-2、図4.2-5、図4.2-6とパラメーター表から位置を導出し、
001の図でも同じ境界を確認した。以下はMSBから数えた0始まりの位置。

| 領域 | 開始ビット | 長さ |
| --- | ---: | ---: |
| CAMF | 24 | 122 |
| A12（主楕円の緯度） | 80 | 16 |
| Extended / EX1 | 146 | 74 / 16 |
| Vn | 214 | 6 |
| Reserved | 220 | 6 |
| CRC | 226 | 24 |

VnはExtended内の69番目からなので、`146 + 68 = 214`。
NMEAの63桁hexは252bitで、末尾2bitがpadding、先頭250bitが電文である。
整数シフト、ビット文字列、バイト列、hex桁の4方式でVnを抽出し、全件一致した。
公式表の幅から導出した全CAMFフィールドも`azarashi 0.17.1`と一致した。
NMEA XORと独立したGF(2)長除法によるCRC-24Qは、211件すべて正常。
チェックサムの正常性は、Vnを含む内容が仕様どおりであることまでは保証しない。

Vn=1の2件はfixtureの68行目・78行目（電文番号65・75）。どちらもLアラートで
A4=0（Not used）、EX1=22100。現行処理では`unknown_hazard`となる。
これらも公式原本に存在するが、9月の公表シナリオとの対応・配信理由は未確認。

## 公式L1Sアーカイブとの照合

[公式API](https://sys.qzss.go.jp/dod/en/api.html)から2026-09-17の3衛星分を取得した。
ここでいう原本は公式公開アーカイブを指す。
各3,110,365 bytes、86,399レコード。`azarashi`を使わず、先頭PRN 1byteと
GPS時刻4byte＋電文32byteのレコードを直接読み、250bitと零paddingを比較した。
原本のMT44各20,160レコードのCRCは全件正常だった。

| 原本 | 非NULLレコード数 | 非NULL Vn=0 / 1 | 一致するfixture電文数 |
| --- | ---: | ---: | ---: |
| [Q003](https://sys.qzss.go.jp/dod/api/get/l1s?id=Q003_20260917.l1s) | 114 | 114 / 0 | 57 |
| [Q004](https://sys.qzss.go.jp/dod/api/get/l1s?id=Q004_20260917.l1s) | 308 | 304 / 4 | 154 |
| [Q005](https://sys.qzss.go.jp/dod/api/get/l1s?id=Q005_20260917.l1s) | 422 | 418 / 4 | 211 |

原本のレコード数は再送を含む。NULL判定は表4.3-1に従いA1=0、A2=111、
ビット35〜219が全0とし、ReservedとCRCは判定から除いた。
Vn=0のNULLメッセージを、上表の非NULL件数に混ぜていない。

**fixture全211件のpayloadと、そのNMEA Satellite IDに対応する衛星の原本が一致した。**
Q005には全211件のpayloadが含まれていた。
3衛星の非NULL通報を時刻順に並べ、CAMF＋Extended（ビット24〜219）で初出を残すと
211件になり、payloadの並びもfixtureと一致した。同時刻に受信した衛星の選択は別に照合した。
Vn=1の2件もQ004/Q005にあり、初出はそれぞれ01:32:00 UTC、01:36:24 UTC。

上流の[ログ追加コミット](https://github.com/nbtk/azarashi/commit/4e395cad8022a29594307a9a38fd59cc6b2d0c3a)と
fixture取得元の固定コミット`fe5d632f56c1f600718a3727c2e19fa21aeffee2`のL1S→NMEA処理も
確認した。payloadのVnを書き換える処理はない。
上流の[固定goldenデータ](https://raw.githubusercontent.com/nbtk/azarashi/fe5d632f56c1f600718a3727c2e19fa21aeffee2/tests/golden/qzqsm_dcx_260917.ndjson)
も、Vn=0を`data.version.status = undefined`として扱っている。

9月の公表シナリオと、Vn=0のLアラート113件のA4=1〜113、JアラートのA3=2/3各24件、
自治体情報のA4=68/74/82各16件という構成は一致した。
ただし、シナリオ表のVn=1と原本のVn=0は一致しない。
一致するフィールドがあることから、未定義のVn=0全般の互換性までは導けない。

## 残る未確認点と対応を追加する条件

- 2026年9月の試験でVn=0が配信された理由、およびVn=1/A4=0の2件の位置づけ。
- 2023年12月〜2024年3月のサービス前試験にVn=0があったかどうか。
  公開仕様001〜004で定義されていないことと、過去に存在しなかったことは別である。
- Vn=0の全フィールド・無視指定・区域判定・避難指示を定義する公式資料や互換性の説明。

Vn=0対応を追加するには、公式な解釈の根拠と、それに基づく独立した期待値を先に確保する。
元電文をVn=1へ置換して描画できても、それだけでは互換性の検証にならない。
公開受信fixtureは変更せず、既存のVn=1合成例は引き続き合成と明示する。
今回、復号・描画条件、画像、公開済みURLの版は変更していない。

## fixture集計の再現

リポジトリルートで次を実行すると、ネットワーク・デコーダー・受信設定を使わず
Vnの位置、件数、NMEAとCRCを再確認できる。Python標準ライブラリだけを使用する。

```sh
python - <<'PY'
from collections import Counter
from hashlib import sha256
from pathlib import Path

source = Path('tests/fixtures/qzqsm/qzqsm_dcx_260917.log')
print('sha256:', sha256(source.read_bytes()).hexdigest())
counts = Counter()
nonzero = []
for line_no, sentence in enumerate(source.read_text().splitlines(), 1):
    if not sentence.startswith('$QZQSM,'):
        continue
    body, checksum = sentence.split('*')
    calculated = 0
    for byte in body[1:].encode('ascii'):
        calculated ^= byte
    assert calculated == int(checksum, 16)
    encoded = body.split(',')[2]
    assert len(encoded) == 63 and int(encoded, 16) & 3 == 0
    frame = int(encoded, 16) >> 2
    def field(start, width):
        return (frame >> (250 - start - width)) & ((1 << width) - 1)
    remainder = frame
    while remainder.bit_length() > 24:
        remainder ^= 0x1864CFB << (remainder.bit_length() - 25)
    assert remainder == 0
    assert (field(8, 6), field(24, 2), field(26, 9)) == (44, 0, 111)
    provider, vn = field(35, 5), field(214, 6)
    kind = {1: 'LAlert', 2: 'JAlert', 3: 'JAlert', 4: 'MTInfo'}[provider]
    counts[(kind, vn)] += 1
    if vn:
        nonzero.append((line_no, vn, field(40, 7), field(146, 16)))
print('kind/Vn:', sorted(counts.items()))
print('nonzero (line, Vn, A4, EX1):', nonzero)
print('checksum/CRC pass:', sum(counts.values()))
PY
```

期待値はLAlert/0=113、LAlert/1=2、JAlert/0=48、MTInfo/0=48、
非ゼロ行は`(68, 1, 0, 22100)`と`(78, 1, 0, 22100)`、checksum/CRC pass=211。

## 取得物の識別

PDF・1日分のL1S原本はリポジトリに追加していない。以下は2026-11-10取得時のSHA-256。

| 取得物 | SHA-256 |
| --- | --- |
| IS-QZSS-DCX-001.pdf | `ac8959cd60fb74ca15c2275e4c839138f8b3887b02808c26b5b2d5db24cb9f6b` |
| IS-QZSS-DCX-002.pdf | `e38e3a4f5bcc12ed28953fe820bff8ecab1088e8f5b0995248718a6306cd94ea` |
| IS-QZSS-DCX-003.pdf | `22119ed9637d2e6a0a8f63e2796b44a439898178956cee3779913c702854f681` |
| IS-QZSS-DCX-004.pdf | `038dd2c130998514ae4001545cc9ce99905a8a94fe6b3a9ae1ee1018605f299e` |
| dcx202609.pdf（第三者保管） | `b3185ed05feca95a2bdd9baa983f286a3e74cb568960bc0cf8e57ec7bbf0a1e9` |
| Q003_20260917.l1s | `dd24762000e14871689bf487053d8e35f27367f5d4889ac8f281a41e820f2e08` |
| Q004_20260917.l1s | `41db166f9370e46e9e897a31fc066c6f86b8ce5b64b877a01806a30b752a51e3` |
| Q005_20260917.l1s | `b22b64f58119bbf004f8f7c7c0d7a5d392028d9459758f9faab2a7e817ec51d9` |
