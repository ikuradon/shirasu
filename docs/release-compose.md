# 固定した公開候補を使うCompose設定

2026-11-12 19:38（JST）に、稼働中の受信機を`images-v5-r2`の固定イメージと`v5/w1`へ切り替えました。
通常の運用コマンドはリポジトリルートで実行します。`.env`の`COMPOSE_FILE`に公開用設定を保存済みです。

```sh
docker compose ps
docker compose exec -T shirasu python main.py --status --format text
docker compose logs --tail 50 shirasu
```

`state/releases/images-v5-r2/`は運用中の保持先です。`dist`とは異なり、掃除で削除しないでください。
以降は別リリースの準備・切替にも使う手順です。準備・隔離検証だけでは本番を変更しません。

## 本番切替の記録（2026-11-12）

| 項目 | 結果 |
| --- | --- |
| 配布元 | [images-v5-r2](https://github.com/ikuradon/shirasu/releases/tag/images-v5-r2)、ソース`c15fe24` |
| GHCR受信イメージ | `ghcr.io/ikuradon/shirasu-receiver@sha256:a57fa2a1a04642ed052b7f72326aba4b412f4ccb896f976733c3d27c730206bc` |
| 3サービスの固定image ID | `sha256:2334bddf4b3bea7c2ff728dcde55f9f2fcfa246322eb2f2c58ff867ef6bc2b42` |
| 候補manifest SHA-256 | `97e15966568f7729c38107ab5e0675c441032726d6774f531658c0f902c2311e` |
| Compose設定 | `/root/shirasu/state/releases/images-v5-r2/compose.release.yaml` |
| 直前バックアップ | `backups/before-images-v5-r2-20261112T103819Z/state.sqlite3`、送信済み15件の署名・ID・DB整合性を検証 |
| DBバックアップ SHA-256 | `0e486a7f40d1cb39c4c56acea9be03d133f6de5dd5bad7e1d620b8d9fd05bc1d` |
| 維持した設定 | project=`shirasu`、volume=`shirasu_shirasu-state`、configのread-only bind、画像鍵、GPSD・Prometheusネットワーク |
| 起動後 | 3サービスhealthy、再起動0、実受信・Nostr接続・監視HTTP・自動バックアップhealth正常 |
| 保存済み投稿 | 15件すべてのJSON・ID・署名が不変、送信待ち0、画像生成・受信・配送エラーなし |

GHCRから両イメージを復元し、候補121ファイルと受信イメージを照合しました。Composeの変更は固定イメージと版・形式のみです。
停止後の最新DBをバックアップし、同じvolumeで再起動しました。DBの復元・上書きは行っていません。
`.env`・`config.py`・host overrideも上記バックアップディレクトリに保全し、ディレクトリは700、設定ファイルは600です。

既存投稿の受信データと実運用の画像鍵で、本番の旧v4 PNG・v5 PNG・v5 WebPがHTTP 200になることを確認しました。
v5のPNG／WebPは1920×1080で全画素一致し、この入力では327,035 bytesから116,050 bytesになりました。
検証用のNostr投稿は行っていません。送信待ちは0件だったため、実機の再送対象はなく、再送・集約の動作検証は正式リリースのCIによります。
切替後の新規投稿と実nostter上の表示確認は[TODOの⑤](../TODO.md)に残しています。

ホストの復元・画像HTTP・切替結果は、保持先の`host-restore-result.json`、`primary-key-http-check.json`、`switch-result.json`に保存しています。

## 生成する設定

`scripts/prepare_release_compose.py`は候補manifestを検証し、ローカルのDockerイメージの
`/app`内の全配布ファイルと起動設定を照合します。確認用コンテナはネットワークなし・
read-only・ホストのマウントなしで動き、アプリの設定を読み込みません。
可変タグを入力しても、出力には確認した完全な`sha256:`イメージIDを記録します。

生成物は次の設定だけを`shirasu`・`metrics`・`backup`に重ねます。

```yaml
services:
  shirasu:
    build: !reset null
    image: "sha256:<確認したイメージID>"
    pull_policy: never
```

同じDBを扱う3サービスを同じ候補のコードに揃えます。`command`、環境変数、DB volume、
configのread-only bind、ネットワーク、healthcheck、停止猶予は既存設定から継承します。
画像の版・形式も上書きしません。`v5/w1`への切替は別途行います。
ローカルに固定IDのイメージがなければ起動に失敗し、通常ソースのビルドやpullへ退避しません。

`!reset`によるbuild削除は[DockerのComposeマージ仕様](https://docs.docker.com/reference/compose-file/merge/)に従います。
複数のComposeファイルは指定順に合成され、相対パスは最初のファイルを基準に解決されます。
明示的な`-f`指定時は既存のhost overrideも含めます。
([Dockerの複数ファイルの説明](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/))

## 準備

正式公開では[GitHub Actionsの保管・復元手順](ci-release.md)を先に行います。
記録されたdigestから取得した受信イメージと、復元した候補をそのまま指定します。
受信機での再ビルドは不要です。下記はWebP統合検証で使ったローカル候補の確認例です。

```sh
python3 -B scripts/prepare_release_compose.py \
  --bundle dist/webp-integration/candidate \
  --image shirasu:webp-rehearsal \
  --output dist/release-compose/compose.release.yaml
```

候補と不一致のイメージ、異なる起動コマンド、既存の出力先、候補内への出力は拒否します。
成功時はmanifestのSHA-256・イメージID・生成先だけを表示します。秘密鍵は出力に含まれません。
準備処理は本番への適用もComposeサービスの起動も行いません。
依存ライブラリの検証は前段の候補ビルド・通し検証で行い、この処理では配布ファイルと起動設定を照合します。

`dist`は作業領域です。本番ではCIの正式保管から運用用の保持ディレクトリへ復元し、
この設定もそこへ生成して絶対パスを確定してください。
イメージIDはDockerホスト内の識別子なので、別ホストへの移行時も記録されたGHCR digestから取得して再照合します。

## 既存設定との合成と、公開後の通常コマンド

合成順は **既存のbase → 既存のhost override等 → 生成した公開用設定** です。
既存のComposeプロジェクト名・DB volumeの割当は変更しません。
標準の構成では、リポジトリルートから次のように確認できます。
独自の`-p`や追加のComposeファイルを使っている場合は、同じ指定を引き継ぎます。

```sh
release_compose_file=/absolute/retained/path/compose.release.yaml
release_compose=(docker compose -f compose.yaml)
if [ -f compose.override.yaml ]; then
  release_compose+=(-f compose.override.yaml)
fi
release_compose+=(-f "$release_compose_file")
"${release_compose[@]}" config --quiet
```

本番の合成結果には鍵が入るため、設定全体を表示せず`config --quiet`を使います。
独自のネットワークやvolumeを持つ`compose.override.yaml`を省かないでください。

公開時には既存`.env`の`COMPOSE_FILE`へ同じファイル列を設定し、公開用設定を継続適用します。
Linuxの既定区切り文字では次の形です。host overrideがなければその要素を省き、
既存の追加Composeファイルがあれば公開用設定の前に残します。
すでに別の区切り文字を設定している場合はその指定に合わせます。

```text
COMPOSE_FILE=compose.yaml:compose.override.yaml:/absolute/retained/path/compose.release.yaml
```

これにより、公開後の`docker compose up`・`exec`・`logs`・`stop`にも同じ公開設定が適用されます。
`up --build`を使っても3サービスにはbuild定義がありません。
既存のプロジェクト名を維持し、環境変数や明示的な`-f`でこのファイル列を省略しないようにします。
公開用ファイルを外すと通常ビルドへ戻るため、切り戻しにも同じファイル列を使います。

## 切替とPNGへの切り戻し

以下は画像サービスの先行公開・本番確認・バックアップ検証後に行う作業です。
①の準備だけでは実行しません。

```text
SHIRASU_IMAGE_VERSION=v5
SHIRASU_IMAGE_ENCODING=w1
```

画像専用の既存origin・key-id・署名鍵も維持し、公開用設定を継続適用した状態で実行します。

```sh
docker compose config --quiet
docker compose up -d --no-build --pull never shirasu metrics backup
docker compose ps
docker compose exec -T shirasu python main.py --status --format text
```

PNGへ戻す場合は固定イメージとDBをそのまま使い、版・形式を`v4/png`へ変更して
`docker compose up -d --no-build --pull never shirasu`で再作成します。
画像を無効にする場合は既存手順どおり画像origin・key-id・署名鍵の3項目を空にします。
保存済み投稿はJSON・ID・署名ごと維持します。画像サービスのv5/w1経路と検証鍵は残します。

## 隔離した起動検証

```sh
python3 -B -m unittest discover -s tests -p 'test_release_compose*.py' -v
python3 -B scripts/check_release_compose.py \
  --bundle dist/webp-integration/candidate \
  --image shirasu:webp-rehearsal \
  --output dist/release-compose/results.json
```

合成したhost override、テスト専用の鍵とconfig、一意のComposeプロジェクト・新規volumeを使います。
本物の`main.py`を`network_mode: none`で起動し、WebPの署名済み投稿を保存したまま
v4/pngへ再作成・再起動して、新規投稿だけがPNGになることを確認します。
GPSDとリレーへ接続できないためhealth失敗は期待値です。受信・配送の接続成功を確認する試験ではありません。
最後に検証用のコンテナとvolumeだけを削除します。既存のComposeプロジェクトには触れません。

2026-11-12（JST）にDocker Compose 5.7.0で確認し、生成処理8件・実Composeマージ5件が成功しました。
実コンテナのWebP起動・PNG切戻し・PNG再起動の3段階でも、同じ一時DBとconfigを維持し、
保存済みイベントのJSON・ID・署名が不変で、すべて正常終了（exit 0）しました。
3サービスの合成設定を確認し、実プロセスの起動対象は受信サービスです。

| 記録 | 値 |
| --- | --- |
| 候補manifest SHA-256 | `bbe56252eb30f1972abc8dffe7c77679f19bc5783d1146870ee8ed30aaf6ff1d` |
| 固定した受信イメージ | `sha256:be18867c298671b4e26efbe2b77c3c80926169d0b9bc76c16b12e1933d4e50ff` |
| 生成したCompose設定 | `dist/release-compose/compose.release.yaml` |
| 実Compose検証結果 | `dist/release-compose/results.json` |

生成処理・実Composeマージのテストと隔離起動検証をCIにも追加しています。
本番への切替状況は[TODO](../TODO.md)、候補の検証結果は[WebP統合](webp-integration.md)を参照してください。
