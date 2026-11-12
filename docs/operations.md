# 運用

コマンドはリポジトリルートで実行します。固定イメージを利用している場合は、
`.env`の`COMPOSE_FILE`と既存のhost overrideを維持してください。
旧リリースのイメージでは、そのリリースの起動コマンドを使います（旧版は`python main.py`）。

## 状態と履歴

```sh
docker compose ps
docker compose logs --tail 100 -f shirasu
docker compose exec -T shirasu python -m shirasu --status --format text
docker compose exec -T shirasu python -m shirasu --history --state pending
docker compose exec -T shirasu python -m shirasu --show EVENT_ID
```

`--status`は接続状態、最終受信・送信、配送待ち、集約待ち、エラーを表示します。
`--history`は保存中の配送履歴、`--show`は保存当時の投稿本文とNMEAを表示します。
イベントIDは12桁以上の一意な先頭部分でも指定できます。`--format json`でJSON出力になります。
これらの確認操作はDBを読み取るだけで、投稿や配送状態の更新を行いません。

ヘルスチェックは本体・gpsd・リレー・受信・配送の状態を確認します。
Dockerの`unhealthy`だけでは再起動されません。通信断はアプリが再接続し、
プロセス終了はComposeの`restart: always`が再起動します。
ログは各コンテナ10MB×3世代に制限しています。

## バックアップ

`backup`サービスは既定で1時間ごとに取得し、`backups/auto/`に168世代を保持します。
作成と検証に成功した場合だけ古い自動バックアップを削除します。
間隔と保持数は`.env`で変更できます。

```dotenv
SHIRASU_BACKUP_INTERVAL_SECONDS=3600
SHIRASU_BACKUP_KEEP_COUNT=168
```

```sh
docker compose up -d backup
docker compose logs --tail 50 backup
```

手動バックアップは自動世代管理の対象外です。

```sh
mkdir -p backups
backup_name="manual-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
docker compose run --rm --no-deps -T \
  -v "$PWD/backups:/backups" \
  shirasu python -m shirasu --backup "/backups/$backup_name"
docker compose run --rm --no-deps -T \
  -v "$PWD/backups:/backups:ro" \
  shirasu python -m shirasu --verify-backup "/backups/$backup_name"
```

SQLiteのオンラインバックアップAPIでWAL内の確定済み変更もコピーし、
DB整合性・スキーマ・保存済みイベントの署名とIDを検証します。
出力は単体SQLiteファイルです。稼働中のDBファイルだけを通常コピーしないでください。
`config.py`、`.env`、秘密鍵、gpsd設定は含まれないため別途保管します。
ホスト障害に備える場合は、検証済みファイルを別の保存先にもコピーします。

## DBの復元

古いバックアップを使う場合、取得後に送信した投稿の履歴が欠落します。
公開を再開する前に元DB等と送信履歴を照合してください。照合できなければ重複投稿を防げません。
切替後に投稿した場合、元volumeへ戻す際にも同じ確認が必要です。

1. `docker compose stop shirasu metrics backup`で全サービスを停止する。
2. 元DBが読める場合は停止後の最新版をバックアップし、復元元を`--verify-backup`で検証する。
3. 空の新規Docker volumeへ単体SQLiteを`shirasu.sqlite3`として配置する。元volumeとバックアップを残し、`-wal`・`-shm`はコピーしない。
4. 既存の`compose.override.yaml`へ下記のvolume割当を統合する。既存のネットワーク等は維持する。
5. `docker compose config --quiet`で構文を確認して起動し、状態・履歴を確認する。

```yaml
volumes:
  shirasu-state:
    external: true
    name: <復元先のvolume名>
```

```sh
docker compose up -d --no-build
docker compose exec -T shirasu python -m shirasu --status --format text
docker compose exec -T shirasu python -m shirasu --history
```

## 監視

`metrics`サービスは`http://127.0.0.1:9016/metrics`を公開します。
本体と別プロセスでDB・バックアップ状態を読み、本体停止も検出します。
収集設定は[`monitoring/prometheus.scrape.yml`](../monitoring/prometheus.scrape.yml)、
アラート条件は[`monitoring/alerts.yml`](../monitoring/alerts.yml)を参照してください。

既存PrometheusのDockerネットワークへ接続する例:

```sh
cp -n monitoring/compose.observer.yaml compose.override.yaml
# .envに SHIRASU_PROMETHEUS_NETWORK=<既存ネットワーク名> を設定する
docker compose config --quiet
docker compose up -d metrics
```

Prometheus側は`shirasu-metrics:9016`をscrapeします。受信・配送・状態更新・バックアップの
停滞や失敗を評価し、短い切断では通知しないルールを同梱しています。
メール等への配送には、別途Alertmanagerなどの通知先設定が必要です。
