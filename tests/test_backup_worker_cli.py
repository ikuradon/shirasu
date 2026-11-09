"""Run the backup scheduler in isolated processes with real signed snapshots."""

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

from backup import create_backup, verify_backup
from outbox import Outbox, read_history


ROOT = Path(__file__).resolve().parents[1]

GUARDED_WORKER = '''
from unittest.mock import patch
import nostr_sdk
import outbox
import backup_worker

def forbidden(*args, **kwargs):
    raise AssertionError('AUTO_BACKUP_SIDE_EFFECT: signing, network or outbox writer')

with patch.object(nostr_sdk.Keys, 'parse', side_effect=forbidden), \\
     patch.object(nostr_sdk.Keys, 'generate', side_effect=forbidden), \\
     patch.object(nostr_sdk.EventBuilder, 'finalize', side_effect=forbidden), \\
     patch.object(outbox, 'Outbox', side_effect=forbidden), \\
     patch('socket.create_connection', side_effect=forbidden), \\
     patch('socket.socket.connect', side_effect=forbidden):
    raise SystemExit(backup_worker.main())
'''


class BackupWorkerCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-backup-worker-cli-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ('backup_worker.py', 'backup.py', 'outbox.py', 'typhoon.py'):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / 'config.py').write_text(
            "raise AssertionError('AUTO_BACKUP_SIDE_EFFECT: config was imported')\n", encoding='utf-8',
        )
        (self.root / 'guarded_worker.py').write_text(GUARDED_WORKER, encoding='utf-8')
        self.path = self.root / 'live' / 'state.sqlite3'
        self.output = self.root / 'auto'
        self.keys = Keys.generate()
        self.box = Outbox(self.path, self.keys.public_key().to_hex(), max_age_seconds=0)
        self.addCleanup(self.box.close)
        self.enqueue('pending')
        self.enqueue('sent')
        self.box.mark_sent('sent', time.time())
        self.box.set_status('heartbeat', time.time(), time.time())
        self.processes = {}
        self.addCleanup(self.stop_all)

    def enqueue(self, key):
        event = EventBuilder(Kind(1), f'保存する通報: {key}').finalize(self.keys)
        self.box.enqueue(key, event.as_json(), event.id().to_hex(), time.time())
        return event

    def environment(self, *, interval=60, keep=3, state_path=None):
        return {'PATH': os.environ.get('PATH', ''), 'PYTHONUNBUFFERED': '1',
                'PYTHONDONTWRITEBYTECODE': '1',
                'SHIRASU_STATE_PATH': str(self.path if state_path is None else state_path),
                'SHIRASU_BACKUP_DIR': str(self.output),
                'SHIRASU_BACKUP_INTERVAL_SECONDS': str(interval),
                'SHIRASU_BACKUP_KEEP_COUNT': str(keep)}

    def start(self, **settings):
        log = self.root / f'worker-{len(self.processes)}.log'
        with log.open('ab') as output:
            process = subprocess.Popen(
                [sys.executable, '-B', 'guarded_worker.py'], cwd=self.root,
                env=self.environment(**settings), stdout=output, stderr=subprocess.STDOUT,
            )
        self.processes[process] = log
        return process

    def logs(self, process):
        text = self.processes[process].read_text(encoding='utf-8')
        self.assertNotIn('AUTO_BACKUP_SIDE_EFFECT', text)
        return text

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                self.fail(f'Backup worker ignored SIGTERM:\n{self.logs(process)}')
            self.assertEqual(process.returncode, 0, self.logs(process))
        self.logs(process)

    def stop_all(self):
        for process in self.processes:
            self.stop(process)

    def status(self):
        try:
            return json.loads((self.output / 'status.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None

    def wait_status(self, process, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), self.logs(process))
            saved = self.status()
            if saved is not None and predicate(saved):
                return saved
            time.sleep(0.05)
        self.fail(f'Backup worker did not reach expected state:\n{self.logs(process)}')

    def health(self, **settings):
        result = subprocess.run(
            [sys.executable, '-B', 'guarded_worker.py', '--health'], cwd=self.root,
            env=self.environment(**settings), capture_output=True, text=True, timeout=10,
        )
        self.assertEqual((result.stdout, result.stderr), ('', ''))
        self.assertIn(result.returncode, (0, 1))
        return result.returncode

    def archives(self):
        return sorted(self.output.glob('auto-*.sqlite3'))

    def test_health_before_start_is_silent_and_creates_no_directory(self):
        before = list(self.box.connection.iterdump())
        self.assertEqual(self.health(), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.box.connection.iterdump()), before)

    def test_periodic_snapshots_keep_one_generation_and_leave_manual_files_untouched(self):
        self.output.mkdir()
        manual = self.output / 'manual.sqlite3'
        manual.write_bytes(b'owned by the operator')
        process = self.start(interval=2, keep=1)
        first = self.wait_status(process, lambda state: state.get('last_success') is not None)
        first_name = first['last_file']
        self.assertEqual(self.health(interval=2, keep=1), 0)

        new_event = self.enqueue('received-after-first-backup')
        before = list(self.box.connection.iterdump())
        latest = self.wait_status(process, lambda state: state.get('last_file') not in (None, first_name))
        self.stop(process)
        archive = self.output / latest['last_file']
        self.assertEqual(self.archives(), [archive])
        self.assertFalse((self.output / first_name).exists())
        self.assertEqual(manual.read_bytes(), b'owned by the operator')
        self.assertEqual(verify_backup(archive)['counts'], {'pending': 2, 'sent': 1, 'expired': 0})
        saved = {row['key']: row for row in read_history(archive)['events']}
        self.assertEqual(saved['received-after-first-backup']['event_json'], new_event.as_json())
        self.assertEqual(list(self.box.connection.iterdump()), before)
        self.assertFalse(self.status()['running'])
        self.assertEqual(self.health(interval=2, keep=1), 1)

    def test_restart_reuses_a_recent_verified_backup_instead_of_creating_another(self):
        before = list(self.box.connection.iterdump())
        first_process = self.start(interval=60)
        first = self.wait_status(first_process, lambda state: state.get('last_success') is not None)
        self.stop(first_process)
        stopped_heartbeat = self.status()['heartbeat']
        second_process = self.start(interval=60)
        resumed = self.wait_status(second_process, lambda state: state.get('running')
                                   and state['heartbeat'] > stopped_heartbeat)
        self.assertEqual(resumed['last_success'], first['last_success'])
        self.assertEqual(resumed['last_file'], first['last_file'])
        self.assertEqual(self.health(interval=60), 0)
        self.stop(second_process)
        self.assertEqual(self.archives(), [self.output / first['last_file']])
        self.assertEqual(list(self.box.connection.iterdump()), before)

    def test_a_second_worker_cannot_create_backups_or_replace_the_active_status(self):
        first_process = self.start(interval=60)
        first = self.wait_status(first_process, lambda state: state.get('last_success') is not None)
        second_process = self.start(interval=60)
        second_process.wait(timeout=5)
        self.assertNotEqual(second_process.returncode, 0, self.logs(second_process))
        self.assertIsNone(first_process.poll(), self.logs(first_process))
        self.assertEqual(self.status()['last_file'], first['last_file'])
        self.assertTrue(self.status()['running'])
        self.assertEqual(self.archives(), [self.output / first['last_file']])
        self.assertEqual(self.health(interval=60), 0)

    def test_missing_database_records_a_failure_without_pruning_or_creating_the_source(self):
        self.output.mkdir()
        existing = []
        for number in ('1', '2'):
            archive = self.output / f'auto-20260101T000000000000Z-{number * 32}.sqlite3'
            create_backup(self.path, archive)
            existing.append(archive)
        before = {path: path.read_bytes() for path in existing}
        missing = self.root / 'missing' / 'state.sqlite3'
        process = self.start(interval=60, keep=1, state_path=missing)
        failed = self.wait_status(process, lambda state: bool(state.get('last_error')))
        self.assertTrue(failed['running'])
        self.assertIsNone(failed['last_success'])
        self.assertEqual(self.health(interval=60, keep=1, state_path=missing), 1)
        self.assertIsNone(process.poll(), self.logs(process))
        self.assertFalse(missing.parent.exists())
        self.stop(process)
        self.assertEqual(self.archives(), existing)
        self.assertEqual({path: path.read_bytes() for path in existing}, before)


if __name__ == '__main__':
    unittest.main()
