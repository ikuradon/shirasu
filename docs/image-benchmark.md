# 描画時間・画像形式の計測

未公開プレビューの同じ入力から、描画時間とPNG／WebP／JPEG／AVIFを比較するための
オフライン計測です。公開済みレンダラー、署名URL、投稿設定は変更しません。

## 今回の実測

Linux aarch64・論理4CPU、Python 3.12.16、Matplotlib 3.11.2、Pillow 13.0.0、
libwebp 1.6.0、libavif 1.4.2で計測しました。49入力×初回1回・再描画3回の計196描画と、
49入力×7形式の343画像で、描画・変換とも失敗はありません。
基準PNG49件は承認済みギャラリーの40ケース・46投稿とバイト単位で一致しています。

| 形式 | 49画像の容量合計 | PNG比 | 圧縮時間の中央値 | RGBA画素一致 |
| --- | ---: | ---: | ---: | ---: |
| PNG現行 | 12.131 MiB | 100% | 描画時間に含む | 49/49 |
| PNG最適化 | 10.912 MiB | 89.95% | 413.5 ms | 49/49 |
| WebP可逆・method 0 | 8.141 MiB | 67.11% | 109.1 ms | 49/49 |
| **WebP可逆・method 4** | **4.334 MiB** | **35.73%** | **489.0 ms** | **49/49** |
| WebP非可逆・Q90 | 3.608 MiB | 29.74% | 211.9 ms | 0/49 |
| JPEG Q90・4:4:4 | 7.854 MiB | 64.74% | 26.9 ms | 0/49 |
| AVIF Q90・speed 8 | 4.337 MiB | 35.75% | 365.8 ms | 0/49 |

圧縮時間は画像ごとの3回の中央値をさらに49画像で集計した中央値です。元PNG復号や
準備時間を含まない追加のエンコード時間で、PNG生成までの時間とは別です。

**採用候補は可逆WebPのmethod 4**です。今回の入力群ではPNGから約64%削減でき、
文字・警報色・県境を含む画素が完全一致しました。method 0は速い一方、見出しカードでは
PNGより大きくなる例もあり、一律の既定値にはしません。AVIF Q90は今回、可逆WebPと
ほぼ同容量で非可逆だったため優先しません。これは今回の設定の比較で、すべての品質設定での
形式の優劣を示すものではありません。PNG最適化は約10%減に対して追加圧縮が必要で、
JPEGは速いものの、今回の容量と画素維持の両方では可逆WebPが有利でした。

描画の中央値は、import 0.862秒、初回描画14.244秒、再描画6.054秒でした。
再描画は0.189〜16.641秒、描画時ピークRSSの最大値は473.9 MiBです。
トピック画像と地図画像で負荷が大きく異なります。

| 重い入力 | 初回描画 | 再描画中央値 | うちPNG保存 |
| --- | ---: | ---: | ---: |
| 緊急地震速報 | 25.494秒 | 16.641秒 | 0.168秒 |
| 震度情報 | 23.027秒 | 13.821秒 | 0.164秒 |
| 気象取消・明示元報参照 | 19.730秒 | 12.432秒 | 0.142秒 |
| 台風取消・明示元報参照 | 19.301秒 | 12.136秒 | 0.126秒 |
| 気象・複数区域の画像のみ | 19.045秒 | 11.777秒 | 0.159秒 |

重い例ではPNG保存は描画時間の約1%です。画像形式の変更は容量対策として進め、
描画速度は地図作成・配置・検証の処理を別に改善する方針です。
これらはこのローカル環境の値で、Vercelでの実応答時間ではありません。

緊急地震速報をウォームアップ後にcProfileでも確認しました。プロファイラーを含む24.19秒の
実行で、Matplotlibの`Axes._update_patch_limits`が累積14.60秒（20回）、地域の`_geometry`が
累積4.69秒でした。プロファイラーの負荷を含むため、通常計測の秒数とは直接比較しません。生成PNGは計測時と
一致しています。最初に見直す候補は、描画範囲が固定されている地図でのパッチ範囲再計算と、
地域の投影・Path生成の再利用です。この順に効果を検証します。
公開済みの共通レンダラーを変更せず、未公開側で画素・範囲の維持を検証してから進めます。
記録は`dist/benchmark/profile-eew/profile.json`と`render.prof`です。

生成物は`dist/benchmark/index.html`、測定値は同ディレクトリのJSON・CSV、
承認済みプレビューとの照合記録は`gallery-comparison.json`です。

## 計測方法

- 既存ギャラリー40ケースと46投稿の画像入力を同じ方法で組み立て、同じ電文・取消元報参照の
  組を重複排除した49種類を測ります。取消元報を指定した2画像は「画像のみ」用です。
  投稿の画像へ元報参照を追加しません。南海トラフは実際の添付と同じ代表の原電文1ページです。
- 画像ごとに独立したPythonプロセスと空のMatplotlibフォントキャッシュを用意します。
  import、初回描画、同じプロセスでの再描画3回を分離し、再描画の中央値・最小・最大を記録します。
  OSのファイルキャッシュは消去していません。「初回」はVercelの実コールドスタート時間ではありません。
- 描画時間にはPNG生成までを含めます。内部のPillowによるPNG保存時間も透過的に計測します。
  ピークRSSは描画が終わった段階のプロセス高水位です。画像変換前に取得します。
- すべて直列に処理します。画像キャッシュ、HTTP、CDN、ネットワークを経由しません。
  初回と再描画のPNGバイト列が一致することを検証します。
- 各形式への変換は最後のPNGを元に行います。元PNGの復号、色モードの準備、圧縮、変換後の復号を
  分け、圧縮・復号は各3回計測します。ブラウザではなくPillowのメモリ上の復号時間です。
  符号器内部の初回初期化がある場合は最初のサンプルへ含まれます。
