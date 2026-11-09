# Shirasu の監視

`metrics` サービスは本体とは別プロセスで動き、SQLite の保存状態と自動バックアップの `status.json` を読みます。秘密鍵・通報本文・エラー文・ファイル名は公開しません。状態DBを読めない場合も HTTP 200 で `shirasu_state_readable 0` を返すため、監視プロセスの停止とDBの障害を区別できます。

DB接続は `mode=ro` と `query_only` を使います。ボリューム自体は書き込み可能にしています。SQLite が正常停止後のDBを開く際にも WAL/SHM 補助ファイルを作る必要があるためで、監視側は通報・配送履歴・状態を更新しません。

```sh
curl --fail http://127.0.0.1:9016/metrics
docker compose logs --tail 50 metrics
```

`/healthz` は監視プロセス自身の生存確認です。本体の健全性は `shirasu_healthy`、または本体の `python main.py --health` で確認します。メトリクスの HTTP ポートはホストの loopback に限定しています。

## 自動バックアップの監視

Composeはホストの `backups/auto/` を `metrics` に読み取り専用でマウントします。`SHIRASU_BACKUP_DIR` で指定したディレクトリの状態ファイルと、最後のバックアップファイルの存在を確認します。収集時にはバックアップ作成・署名検証・世代整理を実行しません。DBとバックアップは別々に読み取り、一方が読めない場合も他方のメトリクスを公開します。

| メトリクス | 意味 |
|---|---|
| `shirasu_backup_state_readable` | 状態ファイルが存在し、対応する形式として読めるか |
| `shirasu_backup_healthy` | バックアップワーカーのヘルス条件を満たすか |
| `shirasu_backup_process_running` | 最後に記録されたワーカーの稼働状態 |
| `shirasu_backup_heartbeat_timestamp_seconds` | 最終状態更新のUNIX秒 |
| `shirasu_backup_last_attempt_timestamp_seconds` | 最終取得試行のUNIX秒。未試行は0 |
| `shirasu_backup_last_success_timestamp_seconds` | 最終成功のUNIX秒。成功記録なしは0 |
| `shirasu_backup_has_success` | 成功記録の有無。時刻0の成功と未成功を区別 |
| `shirasu_backup_interval_seconds` | ワーカーに設定された実行間隔 |
| `shirasu_backup_failed` | 直近の作成・検証失敗が未解消か |
| `shirasu_backup_retention_failed` | 直近の世代整理失敗が未解消か |
| `shirasu_backup_last_file_present` | 最後の自動バックアップが通常ファイルとして存在するか |

欠落・破損した状態ファイルは `backup_state_readable 0` と `backup_healthy 0` で表し、読めない値を正常な0として補いません。エラーの詳細は `docker compose logs backup` で確認します。稼働状態やファイルの存在は記録時点・収集時点の情報であり、保存済みバックアップの内容を再検証した結果ではありません。

「2周期以上成功なし」は記録された実行間隔の2倍を使うため、間隔設定を変更してもルールを書き換える必要はありません。Dockerのヘルスチェックは実行間隔+120秒で期限超過を判定し、Prometheusの未成功・遅延アラートは下記の継続時間を含めて評価します。

## 既存の Prometheus に接続する

Prometheus が Docker ネットワーク `observer_observer` 上にある場合は、接続設定を一度だけコピーします。既に `compose.override.yaml` がある場合は上書きせず、内容をマージしてください。

```sh
cp -n monitoring/compose.observer.yaml compose.override.yaml
docker compose up -d --build
```

以後の通常の `docker compose` コマンドも、この override を自動で読み込みます。別のネットワーク名を使う場合は override の `name` を変更するか、`SHIRASU_PROMETHEUS_NETWORK` 環境変数を設定します。ホスト固有の override は Git とビルドコンテキストから除外しています。

既存 Prometheus 設定の `scrape_configs` に [prometheus.scrape.yml](prometheus.scrape.yml) のジョブを追加します。Prometheus から `shirasu-metrics:9016` を解決できることを確認してください。

