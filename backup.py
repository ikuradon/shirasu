"""Consistent, standalone SQLite backups without changing delivery state."""

from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from nostr_sdk import Event

from outbox import SCHEMA_VERSION, _read_connection


BACKUP_FORMAT_VERSION = '1'
BACKUP_TIMEOUT_SECONDS = 60
STATES = ('pending', 'sent', 'expired')
REQUIRED_COLUMNS = {
    'events': {'identity', 'key', 'event_json', 'event_id', 'state', 'attempts',
               'created_at', 'last_seen', 'next_attempt_at', 'sent_at', 'last_error'},
    'status': {'identity', 'name', 'value', 'updated_at'},
    'metadata': {'name', 'value'},
}
PRIMARY_KEYS = {'events': ('identity', 'key'), 'status': ('identity', 'name'),
                'metadata': ('name',)}


def _sidecars(path):
    return [Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')]


def _check_destination(path):
    if os.path.lexists(path) or any(os.path.lexists(item) for item in _sidecars(path)):
        raise FileExistsError('保存先または付随ファイルが既に存在します。別のファイル名を指定してください')
    if not path.parent.is_dir():
        raise FileNotFoundError('バックアップ先のディレクトリがありません')


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label}が不正です')
    return value


def _identity(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('保存された投稿アカウントが不正です')
    return value


def _reject_json_constant(value):
    raise ValueError('状態JSONに有限でない数値があります')


def _validate_schema(connection):
    for table, expected in REQUIRED_COLUMNS.items():
        kind = connection.execute('SELECT type FROM sqlite_master WHERE name = ?', (table,)).fetchone()
        if kind is None or kind['type'] != 'table':
            raise ValueError('状態DBのテーブル構造が不正です')
        columns = connection.execute(f'PRAGMA table_info({table})').fetchall()
        if not expected.issubset({row['name'] for row in columns}):
            raise ValueError('状態DBに必要な列がありません')
        primary = tuple(row['name'] for row in sorted(columns, key=lambda row: row['pk']) if row['pk'])
        if primary != PRIMARY_KEYS[table]:
            raise ValueError('状態DBの主キーが不正です')


def verify_backup(path):
    """Validate a standalone archive; success does not establish relay history freshness."""
    path = Path(path).absolute()
    if not path.is_file():
        raise FileNotFoundError('バックアップファイルがありません')
    if any(os.path.lexists(item) for item in _sidecars(path)):
        raise ValueError('付随ファイルがあります。単独のバックアップファイルを指定してください')
    # Refuse WAL format before SQLite can create shared-memory sidecars. The
    # backup writer switches its copy to DELETE mode before publishing it.
    with path.open('rb') as handle:
        header = handle.read(20)
    if len(header) != 20 or header[:16] != b'SQLite format 3\x00':
        raise ValueError('SQLiteのバックアップファイルではありません')
    if header[18:20] != b'\x01\x01':
        raise ValueError('WALに依存しない単独のバックアップファイルを指定してください')

    with _read_connection(path, None) as (connection, identity):
        _validate_schema(connection)
        # Collections share this database and must survive a restore too.
        from typhoon import validate_storage
        validate_storage(connection)
        result = connection.execute('PRAGMA integrity_check').fetchall()
        if [row[0] for row in result] != ['ok']:
            raise ValueError('バックアップのSQLite整合性検査に失敗しました')
        metadata = dict(connection.execute('SELECT name, value FROM metadata').fetchall())
        if metadata.get('backup_format_version') != BACKUP_FORMAT_VERSION:
            raise ValueError('この形式のバックアップには対応していません')
        try:
            created_at = float(metadata['backup_created_at'])
            _finite(created_at, 'バックアップ作成日時')
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError('バックアップ作成日時が不正です') from error
        identity = _identity(identity)
        accounts = {}

        def account(value):
            value = _identity(value)
            return accounts.setdefault(value, {'identity': value,
                                               'counts': dict.fromkeys(STATES, 0),
                                               'status_updated_at': None})

        account(identity)
        total = dict.fromkeys(STATES, 0)
        for row in connection.execute('SELECT * FROM events'):
            owner = account(row['identity'])
            state = row['state']
            if state not in STATES:
                raise ValueError('保存された配送状態が不正です')
            if not isinstance(row['key'], str) or not row['key']:
                raise ValueError('保存された通報キーが不正です')
            if not isinstance(row['attempts'], int) or row['attempts'] < 0:
                raise ValueError('保存された失敗回数が不正です')
            if not isinstance(row['last_error'], str):
                raise ValueError('保存された失敗理由が不正です')
            for name in ('created_at', 'last_seen', 'next_attempt_at'):
                _finite(row[name], '配送日時')
            if state == 'sent':
                _finite(row['sent_at'], '送信日時')
            elif row['sent_at'] is not None:
                raise ValueError('未送信の記録に送信日時があります')
            try:
                event = Event.from_json(row['event_json'])
                valid = (event.verify() and event.id().to_hex() == row['event_id']
                         and event.author().to_hex() == row['identity'])
            except Exception as error:
                raise ValueError('保存された署名イベントを読み取れません') from error
            if not valid:
                raise ValueError('保存された署名イベントの署名・ID・投稿アカウントが一致しません')
            owner['counts'][state] += 1
            total[state] += 1

        updated_at = None
        for row in connection.execute('SELECT identity, name, value, updated_at FROM status'):
            owner = account(row['identity'])
            if not isinstance(row['name'], str) or not row['name']:
                raise ValueError('保存された状態項目が不正です')
            try:
                json.loads(row['value'], parse_constant=_reject_json_constant)
            except (TypeError, ValueError, RecursionError) as error:
                raise ValueError('保存された状態JSONが不正です') from error
            timestamp = _finite(row['updated_at'], '状態更新日時')
            previous = owner['status_updated_at']
            owner['status_updated_at'] = timestamp if previous is None else max(previous, timestamp)
            updated_at = timestamp if updated_at is None else max(updated_at, timestamp)

        return {'path': str(path), 'schema_version': SCHEMA_VERSION,
                'backup_created_at': created_at, 'status_updated_at': updated_at,
                'identity': identity, 'counts': total,
                'accounts': [accounts[key] for key in sorted(accounts)],
                'events_verified': sum(total.values())}


def create_backup(source_path, destination_path):
    """Create a verified archive at a new path without overwriting any file."""
    destination = Path(destination_path).absolute()
    _check_destination(destination)
    source_file = Path(source_path).resolve()
    if destination.resolve() in (source_file, *_sidecars(source_file)):
        raise ValueError('元DBまたはその付随ファイルをバックアップ先には指定できません')
    temporary = None
    try:
        deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise TimeoutError('バックアップの待機時間が上限を超えました')

        # Opening and reading this transaction fixes the source snapshot. WAL
        # writers remain free to commit; the source transaction ends immediately
        # after copying, before validation or fsync of the resulting archive.
        with _read_connection(source_path, None) as (source, _):
            descriptor, name = tempfile.mkstemp(prefix=f'.{destination.name}.tmp-', dir=destination.parent)
            os.close(descriptor)
            temporary = Path(name)
            with closing(sqlite3.connect(temporary, timeout=5)) as target:
                source.backup(target, pages=256, progress=progress, sleep=0.05)
        with closing(sqlite3.connect(temporary, timeout=5)) as target:
            if target.execute('PRAGMA journal_mode = DELETE').fetchone()[0] != 'delete':
                raise RuntimeError('バックアップを単独ファイルに変換できません')
            target.execute('PRAGMA synchronous = FULL')
            with target:
                target.executemany('''
                    INSERT INTO metadata (name, value) VALUES (?, ?)
                    ON CONFLICT(name) DO UPDATE SET value = excluded.value
                ''', [('backup_format_version', BACKUP_FORMAT_VERSION),
                      ('backup_created_at', str(time.time()))])

        summary = verify_backup(temporary)
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        _check_destination(destination)
        # link() atomically refuses an existing name, including a raced symlink.
        # Both files are in the same directory, so this stays on one filesystem.
        os.link(temporary, destination)
        temporary.unlink()
        temporary = None
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return dict(summary, path=str(destination))
    finally:
        if temporary is not None:
            for item in [*_sidecars(temporary), temporary]:
                item.unlink(missing_ok=True)
