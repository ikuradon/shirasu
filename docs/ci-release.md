# GitHub Actionsでの候補検証・保管・復元

公開候補のビルド、通し検証、配布物の保管をGitHub Actionsで行います。
検証したDockerイメージをそのままGHCRへ保存し、受信機ではdigestを指定して取得します。
Vercelへのデプロイと受信側の本番切替は、引き続き別の作業です。

## 通常の検証と正式な保管

| Workflow | 起動条件 | 処理・保存先 |
| --- | --- | --- |
| `Tests` | mainへのpush、PR、手動 | 受信・画像・HTTP・Composeの検証。公開可能な結果とプレビューだけをActions artifactへ14日間保存 |
| `Publish verified image release` | mainの手動実行、または`images-v*`タグのpush | 同じcommitの受信テスト後、公開用runnerで候補をビルド・検証。そのイメージをprivate GHCRへ保存し、別runnerで復元確認 |

検証の本体は`.github/actions/check-receiver`と`check-images`で共有します。
通常CIの権限は`contents: read`です。正式保管と復元確認のjobにだけ`contents: write`と
`packages: write`を与え、Actionsの`GITHUB_TOKEN`を使用します。
復元確認側の書き込みは、成功後の`latest`更新と結果JSONの保存に使用します。
本番のNostr鍵・画像HMAC鍵・`.env`・配送DBをActionsへ登録する必要はありません。

初回は通常の`Tests`が成功したcommitのartifactをダウンロードし、
`png-gallery/index.html`の投稿・画像を承認済みのローカルプレビューと比較します。
CI内の比較対象は、そのCIで作ったPNGギャラリーです。
**過去に承認したローカル画像との一致を、このworkflowが自動で保証するわけではありません。**
確認後、同じcommitがmainの先頭であることを確かめて正式保管を手動実行するか、
そのcommitへ候補版に対応するGitタグを付けてpushします。
正式保管でも再検証し、そこで検証したイメージを再ビルドせず保存します。

```sh
git tag images-v5 <確認したcommit>
git push origin refs/tags/images-v5
```

タグからの保管では、タグの指すcommitと候補の元commitが同じで、mainの履歴に含まれることを確認します。
通常のmainへのpushでは、正式保管は起動しません。

## イメージタグの意味

| 指定 | 意味 |
| --- | --- |
| `:images-v5` | 同名Gitタグに対応する固定版。既存のイメージタグは上書きしない |
| `:latest` | 別runnerでの復元確認まで成功した最新の正式版 |
| `:run-<run-id>-<attempt>` | そのActions実行で検証・保存したイメージ |
| `@sha256:...` | `release.json`に記録した内容の識別子。復元と本番運用ではこれを使う |

HEADを追いかける開発用イメージを追加するなら`:main`と分けます。今回の変更ではmainの自動配布は追加しません。
`latest`には自動的にHEADを意味する性質はなく、このworkflowが復元成功後に付け替える名前です。
タグごとに用途を分ける方式は[Docker Metadata actionのタグ設定](https://github.com/docker/metadata-action#tags-input)でも使えます。

`images-v5`は描画URLの世代に対応した今回の固定候補です。アプリ全体のSemVerではありません。
後日の受信アプリ更新やデザイン変更で同じタグを付け替えず、別の配布版として扱います。

## 非公開packageの初回作成

公開repositoryの`GITHUB_TOKEN`で新しいpackageを作った場合、非公開になるとは限りません。
[Actionsから作成するpackageの既定設定](https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions#default-permissions-and-access-settings-for-packages-modified-through-workflows)
には、実行元repositoryのvisibilityと権限モデルを継承する旨が記載されています。
2026-11-12の初回実行でも、空のreceiver bootstrapがpublicになり、実データを送る前に停止しました。

初回だけ所有者のPAT（classic、`write:packages`と付随する`read:packages`）で空のpackageを用意し、
privateかつ同じrepositoryへリンクされていることを確認します。`repo`権限は不要です。
作成後は別のDocker認証設定に切り替え、通常の`GITHUB_TOKEN`でもdigest指定のpullが成功することを確認します。
以後の保管・復元には`GITHUB_TOKEN`を使い、初回用PATは失効させ、登録したActions secretも削除します。

一度publicになったpackageはprivateへ戻せません。今回の空bootstrapについては、既知のタグ・digestだけで
ファイル層がないことを照合した場合に限り、packageを削除して作り直します。
実データや未知のversionを含む既存packageの削除・公開設定変更は行いません。

## 保存するもの

| 保存先 | 内容 |
| --- | --- |
| `ghcr.io/<owner>/<repo>-receiver`（private） | 固定依存を含む受信イメージ |
| `ghcr.io/<owner>/<repo>-images`（private） | レンダラー、固定依存、フォント、GISを含む画像サービスのイメージ |
| GitHub Release `images-v5` | `release.json`、元の`candidate-manifest.json`、`public-verification.tar.gz`、成功後の`restore-result.json` |
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
ただしタグpushで起動した実行では、そのタグが今回のcommitを指すことを確認して使用します。
保管途中で失敗した場合はdraftを残し、保存済みの情報を調べてから対応します。
成功したasset uploadを確認して最後にReleaseを公開します。再実行による自動置換は行いません。

## 別runnerでの復元確認

保管jobが成功すると、Dockerイメージも作業ディレクトリも共有しない新しいrunnerで復元します。
実際のGitHub Releaseからmetadataをダウンロードし、元commit・run ID・manifestを照合してから
GHCRの両イメージをdigestで取得します。全配布ファイルと受信イメージの起動設定を確認し、
Compose overrideを生成できることも確かめます。

すべて成功した場合だけ、同じdigestを指す`latest`タグを付けて`restore-result.json`をReleaseへ保存します。
過去のReleaseを再実行して最新Releaseから`latest`を巻き戻す操作は拒否します。
復元した候補本体とGISはrunner内に置き、公開artifactへuploadしません。

保管に成功して復元jobだけが失敗した場合は、原因を確認して失敗jobのみ再実行します。
保管job全体をやり直して同じ版を上書きしないでください。

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
後続の[通常CI](https://github.com/ikuradon/shirasu/actions/runs/43122044922)は`b9b5f23`で成功し、
実artifactの200ファイルを検証しました。40ケース・46投稿の本文・ALT・画像も承認済みプレビューと一致しています。
続いて`8b156d5`の[通常CI](https://github.com/ikuradon/shirasu/actions/runs/43129666692)も成功しました。
[正式保管の初回実行](https://github.com/ikuradon/shirasu/actions/runs/43129666472)では、受信・画像・HTTP・Composeの検証が成功し、
取得した公開artifactの200ファイルと、40ケース・46投稿の本文・ALT・画像が承認済みプレビューと一致しました。
候補manifestは`5c6bb92f451606499a1a5931bdab6e8863031a41dedb58c3d0e78be8caac1d38`です。
ただし空bootstrapのprivate確認で停止したため、実イメージの正式保管・別runnerでの復元はまだ完了していません。
実装後のCI実行結果は[GitHub ActionsのTests](https://github.com/ikuradon/shirasu/actions/workflows/test.yml)で確認します。
正式保管と実registryからの復元結果は、[公開workflow](https://github.com/ikuradon/shirasu/actions/workflows/release.yml)と
GitHub Releaseの`restore-result.json`で確認します。

現在のローカル候補、承認済みプレビュー、Docker export、成功した検証記録は残します。
最初のActions候補との比較、正式保管と復元の成功を確認してから、重複するローカル保管物を整理します。
ローカルの過去候補をCI由来と書き換えたり、同じ候補として扱ったりはしません。
