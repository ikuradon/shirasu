# 状態DBのバックアップと復元

`--backup FILE` は稼働中の状態DBから、整合性のあるSQLiteのバックアップを作ります。SQLiteのオンラインバックアップAPIを使うため、WALにある確定済みの変更も含められます。稼働中の `.sqlite3` ファイルだけを通常のファイルコピーで保存する方法は使いません。[SQLiteのオンラインバックアップAPI](https://www.sqlite.org/backup.html)

対象はDB内の全アカウントです。署名済みイベント、送信待ち、送信済み・期限切れの記録、重複判定用の履歴、保存された状態を含みます。`config.py`、秘密鍵、ホストのgpsd・ネットワーク設定は含みません。

## Composeの自動バックアップ

`docker compose up -d --build` で `backup` サービスも起動します。既定では1時間ごとに検証済みのバックアップを作成し、ホストの `backups/auto/` に最新168世代（通常運転で約7日分）を保持します。本体とは別プロセスで動き、秘密鍵・設定ファイルはマウントせず、コンテナのネットワークも無効にしています。

初回はすぐに取得します。再起動時は保存した最終成功日時とバックアップを確認し、すでに1時間経っていれば次を作成、経っていなければ残り時間を待ちます。前回の作成失敗や状態ファイルの破損、バックアップの欠落などで引き継げない場合は、起動後に取得します。作成・検証に失敗した場合は60秒後に再試行し、過去のバックアップを削除しません。

世代整理は新しいバックアップの作成・検証が成功した場合だけ行います。対象はこの専用ディレクトリの `auto-日時-識別子.sqlite3` という自動生成名の通常ファイルです。手動バックアップは `backups/` 直下など別の場所に保存してください。対象外の名前・シンボリックリンクは整理せず、同じ保存先での二重起動はロックで拒否します。世代整理だけに失敗した場合は成功したバックアップを保持し、エラーを記録して次の定期取得時に再び整理します。

```sh
docker compose ps backup
docker compose logs --tail 50 -f backup
docker compose exec -T backup python backup_worker.py --health
docker compose exec -T backup cat /backups/status.json
```

成功・失敗と世代整理の結果は `docker compose logs backup`、最終成功日時・保存ファイル名・直近エラーは `status.json` で確認できます。ヘルスチェックは状態更新の停滞、成功からの経過時間、ファイルの欠落、直近のバックアップ・世代整理失敗を検出します。外部への通知先は引き続き保留です。

実行間隔と保持数は環境変数またはリポジトリの `.env` で変更し、`docker compose up -d backup` で反映します。以下は既定値です。

```dotenv
SHIRASU_BACKUP_INTERVAL_SECONDS=3600
SHIRASU_BACKUP_KEEP_COUNT=168
```

状態ボリュームはSQLiteのWAL/SHM補助ファイル作成のため書き込み可能ですが、DB接続は読み取り専用です。本体停止中も、保存済みのDBがあれば取得できます。停止には `docker compose stop backup` を使い、進行中の取得を終了する猶予を90秒設けています。復元・ボリューム切替の間は本体・監視とともに停止します。

## バックアップを作る

以下はリポジトリのルートで実行するBashの例です。出力先の親ディレクトリは先に作成します。既存ファイルは上書きしません。

```sh
mkdir -p backups
shirasu_backup_name="shirasu-$(date -u +%Y%m%dT%H%M%SZ)-$$.sqlite3"
docker compose run --rm --no-deps \
  -v "$PWD/backups:/backups" \
  shirasu python main.py --backup "/backups/$shirasu_backup_name"
```

本体・監視は稼働したままで実行できます。Composeが設定ファイルをマウントしても、このコマンドは設定・秘密鍵を読み込まず、受信・署名・Nostr投稿を開始しません。コピー元は `SHIRASU_STATE_PATH` で指定されたDBを読み取り専用で開きます。

出力先と同じディレクトリの一時ファイルにコピーし、検証後に完成ファイルとして公開します。出力はWALに依存しない単体ファイルです。取得に失敗した場合は成功したバックアップとして扱わず、エラーを確認してください。

コピー側だけに `backup_format_version` と `backup_created_at` を保存します。作成日時はバックアップ操作の日時で、全通報の受信日時を意味しません。表示される保存状態の最終更新日時も確認してください。

`backups/` はGit管理・Dockerビルドの対象外です。同じホスト上に保存するだけではホスト障害に備えられないため、完成・検証済みの単体ファイルを必要に応じて別の保存先にもコピーしてください。手動で作成したバックアップの自動削除は行いません。

## バックアップを検証する

```sh
docker compose run --rm --no-deps \
  -v "$PWD/backups:/backups:ro" \
  shirasu python main.py --verify-backup "/backups/$shirasu_backup_name"
```

後日実行する場合は `shirasu_backup_name` に確認したいファイル名を設定してください。検証ではSQLiteの整合性・対応するスキーマ・バックアップ情報と、保存された署名済みイベントを確認します。署名、イベントID、投稿者と保存先アカウントの一致を確認し、全アカウントの状態別件数を表示します。検証に失敗した場合は非0の終了コードを返します。

検証は読み取り専用です。期限切れ処理、履歴削除、再送、保存状態の更新は行いません。`--backup` と `--verify-backup` は、どちらも `--format json` を付けると機械向けの結果を出力できます。

**検証成功は「このバックアップが最新」「復元後も重複投稿しない」「本体が正常に稼働中」という意味ではありません。** 作成日時と状態更新日時、件数を確認し、復元後の公開再開は下記の送信履歴照合を終えてから判断します。

## 復元前に確認すること

バックアップ内の送信待ちは元の本文・署名・イベントIDを保持します。復元後も同じイベントを再送できますが、通常の再送期限は適用されるため、古い通報が必ず再送されるわけではありません。

バックアップ取得後に作成・送信した通報は、そのバックアップに含まれません。この欠落を残したまま再受信すると、同じ通報を新しいイベントIDで投稿する可能性があります。公開を再開する前に、対象アカウントとバックアップ取得後の送信履歴を照合し、欠落分の重複判定情報を補う必要があります。

元のDBが読めるなら、停止後の最新版をバックアップして使うのが確実です。古いバックアップを使う場合は、残っているDBやリレー側の自分の投稿履歴を根拠に照合します。リレーから取得できた投稿だけでは、そのリレーに残っていない投稿まで網羅したとは判断できません。

今回の機能には、送信履歴の自動取得・照合・取り込み、復元先の上書きコマンドは含みません。欠落の有無を確認できない場合、重複投稿の防止は保証できません。下記の新規ボリュームへの復元・検証までを行い、本体の公開起動は保留してください。

## 新しいボリュームへ復元する

稼働DBの上書きは避け、新規ボリュームへ復元します。元のボリュームは残します。旧DBの `-wal` / `-shm` やロックファイルを復元先へコピーしないでください。

まず復元元を上記のコマンドで検証し、以後そのファイルを変更しない状態で作業します。以下では同じBashセッションを使います。

明示的にComposeファイルを指定する場合、既存の `compose.override.yaml` も含めます。現在の監視ネットワーク設定を失わないためです。

```sh
shirasu_compose=(docker compose -f compose.yaml)
if [ -f compose.override.yaml ]; then
  shirasu_compose+=(-f compose.override.yaml)
fi
"${shirasu_compose[@]}" stop shirasu metrics backup
```

この時点で受信・投稿、監視、自動バックアップが停止します。元DBが読み取れる場合は、切替前の記録を保全するため、別名で `--backup` を実行して検証します。

新規ボリュームと、復元先だけを変更する一時的なCompose設定を作ります。

```sh
shirasu_restore_volume="shirasu-restored-$(date -u +%Y%m%dT%H%M%SZ)-$$"
docker volume create "$shirasu_restore_volume"
shirasu_restore_override=$(mktemp "$PWD/backups/restore-compose.XXXXXX.yaml")
cat > "$shirasu_restore_override" <<EOF
volumes:
  shirasu-state:
    external: true
    name: $shirasu_restore_volume
EOF
shirasu_restore_compose=("${shirasu_compose[@]}" -f "$shirasu_restore_override")
"${shirasu_restore_compose[@]}" config --quiet
```

検証済みの単体ファイルをコピーします。この処理は空の復元先だけを受け付け、既存ファイルを上書きしません。次のコマンドが失敗した場合は、そのまま次の段階へ進まないでください。

```sh
"${shirasu_restore_compose[@]}" run --rm --no-deps -T \
  -v "$PWD/backups:/backups:ro" \
  shirasu python - "/backups/$shirasu_backup_name" <<'PY'
import os
from pathlib import Path
import shutil
import sys

destination = Path('/data/shirasu.sqlite3')
if any(destination.parent.iterdir()):
    raise SystemExit('復元先が空ではありません。新規ボリュームを使用してください。')
with open(sys.argv[1], 'rb') as source, destination.open('xb') as target:
    shutil.copyfileobj(source, target)
    target.flush()
    os.fsync(target.fileno())
PY

"${shirasu_restore_compose[@]}" run --rm --no-deps \
  shirasu python main.py --verify-backup /data/shirasu.sqlite3
"${shirasu_restore_compose[@]}" run --rm --no-deps \
  shirasu python main.py --history
```

この段階では本体を起動していません。復元元と復元先の作成日時、件数、アカウントを比較し、必要な送信待ち・送信済み記録を確認します。`--show EVENT_ID` でも保存当時の本文を確認できます。古いバックアップの場合は、前節の送信履歴照合・補完を完了させてください。

## 切替を確定して公開を再開する

**送信履歴の確認が完了した場合だけ進めます。** 一時的なCompose設定だけで切り替えると、次の通常の `docker compose up` で元のボリュームに戻るおそれがあります。

`shirasu_restore_override` の内容を確認し、その `volumes.shirasu-state` 設定を既存の `compose.override.yaml` に統合してください。既存の `services`・`networks` 設定は残します。すでに `volumes` がある場合は、その配下へ統合し、同じトップレベルキーを二重に書かないでください。

```sh
cat "$shirasu_restore_override"
```

統合後、`docker compose config` の `volumes.shirasu-state.name` が復元先の名前であることを確認します。

```sh
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
docker compose exec shirasu python main.py --status --format text
docker compose logs --tail 100 shirasu metrics
```

起動後の受信・配送・監視状態を確認します。バックアップの状態欄にあった「稼働」「接続中」は保存当時の記録なので、起動後の新しい状態更新で判断してください。

元のボリュームは、切替後の動作と送信履歴を確認するまで削除しません。切替後に新しい投稿がある状態で元のボリュームへ戻すと、その投稿の重複判定情報が再び欠落します。戻す場合も本体・監視・自動バックアップを停止し、切替後のDBを保全して、送信履歴を照合してから判断してください。
