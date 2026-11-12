# Vercel公開手順

**2026-11-12: 初期設定と`images-v5-r2`の本番公開を完了しました。**
次回の更新は手順2から進めます。手順1はtokenを作り直す場合だけ必要です。

## 1. tokenをGitHubへ登録する（ユーザー・初回のみ）

1. [VercelのTokens](https://vercel.com/account/tokens)を開き、`g-s-n`チームへアクセスできるtokenを作成する。名前は例として`shirasu-github-actions`。
2. [リポジトリのActions Secrets](https://github.com/ikuradon/shirasu/settings/secrets/actions)を開き、**New repository secret**を押す。
3. **Name**に`VERCEL_TOKEN`、**Secret**に作成したtokenを貼り、**Add secret**を押す。
4. チャットで「登録した」と伝える。tokenの値はチャットへ貼らない。

Repository variablesや画像署名鍵の登録は不要です。

## 2. mainをpushしてPreviewを確認する（Codex）

1. 今回の実装をcommitし、`main`へpushする。
2. [Tests](https://github.com/ikuradon/shirasu/actions/workflows/test.yml)で、そのcommitの実行を開く。
3. `test`と`image-service`の成功を確認する。`Configure GitHub Actions access to protected deployments`と`Publish the verified HEAD as a Vercel Preview`がskipされていないことも確認する。
4. 実行のSummaryにある**Vercel Preview**のURLと、**95 image HTTPS checks passed**を確認する。

GHAがVercelの現在の設定を読み、[アクセスルール2件](vercel-trusted-sources.json)を追加します。既存設定を保持し、登録済みなら変更しません。ユーザーがVercelの設定画面を操作する必要はありません。

## 3. 正式版を公開する（Codex）

**この操作は検証成功後に本番ドメインを切り替えます。**

1. [Publish verified image release](https://github.com/ikuradon/shirasu/actions/workflows/release.yml)を開き、**Run workflow**を押す。
2. Branchを`main`、`release_tag`を未使用の配布タグにする。同じ描画版の次の改訂なら`images-v5-r3`（公開済みの`images-v5-r2`は再利用しない）。
3. **Run workflow**で開始し、`verify-receiver` → `release` → `restore` → `vercel`の成功を確認する。
4. Vercel公開jobのSummaryで、候補95画像・匿名の本番95画像の検証成功を確認する。
5. [GitHub Release](https://github.com/ikuradon/shirasu/releases)に配布タグと`vercel-<run>-<attempt>.json`が追加されたことを確認する。

Gitタグ作成・GHCR保管・復元・Vercel候補作成・本番切替はworkflowが行います。受信機の切替は別作業です。
手動実行の代わりに、検証済みcommitへ新しい配布タグを付けてpushしても同じworkflowが動きます。今回のr2はタグpushで公開しました。
`Deploy verified release to Vercel`の単独実行は、既に保管済みの対応Releaseを再デプロイするときだけ使います。

仕組み、検証範囲、過去の結果は[設計と検証記録](vercel-release-reference.md)を参照してください。