- RGBA画素の完全一致と、白背景へ合成したRGBのPSNRを記録します。背景が広い地図では全画面の
  PSNRが文字・細線の差を隠すため、PSNRだけで見やすさを合否判定しません。

現在の方式で別形式を使う総生成時間は、**描画・PNG生成＋元PNG復号・準備＋変換**です。
変換時間だけをPNGの描画時間と比較して「描画が速くなる」とは判断できません。
将来、描画したRGBAから直接エンコードする方法は別途計測が必要です。

## 比較する形式

| 形式 | 設定 | 画質の扱い |
| --- | --- | --- |
| PNG現行 | レンダラーが出したバイト列を保持 | 基準 |
| PNG最適化 | `optimize=True`、不透明画像はRGB化 | RGBA画素一致を必須とする |
| WebP可逆・高速 | `lossless=True, quality=100, method=0, exact=True` | RGBA画素一致を必須とする |
| WebP可逆・標準 | `lossless=True, quality=100, method=4, exact=True` | RGBA画素一致を必須とする |
| WebP非可逆 | `quality=90, method=4` | 色付き文字や細線を目視確認 |
| JPEG | `quality=90, subsampling=0, optimize=True`（4:4:4） | 色付き文字や細線を目視確認。透過があれば白背景へ合成 |
| AVIF | `quality=90, speed=8, subsampling="4:4:4", max_threads=1` | 利用環境にcodecがあれば比較。可逆として扱わない |

各形式のquality値は同じ画質を表しません。可逆WebPのqualityは圧縮への努力量を調整する設定です。
設定の意味は[Pillow公式](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html)に従います。

## 再実行

プレビュー用アセットと描画依存が入った環境で実行します。

```sh
python -B scripts/benchmark_images.py --list
python -B scripts/benchmark_images.py --output dist/benchmark
# 一部だけ測る場合。既存の出力は上書きしません。
python -B scripts/benchmark_images.py --output dist/benchmark-weather --case weather-many
```

画像用コンテナから実行する場合:

```sh
mkdir -p dist
docker run --rm --network none \
  --volume "$PWD:/source:ro" --volume "$PWD/dist:/output" --workdir /source \
  shirasu-images:preview python -B scripts/benchmark_images.py --output /output/benchmark
```

`--warm-repeats`と`--encoding-repeats`は1〜10、各workerの`--timeout`は秒単位です。
全件の初回・反復描画と複数形式への変換を行うため、通常のギャラリー生成より時間がかかります。
他のCPU負荷を避けた同じ環境で比較してください。CIでは結果の正しさをテストし、時間の閾値は設けません。

出力は以下です。

- `index.html`: 容量・処理時間の一覧と、同じ画像の形式・表示幅を切り替える比較画面。
- `manifest.json`: 計測条件・Python／ライブラリ／codec版・各回の測定値・画素比較・画像ハッシュ。
- `results.csv`: 画像×形式ごとの測定結果。
- `records/*.json`: 原入力・参照・ケース／投稿との対応・出典。
- `images/*/`: 実際の各形式画像とworkerの計測結果。

worker失敗やタイムアウトがあっても結果を保存し、後続を処理します。形式変換だけの失敗も
別に集計し、いずれかが失敗した実行は終了コード1です。既存出力は上書きしません。

## 形式を採用する場合

WebP／AVIFは主要な現行ブラウザで扱えますが、Nostrの全クライアントの対応を保証するものではありません。
実際に使うクライアントでURL展開・画像表示を確認します。
[ブラウザの形式対応](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/Image_types)、
[NIP-92](https://github.com/nostr-protocol/nips/blob/master/92.md)。

以前の投稿プレビュー調査と同じnostterの固定commit
`78e22b7a7e79825290ee779a958a368eb90833f7`では、`.webp`と`.avif`が画像の拡張子として
明示的に認識されます。[MediaType.ts](https://github.com/SnowCait/nostter/blob/78e22b7a7e79825290ee779a958a368eb90833f7/web/src/lib/media/MediaType.ts#L1)、
[Url.svelte](https://github.com/SnowCait/nostter/blob/78e22b7a7e79825290ee779a958a368eb90833f7/web/src/lib/components/content/Url.svelte#L166)。
任意の画像最適化設定が有効だと、クライアント側で幅800・quality60のWebPへ再変換する経路もあります。
既定は無効です。[Img.svelte](https://github.com/SnowCait/nostter/blob/78e22b7a7e79825290ee779a958a368eb90833f7/web/src/lib/components/content/Img.svelte#L20)、
[Preference.ts](https://github.com/SnowCait/nostter/blob/78e22b7a7e79825290ee779a958a368eb90833f7/web/src/lib/stores/Preference.ts#L34)。
これはソース上の確認であり、公開中の配布commitや実投稿での表示は別途確認が必要です。

現在はURL拡張子、HTTPのContent-Type、Nostrの`imeta`がPNG固定です。採用時は形式と
エンコード設定をURL契約へ固定し、形式も署名対象に含め、Content-Typeと`imeta`を揃えます。
既存PNG URL・PNGバイト列・ETagは維持します。

Vercelは現行仕様ではAcceptとAccept-Encodingも既定キャッシュキーへ含みます。ただし、
今回の署名URLと固定MIMEの構成では、形式をURLで明示する方が結果を追いやすいという設計判断です。
内容交渉を使うなら下流キャッシュ向けのVaryも整合させる必要があります。
[Vercel CDN cache](https://vercel.com/docs/caching/cdn-cache#vary-header)、
[MDN Vary](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Vary)。
