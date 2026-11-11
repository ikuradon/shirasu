"""Check inspection commands in separate processes without production config."""

import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import azarashi
from nostr_sdk import EventBuilder, Keys, Kind

from outbox import Outbox, read_status


ROOT = Path(__file__).resolve().parents[1]
DCR_NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
MARINE_NMEA = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'


GUARDED_CLI = '''
import os
import sqlite3
from unittest.mock import patch
import main

def forbidden(*args, **kwargs):
    raise AssertionError('INSPECTION_SIDE_EFFECT: daemon, signing, network or database writes')

connect = sqlite3.connect
def read_only_connect(database, *args, **kwargs):
    if os.environ.get('FORBID_DATABASE') == '1':
        forbidden()
    if not kwargs.get('uri') or not str(database).endswith('?mode=ro'):
        forbidden()
    return connect(database, *args, **kwargs)

with patch.object(main, 'Nostr', side_effect=forbidden), \\
     patch.object(main, 'GpsdReceiver', side_effect=forbidden), \\
     patch.object(main, 'Outbox', side_effect=forbidden), \\
     patch.object(main.Keys, 'parse', side_effect=forbidden), \\
     patch.object(main.Keys, 'generate', side_effect=forbidden), \\
     patch.object(main.EventBuilder, 'finalize', side_effect=forbidden), \\
     patch.object(main, 'report_message', side_effect=forbidden), \\
     patch.object(sqlite3, 'connect', side_effect=read_only_connect), \\
     patch('socket.create_connection', side_effect=forbidden), \\
     patch('socket.socket.connect', side_effect=forbidden):
    raise SystemExit(main.main())
'''


class InspectionCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-cli-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ('main.py', 'outbox.py', 'receiver.py', 'inspection.py', 'message_format.py', 'dcx_instructions.py',
                     'image_publication.py', 'image_protocol.py', 'image_alt.py', 'typhoon.py', 'nankai.py'):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / 'config.py').write_text(
            "raise RuntimeError('INSPECTION_SIDE_EFFECT: config was imported')\n",
            encoding='utf-8',
        )
        (self.root / 'guarded_cli.py').write_text(GUARDED_CLI, encoding='utf-8')
        self.path = self.root / 'state.sqlite3'
        self.keys = Keys.generate()
        self.box = Outbox(self.path, self.keys.public_key().to_hex(), max_age_seconds=30)
        self.addCleanup(self.box.close)
        self.now = time.time()
        self.events = {}
        self.enqueue('expired', self.now - 2000)
        self.box.maintain(self.now - 1000)
        # Keep an overdue pending row: inspection must not expire it or resend it.
        self.box.max_age_seconds = 0
        self.enqueue('overdue', self.now - 1200)
        self.box.mark_failed('overdue', 'relay offline', self.now - 1199, 5)
        self.enqueue('sent', self.now - 600)
        self.box.mark_sent('sent', self.now - 599)
        self.enqueue('recent', self.now - 2)
        for name, value in {
            'running': True, 'gpsd_connected': True, 'relay_connected': True,
            'heartbeat': self.now, 'last_received': self.now - 2,
            'last_published': self.now - 599, 'last_delivery_error': 'relay offline',
        }.items():
            self.box.set_status(name, value, self.now)

    def enqueue(self, key, received_at):
        event = EventBuilder(Kind(1), f'テスト通報: {key}\n---\nNMEA: {DCR_NMEA}').finalize(self.keys)
        self.events[key] = event.id().to_hex()
        self.box.enqueue(key, event.as_json(), event.id().to_hex(), received_at)

    def run_cli(self, *arguments, path=None, forbid_database=False):
        before = list(self.box.connection.iterdump())
        snapshot_before = read_status(self.path, now=self.now)
        result = subprocess.run(
            [sys.executable, '-B', 'guarded_cli.py', *arguments], cwd=self.root,
            env={'PATH': os.environ.get('PATH', ''), 'PYTHONUNBUFFERED': '1',
                 'PYTHONDONTWRITEBYTECODE': '1',
                 'FORBID_DATABASE': '1' if forbid_database else '0',
                 'SHIRASU_STATE_PATH': str(self.path if path is None else path)},
            capture_output=True, encoding='utf-8', timeout=10,
        )
        self.assertEqual(list(self.box.connection.iterdump()), before)
        self.assertEqual(read_status(self.path, now=self.now), snapshot_before)
        self.assertFalse((self.root / 'state.sqlite3.lock').exists())
        self.assertNotIn('INSPECTION_SIDE_EFFECT', result.stdout + result.stderr)
        return result

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')

    def test_status_defaults_to_clean_json_and_preserves_saved_state(self):
        result = self.run_cli('--status')
        self.assert_success(result)
        status = json.loads(result.stdout)
        self.assertEqual(status['identity'], self.box.identity)
        self.assertEqual(status['counts'], {'pending': 2, 'sent': 1, 'expired': 1})
        self.assertEqual(status['status'], read_status(self.path)['status'])
        self.assertFalse(status['healthy'])
        self.assertGreaterEqual(status['oldest_pending_age_seconds'], 1200)
        self.assertIn('status_updated_at', status)

    def test_status_can_render_a_human_readable_snapshot(self):
        result = self.run_cli('--status', '--format', 'text')
        self.assert_success(result)
        for text in ('要確認', 'GPSD', 'Nostr', '最終受信', '秒前', '送信待ち', '期限切れ', 'relay offline'):
            self.assertIn(text, result.stdout)
        self.assertFalse(result.stdout.lstrip().startswith('{'))

    def test_history_defaults_to_a_table_of_saved_records(self):
        result = self.run_cli('--history')
        self.assert_success(result)
        for text in ('受信日時', '通報種別', '失敗回数', 'イベントID', '最終失敗理由', '送信待ち', '期限切れ'):
            self.assertIn(text, result.stdout)
        self.assertIn(self.events['overdue'][:12], result.stdout)
        self.assertIn('relay offline', result.stdout)
        self.assertNotIn(DCR_NMEA, result.stdout)

    def test_history_json_filters_and_limits_metadata_without_signed_payloads(self):
        result = self.run_cli('--history', '--format', 'json', '--state', 'pending', '--limit', '1')
        self.assert_success(result)
        history = json.loads(result.stdout)
        self.assertEqual(history['identity'], self.box.identity)
        self.assertEqual(history['total'], 2)
        self.assertEqual(len(history['events']), 1)
        record = history['events'][0]
        self.assertEqual(record['key'], 'recent')
        self.assertEqual(record['event_id'], self.events['recent'])
        self.assertEqual(record['state'], 'pending')
        self.assertTrue(record['report_type'])
        self.assertNotEqual(record['report_type'], '不明')
        self.assertNotIn('event_json', record)
        self.assertNotIn(DCR_NMEA, result.stdout)
        for state in ('sent', 'expired'):
            with self.subTest(state=state):
                result = self.run_cli('--history', '--format', 'json', '--state', state, '--limit', '1000')
                self.assert_success(result)
                history = json.loads(result.stdout)
                self.assertEqual(history['total'], 1)
                self.assertEqual([row['state'] for row in history['events']], [state])

    def test_health_remains_silent_and_uses_exit_status(self):
        result = self.run_cli('--health')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, '', ''))
        self.box.mark_sent('overdue', time.time())
        self.box.set_status('last_delivery_error', None, time.time())
        result = self.run_cli('--health')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_show_defaults_to_saved_body_and_full_delivery_details(self):
        result = self.run_cli('--show', self.events['overdue'])
        self.assert_success(result)
        received = datetime.fromtimestamp(self.now - 1200, timezone(timedelta(hours=9)))
        for text in (self.events['overdue'], '緊急地震速報', 'JST',
                     received.strftime('%Y-%m-%d %H:%M:%S'), '送信待ち', '未送信',
                     '失敗回数', 'relay offline', f'テスト通報: overdue\n---\nNMEA: {DCR_NMEA}'):
            self.assertIn(text, result.stdout)
        self.assertRegex(result.stdout, r'失敗回数[^\n]*1')

    def test_show_accepts_short_and_uppercase_ids_and_json_keeps_saved_content(self):
        event_id = self.events['sent']
        for selector in (event_id, event_id[:12], event_id.upper(), event_id[:12].upper()):
            with self.subTest(selector=selector):
                result = self.run_cli('--show', selector, '--format', 'json')
                self.assert_success(result)
                detail = json.loads(result.stdout)
                self.assertEqual(detail['identity'], self.box.identity)
                record = detail['event']
                self.assertEqual(record['event_id'], event_id)
                self.assertEqual(record['key'], 'sent')
                self.assertEqual(record['state'], 'sent')
                self.assertEqual(record['attempts'], 0)
                self.assertEqual(record['created_at'], self.now - 600)
                self.assertEqual(record['sent_at'], self.now - 599)
                self.assertEqual(record['content'], f'テスト通報: sent\n---\nNMEA: {DCR_NMEA}')
                self.assertIsNone(record['content_error'])
                self.assertEqual(record['report_type'], 'EarthquakeEarlyWarning')
                self.assertNotIn('event_json', record)
        text = self.run_cli('--show', event_id[:12], '--format', 'text')
        self.assert_success(text)
        sent = datetime.fromtimestamp(self.now - 599, timezone(timedelta(hours=9)))
        self.assertIn(sent.strftime('%Y-%m-%d %H:%M:%S'), text.stdout)
        self.assertIn('送信済み', text.stdout)
        self.assertNotIn('未送信', text.stdout)

    def test_show_preserves_a_marine_post_signed_before_the_formatter_changed(self):
        report = azarashi.decode(MARINE_NMEA, timestamp=datetime(2026, 11, 8, tzinfo=timezone.utc))
        content = f'{report}\n---\nNMEA: {MARINE_NMEA}'
        event = EventBuilder(Kind(1), content).finalize(self.keys)
        self.box.seed_sent('old-marine', event.as_json(), event.id().to_hex(), self.now - 30, self.now)
        text = self.run_cli('--show', event.id().to_hex())
        self.assert_success(text)
        self.assertIn(content, text.stdout)
        self.assertNotIn('■ 海上着氷警報', text.stdout)
        result = self.run_cli('--show', event.id().to_hex(), '--format', 'json')
        self.assert_success(result)
        self.assertEqual(json.loads(result.stdout)['event']['content'], content)

    def test_show_no_match_and_ambiguous_prefix_are_brief_failures(self):
        prefix = 'abcdefabcdef'
        for number in ('1', '2'):
            self.box.enqueue(f'collision-{number}', '{"content":"collision"}',
                             prefix + number * 52, self.now)
        for selector in ('0' * 64, prefix):
            with self.subTest(selector=selector):
                result = self.run_cli('--show', selector)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertNotIn('Traceback', result.stderr)
        # A longer, unique prefix and the full ID still select one of the collisions.
        for selector in (prefix + '1', prefix + '2' * 52):
            with self.subTest(selector=selector):
                result = self.run_cli('--show', selector, '--format', 'json')
                self.assert_success(result)
                self.assertTrue(json.loads(result.stdout)['event']['event_id'].startswith(selector))

    def test_show_corrupt_content_retains_delivery_metadata(self):
        with self.box.connection:
            self.box.connection.execute(
                'UPDATE events SET event_json = ? WHERE identity = ? AND key = ?',
                ('broken JSON', self.box.identity, 'overdue'),
            )
        text = self.run_cli('--show', self.events['overdue'])
        self.assert_success(text)
        for value in (self.events['overdue'], '送信待ち', 'relay offline'):
            self.assertIn(value, text.stdout)
        self.assertNotIn('broken JSON', text.stdout)
        result = self.run_cli('--show', self.events['overdue'], '--format', 'json')
        self.assert_success(result)
        record = json.loads(result.stdout)['event']
        self.assertIsNone(record['content'])
        self.assertTrue(record['content_error'])
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(record['last_error'], 'relay offline')

    def test_invalid_option_combinations_fail_before_daemon_or_storage_access(self):
        for arguments in (
            ('--format', 'text'), ('--health', '--format', 'json'),
            ('--state', 'pending'), ('--status', '--limit', '1'),
            ('--history', '--limit', '0'), ('--history', '--limit', '-1'),
            ('--history', '--limit', '1001'), ('--history', '--limit', '1.5'),
            ('--history', '--state', 'unknown'), ('--status', '--format', 'xml'),
            ('--status', '--history'), ('--history', '--health'),
            ('--show', self.events['recent'], '--status'),
            ('--show', self.events['recent'], '--history'),
            ('--show', self.events['recent'], '--health'),
            ('--show', self.events['recent'], '--preview', 'missing.nmea'),
            ('--show', self.events['recent'], '--state', 'pending'),
            ('--show', self.events['recent'], '--limit', '1'),
            ('--show', self.events['recent'], '--input-format', 'nmea'),
            ('--show', self.events['recent'], '--received-at', '2026-11-08T00:00:00Z'),
            ('--show', self.events['recent'], '--format', 'xml'),
            ('--show',), ('--show', ''), ('--show', 'a' * 11), ('--show', 'a' * 65),
            ('--show', 'g' * 64), ('--show', 'a' * 11 + '%'),
            ('--show', 'a' * 12 + '\n'),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, forbid_database=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('error:', result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_missing_database_produces_a_brief_stderr_error_without_creating_it(self):
        missing = self.root / 'missing' / 'state.sqlite3'
        for arguments in (('--status',), ('--status', '--format', 'text'), ('--history',), ('--health',),
                          ('--show', self.events['recent']),
                          ('--show', self.events['recent'], '--format', 'json')):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, path=missing)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertIn('保存状態を読み取れません', result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse(missing.parent.exists())


if __name__ == '__main__':
    unittest.main()
