# 開発

## 修正箇所

| 変更したいもの | 主なファイル |
| --- | --- |
| CLI引数・コマンドの振り分け | `shirasu/cli.py`、`shirasu/__main__.py` |
| 起動設定・受信から投稿までの統括・終了処理 | `shirasu/service.py` |
| gpsd接続・受信 | `shirasu/receiver.py` |
| 再送・重複抑制・保存形式 | `shirasu/outbox.py` |
| 投稿本文・DCXの指示 | `shirasu/reports/format.py`、`shirasu/reports/dcx.py` |
| 台風・南海トラフの集約 | `shirasu/reports/typhoon.py`、`shirasu/reports/nankai.py` |
| 画像URL・ALT | `shirasu/images/protocol.py`、`publication.py`、`alt.py` |
| 状態CLI・監視・バックアップ | `shirasu/inspection.py`、`health.py`、`metrics.py`、`backup*.py` |
| 画像HTTP API | `image_service/app.py` |
| 既存版の描画 | `image_service/render.py`、`render_v2.py`〜`render_v4.py` |
| 次の画像デザイン | `image_service/render_preview.py`、`preview_wide_*.py` |
| 縮尺・地名・ミニマップ | `image_service/preview_scale.py`、`preview_place_labels.py`、`preview_context.py` |
| 固定データの配置・再加工 | `tools/assets/`、`vendor/` |
| ギャラリー・比較・計測 | `tools/preview/` |
| 配布物の作成・検証・公開 | `tools/release/`、`.github/` |

`image_service`の公開済み描画と固定アセットは、同じURLの出力を維持するため分離しています。
新しいデザインは作業版で確認し、公開前に既存画像との互換性を検証してください。
受信処理へMatplotlib等の描画依存を追加しません。

## テスト

受信機・秘密鍵を使わずに実行できます。

```sh
docker build --target test -t shirasu:test .
docker run --rm --network none shirasu:test \
  python -B -m unittest discover -s tests/receiver -v
docker run --rm --network none shirasu:test \
  python -B -m unittest discover -s tests/integration -v
```

画像テストは描画用環境を作り、変更したテストファイルから実行します。

```sh
python3 -m tools.assets.prepare_vendor_assets
python3.12 -m venv .venv-images
.venv-images/bin/pip install --require-hashes -r image_service/requirements.txt
.venv-images/bin/python -B -m unittest discover -s tests/images -p 'test_image_renderer*.py'
```

`tests/receiver`・`images`・`assets`・`release`は対象別のテスト、`tests/integration`は接続・復旧テストです。
全体の検証手順は`.github/actions/check-receiver/`と`check-images/`が基準です。
公開サンプルは`tests/fixtures/qzqsm/`、合成例は`image_service/examples/`と
`tools/preview/gallery_catalog.py`にあります。実受信例と合成訓練例を区別して追加してください。

## プレビュー

受信テキストを投稿せずに確認する例:

```sh
docker run --rm --network none shirasu:test \
  python -m shirasu --preview tests/fixtures/qzqsm/qzqsm_dcx_260917.log \
  --input-format nmea --received-at 2026-09-17T01:00:00+00:00
```

画像は描画用環境で生成します。コマンドは設定・秘密鍵・既存DBを読み込まず、Nostrへ投稿しません。
出力先は新規ファイル・ディレクトリを指定してください。

```sh
.venv-images/bin/python -m image_service --preview \
  --records image_service/examples/typhoon.json --output /tmp/typhoon.png
.venv-images/bin/python -m tools.preview.render_gallery --preview-only --output dist/preview
.venv-images/bin/python -m tools.preview.render_gallery \
  --baseline dist/preview --output dist/preview-next
.venv-images/bin/python -m tools.preview.render_timeline --output dist/timeline
```

ギャラリーの`index.html`では投稿本文・画像・ALT・SNS幅を確認できます。
`--baseline`は前回の作業版との差分、timelineは受信順と集約時刻を確認するためのものです。
`--list`でケース一覧、`--case`または`--scenario`で対象を絞れます。

描画時間・容量・形式の比較には`tools.preview.benchmark_images`、
WebPの比較には`tools.preview.benchmark_webp`を使います。どちらも`--help`でオプションを確認できます。

## 依存とデータの更新

受信依存は`requirements/receiver.in`、画像依存は`image_service/requirements.in`で管理します。
変更後は対応するハッシュ付きlockを再生成し、`pip check`と関連テストを実行します。
画像の依存変更では既存URLの出力比較も必要です。

通常のビルドは`vendor/`の固定データを検証・配置するだけで、外部GISを再取得しません。
データの更新方法、取得元、ライセンスは[データ更新手順](../vendor/README.md#データ更新)を参照してください。
`dist/`は生成物、`state/`・`backups/`は運用データです。後者をビルド掃除の対象にしないでください。
