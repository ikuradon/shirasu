# QZQSM画像の運用

受信機は通報をまとめて署名付きURLを作り、画像はVercel側で描画します。
PNGの永続保存やBlob/R2は使いません。受信機にMatplotlib・地図・フォントは入りません。
表示内容とHTTP仕様は [画像サービス](image_service/README.md) を参照してください。

運用先は [shirasu-images](https://shirasu-images.vercel.app/healthz)（Vercelの `g-s-n` チーム）です。
プロジェクトは受信機のGitリポジトリには接続せず、上記の専用パッケージをデプロイします。

## 投稿の動き

画像を有効にすると、台風は同じ投稿アカウント・台風番号・発表日時（年を含む）・
情報種別・通報区分ごとに集約します。最初の新しい通報から180秒待って、受信できた
地点だけを1投稿・1画像にします。6通揃うことは前提にせず、重複では待ち時間を延ばしません。

集約途中の原文・元の受信日時・締切を既存SQLiteに保存します。再起動で失われず、
通常のバックアップにも含まれます。締切後の新しい地点は「追加更新」として、既知の
全地点を含む新しい画像URLで投稿します。訂正は未送信の古い集約を置き換え、取消は
該当する未送信の集約を抑制して即時に配送キューへ入ります。すでに署名して配送キューへ
入れた投稿や公開済み投稿は変更しません。

台風以外は集約待ちをせず従来の配送キューへ入ります。本文とPNGのURLを併記し、
対応するNostrクライアント用に `imeta` も付けます。画像表示に未対応のクライアントでも
本文を読めます。南海トラフの複数ページ通報は、現段階では画像側は単一受信ページの
状態表示になります。本文側の既存の組立動作は維持します。

画像URLは署名済みイベントとともに保存し、再送で作り直しません。画像URL生成に
失敗したときも本文を配送し、`--status` の「画像URL生成エラー」に記録します。
Vercelへの問い合わせは受信・配送経路では行いません。描画サービスの一時停止中も
本文の配送は継続し、画像はサービス復旧後に同じURLから取得できます。

## 公開するソースの準備

リポジトリ全体ではなく、次のコマンドで作る新しいディレクトリをデプロイします。
既存のディレクトリには出力しません。必要なファイルを列挙してコピーするため、
`config.py`、Nostr鍵、SQLite、バックアップ、`.env` が混入しません。

```sh
python3 scripts/prepare_image_service.py dist/image-service
# Vercel CLIを使う場合: 対象プロジェクトとチームを確認してから実行
cd dist/image-service
vercel --prod
```

VercelはPython 3.12、Framework PresetはOther、Root Directoryはこの出力先です。
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

受信機のリポジトリ直下の `.env` に次を設定し、ファイルの権限を600にします。
このファイルと `state/` はGitの対象外です。鍵を含むCompose設定全体を画面へ出さず、
設定確認には `docker compose config --quiet` を使います。

```text
SHIRASU_IMAGE_BASE_URL=https://<本番ドメイン>
SHIRASU_IMAGE_KEY_ID=k1
SHIRASU_IMAGE_SIGNING_KEY=<同じ64桁のhex鍵>
SHIRASU_IMAGE_VERSION=v4
SHIRASU_TYPHOON_WAIT_SECONDS=180
```

先頭の3項目が全て空なら画像は無効です。一部だけの設定は起動時にエラーになります。
BASE_URLにはHTTPSのoriginだけを指定します。待ち時間は1〜300秒かつ配送の再送期限より
短くします。集約待ちの生通報と配送待ちは `MAX_PENDING` の上限を共有します。

`SHIRASU_IMAGE_VERSION` は `v1`（既定）・`v2`・`v3`・`v4` です。v2では海上警報・震度・津波、
v3では気象・緊急地震速報・火山の地図、v4では気象の情報ごとの配色を追加します。
変更するのは新しい投稿のURLだけで、
保存済みの署名イベントや投稿済みURLを作り直しません。受信入力形式は各版で共通ですが、
版もHMACに含めるため、パスの版だけを書き換えたURLは拒否します。

公開後はまず `/healthz`、正規署名URLのPNG、改変URLの拒否を確認します。
その後 `docker compose up -d --build shirasu` で受信側の設定を反映し、
`docker compose exec -T shirasu python main.py --status --format text` を確認します。
画像を停止するときは先頭3項目を全て空にして再作成します。既存の集約は締切後に
テキストで配送し、新規の通報は従来の即時キュー投入に戻ります。

## キャッシュと世代管理

URLは `/v1/<key-id>/<payload>.<signature>.png`、`/v2/...`、`/v3/...`、`/v4/...` です。
payloadはキー順を固定したJSON、zlib、paddingなしbase64urlの順に変換します。
元のNMEAと元の受信Unixミリ秒を含め、復号の年月補完も再現します。HMACはバージョン・
key-id・payloadを対象にします。署名確認後にサイズ上限付きで展開・復号します。

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

## 検証

CIは受信機と画像サービスを別のイメージで検証します。描画だけを確認する例:

```sh
python3 scripts/prepare_image_service.py dist/image-test
docker build -f image_service/Dockerfile -t shirasu-images:test dist/image-test
docker run --rm --network none --volume "$PWD:/source:ro" --workdir /source \
  shirasu-images:test python -B -m unittest discover -s tests -p test_image_renderer.py -v
```

ローカルプレビュー・回帰テストはNostrへ投稿しません。
