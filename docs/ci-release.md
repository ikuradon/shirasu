# GitHub Actionsでの候補検証・保管・復元

公開候補のビルド、通し検証、配布物の保管をGitHub Actionsで行います。
検証したDockerイメージをそのままGHCRへ保存し、受信機ではdigestを指定して取得します。
Vercelへのデプロイと受信側の本番切替は、引き続き別の作業です。

## 通常の検証と正式な保管

| Workflow | 起動条件 | 処理・保存先 |
| --- | --- | --- |
| `Tests` | mainへのpush、PR、手動 | 受信・画像・HTTP・Composeの検証。公開可能な結果とプレビューだけをActions artifactへ14日間保存 |
| `Publish verified image release` | mainを指定した手動実行のみ | 同じcommitの受信テスト後、公開用runnerで候補をビルド・検証。そのイメージをprivate GHCRへ、識別情報と検証結果をGitHub Releaseへ保存 |

検証の本体は`.github/actions/check-receiver`と`check-images`で共有します。
通常CIの権限は`contents: read`です。正式保管のjobにだけ`contents: write`と
`packages: write`を与え、Actionsの`GITHUB_TOKEN`を使用します。
本番のNostr鍵・画像HMAC鍵・`.env`・配送DBをActionsへ登録する必要はありません。

初回は通常の`Tests`が成功したcommitのartifactをダウンロードし、
`png-gallery/index.html`の投稿・画像を承認済みのローカルプレビューと比較します。
CI内の比較対象は、そのCIで作ったPNGギャラリーです。
**過去に承認したローカル画像との一致を、このworkflowが自動で保証するわけではありません。**
確認後、同じcommitがmainの先頭であることを確かめて正式保管を手動実行します。
正式保管でも再検証し、そこで検証したイメージを再ビルドせず保存します。

## 保存するもの

| 保存先 | 内容 |
| --- | --- |
| `ghcr.io/<owner>/<repo>-receiver`（private） | 固定依存を含む受信イメージ |
| `ghcr.io/<owner>/<repo>-images`（private） | レンダラー、固定依存、フォント、GISを含む画像サービスのイメージ |
| GitHub Release `images-v5` | `release.json`、元の`candidate-manifest.json`、`public-verification.tar.gz` |
| Actions artifact `image-checks-<run-id>-<attempt>` | 検証JSON、PNG/WebP、投稿ギャラリー、候補manifest、全公開ファイルのSHA-256とサイズ |

洪水GISの原データは、プロジェクトの配布方針に従い公開artifactへ含めません。
両GHCR packageはprivateかつ同じリポジトリへリンクされていることを確認します。
新規packageは空のscratchイメージだけで作り、可視性を確認してから実データを送ります。
既存packageがpublic、または別リポジトリへのリンクなら停止します。

公開artifactは候補・イベント・HTTP結果・画像のハッシュを照合して、許可したファイルだけを取り出します。
作業ディレクトリ全体やDocker tarをuploadする処理はありません。
HTMLは検証済みギャラリーの内容から生成します。公開結果の確認はローカルでも行えます。

```sh
python3 -B -c 'from scripts.prepare_ci_artifact import verify_public; from pathlib import Path; print(verify_public(Path("/absolute/path/to/downloaded-artifact"))["candidate_manifest_sha256"])'
```

正式な保管先はGHCRとGitHub Releaseです。Actions artifactには保存期限があり、正式保管の代わりにはしません。
公開済みのdigestとRelease assetsは削除しない運用にします。GHCRをpublicへ変更するとGISも公開されるため、privateを維持します。
各イメージにはrunごとのタグを付けますが、復元では可変タグを使いません。

`release.json`は次を結び付けます。

- source commit、候補manifestのSHA-256、画像版
- GitHub Actionsのrepository・run ID・attempt
- 受信／画像イメージそれぞれの**registry digest**と**Docker image ID**

registry digestとDocker image IDは別の値です。push後にdigestでpullし、
検証したイメージIDと一致することを確認してから記録します。

