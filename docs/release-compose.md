# 固定した公開候補を使うCompose設定

公開前の残作業①として、検証済みの受信イメージへComposeを接続する準備を行います。
この手順の準備・隔離検証では、本番の設定・DB・コンテナを変更しません。
画像サービスの先行公開と実環境確認、直前バックアップが済んでから本番を切り替えます。

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

検証済み候補をそのまま使います。イメージは同じ候補の`receiver/`からビルド済みであることが必要です。
この例ではWebP統合検証で使ったローカルイメージを指定します。

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

`dist`は作業領域です。公開前の残作業②で、候補・イメージ・検証結果とともにこの設定も
継続して保管する場所へ移し、本番で使う絶対パスを確定してください。
イメージIDはDockerホスト内の識別子なので、別ホストへの移行時には同じイメージを用意して再照合します。

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
