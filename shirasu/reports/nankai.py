"""Durable, complete-page Nankai text collection sharing the delivery database.

The decoder's process-wide assembly is deliberately never read or replayed.
Each received page is saved before acknowledgement; only complete UTF-8 text,
or an explicit metadata-only notice, becomes a signed outbox event.
"""

from calendar import isleap
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from types import SimpleNamespace

from azarashi.definitions.qzss.dcr.information_serial_code import information_serial_code
from azarashi.definitions.qzss.dcr.information_type import information_type
from azarashi.definitions.qzss.dcr.report_classification import report_classification
from azarashi.reports.dcr import NankaiTroughEarthquake

from shirasu.images.protocol import _nmea
from shirasu.reports.format import JST, _header, post_message
from shirasu.reports.typhoon import report_identity


SCHEMA_VERSION = '1'
GROUP_STATES = ('active', 'done', 'superseded', 'cancelled', 'expired')
REPORT_STATES = ('pending', 'done', 'superseded', 'expired')


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('A finite timestamp is required')
    return float(value)


def _group_key(issued, classification, information, serial, total):
    fields = [issued.isoformat(), classification, information, serial, total]
    return hashlib.sha256(json.dumps(fields, separators=(',', ':')).encode()).hexdigest()


def _metadata(report):
    if not isinstance(report, NankaiTroughEarthquake):
        raise ValueError('Not a Nankai report')
    classification, information = report.report_classification_no, report.information_type_no
    serial, page, total = report.information_serial_code_raw, report.page_number, report.total_page
    if (classification not in (1, 2, 3, 7) or information not in (0, 1, 2)
            or serial not in (1, 2, 3, 4, 5, 6, 15)
            or not 1 <= page <= total <= 63
            or not isinstance(report.text_information, bytes) or len(report.text_information) != 18):
        raise ValueError('Unknown Nankai metadata or page position')
    for value in (report.report_time, report.timestamp):
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError('A timezone-aware report time is required')
    issued = report.report_time.astimezone(timezone.utc)
    return (_group_key(issued, classification, information, serial, total),
            issued.timestamp(), classification, information, serial, page, total,
            round(report.timestamp.timestamp() * 1000))


@dataclass(frozen=True)
class NankaiBatch:
    key: str
    group_key: str
    generation: int
    report_keys: tuple[str, ...]
    entries: tuple[tuple[str, int], ...]
    pages: tuple[int, ...]
    body: bytes
    issued_at: float
    classification: int
    information: int
    serial: int
    total: int
    mode: str
    update: bool
    created_at: float


