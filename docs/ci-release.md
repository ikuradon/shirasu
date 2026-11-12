# GitHub Actionsでの候補検証・保管・復元

公開候補のビルド、通し検証、配布物の保管をGitHub Actionsで行います。
検証したDockerイメージをそのままGHCRへ保存し、受信機ではdigestを指定して取得します。
Vercelへのデプロイは[専用workflow](vercel-release.md)へ接続しました。受信側の本番切替は別の作業です。

今回の正式配布対象は、受信機に合わせた`linux/arm64`です。
正式workflowの受信検証・保管・復元jobはすべて`ubuntu-24.04-arm`で動かします。
保管前に両イメージのOS／Architectureを実際に照合し、schema 2の`release.json`へ
`platform: linux/arm64`を記録します。復元側もこの値を確認します。
通常の`Tests`はAMD64上の回帰検証を継続するため、正式保管のARM64検証とは別です。

## 通常の検証と正式な保管

| Workflow | 起動条件 | 処理・保存先 |
| --- | --- | --- |
| `Tests` | mainへのpush、PR、手動 | 受信→画像・HTTP・Composeの検証。公開可能な結果をartifactへ14日間保存。mainは認証設定済みならVercel Previewを作成し実HTTPS検証 |
| `Publish verified image release` | mainの手動実行、または`images-v*`タグのpush | 同じcommitの受信テスト後、公開用runnerで候補をビルド・検証。そのイメージをpublic GHCRへ保存し、別runnerで匿名取得・復元確認 |
| `Deploy verified release to Vercel` | 正式保管・復元の成功後、またはmainで既存Releaseのタグを指定して手動実行 | 固定候補を復元し、ドメイン未割当の本番候補をHTTPS検証。同じbuildを昇格し本番ドメインで再検証 |

検証の本体は`.github/actions/check-receiver`と`check-images`で共有します。
通常CIのリポジトリ権限は`contents: read`です。正式保管・復元jobは`contents: write`と
`packages: write`、Vercel公開jobは結果の追記用に`contents: write`を使います。
Vercel候補の保護されたURLへのアクセス用に、対象jobだけ`id-token: write`も付与します。
復元確認側の書き込みは、成功後の`latest`更新と結果JSONの保存に使用します。
本番のNostr鍵・画像HMAC鍵・`.env`・配送DBをActionsへ登録する必要はありません。

地図・フォントは取得元別の`vendor/`で固定し、最初にチェックサム付きでruntimeパスへ配置します。
通常CI・正式保管とも上流からのデータ取得やGIS変換は行いません。
Vercel自動化は初期設定後に有効になります。現時点で新workflowの実行と本番公開は未実施です。

通常の`Tests`（AMD64）と正式workflow（ARM64）は、同じcommitで並行実行できます。
正式workflowは自身の受信・画像・HTTP・Composeの検証を通してから保管し、
通常CIの成功を自動で待つ依存関係はありません。
配布対象のcommitをmainへpushし、そのcommitへ候補版に対応するGitタグを付けてpushするか、
mainで新しい`release_tag`を指定して正式保管を手動実行します。
既に存在するタグの再利用・移動は拒否します。正式workflowで検証したイメージを再ビルドせず保存します。

承認済みのローカルプレビューとの一致は、それぞれのartifactをダウンロードして、
`png-gallery/index.html`に対応する投稿・画像と別途比較し、結果を記録します。
CI内の比較対象は、そのCIで作ったPNGギャラリーです。
**過去に承認したローカル画像との一致を、このworkflowが自動で保証するわけではありません。**

