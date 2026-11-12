# Vercel自動公開の設計と検証記録

操作は[公開手順](vercel-release.md)を参照してください。この文書は設計の背景と検証記録です。

## HEADと配布タグ

| 対象 | 配布元 | 用途 |
| --- | --- | --- |
| mainのHEAD | 同じCIで生成・検証した`candidate/images/` | commitごとのPreview |
| `images-v5-r1`等の正式タグ | GitHub Releaseの固定digestから復元した`candidate/images/` | 本番候補の検証・公開 |
| GHCRの`latest` | 復元確認済みの最新配布版の別名 | Vercel公開の入力には使わない |

HEADをVercelのGit連携で直接ビルドしません。v5の登録やAPI入口を
候補生成時に用意し、その候補とCIの検証記録を対応させるためです。通常の
`prepare_image_service.py`はv1〜v4を配布するツールで、正式候補の代用にはなりません。

Previewでは、画像比較にdeployment固有URLを使います。画像の長期キャッシュにより、
可変のbranch aliasでは以前のHEADの画像が残る可能性があります。
正式公開ではタグ・source commit・候補manifest・deployment IDを記録します。
同じ描画URLの内容を維持する必要があるため、配布改訂タグ`-rN`を画像デザインの版の代用にはしません。

## HEADの自動Preview

`Tests` workflowに、受信検証→画像候補の検証→Preview作成を接続しました。
`publish_vercel_preview.py`が候補と公開検証資料、cleanなsource commitを照合し、
許可した画像サービスのファイルだけをVercelへ送ります。
再パッケージや環境変数の取得は行わず、Vercelに登録済みのPreview環境変数を使います。
返されたteam・project・targetを確認し、ビルドが`READY`になるまで待ちます。
続いてGitHub ActionsのOIDC tokenで保護を通過し、deployment固有URLで95画像とHTTP契約を検証します。
Preview URLは結果に残り、ブラウザーからは通常のVercel認証で閲覧できます。

mainのpush／mainでの手動実行だけを対象にし、PRには認証情報を渡しません。
`VERCEL_TOKEN`がなければデプロイだけをスキップし、通常の検証を実行します。
有効化には、GitHub Actionsのsecretへ`VERCEL_TOKEN`を一度登録します。
mainのGHAが後述のTrusted SourcesをVercelへ追加し、既存ルールを保持します。
`g-s-n`チームの対象projectをデプロイできるtokenが必要です。
次のrepository variablesは既定値を変更する場合だけ設定します。tokenはチャットやGitへ保存しません。
認証情報を使うmainのコードはレビューし、branch protectionで管理してください。

| variable | 値 |
| --- | --- |
| `VERCEL_ORG_ID` | `team_lpgtJBIpMvlETDi41eFcTZnH` |
| `VERCEL_PROJECT_ID` | `prj_dNNCX2HNDuIJ6Zn8l90BPLnqKCyH` |
| `VERCEL_PRODUCTION_DOMAIN` | `shirasu-images.vercel.app`（正式公開のみ） |

画像HMAC鍵のGitHub登録は不要です。後述の検証専用鍵をデプロイごとに生成します。
Preview URLと候補manifestはActions summaryと`vercel-preview-<run>-<attempt>` artifactへ、
秘密情報を除いたHTTP結果も同artifactへ保存します。現時点では実装済み・有効化前です。

外部データは`vendor/jma/`、`vendor/natural-earth/`、`vendor/noto-cjk/`へ取得元別に保存します。
GHAの最初に`prepare_vendor_assets.py`でSHA-256とサイズを照合し、従来のruntimeパスへ配置します。
上流サイトへの取得やGIS再変換は行いません。洪水流路も出典付きの固定データを使用します。
詳細は[vendorの管理方法](../vendor/README.md)を参照してください。

## 正式タグの公開

`images-v*`タグの正式workflowでは、GHCR保管と別runnerでの復元成功後に
`Deploy verified release to Vercel` workflowを呼び出します。
既存の正式Releaseを対象に、mainから`release_tag`を指定して手動実行することもできます。

1. GitHub Releaseの`release.json`、`candidate-manifest.json`、`restore-result.json`を照合する。
2. 固定digestから候補を匿名復元し、公開検証tarのハッシュと同じ候補であることを確認する。
3. その`images/`を本番環境・ドメイン未割当でビルドし、固有URLで旧版を含む95画像を検証する。
4. 最新Releaseと本番ドメインの割当先が検証中に変わっていないことを確認する。
5. 同じ本番deploymentを昇格し、本番ドメインで95画像の匿名HTTPS・キャッシュを再確認する。

本番公開は並列実行しません。候補検証失敗時は昇格せず、昇格後の検証失敗時は
前後のdeployment IDと失敗段階を残してworkflowを失敗させます。自動で別版へ戻しません。
成功時はdeployment ID・source・manifestを含む`vercel-<run>-<attempt>.json`を
同じGitHub Releaseに追記します。既存のRelease assetsは置換しません。

