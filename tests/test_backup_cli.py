"""Exercise backup commands in processes forbidden from starting live delivery."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from nostr_sdk import EventBuilder, Keys, Kind

from outbox import Outbox


ROOT = Path(__file__).resolve().parents[1]

GUARDED_CLI = '''
import os
import sqlite3
from unittest.mock import patch
import main

def forbidden(*args, **kwargs):
    raise AssertionError('BACKUP_SIDE_EFFECT: daemon, signing, network or unexpected database access')

connect = sqlite3.connect
def guarded_connect(database, *args, **kwargs):
    if os.environ.get('FORBID_DATABASE') == '1':
        forbidden()
    if os.environ.get('VERIFY_ONLY') == '1':
        if not kwargs.get('uri') or 'mode=ro' not in str(database):
            forbidden()
    return connect(database, *args, **kwargs)

with patch.object(main, 'Nostr', side_effect=forbidden), \\
     patch.object(main, 'GpsdReceiver', side_effect=forbidden), \\
     patch.object(main, 'Outbox', side_effect=forbidden), \\
     patch.object(main.Keys, 'parse', side_effect=forbidden), \\
     patch.object(main.Keys, 'generate', side_effect=forbidden), \\
     patch.object(main.EventBuilder, 'finalize', side_effect=forbidden), \\
     patch.object(main, 'report_message', side_effect=forbidden), \\
     patch.object(sqlite3, 'connect', side_effect=guarded_connect), \\
     patch('socket.create_connection', side_effect=forbidden), \\
     patch('socket.socket.connect', side_effect=forbidden):
    raise SystemExit(main.main())
'''


class BackupCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-backup-cli-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ('main.py', 'outbox.py', 'receiver.py', 'inspection.py',
                     'message_format.py', 'dcx_instructions.py', 'backup.py', 'typhoon.py', 'nankai.py',
                     'image_publication.py', 'image_protocol.py'):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / 'config.py').write_text(
            "raise AssertionError('BACKUP_SIDE_EFFECT: config was imported')\n",
            encoding='utf-8',
        )
        (self.root / 'guarded_cli.py').write_text(GUARDED_CLI, encoding='utf-8')
        self.path = self.root / 'state.sqlite3'
        self.destination = self.root / 'snapshot.sqlite3'
        self.keys = Keys.generate()
        self.box = Outbox(self.path, self.keys.public_key().to_hex(), max_age_seconds=30)
        self.addCleanup(self.box.close)
        self.now = time.time()
        self.events = {}
        self.enqueue('expired', self.now - 300)
        self.box.maintain(self.now - 200)
        self.box.max_age_seconds = 0
        self.enqueue('pending', self.now - 150)
        self.box.mark_failed('pending', 'relay offline', self.now - 149, 5)
        self.enqueue('sent', self.now - 100)
        self.box.mark_sent('sent', self.now - 99)
        self.box.set_status('running', True, self.now - 5)
        self.box.set_status('heartbeat', self.now - 5, self.now - 5)

    def enqueue(self, key, received_at):
        event = EventBuilder(Kind(1), f'保存済み通報: {key}').finalize(self.keys)
        self.events[key] = event
        self.box.enqueue(key, event.as_json(), event.id().to_hex(), received_at)

    def run_cli(self, *arguments, state_path=None, forbid_database=False, verify_only=False):
        before = list(self.box.connection.iterdump())
        result = subprocess.run(
            [sys.executable, '-B', 'guarded_cli.py', *map(str, arguments)], cwd=self.root,
            env={'PATH': os.environ.get('PATH', ''), 'PYTHONUNBUFFERED': '1',
                 'PYTHONDONTWRITEBYTECODE': '1',
                 'FORBID_DATABASE': '1' if forbid_database else '0',
                 'VERIFY_ONLY': '1' if verify_only else '0',
                 'SHIRASU_STATE_PATH': str(self.path if state_path is None else state_path)},
            capture_output=True, encoding='utf-8', timeout=15,
        )
        self.assertEqual(list(self.box.connection.iterdump()), before)
        self.assertFalse(Path(str(self.path) + '.lock').exists())
        self.assertNotIn('BACKUP_SIDE_EFFECT', result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')

    def assert_failure(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertNotIn('\x1b', result.stderr)

    def create(self):
        result = self.run_cli('--backup', self.destination, '--format', 'json')
        self.assert_success(result)
        return json.loads(result.stdout)

    def test_backup_defaults_to_text_and_keeps_live_wal_records_unchanged(self):
        self.assertTrue(Path(str(self.path) + '-wal').exists())
        result = self.run_cli('--backup', self.destination)
        self.assert_success(result)
        for text in ('作成', '件', '送信待ち', '送信済み', '期限切れ'):
            self.assertIn(text, result.stdout)
        self.assertTrue(self.destination.is_file())
        self.assertFalse(Path(str(self.destination) + '-wal').exists())
        self.assertFalse(Path(str(self.destination) + '-shm').exists())
        with sqlite3.connect(self.destination.as_uri() + '?mode=ro', uri=True) as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            saved = dict(connection.execute('SELECT key, event_json FROM events'))
        self.assertEqual(saved, {key: event.as_json() for key, event in self.events.items()})

    def test_backup_json_reports_verified_counts_and_distinguishes_record_age(self):
        summary = self.create()
        self.assertEqual(summary['path'], str(self.destination))
        self.assertEqual(summary['schema_version'], 1)
        self.assertEqual(summary['identity'], self.box.identity)
        self.assertEqual(summary['counts'], {'pending': 1, 'sent': 1, 'expired': 1})
        self.assertEqual(summary['events_verified'], 3)
        self.assertEqual(summary['status_updated_at'], self.now - 5)
        self.assertGreaterEqual(summary['backup_created_at'], self.now)
        self.assertEqual([account['identity'] for account in summary['accounts']], [self.box.identity])
        self.assertEqual(summary['accounts'][0]['counts'], summary['counts'])
        self.assertNotIn('event_json', summary)

    def test_verification_needs_only_the_backup_and_never_changes_it(self):
        summary = self.create()
        before = self.destination.read_bytes()
        missing_state = self.root / 'unavailable-state' / 'state.sqlite3'
        for format_name in ('text', 'json'):
            with self.subTest(format=format_name):
                result = self.run_cli('--verify-backup', self.destination, '--format', format_name,
                                      state_path=missing_state, verify_only=True)
                self.assert_success(result)
                if format_name == 'json':
                    verified = json.loads(result.stdout)
                    for field in ('identity', 'counts', 'accounts', 'backup_created_at',
                                  'status_updated_at', 'events_verified'):
                        self.assertEqual(verified[field], summary[field])
                else:
                    self.assertIn('検証', result.stdout)
                    self.assertIn('件', result.stdout)
                self.assertEqual(self.destination.read_bytes(), before)
                self.assertFalse(missing_state.parent.exists())
                self.assertFalse(Path(str(self.destination) + '-wal').exists())
                self.assertFalse(Path(str(self.destination) + '-shm').exists())

    def test_backup_never_overwrites_an_existing_file_or_the_source(self):
        self.destination.write_text('keep this existing file', encoding='utf-8')
        result = self.run_cli('--backup', self.destination)
        self.assert_failure(result)
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'keep this existing file')
        self.assert_failure(self.run_cli('--backup', self.path))
        link = self.root / 'linked.sqlite3'
        link.symlink_to(self.destination)
        self.assert_failure(self.run_cli('--backup', link))
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'keep this existing file')

    def test_missing_inputs_produce_brief_errors_without_creating_files(self):
        missing = self.root / 'not-present' / 'state.sqlite3'
        self.assert_failure(self.run_cli('--backup', self.destination, state_path=missing))
        self.assertFalse(missing.parent.exists())
        self.assertFalse(self.destination.exists())
        self.assert_failure(self.run_cli('--verify-backup', missing, verify_only=True))
        self.assertFalse(missing.parent.exists())
        controls = self.root / 'missing\n\x1b[31m.sqlite3'
        self.assert_failure(self.run_cli('--verify-backup', controls, verify_only=True))
        self.assertFalse(controls.exists())

    def test_verification_rejects_corruption_version_mismatch_and_altered_signatures(self):
        self.create()
        for failure in ('corrupt', 'schema', 'signature'):
            with self.subTest(failure=failure):
                bad = self.root / f'{failure}.sqlite3'
                shutil.copyfile(self.destination, bad)
                if failure == 'corrupt':
                    bad.write_bytes(b'not a SQLite backup')
                else:
                    with sqlite3.connect(bad) as connection:
                        if failure == 'schema':
                            connection.execute('PRAGMA user_version = 99')
                        else:
                            raw = json.loads(self.events['pending'].as_json())
                            raw['content'] = 'tampered content'
                            connection.execute('UPDATE events SET event_json = ? WHERE key = ?',
                                               (json.dumps(raw), 'pending'))
                before = bad.read_bytes()
                self.assert_failure(self.run_cli('--verify-backup', bad, verify_only=True))
                self.assertEqual(bad.read_bytes(), before)

    def test_bad_source_is_not_published_as_a_successful_backup(self):
        raw = json.loads(self.events['pending'].as_json())
        raw['content'] = 'tampered before backup'
        with self.box.connection:
            self.box.connection.execute('UPDATE events SET event_json = ? WHERE key = ?',
                                        (json.dumps(raw), 'pending'))
        before = set(self.root.iterdir())
        self.assert_failure(self.run_cli('--backup', self.destination))
        self.assertFalse(self.destination.exists())
        self.assertEqual(set(self.root.iterdir()), before)

    def test_invalid_options_fail_before_database_access_or_output_creation(self):
        selector = self.events['pending'].id().to_hex()
        for arguments in (
            ('--backup', self.destination, '--verify-backup', self.path),
            ('--backup', self.destination, '--status'),
            ('--backup', self.destination, '--history'),
            ('--backup', self.destination, '--show', selector),
            ('--backup', self.destination, '--health'),
            ('--backup', self.destination, '--preview', 'missing.nmea'),
            ('--verify-backup', self.path, '--history'),
            ('--backup', self.destination, '--state', 'pending'),
            ('--verify-backup', self.path, '--limit', '1'),
            ('--backup', self.destination, '--input-format', 'nmea'),
            ('--verify-backup', self.path, '--received-at', '2026-11-08T00:00:00Z'),
            ('--backup', self.destination, '--format', 'xml'),
            ('--backup',), ('--verify-backup',),
            ('--backup', ''), ('--verify-backup', ''),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, forbid_database=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('error:', result.stderr)
                self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