さらに [alerts.yml](alerts.yml) を Prometheus コンテナへ読み取り専用でマウントし、そのパスを既存設定の `rule_files` に追加します。例:

```yaml
rule_files:
  - /etc/prometheus/rules/shirasu.yml
```

ファイルのマウント追加はコンテナの再作成、以後のルール変更は Prometheus の設定リロードが必要です。既存の scrape・remote_write・alerting 設定は残します。

## 通知先への接続

通知先への接続はユーザー指示により保留しています。現在の状態と再開時の作業は [TODO](../TODO.md) に記録しています。

メトリクスの保存と通知配送は別の設定です。**Prometheus → Mimir の remote_write だけではメールやチャットへ通知されません。** 既存の Alertmanager を使う場合は、Prometheus の `alerting.alertmanagers` にその接続先を設定し、Alertmanager の route/receiver で通知先を指定します。

Grafana を使う場合は、同じ条件を Grafana のアラートルールとして登録するか、Mimir の Ruler にルールを読み込ませ、利用中の Alertmanager と通知先へつなぎます。このリポジトリの YAML は Prometheus 形式で、Grafana 管理ルールの provisioning YAML とは形式が異なります。URLや認証情報を含む通知先設定は、このリポジトリへコミットしないでください。

| アラート | 条件と継続時間 |
|---|---|
| `ShirasuExporterDown` | 収集失敗、または収集対象がなくなって2分 |
| `ShirasuStateUnavailable` | 状態DBが読めない状態が1分 |
| `ShirasuProcessStalled` | 本体停止、または90秒以上の状態更新停止などが2分 |
| `ShirasuReceptionStalled` | gpsd切断、または90秒以上の受信停止などが2分 |
| `ShirasuDeliveryStalled` | リレー切断・配送失敗・90秒以上の送信待ち・100件以上の滞留のいずれかが2分 |
| `ShirasuReportsDropped` | 過去10分に保存失敗があり、その判定が1分 |
| `ShirasuBackupStateUnavailable` | バックアップ状態を読めない、または収集成功中なのに対応メトリクスが欠落して1分 |
| `ShirasuBackupWorkerStalled` | ワーカー停止、90秒以上の状態更新停止、状態更新・取得試行時刻が30秒超未来のいずれかが2分 |
| `ShirasuBackupOverdue` | 成功記録なし・2周期以上成功なし・成功時刻が30秒超未来・最後のファイル欠落のいずれかが2分 |
| `ShirasuBackupFailed` | 作成・検証失敗が2分以上未解消 |
| `ShirasuBackupRetentionFailed` | 世代整理失敗が2分以上未解消 |

本体停止時には受信・配送のアラートを抑え、同じ原因で通知が重ならないようにしています。障害条件が解消すればアラートも解消します。通知のまとめ方・再通知間隔・復旧通知の有無は Alertmanager/Grafana 側で設定します。ホスト全体の停止は、別ホストの監視側でこのホストのデータ消失を検出してください。

バックアップ監視は本体の稼働状態に依存しません。収集停止時はバックアップ側のアラートを抑え、状態ファイルを読めない場合は状態欠落だけ、ワーカーが停止・停滞している場合はワーカー停止だけを評価します。稼働中の失敗が長期化した場合、失敗と最後の成功からの遅延は両方が発火することがあります。

## ルールの検証

```sh
docker run --rm --network none --entrypoint promtool \
  --volume "$PWD/monitoring:/rules:ro" \
  prom/prometheus:v3.1.0@sha256:6559acbd5d770b15bb3c954629ce190ac3cbbdb2b7f1c30f0385c4e05104e218 \
  test rules /rules/alerts.test.yml
```

`alerts.test.yml` で短い切断を無視すること、継続障害で発火すること、復旧で解消することを検証します。CI も同じコマンドを実行します。
