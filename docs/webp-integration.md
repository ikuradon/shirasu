# WebPの署名URL・HTTP・Nostr接続

可逆WebP method 4を、隔離した公開候補のv5へ接続しました。
前段の[採用検証](webp-adoption.md)で確認したRGBAからの直接出力を使います。
通常配布はv1〜v4のPNG、受信側の既定値もPNGです。本番への公開や設定変更は行っていません。

## URLと固定プロファイル

```text
PNG:  /v5/<key-id>/<payload>.<signature>.png
WebP: /v5/<key-id>/<payload>.<signature>.w1.webp
```

既存v1〜v4と候補v5のPNGは、従来の署名計算を維持します。
WebPは`shirasu-image-encoding\0`という別のHMACドメインで、
描画版・key-id・圧縮payload・`w1`・`webp`をNUL区切りで認証します。
署名を検証してから、従来と同じ上限付きの展開・JSON／原電文検証を行います。
拡張子だけ、プロファイルだけ、描画版だけを書き換えても同じ署名は使えません。

`w1`は`lossless=True, quality=100, method=4, exact=True`です。
`image_service/webp_w1.py`でPillow 13.0.0とlibwebp 1.6.0も確認し、異なる符号器なら
WebP生成を拒否します。PNGの描画はこの検査に依存しません。公開済みURLの結果を変えないため、
符号器を更新する場合は新しいエンコードプロファイルを追加します。

`render_preview.render()`は従来どおりPNG、`render_webp()`は同じ地図・文字効果・表示判定を
WebPへ直接保存します。PNG中間ファイル・PNG圧縮は挟みません。
生成成功・エンコード例外・要旨への切替・南海トラフのどの経路でも、Figureの後始末を共有します。

## HTTPと投稿

- GET／HEADは`image/webp`と実WebPのContent-Length、SHA-256由来のETagを返します。
- 条件付きGET／HEADの304にも同じMIME・ETag・キャッシュ設定を付け、本文は返しません。
- URLで形式が決まり、Acceptでは変更しません。queryによるqualityなどの指定も拒否します。
- WebP生成の失敗や、WebPの代わりにPNGが返る実装不備はエラーにし、同じURLでPNGへ退避しません。
- 成功応答のimmutableキャッシュ、失敗時の`no-store`はPNGと共通です。
  候補のhealthではアセットとWebP符号器の両方を確認します。

候補の受信側では、既存の画像設定に次を追加すると、新規イベントにWebP URLを付けます。

```text
SHIRASU_IMAGE_VERSION=v5
SHIRASU_IMAGE_ENCODING=w1
```

`encoding`未指定は`png`です。形式設定だけでは画像機能は有効にならず、通常配布や
v1〜v4で`w1`を選ぶと設定エラーになります。Composeはこの変数を受信プロセスへ渡します。
`ImagePublisher`がURLとMIMEを決め、実Nostr SDKが本文中の同じURLと
`imeta`の`url`・`m image/webp`・`alt`をイベントの署名対象にします。
本文、元のNMEA、ALT生成は共通です。受信機は画像を取得・生成しません。

PNG↔WebPや画像無効へ切り替えても、保存済みイベントはJSON・ID・署名ごと再送します。
新規投稿をPNGへ戻す場合も、保存・公開済みWebP URLを処理できる画像サービスと検証鍵を残します。
切り戻しは`SHIRASU_IMAGE_ENCODING=png`を明示します。描画版もv4へ戻す場合は
`SHIRASU_IMAGE_VERSION=v4`と併せて設定し、v4に`w1`を残さないようにします。

## 検証する入力の区別

承認済みギャラリーは40ケース・46投稿・49種類の画像入力です。
実投稿46件はすべて署名URL→実HTTPの対象にします。
HTTPで承認済み画像と直接比較できるのは45入力・82使用箇所です。
残る画像専用4例は、49入力すべてを対象にした直接描画の比較で確認します。

| 画像専用例 | HTTPの比較から分ける理由 |
| --- | --- |
| 気象取消・元報参照、台風取消・元報参照 | 元報参照はオフライン用で、URLの入力には含めない |
| 降灰の全通報束、気象の複数通報束 | 同時刻の原電文順がURL正規化で変わり、元の順序の画像と比較条件が異なる |

通報束自体が署名不能なわけではありません。画像専用の気象束では警報名の最初の出現順を
表示に使うため、正規化後と表示順が変わることを今回検出しました。
実際の気象投稿は単電文へ分かれます。実投稿を含む入力は順序が変わっても除外せず、
台風の集約投稿もHTTP確認を必須にしています。既存の描画や正規化規則は変更していません。

