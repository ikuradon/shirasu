"""Durable delivery state for signed Nostr events.

Only the process owning the receiver should write this database.  An event is
committed before delivery and its signed JSON is never replaced by a duplicate
report, so a retry after a crash uses the same Nostr event ID.
"""

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time


SCHEMA_VERSION = 1


def _timestamp(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('A finite timestamp is required')
    return value


def _snapshot(connection, identity, now):
    counts = {'pending': 0, 'sent': 0, 'expired': 0}
    for row in connection.execute(
        'SELECT state, COUNT(*) AS total FROM events WHERE identity = ? GROUP BY state',
        (identity,),
    ):
        counts[row['state']] = row['total']
    pending = connection.execute(
        '''SELECT created_at, next_attempt_at
           FROM events WHERE identity = ? AND state = 'pending'
           ORDER BY created_at, rowid LIMIT 1''',
        (identity,),
    ).fetchone()
    status = {}
    updated = {}
    for row in connection.execute(
        'SELECT name, value, updated_at FROM status WHERE identity = ?', (identity,),
    ):
        status[row['name']] = json.loads(row['value'])
        updated[row['name']] = row['updated_at']
    oldest = pending['created_at'] if pending is not None else None
    return {
        'identity': identity,
        'counts': counts,
        'oldest_pending_at': oldest,
        'oldest_pending_age_seconds': None if oldest is None else max(0.0, now - oldest),
        'next_attempt_at': pending['next_attempt_at'] if pending is not None else None,
        'status': status,
        'status_updated_at': updated,
    }


@contextmanager
def _read_connection(path, identity):
    """Open one read-only snapshot shared by all queries in an inspection."""
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only = ON')
        # Keep counts and rows from the same snapshot while the daemon writes.
        connection.execute('BEGIN')
        if connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            raise RuntimeError('Unsupported outbox database version')
        if identity is None:
            row = connection.execute(
                "SELECT value FROM metadata WHERE name = 'current_identity'",
            ).fetchone()
            if row is None:
                raise RuntimeError('Outbox identity has not been initialized')
            identity = row['value']
        yield connection, identity
    finally:
        connection.close()


def read_status(path, now=None, identity=None):
    """Read saved state without creating a database or changing any records."""
    now = _timestamp(time.time() if now is None else now)
    with _read_connection(path, identity) as (connection, identity):
        return _snapshot(connection, identity, now)


def read_history(path, *, state=None, limit=20, identity=None):
    """Inspect stored delivery records without expiring, purging or retrying them.

    total is the filtered count before limiting. Newest received records appear
    first, with reverse insertion order breaking ties in receipt timestamps.
    """
    if state not in (None, 'pending', 'sent', 'expired'):
        raise ValueError('state must be pending, sent, expired, or None')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError('limit must be an integer between 1 and 1000')
    with _read_connection(path, identity) as (connection, identity):
        condition = 'identity = ?'
        parameters = [identity]
        if state is not None:
            condition += ' AND state = ?'
            parameters.append(state)
        total = connection.execute(
            f'SELECT COUNT(*) FROM events WHERE {condition}', parameters,
        ).fetchone()[0]
        rows = connection.execute(f'''
            SELECT key, event_id, event_json, state, attempts, created_at,
                   last_seen, next_attempt_at, sent_at, last_error
            FROM events WHERE {condition}
            ORDER BY created_at DESC, rowid DESC LIMIT ?
        ''', [*parameters, limit])
        return {'identity': identity, 'total': total, 'events': [dict(row) for row in rows]}


class Outbox:
    def __init__(self, path, identity, dedup_seconds=86400, max_age_seconds=900,
                 max_pending=1000):
        if not isinstance(identity, str) or not identity:
            raise ValueError('An identity is required')
        self.identity = identity
        self.dedup_seconds = _timestamp(dedup_seconds)
        self.max_age_seconds = _timestamp(max_age_seconds)
        if self.dedup_seconds <= 0 or self.max_age_seconds < 0:
            raise ValueError('dedup_seconds must be positive and max_age_seconds nonnegative')
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending <= 0:
            raise ValueError('max_pending must be a positive integer')
        self.max_pending = max_pending
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute('PRAGMA busy_timeout = 5000')
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise RuntimeError('Unsupported outbox database version')
            self.connection.execute('PRAGMA journal_mode = WAL')
            self.connection.execute('PRAGMA synchronous = FULL')
            with self.connection:
                self.connection.execute('BEGIN')
                self.connection.execute('''
                    CREATE TABLE IF NOT EXISTS events (
                        identity TEXT NOT NULL,
                        key TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'expired')),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        last_seen REAL NOT NULL,
                        next_attempt_at REAL NOT NULL,
                        sent_at REAL,
                        last_error TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (identity, key)
                    )
                ''')
                self.connection.execute('''
                    CREATE INDEX IF NOT EXISTS events_due
                    ON events (identity, state, next_attempt_at, created_at)
                ''')
                self.connection.execute('''
                    CREATE TABLE IF NOT EXISTS status (
                        identity TEXT NOT NULL,
                        name TEXT NOT NULL,
                        value TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (identity, name)
                    )
                ''')
                self.connection.execute('''
                    CREATE TABLE IF NOT EXISTS metadata (
                        name TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                ''')
                self.connection.execute('''
                    INSERT INTO metadata (name, value) VALUES ('current_identity', ?)
                    ON CONFLICT(name) DO UPDATE SET value = excluded.value
                ''', (identity,))
                self.connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        except BaseException:
            self.connection.close()
            raise

    def _maintain(self, now):
        expired = 0
        if self.max_age_seconds:
            expired = self.connection.execute('''
                UPDATE events SET state = 'expired'
                WHERE identity = ? AND state = 'pending' AND created_at <= ?
            ''', (self.identity, now - self.max_age_seconds)).rowcount
        purged = self.connection.execute('''
            DELETE FROM events
            WHERE identity = ? AND state != 'pending' AND last_seen <= ?
        ''', (self.identity, now - self.dedup_seconds)).rowcount
        return {'expired': expired, 'purged': purged}

    def maintain(self, now):
        """Expire old pending events and remove inactive deduplication records."""
        now = _timestamp(now)
        with self.connection:
            return self._maintain(now)

    def enqueue(self, key, event_json, event_id, now):
        """Save a signed event, or refresh a duplicate without replacing its ID."""
        now = _timestamp(now)
        if not all(isinstance(value, str) and value for value in (key, event_json, event_id)):
            raise ValueError('key, event_json and event_id must be nonempty strings')
        with self.connection:
            self._maintain(now)
            existing = self.connection.execute('''
                UPDATE events SET last_seen = MAX(last_seen, ?)
                WHERE identity = ? AND key = ?
            ''', (now, self.identity, key)).rowcount
            if existing:
                return 'duplicate'
            pending = self.connection.execute('''
                SELECT COUNT(*) FROM events WHERE identity = ? AND state = 'pending'
            ''', (self.identity,)).fetchone()[0]
            if pending >= self.max_pending:
                return 'full'
            self.connection.execute('''
                INSERT INTO events
                    (identity, key, event_json, event_id, state,
                     created_at, last_seen, next_attempt_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            ''', (self.identity, key, event_json, event_id, now, now, now))
            return 'queued'

    def seed_sent(self, key, event_json, event_id, created_at, now):
        """Import a published event without replacing locally queued delivery state."""
        now = _timestamp(now)
        created_at = _timestamp(created_at)
        if not all(isinstance(value, str) and value for value in (key, event_json, event_id)):
            raise ValueError('key, event_json and event_id must be nonempty strings')
        with self.connection:
            cursor = self.connection.execute('''
                INSERT OR IGNORE INTO events
                    (identity, key, event_json, event_id, state,
                     created_at, last_seen, next_attempt_at, sent_at)
                VALUES (?, ?, ?, ?, 'sent', ?, ?, ?, ?)
            ''', (self.identity, key, event_json, event_id, created_at, now,
                  created_at, created_at))
            return bool(cursor.rowcount)

    def next_due(self, now):
        """Return the FIFO head only when due, so updates never overtake warnings."""
        now = _timestamp(now)
        with self.connection:
            self._maintain(now)
            row = self.connection.execute('''
                SELECT * FROM events
                WHERE identity = ? AND state = 'pending'
                ORDER BY created_at, rowid LIMIT 1
            ''', (self.identity,)).fetchone()
            return dict(row) if row is not None and row['next_attempt_at'] <= now else None

    def mark_sent(self, key, now):
        """Record an ACK and keep its deduplication record for another window.

        ACKs arriving just after the retry deadline still count as successful.
        Refreshing retention also protects events sent after a long outage when
        the delivery deadline has explicitly been disabled.
        """
        now = _timestamp(now)
        with self.connection:
            self.connection.execute('''
                UPDATE events SET state = 'sent', sent_at = ?,
                                  last_seen = MAX(last_seen, ?), last_error = ''
                WHERE identity = ? AND key = ? AND state != 'sent'
            ''', (now, now, self.identity, key))

    def mark_failed(self, key, error, now, retry_delay):
        """Keep the signed event and schedule another attempt after a failure.

        attempts counts failed delivery attempts; acknowledging a delivery does
        not increment it.  It is never reset by receipt of a duplicate report.
        """
        now = _timestamp(now)
        retry_delay = _timestamp(retry_delay)
        if retry_delay < 0:
            raise ValueError('retry_delay must be nonnegative')
        with self.connection:
            self.connection.execute('''
                UPDATE events
                SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?,
                    state = CASE WHEN ? > 0 AND created_at <= ? THEN 'expired' ELSE state END
                WHERE identity = ? AND key = ? AND state = 'pending'
            ''', (str(error)[:1000], now + retry_delay, self.max_age_seconds,
                  now - self.max_age_seconds, self.identity, key))

    def set_status(self, name, value, now):
        now = _timestamp(now)
        if not isinstance(name, str) or not name:
            raise ValueError('A status name is required')
        value_json = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self.connection:
            self.connection.execute('''
                INSERT INTO status (identity, name, value, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(identity, name) DO UPDATE
                SET value = excluded.value, updated_at = excluded.updated_at
            ''', (self.identity, name, value_json, now))

    def snapshot(self, now):
        # Only this connection writes the database, so queries cannot interleave
        # with another writer in the daemon.  read_status uses a read transaction.
        return _snapshot(self.connection, self.identity, _timestamp(now))

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
