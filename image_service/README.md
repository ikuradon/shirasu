# 署名付き QZQSM 画像サービス

受信機とは別に動かす、Python 3.12 / WSGI の画像レンダラーです。画像の入力は
`/v1/<key-id>/<JSON+zlib+base64url>.<HMAC-SHA256>.png` に含まれます。
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

`SHIRASU_IMAGE_KEYS` を設定して `python -m image_service --serve` を実行すると
`127.0.0.1:8080` で WSGI サーバーが起動します。このサーバーはローカル確認用です。
署名鍵は Nostr の鍵とは別の暗号学的乱数 32 バイトを使い、環境変数には次の形で設定します。
実際の秘密鍵をリポジトリ、URL、ログに記録しないでください。

```text
{"k1":"<32 bytes encoded as 64 hexadecimal characters>"}
```

## デプロイと HTTP 契約

デプロイパッケージには `image_service/` と共通の `image_protocol.py` が必要です。
Vercel の `api/image.py` から `from image_service.app import app` で WSGI アプリを公開し、
ルートのすべてのリクエストをこの関数へ rewrite します。受信機の `config.py`、SQLite、
バックアップ、秘密鍵をパッケージに含めないでください。

- `GET /healthz`: 鍵設定と固定アセットを検査します。成功は `200`、設定不備は `503`。
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
日本語の地名を欠落させないためフォントを部分化していません。アセットの合計は約 35 MB です。

公開後に v1 の見た目、デコード規則、依存、アセットを変更すると同じ URL の内容が変わります。
そのような変更は v2 の描画経路として追加し、古い経路と検証鍵を残してください。
Matplotlib の描画と azarashi の multipart 組立状態はリクエスト全体のロックで保護し、
各リクエストの前後で組立状態を破棄します。