## 検証結果

2026-11-11に、実Dockerfileから作ったPython 3.12.16環境で確認しました。
候補は119ファイル、manifest SHA-256は
`bbe56252eb30f1972abc8dffe7c77679f19bc5783d1146870ee8ed30aaf6ff1d`です。
検証の前後で候補が不変であること、配布対象のソースが現在の作業版と一致することも確認しています。

| 確認対象 | 結果 | 記録（`dist/webp-integration/`内） |
| --- | --- | --- |
| 直接描画49入力 | PNGは承認済み画像とbyte一致、WebPは採用検証時とbyte一致。表示判定も一致し、毎回の描画後に残存Aggバッファ0 | `results/direct-corpus.json` |
| 実Service・Nostr署名 | WebP／既定PNGの各40ケース46投稿・切替等20イベントが成功。本文・原電文・ALTと、保存済みイベントの再送を確認 | `results/webp-posts.json`、`results/png-posts.json` |
| 実HTTP | 45入力・82使用箇所のWebPが承認済みPNGと全画素一致。全46投稿を網羅。HEAD／304、Accept非依存、形式・署名改ざん拒否なども成功 | `results/webp-http-final.json` |
| 旧版互換 | `0913f70`から固定したv1〜v4の5代表で、署名URL・PNGバイト列・ETagが同一 | `results/legacy.json`、`results/legacy-preserved.json` |
| 公開版ソース | レンダラー・アセット・依存ロックの42ファイルはbyte一致。HTTP／プロトコルは実応答で別途比較 | `source-comparison.json` |
| 回帰テスト | 画像側95件、受信側69件、重複を除いて計164件成功。新規30件を含む | `results/tests-summary.json` |

承認済みギャラリーは再生成せず、manifest SHA-256
`fcecddef0cc0fd5f4e7a6e780ded49f5d1b8f9f6f231710f6487da6081d621d2`に照合しました。
直接描画・旧版比較・候補ソース照合に使った補助スクリプトは`results/diagnostics/`に保存しています。
画質・容量・生成時間の採用判断と、実nostterの表示確認は前段の[採用検証](webp-adoption.md)を参照してください。

## 再現手順

候補・旧版の準備とDockerビルドは[公開前の通し検証](image-release-rehearsal.md)と同じです。
受信側と画像側の同じcandidate-manifestハッシュを使います。出力先は候補の外に新規作成します。

```sh
docker run --rm --network none \
  --volume "$PWD/dist/webp-integration/candidate:/candidate:ro" \
  --volume "$PWD:/checks:ro" --volume "$PWD/dist/webp-integration/results:/output" \
  --workdir /candidate/receiver --env PYTHONPATH=/candidate/receiver:/checks:/checks/tests \
  shirasu:webp-rehearsal python -B /checks/scripts/check_webp_release.py receiver \
  --bundle /candidate --output /output/webp-posts.json

docker run --rm --network none \
  --volume "$PWD/dist/webp-integration/candidate:/candidate:ro" \
  --volume "$PWD:/checks:ro" --volume "$PWD/dist/webp-integration/results:/output" \
  --workdir /candidate/images --env PYTHONPATH=/candidate/images:/checks \
  shirasu-images:webp-rehearsal python -B /checks/scripts/check_webp_release.py images \
  --bundle /candidate --events /output/webp-posts.json --gallery /checks/dist/preview \
  --output /output/webp-http-final.json
```

受信側は実Service・SQLite・Nostr署名を使い、配送だけを外部へ送信しない実装へ差し替えます。
画像側は固定候補の実WSGIアプリをTCP loopbackで動かし、HTTPから得たWebPを復号します。
設定ファイル・本番鍵・既存DBは読まず、テスト鍵と新しいDBだけを使います。

CIにもWebP投稿→実HTTPの確認を追加しました。CIでは現在のPNGギャラリーを作って比較し、
ローカルでは保存済みの承認済みギャラリーへ照合します。既存PNGの署名URL・画像・ETagは
別途、固定コミットの旧版パッケージとの実応答比較を行います。
ソース比較の`--allow-routing-changes`はHTTP／プロトコルの変更を明示するための指定で、
レンダラー・アセット・依存ロックのbyte一致条件は緩めません。

Vercelへの公開、実TLS・CDN・本番鍵・実リレーの確認は一括公開時に行います。
