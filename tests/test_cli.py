"""Check inspection commands in separate processes without production config."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from nostr_sdk import EventBuilder, Keys, Kind

from outbox import Outbox, read_status


ROOT = Path(__file__).resolve().parents[1]
DCR_NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'


class InspectionCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-cli-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ('main.py', 'outbox.py', 'receiver.py', 'inspection.py'):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / 'config.py').write_text(
            "raise RuntimeError('Inspection must never load config or start the daemon')\n",
            encoding='utf-8',
        )
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

    def run_cli(self, *arguments, path=None):
        before = list(self.box.connection.iterdump())
        snapshot_before = read_status(self.path, now=self.now)
        result = subprocess.run(
            [sys.executable, '-B', 'main.py', *arguments], cwd=self.root,
            env={'PATH': os.environ.get('PATH', ''), 'PYTHONUNBUFFERED': '1',
                 'PYTHONDONTWRITEBYTECODE': '1',
                 'SHIRASU_STATE_PATH': str(self.path if path is None else path)},
            capture_output=True, encoding='utf-8', timeout=10,
        )
        self.assertEqual(list(self.box.connection.iterdump()), before)
        self.assertEqual(read_status(self.path, now=self.now), snapshot_before)
        self.assertFalse((self.root / 'state.sqlite3.lock').exists())
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

    def test_invalid_option_combinations_fail_before_daemon_or_storage_access(self):
        for arguments in (
            ('--format', 'text'), ('--health', '--format', 'json'),
            ('--state', 'pending'), ('--status', '--limit', '1'),
            ('--history', '--limit', '0'), ('--history', '--limit', '-1'),
            ('--history', '--limit', '1001'), ('--history', '--limit', '1.5'),
            ('--history', '--state', 'unknown'), ('--status', '--format', 'xml'),
            ('--status', '--history'), ('--history', '--health'),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('error:', result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_missing_database_produces_a_brief_stderr_error_without_creating_it(self):
        missing = self.root / 'missing' / 'state.sqlite3'
        for arguments in (('--status',), ('--status', '--format', 'text'), ('--history',), ('--health',)):
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
