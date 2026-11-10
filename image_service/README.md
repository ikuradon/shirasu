# 署名付き QZQSM 画像サービス

受信機とは別に動かす、Python 3.12 / WSGI の画像レンダラーです。画像の入力は
`/v1/<key-id>/<JSON+zlib+base64url>.<HMAC-SHA256>.png` 、`/v2/...`、`/v3/...`、`/v4/...` に含まれます。
共有する `image_protocol.py` が署名とサイズを検証してから、azarashi で復号します。
現在時刻や外部 API は参照せず、URL に入っている受信時刻を復号の日付の基準にします。

PNG はメモリ内で生成して返します。Vercel Blob / R2 / データベースや PNG 保存先は不要です。
CDN のキャッシュが消えた場合は同じ入力から再生成します。Matplotlib のフォントキャッシュ
だけは一時ディレクトリを使用します。

## v1 の表示

- 台風: 同じ発表の中心位置を一枚の地図に集約します。実況は水色、推定・予報は白。
  時刻は JST で日付を併記し、年をまたぐ場合は年も表示します。予報円・暴風域は描きません。
- 震源情報: 受信した震央を地図に表示します。震度分布や揺れの到達範囲は推定しません。
- その他の DCR / DCX: 復号した通報のテキスト要旨を表示します。長文は画像内に抜粋である旨を明記します。
  未対応の地理データを警戒区域に見立てて描くことはしません。
- 訓練・試験、取消、DCX の解除を目立つ位置に表示します。取消の台風に予報経路は表示しません。
  未定義の位置・時刻・台風時刻分類はテキスト表示に切り替えます。

地図の範囲とラベル位置はデータから計算します。ラベルは実際の文字サイズで測り、
文字・引き出し線・予報経路が互いに重ならない配置を探索します。位置ラベルが入り切らない場合も要旨に
切り替えます。束ねられるのは同じ災害種別、発表日時、情報種別、通報区分の通報です。
異なる台風や同時刻の相反する位置を混ぜた入力は拒否します。入力は最大 32 通報です。

## ローカルで確認

依存は受信機の環境へ追加せず、別の環境へインストールします。

```sh
python3.12 -m venv /tmp/shirasu-images-venv
/tmp/shirasu-images-venv/bin/pip install --require-hashes -r image_service/requirements.txt
/tmp/shirasu-images-venv/bin/python -m image_service \
  --records image_service/examples/typhoon.json --output /tmp/typhoon-preview.png
/tmp/shirasu-images-venv/bin/python -m unittest discover -s tests -p 'test_image_renderer.py'
```

オフラインプレビューだけは指定した新規 PNG ファイルへ書き込みます。既存ファイルは上書きしません。
同梱サンプルは 2026-11-09 に受信した公開台風通報で、最新情報ではありません。

## オフライン画像ギャラリー

