# 画像一括公開前の通し検証

未公開画像を含む候補を独立した配布物に固定し、受信側の署名イベントから画像サービスのPNG応答までをローカルで確認する。
この手順は本番のVercel、Compose設定、配送DB、署名鍵を変更しない。
以下の結果は当時のPNG候補の記録です。後続の[WebP接続検証](webp-integration.md)では
PNGを維持して`w1`を追加し、HTTP・署名の変更を明示したうえで新しい候補を固定します。
今後の正式候補は[GitHub Actionsでビルド・検証・保管](ci-release.md)します。以下のローカル手順は、CIで共有する検証の説明と過去の確認記録です。

## 今回の確認結果（2026-11-11）

候補の識別子は`98b327c59bd69e76a3ad375e2dddb36d7ba1fb9601b8120113be7b8ed5977de3`。
元コミットは`43e0fa4`で、この通し検証を追加した作業ツリーから作成した。

| 確認 | 結果 |
| --- | --- |
| 画像96・受信21ファイル | 全117ファイルをmanifestに固定。正規Dockerfileで両イメージをビルドし、格納ファイルの一致・`pip check`も成功 |
| 実Serviceの投稿 | 40ケース46投稿。全文・原NMEA・署名・画像URL・ALTが一致 |
| 欠落・取消・切替・再送 | 20イベント。v4→v5→v4→画像無効の切替後も保存済みイベントが不変 |
| 旧版のソース | 42ファイルがbyte一致。プロトコルとHTTP入口も版の宣言・参照移動を除きAST一致 |
| 旧版の実HTTP | 代表5例のURL・PNG・ETagが一致 |
| 候補の実HTTP | 全46投稿と追加の切り戻し用2画像を描画。HEAD・304・不正URL8条件の拒否も成功 |
| 承認済みプレビューとの比較 | 全40ケース46投稿で原入力・本文・ALT・PNGのSHA-256が一致 |

生成物は今回の作業環境の`/tmp/shirasu-release-check-20261111/`に保存した。
`results/posts-final.json`、`results/images.json`、`results/preview-comparison.json`が今回の最終結果。
以下の再現手順では受信結果の名前を`posts.json`としている。実Vercel・CDN・TLS・本番鍵・実リレーは確認対象外。

## 通常配布と候補の区別

通常の`image_versions.py`と`prepare_image_service.py`の出力はv1〜v4だけを登録する。
`--include-preview`も従来どおりオフライン描画用で、署名URLへ新しい版を追加しない。

`prepare_release_candidate.py`は次の構成を新規ディレクトリへ出力する。

| 出力 | 内容 |
| --- | --- |
| `images/` | 公開済みレンダラー・地理データ・フォント・固定依存に、今回の作業版を追加した画像パッケージ |
| `receiver/` | 本体・集約・投稿・バックアップなどの実行コードと固定依存を含む受信パッケージ |
| `candidate-manifest.json` | 候補版・切り戻し版・元コミット・作業ツリー状態・全ファイルのSHA-256 |

両パッケージの候補プロファイルだけが`v5`を登録し、南海トラフの見出しカードを有効にする。
既定値は候補内でも`v1`のままで、検証プログラムが明示的にv5を選ぶ。
通常配布や稼働中のサービスがv5へ切り替わることはない。

コピー元は明示したファイル一覧に限定する。`config.py`、`.env`、秘密鍵、配送DB、バックアップは含めない。
`verify_bundle()`は宣言した全ファイルのハッシュ、追加・欠落・改変、シンボリックリンク、
両パッケージのプロファイルを検査する。返す`manifest_sha256`は候補の識別子であり、署名や公開の承認を表すものではない。

## 候補と旧版の基準を作る

