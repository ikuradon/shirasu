# QZQSM画像の運用

受信機は通報をまとめて署名付きURLを作り、画像はVercel側で描画します。
PNG／WebPの永続保存やBlob/R2は使いません。受信機にMatplotlib・地図・フォントは入りません。
表示内容とHTTP仕様は [画像サービス](image_service/README.md) を参照してください。

運用先は [shirasu-images](https://shirasu-images.vercel.app/healthz)（Vercelの `g-s-n` チーム）です。
プロジェクトは受信機のGitリポジトリには直接接続せず、検証済み候補の画像パッケージをデプロイします。
[GHAによる検証・公開](docs/vercel-release.md)を有効化済みです。mainはPreview URL、正式タグはGHCR復元・候補のHTTPS検証後に本番へ接続します。2026-11-12に`images-v5-r2`を公開し、候補・本番とも95画像の検証に成功しました。同日に受信機も同じ固定配布版のv5/w1へ切り替えました。[受信機の切替記録](docs/release-compose.md)を参照してください。

## 投稿の動き

画像を有効にすると、台風は同じ投稿アカウント・台風番号・発表日時（年を含む）・
情報種別・通報区分ごとに集約します。最初の新しい通報から180秒待って、受信できた
地点だけを1投稿・1画像にします。6通揃うことは前提にせず、重複では待ち時間を延ばしません。

集約途中の原文・元の受信日時・締切を既存SQLiteに保存します。再起動で失われず、
通常のバックアップにも含まれます。締切後の新しい地点は「追加更新」として、既知の
全地点を含む新しい画像URLで投稿します。訂正は未送信の古い集約を置き換え、取消は
該当する未送信の集約を抑制して即時に配送キューへ入ります。すでに署名して配送キューへ
入れた投稿や公開済み投稿は変更しません。

台風・南海トラフ以外は集約待ちをせず従来の配送キューへ入ります。画像を添える通報では本文と画像URLを併記し、
対応するNostrクライアント用に `imeta` も付けます。画像表示に未対応のクライアントでも
本文を読めます。

新規の画像付き投稿では、`imeta` の `alt` に情報種別、JSTの日時、訓練・試験／訂正／取消／解除を
短く記録します。DCRは発表日時、DCXは復号に使う受信基準日時を明示し、確認できない日時や状態を
補いません。取消・解除では古い現象値を有効な情報として説明せず、詳細は投稿本文へ案内します。
画像の描画方法に依存しない文面で、地図・要旨・トピックへ切り替わる場合も使えます。
代替テキストの生成だけが失敗した場合は従来の説明に戻し、本文と画像の投稿を続けます。
代替テキストも署名イベントに保存するため、再送時には作り直しません。
本番の固定配布版に反映済みです。保存済み投稿の代替テキストは変更しません。

南海トラフは、ページをまとめてテキスト主体の1投稿にします。画像の有効／無効に
関係なく受信したページを独自にSQLiteへ保存し、全ページが揃えばすぐに全文を配送キューへ入れます。
完成後に180秒待つことはなく、受信途中のページごとの投稿もしません。途中のページと締切は再起動後も
引き継ぎ、通常のDBバックアップに含まれます。

最初のページから既定180秒で揃わなければ、見出し・情報種別・JSTの発表時刻・受信状況などの
メタ情報だけを1回通知します。誤解を避けるため、未完成の本文断片は添えません。
重複ページで締切を延ばさず、後着のページで全文が完成した場合は全文を追加で1回投稿します。
訂正では未確定の古い組立を置き換え、取消では該当する組立を抑制して即時にメタ情報を通知します。
同じページ番号で本文が変わった場合は、それまでの断片を破棄して集め直し、待ち時間も新たに計ります。

訓練／訂正／取消も本文で明示します。v5では、完成した全文と取消報に
16:9の見出しカードを添えます。見出し・情報種別・JSTの発表日時・訓練／訂正／取消だけを
表示し、本文・ページ数・地理的範囲は画像に補いません。「調査終了」は解除として扱いません。
画像用URLには代表の原電文1ページを従来のJSON＋zlib形式で渡し、投稿本文には全ページのNMEAを残します。
未完了通知やUTF-8として読めない完成本文には画像を付けず、後着で本文が完成すれば画像付きの全文を追加投稿します。
公開済みv1〜v4は見出しカードに未対応です。通常配布の`ImagePublisher.supports_nankai_topic`は無効で、
新しい集約処理からそれらのURL・`imeta`は付けません。[固定した候補パッケージ](docs/image-release-rehearsal.md)内の
v5で有効にし、本文・署名URL・画像・ALTを通し検証して本番へ反映しています。
すでに配送キューへ保存した署名イベントと公開済み画像URLは維持します。

画像URLは署名済みイベントとともに保存し、再送で作り直しません。画像URL生成に
失敗したときも本文を配送し、`--status` の「画像URL生成エラー」に記録します。
Vercelへの問い合わせは受信・配送経路では行いません。描画サービスの一時停止中も
本文の配送は継続し、画像はサービス復旧後に同じURLから取得できます。

## 公開するソースの準備

本番には、GitHub Actionsで検証した候補の画像パッケージだけをデプロイします。
必要なファイルを列挙してコピーするため、
`config.py`、Nostr鍵、SQLite、バックアップ、`.env` が混入しません。
下のコマンドで作る通常パッケージはv1〜v4のローカル確認用です。既存の出力先は上書きしません。
v5を含む候補パッケージは
[一括公開前の通し検証](docs/image-release-rehearsal.md)で別途作成します。
候補v5の受信側には[公開用Compose設定](docs/release-compose.md)を使います。
正式な候補は[GitHub Actionsで検証・保管](docs/ci-release.md)し、GHCRの固定digestから復元した`images/`を公開に使います。
通常checkoutのビルドはv1〜v4用なので、環境変数だけをv5/w1へ変更して起動する手順ではありません。

地図・フォント・出典は取得元別の[`vendor/`](vendor/README.md)で固定管理します。
新しいcheckoutでは最初に `prepare_vendor_assets.py` を実行してください。
manifestのSHA-256とサイズを照合して従来の `image_service/assets/` へ配置するだけで、
外部データのダウンロードやGISの再加工は行いません。既存ファイルが一致すれば保持し、異なれば停止します。

```sh
python3 -B scripts/prepare_vendor_assets.py
python3 scripts/prepare_image_service.py dist/image-service
```

VercelはPython 3.12、Framework PresetはOtherで、固定候補の`images/`をデプロイします。
`requirements.txt` は描画用依存をハッシュ付きで固定しています。
`api/image.py` がWSGIを公開し、`vercel.json` が署名パスをその関数へ振り分けます。
全国の区域を含む通報の配置判定と要旨への切替に余裕を持たせ、関数の実行上限は60秒です。
本番ドメインはNostrクライアントが認証なしで取得できる設定にします。

## 鍵と受信側の設定

Nostr鍵とは別に、暗号学的乱数32バイトの画像専用HMAC鍵を用意します。
VercelのProduction環境変数 `SHIRASU_IMAGE_KEYS` にkey-idと64桁hex鍵のJSONを登録します。
Previewでも確認する場合は同じ環境変数が必要です。

専用鍵は状態DBのバックアップには含まれません。Nostr鍵と同様に別途保管し、
画像サービスを再構築するときも同じ検証鍵を復元できるようにします。

```text
SHIRASU_IMAGE_KEYS={"k1":"<64桁のhex鍵>"}
```

受信機は[固定イメージのCompose設定](docs/release-compose.md)を先に適用します。
リポジトリ直下の `.env` に次を設定し、ファイルの権限を600にします。
このファイルと `state/` はGitの対象外です。鍵を含むCompose設定全体を画面へ出さず、
設定確認には `docker compose config --quiet` を使います。

```text
SHIRASU_IMAGE_BASE_URL=https://<本番ドメイン>
SHIRASU_IMAGE_KEY_ID=k1
SHIRASU_IMAGE_SIGNING_KEY=<同じ64桁のhex鍵>
SHIRASU_IMAGE_VERSION=v5
SHIRASU_IMAGE_ENCODING=w1
SHIRASU_TYPHOON_WAIT_SECONDS=180
SHIRASU_NANKAI_WAIT_SECONDS=180
```

先頭の3項目が全て空なら画像は無効です。一部だけの設定は起動時にエラーになります。
BASE_URLにはHTTPSのoriginだけを指定します。台風の集約待ちと、南海トラフの未完了通知までの
待ち時間はそれぞれ1〜300秒で、配送の再送期限を設定している場合はその期限より短くします。
`SHIRASU_NANKAI_WAIT_SECONDS` は画像設定から独立して有効で、全ページが揃った時点で待ち時間を打ち切ります。
集約待ちの生通報と配送待ちは `MAX_PENDING` の上限を共有します。

通常配布の`SHIRASU_IMAGE_VERSION` は `v1`（既定）・`v2`・`v3`・`v4` です。v2では海上警報・震度・津波、
v3では気象・緊急地震速報・火山の地図、v4では気象の情報ごとの配色を追加します。
変更するのは新しい投稿のURLだけで、
保存済みの署名イベントや投稿済みURLを作り直しません。受信入力形式は各版で共通ですが、
版もHMACに含めるため、パスの版だけを書き換えたURLは拒否します。

公開後はまず `/healthz`、正規署名URLのPNG／WebP、改変URLの拒否を確認します。
固定イメージを使う`COMPOSE_FILE`を維持して、`docker compose up -d --no-build --pull never shirasu` で受信側の設定を反映し、
`docker compose exec -T shirasu python main.py --status --format text` を確認します。
画像を停止するときは先頭3項目を全て空にして再作成します。既存の台風の集約は締切後に
テキストで配送し、新規の台風は従来の即時キュー投入に戻ります。南海トラフのページ集約は
画像停止後も継続します。

## キャッシュと世代管理

WebPへの接続は[固定した公開候補](docs/webp-integration.md)で検証済みです。
本番はその配布版を使い、`v5`＋`SHIRASU_IMAGE_ENCODING=w1`で運用しています。
通常配布の対応はv1〜v4のPNGで、形式の既定値も`png`です。形式設定だけでは画像は有効になりません。
通常配布やv1〜v4で`w1`を指定すると起動時の設定エラーになります。

PNGのURLは `/v1/<key-id>/<payload>.<signature>.png`、`/v2/...`、`/v3/...`、`/v4/...`、固定配布版では`/v5/...`です。
payloadはキー順を固定したJSON、zlib、paddingなしbase64urlの順に変換します。
元のNMEAと元の受信Unixミリ秒を含め、復号の年月補完も再現します。HMACはバージョン・
key-id・payloadを対象にします。署名確認後にサイズ上限付きで展開・復号します。

本番のWebPは`/v5/<key-id>/<payload>.<signature>.w1.webp`です。PNGとは別の署名ドメインで
描画版・key-id・payload・`w1`・`webp`を認証し、形式や設定を書き換えたURLを拒否します。
`w1`は可逆・quality 100・method 4・exactを固定し、Pillow 13.0.0／libwebp 1.6.0以外では
生成を拒否します。符号器を変えるときは別プロファイルを追加し、公開済み`w1`は残します。
新規投稿の`imeta`も`m image/webp`になります。PNGへ切り戻しても、保存済みWebP投稿の
URL・本文・署名は作り直さず、画像サービスの`w1`経路と検証鍵を維持します。
PNGへの切り戻しでは`SHIRASU_IMAGE_ENCODING=png`を明示します。描画版もv4へ戻す場合は
版と形式の両方を変更します。v4と`w1`の組は設定エラーです。

CDNはこのパス全体でキャッシュし、保持されていれば再描画を省きます。保存容量を
予約する仕組みではないため、消えたキャッシュは同じURLから再生成します。エラーは
`no-store`、成功PNGは1年間のキャッシュを許可します。短期の期限・nonceは付けません。

公開済みURLの取得は誰でも可能です。HMACは任意の入力から新しい画像を作る操作を制限し、
公開URLの再取得を禁止するものではありません。古い投稿を維持する間は旧検証鍵を残します。
鍵を漏らした場合はそのkey-idを廃止できますが、その鍵を使う旧画像URLも取得できなくなります。

既存URLのPNGが変わらない修正では版を上げません。新しい種類の画像やデザイン調整は、
未公開の作業版でまとめて実装し、プレビューで確認します。変更のたびに本番反映・版追加を
行わず、公開の区切りで差分を確認し、既存URLの出力が変わる場合に新しい版を追加します。
表示・デコーダ・依存・アセットを変更する場合も、既存の出力への影響を確認します。
旧版を同時に残せない大きな依存変更では、旧サービスを維持した上で新版を別サービスへ
切り出してください。Vercelの再デプロイはCDNキャッシュを持ち越す保証にはなりません。

v5は専用の候補パッケージ内で登録し、全ファイルのハッシュをmanifestに保存します。
検証済みの配布物を固定して公開し、後日の作業版を同じv5へ重ねません。
受信側の新規投稿をv4へ切り戻しても、保存・投稿済みのv5 URLを維持するため画像サービスと検証鍵は残します。
候補の作成・照合・切り戻し条件は[通し検証の手順](docs/image-release-rehearsal.md)を参照してください。

## 検証

CIは受信機と画像サービスを別のイメージで検証します。描画だけを確認する例:

```sh
python3 -B scripts/prepare_vendor_assets.py
python3 scripts/prepare_image_service.py dist/image-test
docker build -f image_service/Dockerfile -t shirasu-images:test dist/image-test
docker run --rm --network none --volume "$PWD:/source:ro" --workdir /source \
  shirasu-images:test python -B -m unittest discover -s tests -p test_image_renderer.py -v
```

ローカルプレビュー・回帰テストはNostrへ投稿しません。
複数の種類を一覧で比較する場合は、[オフライン画像ギャラリー](image_service/README.md#オフライン画像ギャラリー)を使います。
公開受信ログと合成訓練の40ケースをPNG・HTMLへ一括出力し、出典・訓練区分や解除／取消を確認できます。
受信側の `--preview` は南海トラフも各ページを読み取った時点で表示し、受信途中は「受信中」を示します。
実投稿用のSQLiteへのページ保存・待ち時間・集約配送は再現しません。

未公開の画像を確認する場合は、`--include-preview` を明示してローカル用のソースを作ります。
通常の配布用ソースには未公開レンダラー・アセットが入りません。
洪水の流路を含む固定アセットは `vendor/` からオフラインで配置します。
通常配布とプレビューの両方で同じ準備コマンドを使います。

```sh
python3 -B scripts/prepare_vendor_assets.py
python3 scripts/prepare_image_service.py --include-preview dist/image-preview
docker build -f image_service/Dockerfile -t shirasu-images:preview dist/image-preview
docker run --rm --network none \
  --volume "$PWD/image_service/examples:/samples:ro" --volume /tmp:/output \
  shirasu-images:preview python -B -m image_service --preview \
  --records /samples/northwest_pacific_tsunami.json --output /output/nwp-preview.png
```

これはオフラインの確認専用です。HTTP・署名URL・受信機の画像バージョンは変わりません。
降灰予報の訓練例は、同じコマンドの入力を `--records /samples/ash_fall.json`、
出力を `--output /output/ash-preview.png` にして確認できます。
指定河川洪水予報は `--records /samples/flood.json`、`--output /output/flood-preview.png` です。
これらは固定配布版v5に収録済みです。通常checkoutのv1〜v4用パッケージには含めません。
台風・震源・火山・気象・緊急地震速報・海上警報・震度・津波の拡大図も、
プレビューでは県境と右下のミニマップを使います。公開済みの描画経路は維持します。

[公開受信ログ](tests/fixtures/qzqsm/README.md)からの描画も、同じプレビュー用イメージで検証できます。
このテストは、固定した受信データから画像URLの署名・検証とPNG描画を行い、
公式訓練の降灰予報にある3市町村・6時間分の18項目も照合します。

```sh
docker run --rm --network none --volume "$PWD:/source:ro" --workdir /source \
  shirasu-images:preview python -B -m unittest discover -s tests -p test_public_qzqsm_images.py -v
```
