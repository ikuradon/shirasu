# 配布・デプロイ

画像サービスを先に公開し、その版を確認してから受信機を切り替えます。
通常ソースの画像パッケージはv1〜v4、v5は正式候補パッケージで提供します。
環境変数だけを変更して通常ビルドにv5を指定することはできません。

## GitHub Actions

| 起動条件 | 結果 |
| --- | --- |
| PR・mainへのpush | 回帰テスト。mainは設定済みならVercel Previewも作成・検証 |
| `images-v*`タグ、または正式公開workflowの手動実行 | ARM64で検証したイメージをGHCRへ保存し、別runnerで復元後、Vercel本番候補を検証・昇格 |
| Vercel workflowの手動実行 | 既存の正式Releaseを固定digestから再デプロイ |

正式イメージは`linux/arm64`です。`latest`は復元確認を通った最新の正式版を指し、mainのHEADではありません。
本番の取得にはReleaseの`release.json`に記録されたdigestを使います。
GitHub Releaseにはmanifest・検証結果・復元結果を保存します。期限付きActions artifactは正式保管の代用にしません。

### 初回設定

1. Vercelに画像用プロジェクトを作成する（Python 3.12、Framework PresetはOther）。
2. VercelのPreview／Production環境へ`SHIRASU_IMAGE_KEYS`を登録する。下記の受信側と対応させる。
3. GitHub Actions Secretへ`VERCEL_TOKEN`を登録する。
4. forkや別プロジェクトでは、Repository Variablesの`VERCEL_ORG_ID`・`VERCEL_PROJECT_ID`・`VERCEL_PRODUCTION_DOMAIN`と、[Trusted Sources設定](../config/vercel-trusted-sources.json)のrepository／audienceを変更する。
5. GHCR packageを対象repositoryへリンクし、公開設定を確認する。workflowは既存packageの可視性を自動変更しない。

本番のNostr鍵・画像署名鍵をGitHub Actionsへ登録する必要はありません。
CIは別の検証用画像鍵とGitHub OIDCを使います。本番の画像URLはログインなしで取得できる必要があります。

### 公開

1. mainをpushし、`Tests`とPreviewのHTTPS検証成功を確認する。
2. `Publish verified image release`をmainで手動実行し、未使用の配布タグ（例:`images-v5-r3`）を指定する。
3. 受信検証・保管・復元・Vercelの各jobと、公開ドメインでのHTTPS検証成功を確認する。
4. Release assetsとGHCRの固定digestを確認してから、受信機を更新する。

検証済みcommitへ新しいタグを付けてpushしても同じworkflowが動きます。
既存タグ・Releaseは上書きしません。`-rN`は配布改訂であり、描画URLの版とは別です。
同じ描画版を使うには既存URLの出力と署名の互換性を維持する必要があります。

## 受信機へ取得

操作は対象Releaseの`source_commit`に対応するcheckoutで行います。
過去の配布物には、その当時のヘルパーとコマンドを使用してください。
復元先は掃除対象の`dist/`ではなく、保持する新規ディレクトリにします。

```sh
release_tag=images-v5-r3
release_dir="$PWD/state/releases/$release_tag"
mkdir -p "$release_dir/metadata"
gh release download "$release_tag" --repo OWNER/REPO \
  --pattern release.json --pattern candidate-manifest.json \
  --dir "$release_dir/metadata"
python3 -m tools.release.restore_release_candidate \
  --release "$release_dir/metadata/release.json" \
  --manifest "$release_dir/metadata/candidate-manifest.json" \
  --output "$release_dir/candidate"
```

復元はdigest・image ID・全ファイルを照合します。アプリの起動は行いません。
`release.json`の`images.receiver.image_id`を使ってCompose設定を生成します。

```sh
python3 -m tools.release.prepare_release_compose \
  --bundle "$release_dir/candidate" --image '<receiverの完全image ID>' \
  --output "$release_dir/compose.release.yaml"
```

`.env`の`COMPOSE_FILE`へ **base → 既存host override → 公開用設定** の順で登録します。
追加のComposeファイルがある場合も保持し、project名・DB volumeを変えないでください。

```dotenv
COMPOSE_FILE=compose.yaml:compose.override.yaml:/absolute/path/to/compose.release.yaml
```

固定設定は受信・監視・バックアップを同じimage IDへ揃え、通常ビルドやpullへの代替を禁止します。
`docker compose config --quiet`で合成を確認します。設定全体には鍵が入るため画面へ出さないでください。

## 画像の設定と切替

画像専用の32 bytes以上の秘密鍵を用意し、Nostr秘密鍵とは分けます。
Vercelの環境変数はkey-idとhex鍵のJSONです。

```dotenv
SHIRASU_IMAGE_KEYS={"k1":"<64桁以上のhex鍵>"}
```

受信側の`.env`へ同じ画像鍵を設定します。ファイルの権限は600にします。

```dotenv
SHIRASU_IMAGE_BASE_URL=https://<画像サービスのドメイン>
SHIRASU_IMAGE_KEY_ID=k1
SHIRASU_IMAGE_SIGNING_KEY=<同じhex鍵>
SHIRASU_IMAGE_VERSION=v5
SHIRASU_IMAGE_ENCODING=w1
```

先頭3項目がすべて空なら画像は無効、一部だけの設定はエラーです。
originはHTTPSでパス・queryを含めません。鍵を変更する場合、旧URLに必要な検証鍵もVercelへ残します。

1. 公開画像の`/healthz`と正規署名URL、旧版URLの取得を確認する。
2. [バックアップ](operations.md#バックアップ)を作成・検証する。
3. 固定Compose設定を適用し、再起動する。
4. 状態・受信・配送・新規画像URLを確認する。

```sh
docker compose config --quiet
docker compose up -d --no-build --pull never shirasu metrics backup
docker compose exec -T shirasu python -m shirasu --status --format text
```

## 切り戻し

固定イメージと最新DBは維持し、`.env`の版・形式を`v4`／`png`に戻して受信サービスを再作成します。
画像を止める場合はorigin・key-id・署名鍵の3項目を空にします。

```sh
docker compose up -d --no-build --pull never shirasu
```

変更は新規投稿だけに適用されます。保存・投稿済みイベントのURLを生かすため、
画像サービスのv5/WebP経路と検証鍵は削除しません。
