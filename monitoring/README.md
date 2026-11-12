# 監視設定

受信機の`metrics`サービスから、状態と自動バックアップのPrometheusメトリクスを収集します。
接続手順は[運用ドキュメント](../docs/operations.md#監視)を参照してください。

| ファイル | 用途 |
| --- | --- |
| `prometheus.scrape.yml` | 既存Prometheusの`scrape_configs`へ追加する収集例 |
| `compose.observer.yaml` | metricsを既存Dockerネットワークへ接続するoverride例 |
| `alerts.yml` | 受信・配送・保存・バックアップのアラート規則 |
| `alerts.test.yml` | 発火・短い切断の抑制・復旧のテスト |

`/metrics`のHTTP成功だけでは本体の正常を意味しません。DBが読めない場合もHTTP 200で
`shirasu_state_readable 0`を返します。`/healthz`は監視プロセス自身の生存確認です。
通知の配送はAlertmanager等で別途設定します。

```sh
docker run --rm --network none --entrypoint promtool \
  --volume "$PWD/monitoring:/rules:ro" \
  prom/prometheus:v3.1.0@sha256:6559acbd5d770b15bb3c954629ce190ac3cbbdb2b7f1c30f0385c4e05104e218 \
  test rules /rules/alerts.test.yml
```
