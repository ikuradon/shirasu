# 署名付き QZQSM 画像サービス

受信機とは別に動かす、Python 3.12 / WSGI の画像レンダラーです。画像の入力は
`/v1/<key-id>/<JSON+zlib+base64url>.<HMAC-SHA256>.png` 、`/v2/...`、`/v3/...` に含まれます。
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

## デプロイと HTTP 契約

デプロイパッケージには `image_service/` と共通の `image_protocol.py` が必要です。
Vercel の `api/image.py` から `from image_service.app import app` で WSGI アプリを公開し、
ルートのすべてのリクエストをこの関数へ rewrite します。受信機の `config.py`、SQLite、
バックアップ、秘密鍵をパッケージに含めないでください。

- `GET /healthz`: 鍵設定とv1/v2/v3全ての固定アセットを検査します。成功は `200`、設定不備は `503`。
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
v2・v3も公開後は固定です。次の変更は新しい描画経路として追加し、古い経路と検証鍵を残してください。
Matplotlib の描画と azarashi の multipart 組立状態はリクエスト全体のロックで保護し、
各リクエストの前後で組立状態を破棄します。