リポジトリルートで実行する。出力先は毎回新しくし、同じ候補へ追加出力しない。
洪水の流路データは、先に[画像サービスの準備手順](../image_service/README.md#指定河川洪水予報のプレビュー)で用意する。

```sh
release_check_dir=$(mktemp -d /tmp/shirasu-release-rehearsal-XXXXXX)
python3 -B scripts/prepare_release_candidate.py "$release_check_dir/candidate"
python3 -B scripts/prepare_release_candidate.py --verify "$release_check_dir/candidate"

python3 -B scripts/prepare_release_baseline.py "$release_check_dir/legacy" --ref 43e0fa4
python3 -B scripts/prepare_release_baseline.py "$release_check_dir/legacy" \
  --candidate-images "$release_check_dir/candidate/images" \
  --allow-routing-changes \
  > "$release_check_dir/source-comparison.json"
mkdir "$release_check_dir/results"
```

`43e0fa4`は今回の候補準備前の比較基準。旧版の準備は、そのコミットに記録された公開配布用の一覧を読み、
必要なファイルだけをGitから取り出す。過去のスクリプトを実行したり、過去の設定ファイルを読み込んだりしない。

`source-comparison.json`では旧版のレンダラー、アセット、依存ロックの一致を確認する。
当初の候補では`image_protocol.py`と`image_service/app.py`は版の宣言・参照移動以外にAST差がありませんでした。
WebP接続後は`--allow-routing-changes`を明示し、この2ファイルを別途実行確認が必要な変更として報告します。
レンダラー・アセット・依存ロックの一致条件は緩めません。旧URL・PNG・ETagは下記の実応答で比較します。

## 配布物からDockerイメージを作る

両方とも候補内のDockerfileを使う。ビルドでは固定された依存を取得するためのネット接続を使い、
以降の受信再生・描画検証は`--network none`で実行する。

```sh
docker build --pull=false -t shirasu-images:release-rehearsal \
  "$release_check_dir/candidate/images"
docker build --pull=false -t shirasu:release-rehearsal \
  "$release_check_dir/candidate/receiver"
```

## 署名イベントからPNGまで確認する

受信側は本体の`Service`、本文生成、SQLite集約、Nostr署名を使う。差し替えるのは外部への配送部分で、
使う鍵は検証用の画像鍵とその場で生成したNostr鍵、新規DBだけとする。
40ケースを再生し、完成した46投稿の本文・原NMEA・URL・`imeta`・ALTを照合する。
南海トラフの未完了通知、後着での完成、取消、版変更・画像停止・再起動後の再送も確認する。

```sh
docker run --rm --network none \
  --volume "$release_check_dir/candidate:/candidate:ro" \
  --volume "$PWD:/checks:ro" --volume "$release_check_dir/results:/output" \
  --workdir /candidate/receiver \
  --env PYTHONPATH=/candidate/receiver:/checks:/checks/tests \
  shirasu:release-rehearsal python -B /checks/scripts/check_release_receiver.py \
  --bundle /candidate --output /output/posts.json
```

続いて別プロセスで旧版のURL・PNG・ETagを記録する。画像依存は候補のDockerイメージに固定し、
読み込む実行コードは旧版パッケージに限定する。

```sh
docker run --rm --network none \
  --volume "$release_check_dir/legacy:/legacy:ro" \
  --volume "$PWD:/checks:ro" --volume "$release_check_dir/results:/output" \
  --workdir /legacy --env PYTHONPATH=/legacy:/checks \
  shirasu-images:release-rehearsal python -B /checks/scripts/check_release_images.py \
  --legacy-package /legacy --events /output/posts.json --output /output/legacy.json
```

最後に候補のHTTPアプリへ同じ旧URLと新しい投稿URLを渡し、実際にPNGを描画する。
WSGI入口から署名検証、世代選択、描画、HTTP応答までを通し、レンダラーをスタブに置き換えない。

```sh
docker run --rm --network none \
  --volume "$release_check_dir/candidate:/candidate:ro" \
  --volume "$PWD:/checks:ro" --volume "$release_check_dir/results:/output" \
  --workdir /candidate/images --env PYTHONPATH=/candidate/images:/checks \
  shirasu-images:release-rehearsal python -B /checks/scripts/check_release_images.py \
  --bundle /candidate --events /output/posts.json --legacy-result /output/legacy.json \
  --output /output/images.json

python3 -B scripts/prepare_release_candidate.py --verify "$release_check_dir/candidate"
```

各検証プログラムは実行コードの読み込み元も確認する。リポジトリのファイルで配布漏れを補ってしまった場合は失敗する。
`python -B`で候補へ`__pycache__`を作らず、出力JSON・PNGは候補の外に保存する。
描画中も候補のハッシュが変わらないことを検査する。既存の結果JSONやPNGディレクトリは上書きしない。

保存済みの承認用ギャラリーがある場合は、実HTTPから取得したPNGをその投稿画像と比較する。
ケースの一覧画像には取消元報の明示参照など投稿と異なる条件があるため、`posts[].images.preview`を使う。
コミット表示だけの違いは比較せず、原入力・投稿本文・画像入力・ALT・PNGのSHA-256が全件一致することを求める。

```sh
python3 -B scripts/compare_release_preview.py \
  --events "$release_check_dir/results/posts.json" --images "$release_check_dir/results/images.json" \
  --gallery dist/preview --output "$release_check_dir/results/preview-comparison.json"
```

## 結果で確認する内容

| 記録 | 確認内容 |
| --- | --- |
| `candidate-manifest.json` | 検証対象の全ソース・アセット・依存と元コミット |
| `source-comparison.json` | 旧版のファイル不変とプロファイル抽出の範囲 |
| `results/posts.json` | 署名イベント、本文、原NMEA、画像URL、ALT、集約と切り戻しの経過 |
| `results/legacy.json` | v1〜v4の代表入力5例の署名URL、PNGハッシュ、寸法、容量、ETag |
| `results/images.json` | 候補の添付画像・経過イベントの描画、旧URLの一致、HTTP応答の検証 |
| `results/preview-comparison.json` | 承認済みギャラリーと実HTTP結果の投稿入力・本文・ALT・PNGの一致 |

HTTP側では`/healthz`、GET、HEAD、条件付きGETの304、MIME、キャッシュヘッダー、
改変署名・世代書き換え・不正入力の拒否とエラーの`no-store`を確認する。
旧版は代表例のURL・PNG・ETagを比較し、全入力の画像一致を保証したとは扱わない。
結果JSONの候補manifestハッシュと受信イベント出力のハッシュを照合し、別の検証回の結果を混ぜない。

この検証でPNGをローカル保存するのは結果を確認するため。運用時の画像サービスにPNGの永続保存機能を追加するものではない。

## 公開時の固定と切り戻し

公開対象は、検証結果が指す候補パッケージそのものとする。後日の作業ツリーから同じv5を作り直さない。
候補内の`render_preview`などの名前はそのままでも、コピーされたソース・アセット・依存はひとつの固定された配布物として扱う。
次のプレビュー変更を同じ公開済みv5へ再デプロイすると、既存URLの画像が変わるため、
公開済みの配布物を保管し、次回の変更は別の候補・世代として準備する。

切り戻しは、受信側で**新しい投稿の版をv4へ戻す**か、画像を無効にする。
WebPへ切り替えた後は`SHIRASU_IMAGE_ENCODING=png`も指定し、v4と`w1`を組み合わせない。
保存済みの署名イベントは作り直さず、v5 URLを含んでいても元のJSON・ID・署名で再送する。
そのため画像サービスにはv5のレンダラーと対応する検証鍵を残す。
v1〜v4しか持たない旧画像パッケージへ全面的に戻すと、既に投稿・保存されたv5 URLが404になり、この切り戻し条件を満たさない。

今回の確認範囲はローカルの実WSGI処理と描画。Vercelへのデプロイ、実ドメインのTLS、CDNのヒット・失効、
本番鍵、実リレーへの投稿、稼働中Composeの切替は含めない。これらは公開作業で確認する。

CIでも候補パッケージの作成、固定コミットの旧版比較、受信・HTTPの通し検証を行う。
承認用ギャラリーとのPNG一致確認は保存済みギャラリーがあるローカルで行う。
