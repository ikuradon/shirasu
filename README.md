# shirasu

みちびき（QZSS）の災害・危機管理通報を受信し、Nostrへ投稿するプログラムです。
gpsdから取得したQZQSMを[azarashi](https://github.com/nbtk/azarashi)で復号します。

- SQLiteへ投稿を保存し、通信切断後も同じ署名付きイベントを再送します。
- 台風と南海トラフの分割通報をまとめて投稿します。
- 画像サービスを設定すると、地図や見出し画像の署名付きURLを添付できます。画像の描画は受信機では行いません。
- 状態確認CLI、Prometheusメトリクス、自動バックアップを備えています。

## 起動

Linux、Docker Compose、QZQSMを受信できる受信機とgpsdが必要です。
gpsdはホストの`127.0.0.1:2947`で待ち受ける構成を想定しています。

```sh
cp -n config/config.py.example config.py
```

`config.py`の`SECRET_KEY`にNostr秘密鍵、`RELAY_URLS`に投稿先リレーを設定します。
既存の設定がある場合は上書きせず使用してください。

```sh
docker compose up -d --build
docker compose logs --tail 50 -f shirasu
```

受信・監視・バックアップの3サービスが起動します。`config.py`は読み取り専用で渡され、
投稿履歴はDocker volume、自動バックアップは`backups/auto/`へ保存されます。

画像の添付は既定で無効です。設定方法と、検証済みイメージを使う運用は
[配布・デプロイ](docs/deployment.md)を参照してください。

## 状態確認

```sh
docker compose ps
docker compose exec -T shirasu python -m shirasu --status --format text
docker compose exec -T shirasu python -m shirasu --history
```

起動すると実際にNostrへ投稿します。投稿せず確認したい場合は
[開発用プレビュー](docs/development.md#プレビュー)を使用してください。

## ドキュメント

| 内容 | 案内 |
| --- | --- |
| 状態確認、バックアップ、復元、監視 | [運用](docs/operations.md) |
| 修正箇所、テスト、画像プレビュー | [開発](docs/development.md) |
| GitHub Actions、GHCR、Vercel、更新手順 | [配布・デプロイ](docs/deployment.md) |
| 投稿・集約・画像URLの仕様と対応範囲 | [仕様](docs/specification.md) |
| DCXの地理情報と解釈上の制約 | [DCX仕様補足](docs/dcx.md) |
| 未完了の課題 | [TODO](TODO.md) |

地図・フォント等の取得元、加工内容、ライセンスは[`vendor/`](vendor/README.md)にあります。
受信サンプルの出典は[`tests/fixtures/qzqsm/`](tests/fixtures/qzqsm/README.md)を参照してください。