```sh
git tag images-v5-r2 <確認したcommit>
git push origin refs/tags/images-v5-r2
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

正式保管と復元の成功後、従来の非公開初期化用branch`ci/ghcr-bootstrap-check`を
remote・localとも削除し、`/tmp/shirasu-ghcr-bootstrap-check`のworktreeも削除しました。
旧`images-v5`タグ、既存候補、切り戻し用Docker exportは維持しています。

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

`GITHUB_TOKEN`によるGHCR認証はworkflowで先に設定し、匿名取得の確認には別の空Docker認証設定を使います。
すべて成功した場合だけ、その認証を使って同じdigestを指す`latest`タグを付け、
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
この実行は空bootstrapのprivate確認で停止したため、当時は実イメージの正式保管・別runnerでの復元に進みませんでした。
その後、OSM代替は採用せず、現行流路を出典付きで同梱してpublic GHCRを使う方針に変更しました。
旧`images-v5`を動かさず、今回の正式保管は`images-v5-r1`として完了しました。

現在のローカル候補、承認済みプレビュー、Docker export、成功した検証記録は残します。
正式保管と復元の成功を確認できたため、重複するローカル保管物は用途を確認して整理できます。
ローカルの過去候補をCI由来と書き換えたり、同じ候補として扱ったりはしません。
今回作成した一時候補`dist/ghcr-public-check/candidate/`と`dist/ghcr-public-check/committed/`は、
GHCR復元物とのmanifest照合後に削除しました。
正式な復元物は`dist/ghcr-restored/images-v5-r1/`に保持しています。

## 正式保管・復元の実績（2026-11-12）

source commit `241ee4b604012ebcfbb0ae39b96670ba90bf2f69`で、
[通常AMD64 CI](https://github.com/ikuradon/shirasu/actions/runs/43174098113)と
[正式ARM64 workflow](https://github.com/ikuradon/shirasu/actions/runs/43174832148)が成功しました。
同じcommitの両workflowを並行して実行し、正式workflowでは`verify-receiver`、`release`、
別runnerの`restore`まで完了しています。

両runから公開artifactを取得して`verify_public`を実行し、それぞれ201ファイルを確認しました。
40ケース・46投稿・48種類のPNGについて、承認済みの`dist/preview/`と
投稿本文・ALT・原入力・PNGのバイト列が一致しています。
両artifactの候補manifestは同じSHA-256でした。

正式な[GitHub Release `images-v5-r1`](https://github.com/ikuradon/shirasu/releases/tag/images-v5-r1)には、
`release.json`、`candidate-manifest.json`、`public-verification.tar.gz`、`restore-result.json`の4ファイルを保存しました。

| 記録 | SHA-256 |
| --- | --- |
| `candidate-manifest.json` | `39df12e1f83b9fe3059016b2009c60d3d34286fbdcd655eb7cdb41f4fcd365a2` |
| `release.json` | `c712107207d6810ff15de8c08f84d1c291dbe0af806c01aee873bd5d23122a4c` |
| `restore-result.json` | `166babc6008bd4d58ab38d49de984697e236cdfccfaa854c785c1c58c2746e25` |

両GHCR packageはpublic、配布タグは`images-v5-r1`、描画版は`v5`、対象は`linux/arm64`です。

| package | 固定registry digest | Docker image ID |
| --- | --- | --- |
| `ghcr.io/ikuradon/shirasu-receiver` | `sha256:e9a6d9b9bc4623194d33f5d975bc087479bd11f99890b56fc8df45c31c30618b` | `sha256:2aac5d2a3820d8a3861cff99ac50e5d1f9ce81f6f2149bd78d207a24dd21acf3` |
| `ghcr.io/ikuradon/shirasu-images` | `sha256:141f2831e3d58b1a80b4cec018d02d766a126eee6d0e11fc4343cb8fb16feef5` | `sha256:32c808aa79e6d3688ddde0f63ab33204fe762d9fd1d8e5eb6bf007b33ba70711` |

別runnerでは空のDocker認証設定による固定digestの匿名pull、全121ファイル照合、
受信イメージの起動設定・Compose生成、publicとrepositoryリンク、Releaseの整合を確認しました。
成功後に両`latest`を更新し、`restore-result.json`の8項目はすべて`true`です。

さらに受信機ホストでも、空のDocker認証設定で固定digestから匿名復元しました。
121ファイルとARM64を確認し、両packageについて`images-v5-r1`と`latest`の
registry digest・image IDが上記と一致しています。隔離したCompose設定の生成も成功しました。
この結果は`dist/ghcr-restored/images-v5-r1/host-restore-result.json`に保存しています。
同じディレクトリの`metadata/`には公開Releaseのメタデータを保持しています。
公開Releaseの検証tar内の201ファイルも、正式workflowのartifactとすべてバイト一致しました。

ここで完了したのはGitHub／GHCRへの正式保管と復元確認です。
Vercelの本番公開と受信側の切替はまだ実施していません。
