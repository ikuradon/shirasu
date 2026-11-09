"""Durable, bounded collection of the separate points in a typhoon report.

The collector shares the outbox transaction and backup. It does not publish or
render anything. Only a completed, already-signed summary enters the outbox.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

import azarashi
from azarashi.reports.dcr import Typhoon


SCHEMA_VERSION = '1'
MAX_BATCH_REPORTS = 32
JST = timezone(timedelta(hours=9), 'JST')
GROUP_STATES = ('active', 'superseded', 'cancelled')
REPORT_STATES = ('pending', 'done', 'superseded', 'expired')


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('A finite timestamp is required')
    return float(value)


def _aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('A timezone-aware report time is required')
    return value.astimezone(timezone.utc)


def report_identity(report):
    """Same identity as the original single-report outbox, without satellite ID."""
    prefix = f'{report.message_type}:{type(report).__name__}:'.encode()
    return hashlib.sha256(prefix + bytes(report.raw)).hexdigest()


def _group_key(storm, issued, information):
    context = [storm, issued.isoformat(), information]
    return hashlib.sha256(json.dumps(context, separators=(',', ':')).encode()).hexdigest()


def _metadata(report):
    if not isinstance(report, Typhoon):
        raise ValueError('Not a typhoon report')
    number = report.typhoon_number_raw
    classification = report.report_classification_no
    information = report.information_type_no
    if number not in range(1, 100) or classification not in (1, 2, 3, 7) or information not in (0, 1, 2):
        raise ValueError('Unknown typhoon metadata')
    issued = _aware(report.report_time)
    received = _aware(report.timestamp)
    if information != 2:
        target = _aware(report.reference_time)
        if report.reference_time_type_raw not in (1, 2, 3):
            raise ValueError('Unknown typhoon target time type')
        slot = f'{target.isoformat()}:{report.reference_time_type_raw}'
    else:
        slot = 'cancelled'
    # Issued time includes the year. The storm key deliberately spans Dec/Jan,
    # so a cancellation can suppress a still-buffered previous-year report.
    storm = f'{number}:{classification}'
    group = _group_key(storm, issued, information)
    return group, storm, issued.timestamp(), information, slot, round(received.timestamp() * 1000)


@dataclass(frozen=True)
class TyphoonBatch:
    key: str
    group_key: str
    report_keys: tuple[str, ...]
    entries: tuple[tuple[str, int], ...]
    reports: tuple
    update: bool
    created_at: float
    deadline: float


class TyphoonCollector:
    def __init__(self, outbox, wait_seconds=180, *, collect=True):
        self.outbox = outbox
        self.connection = outbox.connection
        self.identity = outbox.identity
        self.collect = bool(collect)
        self.wait_seconds = _finite(wait_seconds)
        if not 0 < self.wait_seconds <= 300:
            raise ValueError('Typhoon collection wait must be in (0, 300] seconds')
        if self.collect and outbox.max_age_seconds and self.wait_seconds >= outbox.max_age_seconds:
            raise ValueError('Typhoon collection wait must be shorter than the delivery retry age')
        with self.connection:
            version = self.connection.execute(
                "SELECT value FROM metadata WHERE name = 'typhoon_schema_version'",
            ).fetchone()
            if version is not None and version['value'] != SCHEMA_VERSION:
                raise RuntimeError('Unsupported typhoon collection schema version')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS typhoon_groups (
                    identity TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    storm_key TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    information_type INTEGER NOT NULL,
                    first_at REAL NOT NULL,
                    deadline REAL,
                    last_seen REAL NOT NULL,
                    batches INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL CHECK(state IN ('active','superseded','cancelled')),
                    PRIMARY KEY(identity, group_key)
                )
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS typhoon_reports (
                    identity TEXT NOT NULL,
                    report_key TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    slot_key TEXT NOT NULL,
                    nmea TEXT NOT NULL,
                    received_ms INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','done','superseded','expired')),
                    PRIMARY KEY(identity, report_key)
                )
            ''')
            self.connection.execute('''
                CREATE INDEX IF NOT EXISTS typhoon_reports_group
                ON typhoon_reports(identity, group_key, state)
            ''')
            self.connection.execute('''
                CREATE INDEX IF NOT EXISTS typhoon_groups_storm
                ON typhoon_groups(identity, storm_key, issued_at)
            ''')
            self.connection.execute('''
                INSERT OR IGNORE INTO metadata(name,value) VALUES ('typhoon_schema_version', ?)
            ''', (SCHEMA_VERSION,))

    def _maintain(self, now):
        if self.outbox.max_age_seconds:
            self.connection.execute('''
                UPDATE typhoon_reports SET state = 'expired'
                WHERE identity = ? AND state = 'pending' AND created_at <= ?
            ''', (self.identity, now - self.outbox.max_age_seconds))
        self.connection.execute('''
            UPDATE typhoon_groups SET deadline = NULL
            WHERE identity = ? AND deadline IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM typhoon_reports r WHERE r.identity = typhoon_groups.identity
                AND r.group_key = typhoon_groups.group_key AND r.state = 'pending'
            )
        ''', (self.identity,))
        self.connection.execute('''
            DELETE FROM typhoon_reports WHERE identity = ? AND state != 'pending' AND last_seen <= ?
        ''', (self.identity, now - self.outbox.dedup_seconds))
        self.connection.execute('''
            DELETE FROM typhoon_groups WHERE identity = ? AND last_seen <= ? AND NOT EXISTS (
                SELECT 1 FROM typhoon_reports r WHERE r.identity = typhoon_groups.identity
                AND r.group_key = typhoon_groups.group_key
            )
        ''', (self.identity, now - self.outbox.dedup_seconds))

    def maintain(self, now):
        with self.connection:
            self._maintain(_finite(now))

    def pending_count(self):
        return self.connection.execute('''
            SELECT COUNT(*) FROM typhoon_reports WHERE identity = ? AND state = 'pending'
        ''', (self.identity,)).fetchone()[0]

    def has_pending(self):
        return bool(self.pending_count())

    def _supersede(self, storm, issued, information):
        # A newer issuance supersedes older unpublished collections. At the
        # same issue time a correction supersedes the issue, and a cancellation
        # supersedes both. Stored/signed outbox events are never rewritten.
        self.connection.execute('''
            UPDATE typhoon_reports SET state = 'superseded'
            WHERE identity = ? AND state = 'pending' AND group_key IN (
                SELECT group_key FROM typhoon_groups WHERE identity = ? AND storm_key = ?
                AND (issued_at < ? OR (issued_at = ? AND information_type < ?))
            )
        ''', (self.identity, self.identity, storm, issued, issued, information))
        self.connection.execute('''
            UPDATE typhoon_groups SET state = 'superseded', deadline = NULL
            WHERE identity = ? AND storm_key = ? AND state = 'active'
            AND (issued_at < ? OR (issued_at = ? AND information_type < ?))
        ''', (self.identity, storm, issued, issued, information))

    def accept(self, report, now):
        """Buffer one point, or tell the caller to use immediate publication.

        The caller must promptly enqueue cancellations returned as ``immediate``.
        Unknown metadata/undefined times also use the original single report.
        """
        now = _finite(now)
        try:
            group, storm, issued, information, slot, received_ms = _metadata(report)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 'immediate'
        key = report_identity(report)
        with self.connection:
            self._maintain(now)
            self.outbox._maintain(now)
            if information == 2:
                # Apply even an already-posted cancellation when aggregation
                # was enabled later and now holds an older unfinished issue.
                self._supersede(storm, issued, information)
                self.connection.execute('''
                    INSERT INTO typhoon_groups
                        (identity,group_key,storm_key,issued_at,information_type,first_at,deadline,last_seen,state)
                    VALUES (?,?,?,?,?,?,NULL,?,'cancelled')
                    ON CONFLICT(identity,group_key) DO UPDATE SET last_seen = MAX(last_seen,excluded.last_seen)
                ''', (self.identity, group, storm, issued, information, now, now))
            # Enabling image summaries must not republish already delivered
            # single reports. This refresh is the original outbox dedup rule.
            if self.connection.execute('''
                UPDATE events SET last_seen = MAX(last_seen, ?)
                WHERE identity = ? AND key = ?
            ''', (now, self.identity, key)).rowcount:
                return 'duplicate'
            existing = self.connection.execute('''
                SELECT group_key FROM typhoon_reports WHERE identity = ? AND report_key = ?
            ''', (self.identity, key)).fetchone()
            if existing:
                self.connection.execute('''
                    UPDATE typhoon_reports SET last_seen = MAX(last_seen, ?)
                    WHERE identity = ? AND report_key = ?
                ''', (now, self.identity, key))
                self.connection.execute('''
                    UPDATE typhoon_groups SET last_seen = MAX(last_seen, ?)
                    WHERE identity = ? AND group_key = ?
                ''', (now, self.identity, existing['group_key']))
                return 'duplicate'

            known = self.connection.execute('''
                SELECT * FROM typhoon_groups WHERE identity = ? AND group_key = ?
            ''', (self.identity, group)).fetchone()
            if information == 2:
                return 'immediate'

            # Reject older packets arriving after a newer issue/correction or a
            # cancellation, including raw packets not seen before that change.
            obsolete = self.connection.execute('''
                SELECT 1 FROM typhoon_groups WHERE identity = ? AND storm_key = ?
                AND (issued_at > ? OR (issued_at = ? AND information_type > ?)) LIMIT 1
            ''', (self.identity, storm, issued, issued, information)).fetchone()
            if obsolete or (known is not None and known['state'] != 'active'):
                if known is not None:
                    self.connection.execute('''
                        UPDATE typhoon_groups SET last_seen = MAX(last_seen, ?) WHERE identity = ? AND group_key = ?
                    ''', (now, self.identity, group))
                return 'duplicate'

            if not self.collect:
                self._supersede(storm, issued, information)
                return 'immediate'

            replacements = self.connection.execute('''
                SELECT COUNT(*) FROM typhoon_reports
                WHERE identity = ? AND group_key = ? AND slot_key = ? AND state = 'pending'
            ''', (self.identity, group, slot)).fetchone()[0]
            group_count = self.connection.execute('''
                SELECT COUNT(*) FROM typhoon_reports
                WHERE identity = ? AND group_key = ? AND state IN ('pending','done')
            ''', (self.identity, group)).fetchone()[0]
            slot_count = self.connection.execute('''
                SELECT COUNT(*) FROM typhoon_reports
                WHERE identity = ? AND group_key = ? AND slot_key = ? AND state IN ('pending','done')
            ''', (self.identity, group, slot)).fetchone()[0]
            queued = self.connection.execute('''
                SELECT COUNT(*) FROM events WHERE identity = ? AND state = 'pending'
            ''', (self.identity,)).fetchone()[0]
            superseded = self.connection.execute('''
                SELECT COUNT(*) FROM typhoon_reports r JOIN typhoon_groups g
                ON r.identity = g.identity AND r.group_key = g.group_key
                WHERE r.identity = ? AND r.state = 'pending' AND g.storm_key = ?
                AND (g.issued_at < ? OR (g.issued_at = ? AND g.information_type < ?))
            ''', (self.identity, storm, issued, issued, information)).fetchone()[0]
            if (group_count - slot_count >= MAX_BATCH_REPORTS
                    or queued + self.pending_count() - replacements - superseded >= self.outbox.max_pending):
                return 'full'

            self._supersede(storm, issued, information)
            self.connection.execute('''
                INSERT INTO typhoon_groups
                    (identity,group_key,storm_key,issued_at,information_type,first_at,deadline,last_seen,state)
                VALUES (?,?,?,?,?,?,?,?,'active')
                ON CONFLICT(identity,group_key) DO UPDATE SET
                    deadline = COALESCE(deadline,excluded.deadline),
                    last_seen = MAX(last_seen,excluded.last_seen)
            ''', (self.identity, group, storm, issued, information, now, now + self.wait_seconds, now))
            self.connection.execute('''
                UPDATE typhoon_reports SET state = 'superseded'
                WHERE identity = ? AND group_key = ? AND slot_key = ? AND state IN ('pending','done')
            ''', (self.identity, group, slot))
            self.connection.execute('''
                INSERT INTO typhoon_reports
                    (identity,report_key,group_key,slot_key,nmea,received_ms,created_at,last_seen,state)
                VALUES (?,?,?,?,?,?,?,?,'pending')
            ''', (self.identity, key, group, slot, report.nmea, received_ms, now, now))
            return 'buffered'

    def due(self, now):
        now = _finite(now)
        with self.connection:
            self._maintain(now)
            group = self.connection.execute('''
                SELECT * FROM typhoon_groups WHERE identity = ? AND state = 'active'
                AND deadline <= ? ORDER BY deadline,group_key LIMIT 1
            ''', (self.identity, now)).fetchone()
            if group is None:
                return None
            rows = self.connection.execute('''
                SELECT * FROM typhoon_reports
                WHERE identity = ? AND group_key = ? AND state IN ('pending','done')
                ORDER BY slot_key,report_key
            ''', (self.identity, group['group_key'])).fetchall()
            pending = [row for row in rows if row['state'] == 'pending']
            if not pending:
                raise RuntimeError('Typhoon collection deadline has no pending reports')
            entries = tuple((row['nmea'], row['received_ms']) for row in rows)
            reports = tuple(azarashi.decode(nmea, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
                            for nmea, ms in entries)
            keys = tuple(row['report_key'] for row in pending)
            key = hashlib.sha256(('typhoon-summary:' + group['group_key'] + ':'
                                  + ':'.join(sorted(row['report_key'] for row in rows))).encode()).hexdigest()
            return TyphoonBatch(key, group['group_key'], keys, entries, reports,
                                bool(group['batches']), min(row['created_at'] for row in pending), group['deadline'])

    def complete(self, batch, event_json, event_id, now):
        """Commit the signed summary and close its raw points in one transaction."""
        now = _finite(now)
        if not all(isinstance(v, str) and v for v in (event_json, event_id)):
            raise ValueError('A signed event and its ID are required')
        with self.connection:
            self._maintain(now)
            self.outbox._maintain(now)
            members = self.connection.execute('''
                SELECT report_key FROM typhoon_reports WHERE identity = ? AND group_key = ? AND state = 'pending'
            ''', (self.identity, batch.group_key)).fetchall()
            if {row['report_key'] for row in members} != set(batch.report_keys):
                return 'stale'
            existing = self.connection.execute('''
                SELECT 1 FROM events WHERE identity = ? AND key = ?
            ''', (self.identity, batch.key)).fetchone()
            queued = self.connection.execute('''
                SELECT COUNT(*) FROM events WHERE identity = ? AND state = 'pending'
            ''', (self.identity,)).fetchone()[0]
            if not existing and queued + self.pending_count() - len(members) >= self.outbox.max_pending:
                return 'full'
            if not existing:
                self.connection.execute('''
                    INSERT INTO events(identity,key,event_json,event_id,state,created_at,last_seen,next_attempt_at)
                    VALUES (?,?,?,?,'pending',?,?,?)
                ''', (self.identity, batch.key, event_json, event_id, now, now, now))
            self.connection.execute('''
                UPDATE typhoon_reports SET state = 'done'
                WHERE identity = ? AND group_key = ? AND state = 'pending'
            ''', (self.identity, batch.group_key))
            self.connection.execute('''
                UPDATE typhoon_groups SET batches = batches + 1, deadline = NULL
                WHERE identity = ? AND group_key = ?
            ''', (self.identity, batch.group_key))
            return 'duplicate' if existing else 'queued'


def typhoon_summary(reports, update=False):
    """Human text remains useful without the image; every source NMEA is kept."""
    reports = tuple(reports)
    if not reports:
        raise ValueError('At least one typhoon point is required')
    metadata = [_metadata(report) for report in reports]
    if len({item[0] for item in metadata}) != 1 or any(item[3] == 2 for item in metadata):
        raise ValueError('A summary requires one non-cancelled typhoon issuance')
    first = reports[0]
    markers = ('【訓練・試験】' if first.report_classification_no == 7 else '')
    markers += ('【訂正】' if first.information_type_no == 1 else '')
    markers += ('【追加更新】' if update else '')
    issued = first.report_time.astimezone(JST)
    lines = [f'{markers}【台風{first.typhoon_number}】{first.information_type}（{first.report_classification}）',
             f'発表時刻：{issued:%Y/%m/%d %H:%M} JST', f'受信した{len(reports)}時点の実況・予報']
    ordered = sorted(reports, key=lambda r: (r.reference_time, r.reference_time_type_raw))
    for report in ordered:
        at = report.reference_time.astimezone(JST)
        lines.extend(('', f'■ {at:%m/%d %H:%M} JST {report.reference_time_type}（{report.elapsed_time_from_reference_time}）',
                      f'中心気圧 {report.central_pressure} / 最大風速 {report.maximum_wind_speed} / 最大瞬間風速 {report.maximum_gust_wind_speed}',
                      f'大きさ：{report.typhoon_scale_category} / 強さ：{report.typhoon_intensity_category}',
                      report.coordinates_of_typhoon))
    lines.append('---')
    lines.extend(f'NMEA: {report.nmea}' for report in ordered)
    return '\n'.join(lines)


def validate_storage(connection):
    """Validate optional collection state during an existing outbox backup."""
    version = connection.execute("SELECT value FROM metadata WHERE name = 'typhoon_schema_version'").fetchone()
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('typhoon_groups','typhoon_reports')")}
    if version is None and not tables:
        return
    if version is None or version[0] != SCHEMA_VERSION or tables != {'typhoon_groups', 'typhoon_reports'}:
        raise ValueError('Invalid typhoon collection schema')
    required = {
        'typhoon_groups': {'identity','group_key','storm_key','issued_at','information_type','first_at','deadline','last_seen','batches','state'},
        'typhoon_reports': {'identity','report_key','group_key','slot_key','nmea','received_ms','created_at','last_seen','state'},
    }
    groups = {}
    for name, columns in required.items():
        schema = list(connection.execute(f'PRAGMA table_info({name})'))
        if not columns.issubset({row['name'] for row in schema}):
            raise ValueError('Missing typhoon collection columns')
        primary = tuple(row['name'] for row in sorted(schema, key=lambda row: row['pk']) if row['pk'])
        if primary != ('identity', 'group_key' if name == 'typhoon_groups' else 'report_key'):
            raise ValueError('Invalid typhoon collection primary key')
    for row in connection.execute('SELECT * FROM typhoon_groups'):
        if (not isinstance(row['identity'], str) or not row['identity'] or row['state'] not in GROUP_STATES
                or row['information_type'] not in (0,1,2) or not isinstance(row['batches'],int) or row['batches'] < 0):
            raise ValueError('Invalid typhoon group')
        for name in ('issued_at', 'first_at', 'last_seen'):
            _finite(row[name])
        if not isinstance(row['storm_key'], str) or not re.fullmatch(r'[1-9][0-9]?:[1237]', row['storm_key']):
            raise ValueError('Invalid typhoon storm identity')
        try:
            issued = datetime.fromtimestamp(row['issued_at'], timezone.utc)
        except (ValueError, OverflowError, OSError) as error:
            raise ValueError('Invalid typhoon issue time') from error
        if row['group_key'] != _group_key(row['storm_key'], issued, row['information_type']):
            raise ValueError('Invalid typhoon group identity')
        if row['deadline'] is not None:
            _finite(row['deadline'])
        groups[(row['identity'], row['group_key'])] = row
    pending_groups = set()
    active_slots = set()
    active_counts = {}
    for row in connection.execute('SELECT * FROM typhoon_reports'):
        group = groups.get((row['identity'], row['group_key']))
        if group is None or row['state'] not in REPORT_STATES or not isinstance(row['received_ms'], int):
            raise ValueError('Invalid stored typhoon report')
        for name in ('created_at', 'last_seen'):
            _finite(row[name])
        try:
            report = azarashi.decode(row['nmea'], timestamp=datetime.fromtimestamp(row['received_ms'] / 1000, timezone.utc))
            derived, storm, issued, information, slot, _ = _metadata(report)
        except Exception as error:
            raise ValueError('Cannot decode stored typhoon report') from error
        if (derived != row['group_key'] or storm != group['storm_key'] or issued != group['issued_at']
                or information != group['information_type'] or slot != row['slot_key']
                or report_identity(report) != row['report_key']):
            raise ValueError('Stored typhoon report does not match its identity or group')
        if row['state'] in ('pending', 'done'):
            group_id = (row['identity'], row['group_key'])
            slot_id = (row['identity'], row['group_key'], row['slot_key'])
            active_counts[group_id] = active_counts.get(group_id, 0) + 1
            if slot_id in active_slots or active_counts[group_id] > MAX_BATCH_REPORTS:
                raise ValueError('Invalid typhoon collection slots')
            active_slots.add(slot_id)
        if row['state'] == 'pending':
            pending_groups.add((row['identity'], row['group_key']))
            if group['state'] != 'active' or group['deadline'] is None:
                raise ValueError('Invalid pending typhoon collection')
    for key, row in groups.items():
        if row['deadline'] is not None and key not in pending_groups:
            raise ValueError('Typhoon deadline has no pending reports')