15種類・20ケースについて、公開済み版と作業版を横に並べるHTMLとPNGを一括生成します。
画像サービス用の依存関係と、[洪水の流路データ](#指定河川洪水予報のプレビュー)を準備した後、
リポジトリルートで次の1コマンドを実行します。

```sh
/tmp/shirasu-images-venv/bin/python -B scripts/render_gallery.py --output dist/gallery
```

`dist/gallery/index.html` をブラウザで開くと、種類・実受信／アーカイブ／合成・訓練区分・
発表／解除／取消で絞り込めます。検索では地名や「離島」「多数区域」も指定できます。
PNGを選ぶと原寸で表示し、出典・復号テキスト・入力JSONを各ケースから確認できます。
HTML・PNG・JSONはすべて同じ出力フォルダに収まり、サーバーやネット接続は必要ありません。

比較する公開済み版は既定でv4です。稼働中のサービス設定とは独立しており、
`--published-version v1` などで変更できます。作業版のラベルを新しい公開バージョンとして
扱うことはありません。画像の「地図／要旨」は実際に保存された図から判定します。

```sh
python -B scripts/render_gallery.py --list
python -B scripts/render_gallery.py --output dist/gallery-ash --case ash-detail
python -B scripts/render_gallery.py --output dist/gallery-v1 --published-version v1 \
  --case typhoon-track --case marine
```

`--list` は描画依存や地図アセットなしでも、受信側の依存関係だけで動作します。
描画コマンドには上記の画像サービス用Pythonを使用してください。
既存の出力先は上書きしません。比較を残す場合は新しい出力先を指定します。
各セルの描画失敗は一覧に明示し、他の画像は生成を続け、終了コード1を返します。
必要なアセットがない場合は、出力フォルダを作る前に停止します。

Dockerでは、プレビュー用イメージを用意した後に次のコマンドで生成できます。

```sh
mkdir -p dist
docker run --rm --network none --volume "$PWD:/source:ro" --volume "$PWD/dist:/output" \
  --workdir /source shirasu-images:preview \
  python -B scripts/render_gallery.py --output /output/gallery
```

台風7時点、降灰5電文・18項目、複数区域の気象、解除・取消・離島、DCXを収録しています。
ケース定義は `scripts/gallery_catalog.py`、公開ログの出典は
[共通fixture](../tests/fixtures/qzqsm/README.md)にあります。
公開ログの日付基準は個別の受信時刻ではなく、年を持たない電文の復号用です。
元の公開ログと合成した訓練例を区別し、派生例の変更内容も一覧に記録します。
南海トラフはテキストのみの運用方針に合わせ、この画像一覧から除外しています。

このツールはリポジトリから実行する確認用です。受信サービスやVercel配布パッケージには
含めず、秘密鍵・設定・配送DB・外部通信・投稿処理を使用しません。
`manifest.json` に各入力の由来・出力PNGのハッシュ・描画結果を保存し、
公開ログのMITライセンスも出力フォルダへ同梱します。

## 未公開画像のプレビュー

`--preview` で未公開の作業版を描画します。`--records` 専用で、`--version`・`--serve`
とは併用できません。指定しなければ従来どおりv1です。HTTP経路や署名対象の版は追加しません。
最初に、後述する[洪水の流路データの準備](#指定河川洪水予報のプレビュー)を行ってください。
流路データ本体はリポジトリに含めず、プレビューの固定アセットとしてローカルに生成します。

```sh
/tmp/shirasu-images-venv/bin/python -m image_service --preview \
  --records image_service/examples/northwest_pacific_tsunami.json --output /tmp/nwp-preview.png
```

北西太平洋津波は[気象庁の予測地点表](https://www.data.jma.go.jp/eqev/data/nwptac/forecastpoint.html)の
61地点を通報の沿岸コードに対応させます。表の図中番号は通報コードではないため、地点名と
沿岸ブロックを明示的に照合しています。受信した地点だけを表示し、震央・伝播経路・浸水範囲は描きません。

- 津波高は範囲、10m超、Huge、High、不明を区別します。国内の津波警報区分には置き換えません。
- 発表・到達予想の時刻はUTCです。翌日・翌月・翌年への繰り越しを保持し、`31:63` は
  「到達済み、または到達時刻不明」と表示します。実際の00:00を解除用の仮時刻とは扱いません。
- 同じ地点の矛盾、未知の地点・高さ、未対応の通報区分・情報種別、読める配置を作れない場合は、
  受信束全体を従来の要旨画像へ渡します。要旨が長い場合は従来どおり抜粋である旨を明記します。
- 津波発生の可能性なし・その他の可能性情報は、活動中の地点予報に変換せず要旨画像で表示します。

`assets/preview/nwp_manifest.json` に出典・取得日・入力と加工後のハッシュを記録しています。
`scripts/prepare_nwp_assets.py` で再生成できます。実行時に地図や地点データを外部から取得しません。
同梱の北西太平洋津波サンプルは合成した訓練通報で、実際の災害情報ではありません。

未公開コードとアセットは通常の配布パッケージから除外します。ローカルDockerで確認する場合は、
新しい出力先に `scripts/prepare_image_service.py --include-preview dist/image-preview` を実行して
ビルドします。プレビューのパッケージは本番デプロイには使用しません。専用プレビューのない画像はv4と同じPNGです。

### 地図の県境と位置表示

未公開プレビューでは洪水・降灰に加え、台風・震源・火山・気象・緊急地震速報・海上警報・震度・津波・
北西太平洋津波の地図にも、薄い県境と必要に応じた右下のミニマップを使います。
県境は日本の原典から抽出した背景の補助線で、危険度を示す線とは区別します。
地域を拡大した図には主図の表示範囲を白枠で示し、全国・広域の図には小図を重複して加えません。
海外の予測地点や日付変更線をまたぐ図でも、同じ経度の扱いで表示位置を示します。

離島が主図の外にある場合は、その区域を位置表示のミニマップへ統合して保持します。
河川・中心位置・対象区域・ラベル・引き出し線が小図で隠れない配置を使い、
情報を保った読みやすい配置を作れない場合は受信束全体の要旨へ戻します。
新しい描画は `--preview` のみで、公開済みv1〜v4のPNG・URL・固定アセットは維持します。

### 降灰予報のプレビュー

```sh
/tmp/shirasu-images-venv/bin/python -m image_service --preview \
  --records image_service/examples/ash_fall.json --output /tmp/ash-preview.png
```

QZQSMの降灰予報（速報／詳細）に含まれる火山・基点時刻・市町村・基点から1〜6時間・
現象を表示します。同じ市町村の異なる時間や、降灰と小さな噴石が同時に含まれる場合も残します。
「基点＋N時間」という通報の時間値を保持し、開始時刻や予報期間の境界を推定しません。
基点時刻と発表時刻はJSTで区別して表示します。

現象ごとのラベルは[気象庁の降灰予報の説明](https://www.jma.go.jp/jma/kishou/know/kazan/qvaf/qvaf_guide.html)
の色分けに合わせ、少量は灰色、やや多量は黄、多量は赤。小さな噴石は独立した赤枠にします。
市町村の輪郭は中立色とし、市町村全域の降灰分布・噴石の落下範囲とは区別します。
火山には公式代表位置の三角印を付けます。噴煙形状、風向き、灰や噴石の軌道は描きません。

地理データは[気象庁の市町村等（火山関係）GIS](https://www.data.jma.go.jp/developer/gis.html)の
1745区域をDCRコード・名称と直接照合しています。「その他の市町村」47コードには区域を割り当てません。
全polygon・穴を保持し、簡略化の面積差を1%以内に収め、離島は必要に応じて小図に残します。
出典・ハッシュ・除外した地物・加工統計は `assets/preview/municipalities_manifest.json` に記録。
`scripts/prepare_municipality_assets.py` で再生成できます。GIS用の依存は加工専用環境だけに必要です。

取消、未知の値、基点や火山の不一致、同じ市町村・時間の矛盾する降灰量、地図や読みやすい配置が
不足する場合は、受信束全体を従来の要旨画像へ渡します。サンプルは桜島の合成訓練通報です。

### 指定河川洪水予報のプレビュー

流路データの加工用依存は受信機や描画サービスへ追加せず、準備専用の環境で使います。
既存の画像用依存に含まれる `azarashi==0.17.1` と、次のハッシュ固定した `pyshp` が必要です。

```sh
python3.12 -m venv /tmp/shirasu-flood-assets
/tmp/shirasu-flood-assets/bin/pip install --require-hashes -r image_service/requirements.txt
/tmp/shirasu-flood-assets/bin/pip install --require-hashes -r scripts/requirements-flood-assets.txt
/tmp/shirasu-flood-assets/bin/python scripts/prepare_flood_assets.py
/tmp/shirasu-images-venv/bin/python -m image_service --preview \
  --records image_service/examples/flood.json --output /tmp/flood-preview.png
```

準備スクリプトは[気象庁提供の固定流路データ](https://www.jmbsc.or.jp/jp/online/c-onlineGsd-oshirase.html#date20260520)
の2026年5月20日版を取得し、SHA-256とDCRのコード表を検証します。保存済みZIPを使う場合は
`--source /path/to/flow.zip` を指定できます。ダウンロードやGIS処理は準備時だけに行い、描画時には外部通信しません。

指定河川洪水予報用の338区間を12桁の予報区域コードで直接対応させ、先頭の0と全ての分岐・区間を保持します。
関連市町村の面、川全体、浸水想定区域には置き換えません。「その他の河川」57コードには位置を割り当てません。
原典は国土数値情報を気象庁が加工した表示用の流路で、現在の川岸や浸水範囲を精密に示すものではありません。
背景には気象庁の都道府県データから抽出した県境を薄く重ね、右下の日本周辺図に主図の表示範囲を枠で示します。
県境は位置を把握するための補助線です。ミニマップには河川・ラベル・引き出し線が重ならない余白を確保します。
県境は原典の隣接県が共有する線を抽出してから一度だけ簡略化し、`assets/preview/prefecture_boundaries_manifest.json`
に出典・ハッシュを記録します。再生成は `scripts/prepare_prefecture_boundary_assets.py`（加工専用のPyshp・Shapely環境）を使います。

氾濫警戒情報は赤、氾濫危険情報は紫、氾濫発生情報は黒とし、黒い線には明るい縁取りを付けます。
通報の名称を保持し、DCRの内部番号を避難の警戒レベルとして表示しません。
警報解除は中立色の破線と明示的なラベルで示し、氾濫注意情報への切替を含むため安全宣言とは扱いません。
訓練・試験・訂正を明記し、取消、未知の区間・値、同一区間で矛盾する値、流路を共有する区間で異なる情報、読める配置を作れない場合は
受信束全体を既存の要旨表示へ戻します。サンプルは合成した訓練通報で、実際の災害情報ではありません。

`assets/preview/flood_manifest.json` と `FLOOD_NOTICE.txt` に出典・入力と加工後のハッシュ・加工内容を記録します。
ZIP固有の再配布条件は公開資料で確定できていないため、生成する `flood.geojson.zlib` はGit対象外です。
ローカル／サービス内部の描画に使い、GIS自体を公開配布する機能は設けません。
PNGには国土数値情報・気象庁の加工とShirasuによる画像作成を明記します。

## v2 の地域別表示

v1の描画をそのまま残し、新しく発行するv2 URLで次の3種類を地図化します。

- 海上警報: 気象庁の地方海上予報区を塗り、区域名と受信した警報を併記します。
  同じ海域の着氷・霧・風などは省略せず、解除は別の色と文字で表示します。
- 震度情報: 都道府県の輪郭と代表位置に受信震度を示します。県全体を同じ震度で
  塗ることはしません。同じ県で複数値を受信した場合は全て併記し、色だけ最大の受信値にします。
  離島が遠く離れている場合は主図を拡大し、離島を含む全域を地図内の小図で補います。
- 津波情報: 津波予報区の沿岸を強調し、予想高と到達予想時刻・状態を表示します。
  浸水範囲や津波の伝播は描きません。解除通報の欠測代用の午前0時は、
  [みちびきの公式通知](https://qzss.go.jp/info/information/dcr_260428.html)に従って時刻として表示しません。

色のない地域は「安全」「警報なし」「揺れなし」を意味しません。画像内にもこの
読み方を明記します。訓練・訂正・取消を区別し、未知の区域、矛盾する区域の重なり、
ラベルや防災上の留意事項が収まらない場合は、通報全体の要旨に戻します。
地域を一部だけ落として地図を完成させることはありません。

区域は [気象庁のGISデータ](https://www.data.jma.go.jp/developer/gis.html) と公式地図の
表示用GeoJSONを固定しています。`assets/v2/manifest.json` に出典・加工方法・ハッシュ、
`JMA_NOTICE.txt` に利用条件と出典表記があります。海域・津波予報区の集約コードは
`groups.json` で基本区域に明示的に展開し、地名のあいまいな一致は使いません。

```sh
/tmp/shirasu-images-venv/bin/python -m image_service --version v2 --records image_service/examples/marine.json --output /tmp/marine.png
/tmp/shirasu-images-venv/bin/python -m image_service --version v2 --records image_service/examples/seismic_intensity.json --output /tmp/intensity.png
/tmp/shirasu-images-venv/bin/python -m image_service --version v2 --records image_service/examples/tsunami.json --output /tmp/tsunami.png
```

海上サンプルは過去の公開通報です。震度・津波サンプルは、分類を訓練に設定した合成通報で、
実際の災害情報ではありません。これらのコマンドは投稿を行いません。

`SHIRASU_IMAGE_KEYS` を設定して `python -m image_service --serve` を実行すると
`127.0.0.1:8080` で WSGI サーバーが起動します。このサーバーはローカル確認用です。
署名鍵は Nostr の鍵とは別の暗号学的乱数 32 バイトを使い、環境変数には次の形で設定します。
実際の秘密鍵をリポジトリ、URL、ログに記録しないでください。

```text
{"k1":"<32 bytes encoded as 64 hexadecimal characters>"}
```

## v3 の予報区域・火山表示

v1/v2 の表示を保ち、v3 URL で次を追加します。

- 気象: 府県予報区の輪郭と受信した警報・情報を表示します。北海道・東京/伊豆・
  鹿児島/奄美・沖縄の細分を都道府県全体へ置き換えません。共通の発表/解除を区別し、
  「全ての気象特別警報」は仕様どおり解除の場合だけ扱います。
- 緊急地震速報: 受信した警報対象区域と、通報全体に1組の予測震度範囲を表示します。
  各区域に固有の震度を割り当てず、震央地名から座標を推定しません。長周期の予測、
  発生時刻、深さ・M、防災上の留意事項を保持します。PLUMの仮定震源はその旨を明記し、
  固定の深さ・Mを実際の地震値として示しません。
- 火山: 気象庁の公式代表位置に印を付け、受信した警戒レベル・現象・活動時刻・
  対象市町村を併記します。「噴火」と「噴火したもよう」、時刻の曖昧さを保持します。
  危険半径、火口の広がり、避難対象区域は推定しません。

地図の輪郭は通報が指す予報区域です。区域全体が一様な危険域であるとは限らず、
未掲載地域の安全も意味しません。離島を含む全範囲を保持し、必要に応じて小図を使います。
未知・矛盾する入力や読める配置を作れない場合は、通報全体の要旨に戻します。

地域データは `assets/v3/regional_manifest.json`、火山位置は `volcano_manifest.json`、
v3専用のNatural Earth 10m底図は `land_manifest.json` に出典・入力ハッシュ・加工条件を
記録しています。大きなGeoJSONはzlib圧縮で同梱し、
実行時にハッシュと展開上限を検証します。外部の地図をリクエスト時に取得しません。
再生成は `scripts/prepare_v3_region_assets.py`、`scripts/prepare_volcano_assets.py`、
`scripts/prepare_v3_land_assets.py` を使います。

```sh
/tmp/shirasu-images-venv/bin/python -m image_service --version v3 --records image_service/examples/weather.json --output /tmp/weather.png
/tmp/shirasu-images-venv/bin/python -m image_service --version v3 --records image_service/examples/eew.json --output /tmp/eew.png
/tmp/shirasu-images-venv/bin/python -m image_service --version v3 --records image_service/examples/volcano.json --output /tmp/volcano.png
```

v3 のサンプルは全て訓練分類の合成通報で、実際の災害情報ではありません。投稿は行いません。

## v4 の気象配色

地域名・代表点は白、予報区の輪郭は灰色のまま、各情報の名称を独立したラベルで表示します。
同じ地域の複数情報を一つの代表色にまとめません。

| 受信情報 | ラベル |
| --- | --- |
| 気象特別警報（DCRコード1〜6） | 黒 `#0c000c` 地・白字・白枠 |
| 土砂災害警戒情報（23） | 紫 `#aa00aa` 地・白字 |
| 記録的短時間大雨情報（21）・竜巻注意情報（22） | 塗りなし・白字・白枠 |
| 解除 | 塗りなし・灰色の字と枠、各情報名に「（解除）」 |

黒・紫は[気象庁の配色指針（表1-1）](https://www.jma.go.jp/jma/kishou/info/colorguide/HPColorGuide_202007.pdf)
によります。21・22の情報そのものの固定色は同指針にないため、名称で区別します。
塗りなしは危険度が低いことを意味しません。竜巻注意情報を通常の注意報と同一視せず、
通報にない警戒レベルや新体系の警報名称も補いません。
解除は個々の受信情報に対するもので、地域全体の安全や「発表なし」を表しません。

パディングと枠を含むラベル寸法で配置を計算し、読める配置を作れない場合は全体を要旨に戻します。
v1〜v3の描画・固定アセットは変更せず、気象以外のv4画像はv3と同じPNGを返します。

```sh
/tmp/shirasu-images-venv/bin/python -m image_service --version v4 --records image_service/examples/weather.json --output /tmp/weather-v4.png
```

## デプロイと HTTP 契約

デプロイパッケージには `image_service/` と共通の `image_protocol.py` が必要です。
Vercel の `api/image.py` から `from image_service.app import app` で WSGI アプリを公開し、
ルートのすべてのリクエストをこの関数へ rewrite します。受信機の `config.py`、SQLite、
バックアップ、秘密鍵をパッケージに含めないでください。

- `GET /healthz`: 鍵設定とv1〜v4全ての固定アセットを検査します。成功は `200`、設定不備は `503`。
- `GET` / `HEAD` の署名付きパス: 成功は `image/png`、PNG 内容の SHA-256 を ETag に使用。
  `Cache-Control: public, max-age=31536000, immutable` と
  `Vercel-CDN-Cache-Control: public, max-age=31536000` を返します。
- `If-None-Match` が ETag と一致する場合は `304`。Cookie・認証ヘッダーは不要です。
- query は拒否します。署名不正は `403`、署名済みでも復号できない通報は `400`、
  レンダラーの不備は `503`。エラーと health は `no-store` です。

HMAC は新しい入力の作成を制限する仕組みです。公開済みの URL は誰でも繰り返し取得でき、
有効期限はありません。鍵をローテーションするときは新しい key-id を追加し、古い投稿を
維持する期間は古い検証鍵も残します。署名 URL は入力内容を暗号化するものではありません。

## 再現性とアセット

`assets/v1/` の Natural Earth 50m land、Noto Sans CJK JP Regular / Bold は固定コピーです。
出典・SHA-256 は `manifest.json`、ライセンスは `NATURAL_EARTH.txt`、`OFL.txt`、
`FONT_NOTICE.txt` にあります。実行時にハッシュを検証します。外部の地図・フォントを
取得しないので、表示される地形や字形は外部サービスの更新に影響されません。
日本語の地名を欠落させないためフォントを部分化していません。v1 のアセットは約 35 MB です。

公開後に v1 の見た目、デコード規則、依存、アセットを変更すると同じ URL の内容が変わります。
v2・v3・v4も公開後の出力を維持します。既存PNGが変わらない修正では版を上げません。
新しい種類の画像やデザイン調整は未公開の作業版とプレビューでまとめ、公開の区切りで
必要な場合だけ新しい描画経路を追加します。古い経路と検証鍵は残してください。
運用方針は [キャッシュと世代管理](../IMAGES.md#キャッシュと世代管理) を参照してください。
Matplotlib の描画と azarashi の multipart 組立状態はリクエスト全体のロックで保護し、
各リクエストの前後で組立状態を破棄します。