class NankaiCollector:
    def __init__(self, outbox, wait_seconds=180):
        self.outbox, self.connection, self.identity = outbox, outbox.connection, outbox.identity
        self.wait_seconds = _finite(wait_seconds)
        if not 0 < self.wait_seconds <= 300:
            raise ValueError('Nankai collection wait must be in (0, 300] seconds')
        if outbox.max_age_seconds and self.wait_seconds >= outbox.max_age_seconds:
            raise ValueError('Nankai collection wait must be shorter than the delivery retry age')
        with self.connection:
            version = self.connection.execute(
                "SELECT value FROM metadata WHERE name = 'nankai_schema_version'").fetchone()
            if version is not None and version['value'] != SCHEMA_VERSION:
                raise RuntimeError('Unsupported Nankai collection schema version')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS nankai_groups (
                    identity TEXT NOT NULL, group_key TEXT NOT NULL,
                    issued_at REAL NOT NULL, classification INTEGER NOT NULL,
                    information_type INTEGER NOT NULL, serial INTEGER NOT NULL,
                    total INTEGER NOT NULL, first_at REAL NOT NULL,
                    deadline REAL, last_seen REAL NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    notified INTEGER NOT NULL DEFAULT 0,
                    batches INTEGER NOT NULL DEFAULT 0,
                    legacy INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL CHECK(state IN ('active','done','superseded','cancelled','expired')),
                    PRIMARY KEY(identity, group_key)
                )
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS nankai_reports (
                    identity TEXT NOT NULL, report_key TEXT NOT NULL,
                    group_key TEXT NOT NULL, generation INTEGER NOT NULL,
                    page INTEGER NOT NULL, text BLOB NOT NULL,
                    nmea TEXT NOT NULL, received_ms INTEGER NOT NULL,
                    created_at REAL NOT NULL, last_seen REAL NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','done','superseded','expired')),
                    PRIMARY KEY(identity, report_key)
                )
            ''')
            self.connection.execute('''CREATE INDEX IF NOT EXISTS nankai_reports_group
                ON nankai_reports(identity,group_key,generation,state)''')
            self.connection.execute('''CREATE INDEX IF NOT EXISTS nankai_groups_issued
                ON nankai_groups(identity,classification,issued_at)''')
            self.connection.execute("INSERT OR IGNORE INTO metadata(name,value) VALUES ('nankai_schema_version', ?)",
                                    (SCHEMA_VERSION,))

    def _maintain(self, now):
        if self.outbox.max_age_seconds:
            self.connection.execute('''UPDATE nankai_reports SET state='expired'
                WHERE identity=? AND state IN ('pending','done') AND group_key IN (
                    SELECT group_key FROM nankai_groups WHERE identity=? AND state='active' AND first_at<=?)''',
                (self.identity, self.identity, now - self.outbox.max_age_seconds))
            self.connection.execute('''UPDATE nankai_groups SET state='expired', deadline=NULL
                WHERE identity=? AND state='active' AND first_at<=?''',
                (self.identity, now - self.outbox.max_age_seconds))
        # Retain pages together while the announcement is active or refreshed.
        # Independently purging one page of a completed group would allow its
        # retransmission to create a pending page under an already-done group.
        self.connection.execute('''DELETE FROM nankai_reports WHERE identity=? AND state!='pending'
            AND last_seen<=? AND NOT EXISTS (SELECT 1 FROM nankai_groups g
                WHERE g.identity=nankai_reports.identity AND g.group_key=nankai_reports.group_key
                AND (g.state='active' OR g.last_seen>?))''',
            (self.identity, now - self.outbox.dedup_seconds, now - self.outbox.dedup_seconds))
        self.connection.execute('''DELETE FROM nankai_groups WHERE identity=? AND last_seen<=?
            AND NOT EXISTS (SELECT 1 FROM nankai_reports r WHERE r.identity=nankai_groups.identity
                            AND r.group_key=nankai_groups.group_key)''',
            (self.identity, now - self.outbox.dedup_seconds))

    def maintain(self, now):
        with self.connection:
            self._maintain(_finite(now))

    def pending_count(self):
        return self.connection.execute("SELECT COUNT(*) FROM nankai_reports WHERE identity=? AND state='pending'",
                                       (self.identity,)).fetchone()[0]

    def has_pending(self):
        return bool(self.pending_count())

    def waiting_count(self):
        return self.connection.execute('''SELECT COUNT(*) FROM nankai_reports r JOIN nankai_groups g
            ON r.identity=g.identity AND r.group_key=g.group_key AND r.generation=g.generation
            WHERE r.identity=? AND g.state='active' AND r.state IN ('pending','done')''',
            (self.identity,)).fetchone()[0]

    def _supersede(self, classification, issued, information):
        parameters = (self.identity, self.identity, classification, issued, issued, information)
        self.connection.execute('''UPDATE nankai_reports SET state='superseded'
            WHERE identity=? AND state IN ('pending','done') AND group_key IN (
                SELECT group_key FROM nankai_groups WHERE identity=? AND classification=? AND state='active'
                AND (issued_at<? OR (issued_at=? AND information_type<?)))''', parameters)
        self.connection.execute('''UPDATE nankai_groups SET state='superseded',deadline=NULL
            WHERE identity=? AND classification=? AND state='active'
            AND (issued_at<? OR (issued_at=? AND information_type<?))''',
            (self.identity, classification, issued, issued, information))

    def accept(self, report, now):
        now = _finite(now)
        try:
            group, issued, classification, information, serial, page, total, received_ms = _metadata(report)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 'immediate'
        key = report_identity(report)
        with self.connection:
            self._maintain(now)
            self.outbox._maintain(now)
            known = self.connection.execute('SELECT * FROM nankai_groups WHERE identity=? AND group_key=?',
                                            (self.identity, group)).fetchone()
            # Never turn a previously signed page into a second publication.
            # Mark the entire issuance as legacy, even if its other pages were
            # buffered before the previously posted one arrived.
            if self.connection.execute('UPDATE events SET last_seen=MAX(last_seen,?) WHERE identity=? AND key=?',
                                       (now, self.identity, key)).rowcount:
                self._supersede(classification, issued, information)
                self._store_group(group, issued, classification, information, serial, total, now)
                self.connection.execute("UPDATE nankai_groups SET legacy=1,state='done',deadline=NULL WHERE identity=? AND group_key=?",
                                        (self.identity, group))
                self.connection.execute("UPDATE nankai_reports SET state='done' WHERE identity=? AND group_key=? AND state='pending'",
                                        (self.identity, group))
                return 'duplicate'
            if known is not None and (known['legacy'] or known['state'] in ('superseded','cancelled','expired')):
                self.connection.execute('UPDATE nankai_groups SET last_seen=MAX(last_seen,?) WHERE identity=? AND group_key=?',
                                        (now, self.identity, group))
                return 'duplicate'
            obsolete = self.connection.execute('''SELECT 1 FROM nankai_groups
                WHERE identity=? AND classification=?
                AND (issued_at>? OR (issued_at=? AND information_type>?)) LIMIT 1''',
                (self.identity, classification, issued, issued, information)).fetchone()
            if obsolete:
                return 'duplicate'
            existing = self.connection.execute('SELECT * FROM nankai_reports WHERE identity=? AND report_key=?',
                                               (self.identity, key)).fetchone()
            if (existing is not None and known is not None
                    and existing['group_key'] == group
                    and existing['generation'] == known['generation'] and existing['state'] in ('pending','done')):
                self.connection.execute('UPDATE nankai_reports SET last_seen=MAX(last_seen,?) WHERE identity=? AND report_key=?',
                                        (now, self.identity, key))
                self.connection.execute('UPDATE nankai_groups SET last_seen=MAX(last_seen,?) WHERE identity=? AND group_key=?',
                                        (now, self.identity, group))
                return 'duplicate'
            generation = known['generation'] if known is not None else 0
            conflict = self.connection.execute('''SELECT 1 FROM nankai_reports WHERE identity=? AND group_key=?
                AND generation=? AND page=? AND state IN ('pending','done')''',
                (self.identity, group, generation, page)).fetchone()
            released = self.connection.execute('''SELECT COUNT(*) FROM nankai_reports r JOIN nankai_groups g
                ON r.identity=g.identity AND r.group_key=g.group_key
                WHERE r.identity=? AND r.state='pending' AND
                ((g.classification=? AND g.state='active' AND (g.issued_at<? OR
                (g.issued_at=? AND g.information_type<?))) OR (g.group_key=? AND ?))''',
                (self.identity, classification, issued, issued, information, group, bool(conflict))).fetchone()[0]
            if self.outbox.pending_count() - released >= self.outbox.max_pending:
                return 'full'
            self._supersede(classification, issued, information)
            self._store_group(group, issued, classification, information, serial, total, now)
            if conflict:
                generation += 1
                self.connection.execute("UPDATE nankai_reports SET state='superseded' WHERE identity=? AND group_key=? AND state IN ('pending','done')",
                                        (self.identity, group))
                # A conflicting page starts a fresh receive cycle. Keep prior
                # signed batches, but give the new generation its own missing-
                # page notification and fixed lifetime, even after completion.
                self.connection.execute('''UPDATE nankai_groups SET generation=?,state='active',
                    first_at=?,deadline=?,notified=0 WHERE identity=? AND group_key=?''',
                    (generation, now, now if information == 2 else now + self.wait_seconds,
                     self.identity, group))
            self.connection.execute('''INSERT INTO nankai_reports
                (identity,report_key,group_key,generation,page,text,nmea,received_ms,created_at,last_seen,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,'pending') ON CONFLICT(identity,report_key) DO UPDATE SET
                group_key=excluded.group_key,generation=excluded.generation,page=excluded.page,
                text=excluded.text,nmea=excluded.nmea,received_ms=excluded.received_ms,
                created_at=excluded.created_at,last_seen=excluded.last_seen,state='pending'
            ''', (self.identity, key, group, generation, page, report.text_information,
                  report.nmea, received_ms, now, now))
            return 'buffered'

    def _store_group(self, key, issued, classification, information, serial, total, now):
        self.connection.execute('''INSERT INTO nankai_groups
            (identity,group_key,issued_at,classification,information_type,serial,total,first_at,deadline,last_seen,state)
            VALUES (?,?,?,?,?,?,?,?,?,?,'active') ON CONFLICT(identity,group_key) DO UPDATE SET
            last_seen=MAX(last_seen,excluded.last_seen)''',
            (self.identity, key, issued, classification, information, serial, total, now,
             now if information == 2 else now + self.wait_seconds, now))

    def due(self, now):
        now = _finite(now)
        with self.connection:
            self._maintain(now)
            groups = self.connection.execute('''SELECT g.* FROM nankai_groups g WHERE identity=? AND state='active'
                AND (information_type=2 OR (notified=0 AND deadline<=?) OR total=(
                    SELECT COUNT(*) FROM nankai_reports r WHERE r.identity=g.identity AND r.group_key=g.group_key
                    AND r.generation=g.generation AND r.state IN ('pending','done')))
                ORDER BY first_at,group_key''', (self.identity, now)).fetchall()
            for group in groups:
                rows = self.connection.execute('''SELECT * FROM nankai_reports WHERE identity=? AND group_key=?
                    AND generation=? AND state IN ('pending','done') ORDER BY page''',
                    (self.identity, group['group_key'], group['generation'])).fetchall()
                if not rows:
                    continue
                mode = ('cancelled' if group['information_type'] == 2 else
                        'complete' if len(rows) == group['total'] else 'missing')
                keys = tuple(row['report_key'] for row in rows)
                key = hashlib.sha256(('nankai:' + group['group_key'] + ':' + mode + ':' + ':'.join(keys)).encode()).hexdigest()
                return NankaiBatch(key, group['group_key'], group['generation'], keys,
                                   tuple((row['nmea'], row['received_ms']) for row in rows),
                                   tuple(row['page'] for row in rows), b''.join(row['text'] for row in rows),
                                   group['issued_at'], group['classification'], group['information_type'],
                                   group['serial'], group['total'], mode, bool(group['batches']), group['first_at'])
        return None

    def complete(self, batch, event_json, event_id, now):
        now = _finite(now)
        if not all(isinstance(value, str) and value for value in (event_json, event_id)):
            raise ValueError('A signed event and its ID are required')
        with self.connection:
            self._maintain(now)
            self.outbox._maintain(now)
            group = self.connection.execute('SELECT * FROM nankai_groups WHERE identity=? AND group_key=?',
                                            (self.identity, batch.group_key)).fetchone()
            if group is None or group['state'] != 'active' or group['generation'] != batch.generation:
                return 'stale'
            rows = self.connection.execute('''SELECT report_key,state FROM nankai_reports WHERE identity=? AND group_key=?
                AND generation=? AND state IN ('pending','done') ORDER BY page''',
                (self.identity, batch.group_key, batch.generation)).fetchall()
            if tuple(row['report_key'] for row in rows) != batch.report_keys or (batch.mode == 'missing' and group['notified']):
                return 'stale'
            existing = self.connection.execute('SELECT 1 FROM events WHERE identity=? AND key=?',
                                               (self.identity, batch.key)).fetchone()
            released = sum(row['state'] == 'pending' for row in rows)
            if not existing and self.outbox.pending_count() - released >= self.outbox.max_pending:
                return 'full'
            if not existing:
                self.connection.execute('''INSERT INTO events
                    (identity,key,event_json,event_id,state,created_at,last_seen,next_attempt_at)
                    VALUES (?,?,?,?,'pending',?,?,?)''',
                    (self.identity, batch.key, event_json, event_id, now, now, now))
            self.connection.execute("UPDATE nankai_reports SET state='done' WHERE identity=? AND group_key=? AND generation=? AND state='pending'",
                                    (self.identity, batch.group_key, batch.generation))
            self.connection.execute('''UPDATE nankai_groups SET deadline=NULL,notified=1,batches=batches+1,state=?
                WHERE identity=? AND group_key=?''',
                ('active' if batch.mode == 'missing' else 'cancelled' if batch.mode == 'cancelled' else 'done',
                 self.identity, batch.group_key))
            return 'duplicate' if existing else 'queued'


def nankai_summary(batch):
    """Publish only a complete text, keeping every original page for inspection."""
    metadata = SimpleNamespace(report_classification_no=batch.classification,
                               report_classification=report_classification[batch.classification],
                               information_type_no=batch.information,
                               information_type=information_type[batch.information])
    marker = '【本文未受信】' if batch.mode == 'missing' else '【追加更新】' if batch.update else ''
    at = datetime.fromtimestamp(batch.issued_at, timezone.utc).astimezone(JST)
    lines = [marker + _header(metadata, '南海トラフ地震関連情報'),
             f'情報種別：{information_serial_code[batch.serial]}',
             f'発表時刻：{at.month}月{at.day}日 {at:%H:%M} JST', '']
    if batch.mode == 'cancelled':
        lines.append('この情報は取り消されました。')
    elif batch.mode == 'missing':
        lines.append(f'本文を受信できていません（受信{len(batch.pages)}/{batch.total}ページ）。')
        missing = '、'.join(str(page) for page in range(1, batch.total + 1) if page not in batch.pages)
        lines.append('不足ページ：' + missing)
    elif batch.mode == 'complete' and batch.pages == tuple(range(1, batch.total + 1)):
        # NUL denotes no character. Decode after joining all page bytes, never
        # discard an incomplete multibyte character at an individual boundary.
        try:
            lines.append(batch.body.replace(b'\0', b'').decode('utf-8'))
        except UnicodeDecodeError:
            lines.append('全ページを受信しましたが、本文の文字コードを読み取れませんでした。')
    else:
        raise ValueError('A complete Nankai text requires every page')
    return post_message('\n'.join(lines), (nmea for nmea, _ in batch.entries))


def _stored_frame(sentence, received_ms):
    """Validate stored raw fields without instantiating the global assembler."""
    _nmea(sentence)
    data = bytes.fromhex(sentence.split(',')[2].split('*')[0] + '0')
    value = int.from_bytes(data, 'big')
    bits = lambda offset, width: (value >> (256 - offset - width)) & ((1 << width) - 1)
    crc = 0
    for bit in range(226):
        carry = (crc >> 23) ^ bits(bit, 1)
        crc = (crc << 1) & 0xffffff
        if carry:
            crc ^= 0x864cfb
    if (bits(0, 8) not in (0x53, 0x9a, 0xc6) or bits(8, 6) != 43
            or bits(17, 4) != 4 or bits(214, 6) != 1 or crc != bits(226, 24)):
        raise ValueError('Invalid stored Nankai frame')
    received = datetime.fromtimestamp(received_ms / 1000, timezone.utc)
    month, day, hour, minute = bits(21, 4), bits(25, 5), bits(30, 5), bits(35, 6)
    year = received.year + (1 if received.month - month > 6 else -1 if month - received.month > 6 else 0)
    if month == 2 and day == 29 and not isleap(year):
        candidates = [candidate for candidate in range(max(1, year - 8), min(9999, year + 8) + 1) if isleap(candidate)]
        year = min(candidates, key=lambda candidate: abs(datetime(candidate, 2, 29, hour, minute, tzinfo=timezone.utc) - received))
    issued = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    classification, information, serial, page, total = bits(14, 3), bits(41, 2), bits(53, 4), bits(201, 6), bits(207, 6)
    raw = data[1:27] + bytes((data[27] & 0xf0,))
    key = hashlib.sha256(b'DCR:NankaiTroughEarthquake:' + raw).hexdigest()
    return (_group_key(issued, classification, information, serial, total), key, issued.timestamp(),
            classification, information, serial, page, total,
            bytes(bits(57 + index * 8, 8) for index in range(18)))


def validate_storage(connection):
    """Validate optional persisted collections when creating or restoring backups."""
    version = connection.execute("SELECT value FROM metadata WHERE name='nankai_schema_version'").fetchone()
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('nankai_groups','nankai_reports')")}
    if version is None and not tables:
        return
    if version is None or version[0] != SCHEMA_VERSION or tables != {'nankai_groups','nankai_reports'}:
        raise ValueError('Invalid Nankai collection schema')
    columns = {
        'nankai_groups': {'identity','group_key','issued_at','classification','information_type','serial','total','first_at','deadline','last_seen','generation','notified','batches','legacy','state'},
        'nankai_reports': {'identity','report_key','group_key','generation','page','text','nmea','received_ms','created_at','last_seen','state'},
    }
    for table, required in columns.items():
        schema = list(connection.execute(f'PRAGMA table_info({table})'))
        primary = tuple(row['name'] for row in sorted(schema, key=lambda row: row['pk']) if row['pk'])
        if (not required.issubset({row['name'] for row in schema})
                or primary != ('identity', 'group_key' if table == 'nankai_groups' else 'report_key')):
            raise ValueError('Invalid Nankai collection columns or primary key')
    groups = {}
    for row in connection.execute('SELECT * FROM nankai_groups'):
        if (not isinstance(row['identity'], str) or not row['identity'] or row['state'] not in GROUP_STATES
                or row['classification'] not in (1,2,3,7) or row['information_type'] not in (0,1,2)
                or row['serial'] not in (1,2,3,4,5,6,15) or not 1 <= row['total'] <= 63
                or row['notified'] not in (0,1) or row['legacy'] not in (0,1)
                or any(not isinstance(row[name], int) or row[name] < 0 for name in ('generation','batches'))):
            raise ValueError('Invalid Nankai group')
        for name in ('issued_at','first_at','last_seen'):
            _finite(row[name])
        try:
            issued = datetime.fromtimestamp(row['issued_at'], timezone.utc)
        except (ValueError, OverflowError, OSError) as error:
            raise ValueError('Invalid Nankai issue time') from error
        if row['group_key'] != _group_key(issued,row['classification'],row['information_type'],row['serial'],row['total']):
            raise ValueError('Invalid Nankai group identity')
        if row['deadline'] is not None:
            _finite(row['deadline'])
        if ((row['state'] != 'active' and row['deadline'] is not None)
                or (row['state'] == 'active' and not row['notified'] and row['deadline'] is None)
                or (row['deadline'] is not None and row['deadline'] < row['first_at'])):
            raise ValueError('Invalid Nankai deadline')
        groups[(row['identity'],row['group_key'])] = row
    slots = set()
    counts = {}
    for row in connection.execute('SELECT * FROM nankai_reports'):
        group = groups.get((row['identity'],row['group_key']))
        if (group is None or row['state'] not in REPORT_STATES or not isinstance(row['received_ms'], int)
                or not isinstance(row['generation'], int) or not 0 <= row['generation'] <= group['generation']):
            raise ValueError('Invalid stored Nankai report')
        for name in ('created_at','last_seen'):
            _finite(row[name])
        try:
            derived = _stored_frame(row['nmea'], row['received_ms'])
        except Exception as error:
            raise ValueError('Cannot read stored Nankai report') from error
        expected = (row['group_key'],row['report_key'],group['issued_at'],group['classification'],
                    group['information_type'],group['serial'],row['page'],group['total'],row['text'])
        if derived != expected:
            raise ValueError('Stored Nankai report does not match its identity or group')
        if row['state'] in ('pending','done'):
            slot = (row['identity'],row['group_key'],row['page'])
            if slot in slots or row['generation'] != group['generation']:
                raise ValueError('Duplicate Nankai page slot or old generation')
            slots.add(slot)
            group_id = (row['identity'],row['group_key'])
            counts[group_id] = counts.get(group_id, 0) + 1
        if row['state'] == 'pending' and group['state'] != 'active':
            raise ValueError('Invalid pending Nankai collection')
    for key, group in groups.items():
        if group['state'] == 'active' and not counts.get(key):
            raise ValueError('Active Nankai collection has no current pages')
