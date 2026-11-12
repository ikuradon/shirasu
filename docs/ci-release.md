# GitHub Actionsでの候補検証・保管・復元

公開候補のビルド、通し検証、配布物の保管をGitHub Actionsで行います。
検証したDockerイメージをそのままGHCRへ保存し、受信機ではdigestを指定して取得します。
Vercelへのデプロイと受信側の本番切替は、引き続き別の作業です。

今回の正式配布対象は、受信機に合わせた`linux/arm64`です。
正式workflowの受信検証・保管・復元jobはすべて`ubuntu-24.04-arm`で動かします。
保管前に両イメージのOS／Architectureを実際に照合し、schema 2の`release.json`へ
`platform: linux/arm64`を記録します。復元側もこの値を確認します。
通常の`Tests`はAMD64上の回帰検証を継続するため、正式保管のARM64検証とは別です。

## 通常の検証と正式な保管

| Workflow | 起動条件 | 処理・保存先 |
| --- | --- | --- |
| `Tests` | mainへのpush、PR、手動 | 受信・画像・HTTP・Composeの検証。公開可能な結果とプレビューだけをActions artifactへ14日間保存 |
| `Publish verified image release` | mainの手動実行、または`images-v*`タグのpush | 同じcommitの受信テスト後、公開用runnerで候補をビルド・検証。そのイメージをpublic GHCRへ保存し、別runnerで匿名取得・復元確認 |

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
git tag images-v5-r1 <確認したcommit>
git push origin refs/tags/images-v5-r1
```

タグからの保管では、タグの指すcommitと候補の元commitが同じで、mainの履歴に含まれることを確認します。
通常のmainへのpushでは、正式保管は起動しません。

## イメージタグの意味

| 指定 | 意味 |
| --- | --- |
| `:images-v5-r1` | 今回の配布改訂版。同名Gitタグに対応し、既存のイメージタグは上書きしない |
| `:latest` | 別runnerでの復元確認まで成功した最新の正式版 |
| `:run-<run-id>-<attempt>` | そのActions実行で検証・保存したイメージ |
| `@sha256:...` | `release.json`に記録した内容の識別子。復元と本番運用ではこれを使う |

HEADを追いかける開発用イメージを追加するなら`:main`と分けます。今回の変更ではmainの自動配布は追加しません。
`latest`には自動的にHEADを意味する性質はなく、このworkflowが復元成功後に付け替える名前です。
タグごとに用途を分ける方式は[Docker Metadata actionのタグ設定](https://github.com/docker/metadata-action#tags-input)でも使えます。

描画URLの世代と配布版を分けます。今回の描画版は`v5`のまま、配布タグは`images-v5-r1`です。
`-r1`は同梱文書や保管設定を改訂した配布版を表し、画像URLへは入りません。
既存Gitタグ`images-v5`は`8b156d5`を指したまま残します。
後日の配布内容変更でも同じタグを付け替えず、新しい配布タグを使います。
描画結果や署名URLの互換性に影響する変更では、別途描画版を更新します。

## 公開packageの作成と確認

両GHCR packageをpublicかつ同じrepositoryへリンクされた状態で使用します。
2026-11-12の初回実行で作成したpublicの空receiver bootstrapは削除せず再利用します。
新規のimages packageはファイル層のないscratchイメージで先に作り、
publicであることとrepositoryへのリンクを確認してから実データを送ります。
private／internal、または別repositoryへのリンクなら停止し、自動で可視性を変更しません。

保管・タグ更新にはworkflowの`GITHUB_TOKEN`を使います。
非公開packageの初回作成用PATや`GHCR_BOOTSTRAP_TOKEN`の登録は不要です。
実際の復元は、別runnerの空のDocker認証設定で匿名pullできることも確認します。

従来の非公開初期化用に用意した一時branch／worktreeは、正式保管と復元の成功後に削除します。
現時点ではその削除と、新方針での正式実行結果の確認は未完了です。

## 保存するもの

| 保存先 | 内容 |
| --- | --- |
| `ghcr.io/<owner>/<repo>-receiver`（public） | 固定依存とライセンス文書を含む受信イメージ |
| `ghcr.io/<owner>/<repo>-images`（public） | レンダラー、固定依存、フォント、GIS、出典・ライセンス文書を含む画像サービスのイメージ |
| GitHub Release `images-v5-r1` | `release.json`、元の`candidate-manifest.json`、`public-verification.tar.gz`、成功後の`restore-result.json` |
| Actions artifact `image-checks-<run-id>-<attempt>` | 検証JSON、PNG/WebP、投稿ギャラリー、候補manifest、全公開ファイルのSHA-256とサイズ |

洪水流路は、ユーザーが選択した出典付きの現行気象庁データを使用します。
画像の出典・加工者表示を維持し、画像イメージには加工済みの`flood.geojson.zlib`と
取得元・加工内容を記した`FLOOD_NOTICE.txt`を同梱します。
ZIP固有のGIS再配布条件が確認済みになったという意味ではありません。
両イメージには、pyserialのBSDライセンス本文も同梱します。
公開artifactは従来どおり検証資料と画像に限定し、GISや元ZIPは含めません。

公開artifactは候補・イベント・HTTP結果・画像のハッシュを照合して、許可したファイルだけを取り出します。
作業ディレクトリ全体やDocker tarをuploadする処理はありません。
HTMLは検証済みギャラリーの内容から生成します。公開結果の確認はローカルでも行えます。

```sh
python3 -B -c 'from scripts.prepare_ci_artifact import verify_public; from pathlib import Path; print(verify_public(Path("/absolute/path/to/downloaded-artifact"))["candidate_manifest_sha256"])'
```

正式な保管先はGHCRとGitHub Releaseです。Actions artifactには保存期限があり、正式保管の代わりにはしません。
公開済みのdigestとRelease assetsは削除しない運用にします。画像イメージに同梱したGISもpublic GHCRから取得可能になります。
各イメージにはrunごとのタグを付けますが、復元では可変タグを使いません。

新しい`release.json`はschema 2を使用し、次を結び付けます。

- source commit、候補manifestのSHA-256、画像版`version`、配布タグ`release_tag`、対象`platform`
- GitHub Actionsのrepository・run ID・attempt
- 受信／画像イメージそれぞれの**registry digest**と**Docker image ID**

registry digestとDocker image IDは別の値です。push後にdigestでpullし、
検証したイメージIDと一致することを確認してから記録します。

schema 1のメタデータは`images-<version>`という従来の暗黙タグとして読み取れます。
schema 1に`release_tag`を混ぜたメタデータは拒否します。
過去の候補本体の復元には、後述のとおりその`source_commit`のヘルパーを使用します。
現在のファイル一覧が過去の候補にも適用できるという意味ではありません。

同じ版のtag、Release、draftがある場合は停止し、上書きしません。
ただしタグpushで起動した実行では、そのタグが今回のcommitを指すことを確認して使用します。
保管途中で失敗した場合はdraftを残し、保存済みの情報を調べてから対応します。
成功したasset uploadを確認して最後にReleaseを公開します。再実行による自動置換は行いません。

## 別runnerでの復元確認

保管jobが成功すると、Dockerイメージも作業ディレクトリも共有しない新しいrunnerで復元します。
保管jobが出力した実際の配布タグをworkflow outputで復元jobへ渡し、
そのGitHub Releaseからmetadataをダウンロードします。
元commit・run ID・`release_tag`・manifestを照合してから、空のDocker認証設定で
GHCRの両イメージを匿名・digest指定で取得します。
今回の候補はライセンス文書を加えた全121ファイルを照合し、受信イメージの起動設定と
Compose overrideの生成も確認します。

すべて成功した場合だけ、`GITHUB_TOKEN`で認証して同じdigestを指す`latest`タグを付け、
workflow outputで受け取った配布タグのReleaseへ`restore-result.json`を保存します。
過去のReleaseを再実行して最新Releaseから`latest`を巻き戻す操作は拒否します。
復元した候補本体とGISはrunner内に置き、公開artifactへuploadしません。

保管に成功して復元jobだけが失敗した場合は、原因を確認して失敗jobのみ再実行します。
保管job全体をやり直して同じ版を上書きしないでください。

## 受信機での取得・復元

正式保管後の両GHCR packageは匿名取得する構成です。受信機にGHCRのPATを保存する必要はありません。
下のmetadata取得例では`gh` CLIを使います。公開Releaseのブラウザーから同じファイルを取得しても構いません。
復元先は`dist`の一時領域ではなく、運用上保持する新規ディレクトリにします。

```sh
release_dir=/absolute/retained/path/images-v5-r1
mkdir -p "$release_dir/metadata"
gh release download images-v5-r1 --repo OWNER/REPO \
  --pattern release.json --pattern candidate-manifest.json \
  --pattern public-verification.tar.gz --dir "$release_dir/metadata"
```

`release.json`の`source_commit`に対応するcheckoutで以下を実行します。
将来のヘルパーは新しい候補版を扱うため、過去版の復元には記録されたcommitのヘルパーを使います。
稼働中のcheckoutを書き換えず、別の作業ディレクトリを使えます。
DockerのGHCRログインは不要です。

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
その後、OSM代替は採用せず、現行流路を出典付きで同梱してpublic GHCRを使う方針に変更しました。
旧`images-v5`を動かさず、今回の正式保管は`images-v5-r1`として実施します。
新方針での正式保管・匿名pull・別runner復元・`latest`更新の成功記録は、実行後に追記します。
実装後のCI実行結果は[GitHub ActionsのTests](https://github.com/ikuradon/shirasu/actions/workflows/test.yml)で確認します。
正式保管と実registryからの復元結果は、[公開workflow](https://github.com/ikuradon/shirasu/actions/workflows/release.yml)と
GitHub Releaseの`restore-result.json`で確認します。

現在のローカル候補、承認済みプレビュー、Docker export、成功した検証記録は残します。
最初のActions候補との比較、正式保管と復元の成功を確認してから、重複するローカル保管物を整理します。
ローカルの過去候補をCI由来と書き換えたり、同じ候補として扱ったりはしません。