GHCRの正式イメージはARM64のままです。Vercel用jobはAMD64 runnerで動かし、
復元した同じ画像候補の固定依存からHTTP照合用コンテナを作ります。
Vercelへは検証済みのソースファイルを送信し、Vercel上でnative buildします。
production環境変数や本来の署名鍵をCIへ取得しません。

CLIでは`vercel deploy --prod --skip-domain`で3の候補作成を行えます。
Previewに対するCLIの`promote`は本番環境で再ビルドするため、
検証した本番ビルドを昇格する手順と区別します。
[公式CLIの昇格実装](https://github.com/vercel/vercel/blob/main/packages/cli/src/commands/promote/request-promote.ts)、
[skip-domain](https://vercel.com/docs/cli/deploy#skip-domain)

GHAのAPI処理はCLIと同じ`autoAssignCustomDomains: false`を**そのdeploymentだけ**に指定します。
プロジェクトの永続設定は変更しません。候補にaliasが割り当てられていれば公開を停止します。

## 検証専用鍵

画像サービスの既存`SHIRASU_IMAGE_KEYS`は引き続き必須です。
CIは暗号学的乱数で別の鍵を生成し、`check-`で始まるIDとともに
デプロイ固有の`SHIRASU_IMAGE_CHECK_KEYS`へ渡します。
CIの照合処理はこの鍵だけを使い、受信機の鍵・Nostr秘密鍵を読みません。
既知のfixture鍵は登録しません。不正な検証鍵や既存IDとの衝突はリクエスト時に503で拒否します。

鍵の値はログ・artifact・共有リンクへ保存しません。ただし検証鍵はdeploymentの環境変数なので、
job終了で失効するものではなく、そのdeploymentが保持される間は有効です。
保護アクセス用のOIDC tokenは短命ですが、画像署名鍵の寿命は別です。

この追加鍵への対応を含まない`images-v5-r1`は新しい自動デプロイ処理では拒否します。
今回の変更を含む新しい配布改訂タグ（例：`images-v5-r2`）が必要です。
旧タグは移動せず、画像のURL版は`v5`のまま維持できます。

## 保護された候補へのアクセス

自動処理は共有リンクを作成しません。Hobbyではアカウント全体で共有リンクが1本に制限され、
別の共有リンクを失効させる可能性があるためです。
[共有リンクの制限](https://vercel.com/docs/deployment-protection/methods-to-bypass-deployment-protection/sharable-links)

`configure_vercel_access.py`がVercelの対象projectの生のAPI応答を読み、
GitHub Actionsのルールを追加します。登録済みなら変更しません。
設定はSettings → Deployment Protection → Trusted Sources → External Servicesで確認できます。
issuerは`https://token.actions.githubusercontent.com`、audienceは`https://github.com/ikuradon`です。
repoを`ikuradon/shirasu`へ限定し、次の2つのルールを用意します。

| 用途 | 必須claims（同じルール内で全て一致） | Vercel側の対象環境 |
| --- | --- | --- |
| main Preview | `aud=https://github.com/ikuradon`、`repository=ikuradon/shirasu`、`ref=refs/heads/main`、`workflow=Tests`、`event_name=push`または`workflow_dispatch` | `preview` |
| 正式候補 | 同じ`aud`・`repository`、`environment=vercel-production`、`event_name=push`または`workflow_dispatch`、`workflow=Publish verified image release`または`Deploy verified release to Vercel` | `production` |

各claimの複数値はOR、異なるclaim同士はANDです。`environment`等の追加claimは
Update Project APIの`trustedSources.oidcProviders`で設定します。更新時は既存のTrusted Sourcesを保持してください。
追加する2ルールの設定値は[vercel-trusted-sources.json](vercel-trusted-sources.json)に固定しました。
これは`oidcProviders`へマージする断片です。現在のproject設定を取得し、既存の`projects`、
他issuer、同issuerの既存ルールを保持したうえで追加します。project全体をこのJSONで置換しません。
GitHubの`vercel-production` environmentの許可対象もmainと`images-v*`タグに限定できます。
GHAには対象jobの`id-token: write`を追加済みです。URLアクセス直前にtokenを取得し、
期限が近づいたら更新します。token・取得用credentialはログやartifactへ保存しません。
本番ドメインの最終検証ではOIDC headerを送らず、匿名の取得を確認します。
[Trusted Sourcesの設定・claim照合](https://vercel.com/docs/deployment-protection/methods-to-bypass-deployment-protection/trusted-sources)

## 実HTTPS検証

`scripts/check_vercel_release.py`は、固定候補と公開検証artifactを先に照合し、
渡された画像鍵で署名し直したURLをHTTPSで取得します。自動処理では上記の検証専用鍵です。

- `before`: 本番切替前の旧v1〜v4の代表5画像。
- `candidate`: 旧5画像＋v5 PNG45入力＋WebP45入力、計95画像。
- `production`: 同じ95画像を匿名取得し、PNG／WebPのCDN HITも必須にする。

画像のバイト列・MIME・ETag・寸法、WebPとPNGのRGBA一致、HEAD／304、
Acceptで形式が変わらないこと、署名・query・method・版・profile・拡張子の不正を確認します。
標準TLS検証を使用し、送信先は明示したHTTPSホストに限定します。
署名URLや鍵、認証Cookie、任意の応答本文を結果JSONへ保存しません。

鍵は環境変数`SHIRASU_IMAGE_KEYS`からメモリ内で読みます。
candidateだけはGitHub OIDCによる保護アクセスを使用します。
単体checkerには従来の手動検証用`VERCEL_SHARE_TOKEN`と
`VERCEL_AUTOMATION_BYPASS_SECRET`も残しています。共有tokenは同一ホストでCookieへ交換します。
before／productionは保護アクセス用の認証設定を無視し、匿名取得します。
公開fixtureの既知鍵をサービスに登録する処理はありません。

```sh
python3 -B scripts/check_vercel_release.py \
  --bundle /absolute/path/to/restored/candidate \
  --public-artifact /absolute/path/to/verified/public \
  --base-url https://shirasu-images.vercel.app \
  --allowed-host shirasu-images.vercel.app \
  --phase production --output /absolute/path/to/new-result.json
```

`Cache-Control: no-cache`を送ってもCDNがHITを返す場合があります。
その結果を関数の再描画時間とは記録せず、HTTP全体の時間とcache状態を区別します。

## 2026-11-12の検証記録

配布タグは`images-v5-r1`、source commitは`241ee4b604012ebcfbb0ae39b96670ba90bf2f69`、
候補manifestは`39df12e1f83b9fe3059016b2009c60d3d34286fbdcd655eb7cdb41f4fcd365a2`です。

| 項目 | 確認結果 |
| --- | --- |
| 切替前の本番 | `dpl_91LJo29zJXcmvX7DZQpbqgmee25V`、旧5画像が一致 |
| Preview | `dpl_WhjFya9JePHx1w18jbiCq9w2mJkH`、`READY`、ビルド約26.5秒 |
| 配布ファイル | Vercel上の99ファイルのSHA1が復元候補とすべて一致、余分なファイルなし |
| 実HTTPS | 95画像・115リクエスト成功、画像bytes／WebP画素一致 |
| HTTPとキャッシュ | HEAD／304・各種拒否・PNG／WebPのrepeat HITを確認 |
| 実行時エラー | 対象Previewの直近30分のerror／fatalログなし |
| 本番公開 | 未実施。新しいGHA自動化は実装・ローカル検証まで。token登録・Trusted Sources設定・新配布タグでの実行は後続 |

検証用Previewは[このdeployment](https://shirasu-images-pd4apnx3h-g-s-n.vercel.app)です。
保護認証が必要です。共有tokenを文書へ載せません。
ローカルの結果は`dist/vercel-check/before.json`、`preview-share.json`、`deployment-record.json`に保持しています。

Previewのv5 PNGは45入力のMISSと2回のHIT、WebPも同じ内訳でした。
HITを含む47回のHTTP所要時間は、PNGの中央値1.13秒・最大8.48秒、
WebPの中央値1.48秒・最大7.52秒です。通信時間を含む値で、純粋な描画時間ではありません。

プラットフォームのpeak memory／関数時間クエリは`402 payment_required`で拒否されました。
Observability Plusの契約は変更していません。したがって、Vercel上のRSS計測を完了したとは扱いません。
[Observability Plusの制限](https://vercel.com/docs/observability/observability-plus)

## 自動化実装のローカル検証

vendor配置・配布パッケージ・CI保管・Vercelの復元／公開／OIDC／HTTPをローカルで検証しました。
Vercel API・GitHub OIDCの新しい自動化部分は模擬応答によるテストであり、
上記r1の実HTTPS検証と区別します。新しいGHA workflowの実実行は初期設定後です。

固定依存のDockerイメージを使い、vendor移行後の40ケースを再描画しました。
描画失敗0件、前のプレビューとの画像比較・46投稿の比較は全て変更なしです。
旧版の42ファイルも固定baselineとバイト一致し、検証鍵の有無で既存画像応答が変わらないことを確認しています。
Vercel関連57件、CI保管69件、vendor11件のテストが成功し、受信機コンテナは439件中5件skipで成功しました。
workflowはactionlint 1.7.12でも検査済みです。一時候補・重複する描画出力・検証専用Dockerイメージは削除し、
ローカルの集計だけを`dist/autodeploy-check/verification.json`へ保存しています。
