# 出典とライセンス

shirasuは、みちびき（QZSS）から受信した災危通報をazarashiで復号し、文章や画像に整えてNostrへ投稿します。
自作コードは[MITライセンス](../LICENSE)です。第三者コード・地図・フォント・通報本文には、各権利者の条件が適用されます。
Nostrへの署名・投稿によって、通報本文や画像に使う第三者の内容を本体MITで再許諾するものではありません。

## 地図・フォント・受信データ

| 対象 | 出典・条件の案内 |
| --- | --- |
| 気象庁の区域・地点等 | [vendorの取得元別案内](https://github.com/ikuradon/shirasu/blob/main/vendor/README.md)。各NOTICEに出典・加工内容・加工者を記録。[気象庁利用規約](https://www.jma.go.jp/jma/kishou/info/coment.html)の第三者権利の扱いにも従う |
| 洪水流路 | [FLOOD_NOTICE](https://github.com/ikuradon/shirasu/blob/main/vendor/jma/preview/FLOOD_NOTICE.txt)。JMBSC配布・気象庁加工・国土数値情報を原典とする。個別ZIPの再配布許諾は未確認で、採用した配布方針と許諾確認を区別する |
| Natural Earth | [出典・加工記録](https://github.com/ikuradon/shirasu/tree/main/vendor/natural-earth)。地理データは[public domain](https://www.naturalearthdata.com/about/terms-of-use/) |
| Noto Sans CJK JP 2.004 | [OFL-1.1・著作権表示](https://github.com/ikuradon/shirasu/tree/main/vendor/noto-cjk/2.004)。未加工フォントと一緒に原文を保持 |
| QZQSM受信サンプル | [fixtureの出典・MIT本文](https://github.com/ikuradon/shirasu/tree/main/tests/fixtures/qzqsm)。upstreamのMIT表示と原通報の権利は別 |
| QZSS公式試験資料 | [fixture README](https://github.com/ikuradon/shirasu/blob/main/tests/fixtures/qzqsm/README.md)に転記元と内容を記録。[QZSSサイト規約](https://qzss.go.jp/policy/rule.html)を参照。衛星受信電文全体への包括許諾とは扱わない |

画像配布では地図・フォントのNOTICEとライセンスを`image_service/assets/`内に保持します。
上記の公開URLから、受信側イメージに含まれないデータの出典も確認できます。

## Python依存と同梱ライセンス

固定バージョンは[受信側](https://github.com/ikuradon/shirasu/blob/main/requirements/receiver.txt)と
[画像側](https://github.com/ikuradon/shirasu/blob/main/image_service/requirements.txt)のrequirementsにあります。
下表はライセンス本文への案内です。著作権表示・許諾文・免責文は、インストールされた
wheelの`site-packages/<package>-<version>.dist-info/`内にある`LICENSE`、`licenses/`、
`NOTICE`等を削除せず保持します。
CIは固定依存の版、本文の存在、wheelの`RECORD`との一致を検査します。
この検査は、全ネイティブ依存の特定や利用条件の適合判定を代替しません。

| 依存 | 固定版 | ライセンス・補足 |
| --- | --- | --- |
| [azarashi](https://github.com/nbtk/azarashi) | 0.17.1 | MIT。災危通報の復号 |
| [nostr-sdk](https://pypi.org/project/nostr-sdk/0.45.1/) | 0.45.1 | 本体MIT。Rust依存の確認状況は下記 |
| [pyserial](https://pypi.org/project/pyserial/3.5/) | 3.5 | BSD-3-Clause。wheelに本文がないため[別途同梱](pyserial-3.5-LICENSE.txt) |
| [Matplotlib](https://matplotlib.org/stable/project/license.html) | 3.11.2 | PSFを基にしたMatplotlib独自ライセンス。同梱部品の条件も保持 |
| contourpy / cycler / kiwisolver | 1.4.0 / 0.12.1 / 1.5.1 | BSD-3-Clause |
| fonttools / pyparsing | 4.68.0 / 3.3.3 | MIT。fonttoolsの`LICENSE.external`も保持 |
| [NumPy](https://numpy.org/doc/stable/license.html) | 2.5.4 | 本体BSD-3-Clause。付属コード・native部品は同梱の個別条件 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [Pillow](https://pillow.readthedocs.io/en/stable/about.html#license) | 13.0.0 | MIT-CMU。native部品の条件も同梱LICENSEに収録 |
| python-dateutil | 4.0.0 | メタデータはBSD-3-Clause。旧BSDと2017年以降のApache/BSD選択の説明を含むLICENSE・AUTHORSを保持 |
| tzdata | 2026.6 | PythonパッケージはApache-2.0。収録データの表示も保持 |
| websockets / pyshp | 17.3 / 2.3.1 | BSD-3-Clause / MIT。テスト専用 / 洪水データ作成専用 |

## ネイティブ部品と配布文書

[FreeType・Independent JPEG Groupへの謝辞](ACKNOWLEDGEMENTS.txt)を配布物に同梱します。
FreeTypeには[FTLを選択](https://freetype.org/license.html)します。

linux/arm64検証イメージでは、PillowにFreeType 2.14.3、libjpeg-turbo 3.2.0、
libwebp 1.6.0が含まれます。MatplotlibにもFreeTypeが組み込まれています。
libwebpはBSD-3-Clause、libjpeg-turboはIJG等の条件に従い、各本文を保持します。
NumPyにはOpenBLAS 0.3.34.106.0 / LAPACKとlibgfortranが含まれます。
OpenBLAS / LAPACKのBSD系条件、libgfortranのGPL-3.0-or-laterと
[GCC Runtime Library Exception 3.1](https://github.com/gcc-mirror/gcc/blob/master/COPYING.RUNTIME)は別々に扱います。
例外があることだけで、ランタイム自体の再配布義務がなくなるわけではありません。

Dockerイメージ内の主な原文の場所は次のとおりです。

- `/usr/local/lib/python3.12/site-packages/pillow-13.0.0.dist-info/licenses/LICENSE`
- `/usr/local/lib/python3.12/site-packages/matplotlib-3.11.2.dist-info/LICENSE`
- `/usr/local/lib/python3.12/site-packages/numpy-2.5.4.dist-info/licenses/LICENSE.txt`
- `/usr/local/lib/python3.12/site-packages/nostr_sdk-0.45.1.dist-info/licenses/LICENSE`
- `/usr/local/lib/python3.12/LICENSE.txt`（CPython）と`/usr/share/doc/`（OSパッケージ）
- `/app/licenses/`（本書、謝辞、pyserialの補完本文）

## 確認範囲

原文の所在とnative部品の確認はlinux/arm64検証イメージに基づきます。
Rust推移依存、実配布物のnativeソース提供条件、別アーキテクチャ・OS全体の確認は未完了です。
洪水流路等の許諾確認を含む残件は[TODO](https://github.com/ikuradon/shirasu/blob/main/TODO.md)に記録しています。