同じ版のtag、Release、draftがある場合は停止し、上書きしません。
保管途中で失敗した場合はdraftを残し、保存済みの情報を調べてから対応します。
成功したasset uploadを確認して最後にReleaseを公開します。再実行による自動置換は行いません。

## 受信機での取得・復元

GitHub Releaseとprivate GHCRを読める認証を用意します。Actions内の認証は`GITHUB_TOKEN`、
外部ホストからprivate packageを読む方法は[GitHubのGHCR認証手順](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-to-the-container-registry)に従います。
復元先は`dist`の一時領域ではなく、運用上保持する新規ディレクトリにします。

```sh
release_dir=/absolute/retained/path/images-v5
mkdir -p "$release_dir/metadata"
gh release download images-v5 --repo OWNER/REPO \
  --pattern release.json --pattern candidate-manifest.json \
  --pattern public-verification.tar.gz --dir "$release_dir/metadata"
```

`release.json`の`source_commit`に対応するcheckoutで以下を実行します。
将来のヘルパーは新しい候補版を扱うため、過去版の復元には記録されたcommitのヘルパーを使います。
稼働中のcheckoutを書き換えず、別の作業ディレクトリを使えます。
DockerのGHCRログインを済ませてから実行してください。

```sh
python3 -B scripts/restore_release_candidate.py \
  --release "$release_dir/metadata/release.json" \
  --manifest "$release_dir/metadata/candidate-manifest.json" \
  --output "$release_dir/candidate"
```

この処理は両イメージをdigestでpullしてIDを照合し、**起動しないコンテナ**から
`/app`を取り出します。全配布ファイルを元のmanifestと照合し、成功した場合だけ出力先を確定します。
候補の作り直し、アプリの起動、本番設定の読み込みは行いません。既存の出力先は上書きしません。

Vercelへ渡すのは復元した`candidate/images/`です。通常checkoutからの再パッケージは行いません。
画像サービスの先行公開・実環境確認が済んだら、取得済みの受信イメージを
[公開用Compose設定](release-compose.md)へ接続します。

```sh
receiver_image_id=$(python3 -B -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["images"]["receiver"]["image_id"])' \
  "$release_dir/metadata/release.json")
python3 -B scripts/prepare_release_compose.py \
  --bundle "$release_dir/candidate" --image "$receiver_image_id" \
  --output "$release_dir/compose.release.yaml"
```

この段階では本番を起動しません。直前バックアップ、既存Compose設定との合成、
`COMPOSE_FILE`の継続適用、v5/w1への切替は[Composeの手順](release-compose.md)へ続きます。
公開済み画像の経路と検証鍵を残す切り戻し条件も同じです。

## ローカル保管処理の整理と確認範囲

開発途中だったローカルtar保管・復元・source照合の専用コード3本と、そのテスト3本を削除しました。
置き換えは`prepare_ci_artifact.py`、`publish_ci_release.py`、`restore_release_candidate.py`です。
既存の候補生成・通し検証・Compose接続はCIから再利用します。

2026-11-12のローカル確認では、追加27テストと実46投稿のPNG検証が成功しました。
既存の検証記録から公開対象200ファイルを抽出し、元の候補を保持したまま検証できています。
既存Dockerイメージからの実`create/cp/rm`で119ファイルの復元・manifest照合も成功しました。
この復元試験のregistry pull/inspectは代替し、cleanなCI記録は合成しています。
**GitHub API・GHCRへの正式保管はまだ行っていません。**
実装後のCI実行結果は[GitHub ActionsのTests](https://github.com/ikuradon/shirasu/actions/workflows/test.yml)で確認します。

現在のローカル候補、承認済みプレビュー、Docker export、成功した検証記録は残します。
最初のActions候補との比較、正式保管と復元の成功を確認してから、重複するローカル保管物を整理します。
ローカルの過去候補をCI由来と書き換えたり、同じ候補として扱ったりはしません。
