# shirasu

gpsd 経由で受信した「みちびき」の災害・危機管理通報をデコードし、Nostr に投稿する常駐プログラムです。受信には u-blox 形式を使います。

## 起動

Python 3.12 の Docker イメージを使用します。Linux ホストで gpsd が `127.0.0.1:2947` を待ち受け、受信機から災危通報を取得できることが前提です。Compose はホストのネットワークを使い、`host.docker.internal` を `127.0.0.1` に対応させます。

初回は設定例をコピーし、`config.py` を編集します。すでに `config.py` がある場合は上書きせず、既存設定を使ってください。

```sh
cp -n config.py.sample config.py
```

`SECRET_KEY` に Nostr の秘密鍵、`RELAY_URLS` に投稿先のリレー URL を指定します。複数のリレーはカンマで区切ります。gpsd の接続先は `GPSD_HOST` と `GPSD_PORT` で設定します。

```sh
docker compose up -d --build
```

`config.py` は Git 管理・イメージへのコピー対象から除外され、コンテナには読み取り専用で渡されます。

## 状態とログ

```sh
docker compose ps
docker compose exec shirasu python main.py --status
docker compose logs --tail 100 -f shirasu
```

`--status` は保存済みの状態を JSON で表示します。送信待ち・送信済み・期限切れの件数、最後の受信・投稿時刻、接続状態、直近のエラーなどを確認できます。時刻は UNIX 秒です。状態確認では秘密鍵を読み込まず、DB を読み取り専用で開きます。

ヘルスチェックは、プロセスの動作、gpsd とリレーの接続、90秒以内の状態更新と通報受信を確認します。未解消の投稿エラーや90秒以上の送信待ちも異常と判定します。Docker の `unhealthy` 判定だけでは自動再起動しません。通常の通信切断はアプリ自身が再接続・再送し、プロセスが終了した場合は Compose の `restart: always` が再起動します。

起動・停止はログに記録します。公開 Nostr 投稿の `booting...` は送信しません。

## 配送と重複防止

- 受信と送信を分離し、送信の再試行中も通報を受信します。空・不明の DCX 通報は投稿しません。
- 署名済みイベントを SQLite にコミットしてから送信します。再送・再起動後も同じイベント ID と署名を使います。
- 少なくとも1つのリレーから受理の ACK を得たら送信済みにします。全リレーへの配送を保証する方式ではありません。
- 送信は受信順の FIFO です。先頭が再送待ちの間は後続も待ち、警報と更新・解除の順序が逆転することを防ぎます。再試行の待ち時間は最大60秒です。
- 未送達のまま受信から15分が経過した通報は `expired` として記録し、新たな再送を開始しません。先頭の期限切れ後は次の通報へ進みます。
- 重複情報は最終受信または送達確認から24時間保持します。同じ通報を繰り返し受信している間は保持期間が延びます。
- 送信待ちの上限は1000件です。満杯で保存できなかった通報はエラーログと `dropped_full` カウンタに記録します。

ACK 受信と DB への成功記録の間に停止すると、次回は同じイベント ID を再送し、リレー側の重複抑制を利用します。システム全体での「exactly once」を保証するものではありません。

## 設定

既存の `config.py` に次の項目がなくても既定値で動きます。

| `config.py` の項目 | 既定値 | 内容 |
|---|---:|---|
| `RETRY_MAX_AGE_SECONDS` | `900` | 最初の受信から再送を続ける秒数。`0` は期限なし |
| `DEDUP_SECONDS` | `86400` | 同一通報の重複情報を保持する秒数 |
| `MAX_PENDING` | `1000` | 保存する送信待ち件数の上限 |
| `GPSD_READ_TIMEOUT` | `30` | gpsd からデータが届かないときに再接続するまでの秒数 |

送信中の履歴を早く消さないよう、`DEDUP_SECONDS` は `RETRY_MAX_AGE_SECONDS + 60` 以上にします。

`SHIRASU_STATE_PATH` 環境変数で DB の保存先を指定できます。Compose では `/data/shirasu.sqlite3`、直接実行時の既定値は `state/shirasu.sqlite3` です。`SHIRASU_LOG_LEVEL` の既定値は `INFO` です。

`config.py` の変更は `docker compose restart shirasu` で反映します。Compose 側の環境変数・ボリューム設定を変えた場合は、`docker compose up -d` でコンテナを再作成してください。

## 永続データと停止

DB は Compose の名前付きボリューム `shirasu-state` に置きます。既定のプロジェクト名 `shirasu` では、Docker 上の名前は **`shirasu_shirasu-state`** です。署名済み公開イベント、配送履歴、状態を保存し、秘密鍵は保存しません。同じ DB を使う多重起動はロックで拒否します。

```sh
docker compose stop shirasu
```

停止時は SIGTERM を受けて受信と送信を終了します。Compose の終了猶予は20秒です。未送達イベントは DB に残り、次回起動時に期限内のものから再開します。

**本番の状態ボリュームを削除しないでください。** `docker compose down -v`、このボリュームへの `docker volume rm`、未使用ボリュームを削除する `docker volume prune -a` などで記録を消すと、未送達イベントと重複判定の記憶を失います。コンテナの再作成・イメージ更新ではボリュームを維持してください。DB は WAL を使うため、稼働中の `.sqlite3` ファイルだけをコピーしてバックアップしないでください。

旧版から空の DB へ切り替えると、それ以前の投稿は重複判定できません。初回移行では、過去24時間の自分の通報投稿を `Outbox.seed_sent()` で送信済み履歴へ登録してから切り替えます。以後は同じ状態ボリュームを継続して使用します。

## テスト

```sh
docker compose run --rm --no-deps -T shirasu python -B -m unittest discover -s tests -v
```

回帰テストで、通報の除外・署名、通信切断後の復帰、保存・再送・順序・期限、再起動後の重複抑制を検証します。
