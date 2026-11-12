"""Check scheduling, retention and worker health without waiting for a clock."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from nostr_sdk import EventBuilder, Keys, Kind

from shirasu import backup_worker as worker
from shirasu.backup import verify_backup
from shirasu.outbox import Outbox


class Clock:
    def __init__(self):
        self.wall = time.time()
        self.elapsed = 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.elapsed += seconds


class WaitingStop:
    def __init__(self, clock, waits=1):
        self.clock = clock
        self.maximum = waits
        self.delays = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.delays.append(seconds)
        self.clock.advance(seconds)
        if len(self.delays) >= self.maximum:
            self.set()
        return self.stopped


class BackupWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='shirasu-worker-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'backups'
        self.directory.mkdir()
        self.source = self.root / 'state.sqlite3'
        self.clock = Clock()
        self.settings = worker.Settings(str(self.source), str(self.directory), 3600, 2)

    def make_worker(self, settings=None, stop_event=None):
        return worker.Worker(settings or self.settings, clock=lambda: self.clock.wall,
                             monotonic=lambda: self.clock.elapsed, stop_event=stop_event)

    def archive(self, date='20260101T000000000000Z', number=0):
        path = self.directory / f'auto-{date}-{number:032x}.sqlite3'
        path.write_bytes(b'owned archive')
        return path

    def saved_success(self, age=10):
        instance = self.make_worker()
        archive = self.archive()
        instance.state.update(last_success=self.clock.wall - age,
                              last_attempt=self.clock.wall - age, last_file=archive.name)
        instance.schedule(max(0, self.settings.interval_seconds - age))
        instance.save_status()
        return instance, archive

    def test_settings_require_positive_integer_intervals_and_retention(self):
        self.assertEqual(worker.Settings.load({}).interval_seconds, 3600)
        self.assertEqual(worker.Settings.load({}).keep_count, 168)
        settings = worker.Settings.load({'SHIRASU_BACKUP_INTERVAL_SECONDS': '1',
                                         'SHIRASU_BACKUP_KEEP_COUNT': '3'})
        self.assertEqual((settings.interval_seconds, settings.keep_count), (1, 3))
        for name in ('SHIRASU_BACKUP_INTERVAL_SECONDS', 'SHIRASU_BACKUP_KEEP_COUNT'):
            for value in ('0', '-1', 'NaN', '1.5', ''):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    worker.Settings.load({name: value})
        for name in ('SHIRASU_STATE_PATH', 'SHIRASU_BACKUP_DIR'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                worker.Settings.load({name: ''})

    def test_first_job_creates_real_verified_archive_without_changing_original(self):
        keys = Keys.generate()
        with Outbox(self.source, keys.public_key().to_hex()) as box:
            event = EventBuilder(Kind(1), 'saved publication').finalize(keys)
            box.enqueue('sample', event.as_json(), event.id().to_hex(), self.clock.wall)
            before = list(box.connection.iterdump())
            instance = self.make_worker()
            instance.recover_schedule()
            self.assertEqual(instance.deadline, self.clock.elapsed)
            with patch('shirasu.backup.time.time', return_value=self.clock.wall):
                instance.run_backup()
            self.assertEqual(list(box.connection.iterdump()), before)
        path = self.directory / instance.state['last_file']
        self.assertIsNotNone(worker.ARCHIVE_NAME.fullmatch(path.name))
        self.assertEqual(verify_backup(path)['events_verified'], 1)
        self.assertTrue(worker.healthy(self.settings, self.clock.wall))
        self.assertEqual(instance.deadline, self.clock.elapsed + 3600)
        self.assertIsNone(instance.state['last_error'])
        self.assertEqual(list(self.directory.glob('.status-*')), [])

    def test_restart_waits_remaining_interval_after_verifying_saved_success(self):
        previous, archive = self.saved_success(age=75)
        instance = self.make_worker()
        with patch('shirasu.backup_worker.verify_backup', return_value={'backup_created_at': self.clock.wall - 76}) as verify:
            instance.recover_schedule()
        verify.assert_called_once_with(archive)
        self.assertEqual(instance.deadline, self.clock.elapsed + 3525)
        self.assertEqual(instance.state['last_success'], previous.state['last_success'])
        self.assertEqual(instance.state['last_file'], archive.name)

    def test_failed_missing_corrupt_or_future_saved_state_does_not_defer_work(self):
        previous, archive = self.saved_success()
        original = copy.deepcopy(previous.state)
        variants = [dict(original, last_error='previous failure'),
                    dict(original, last_success=self.clock.wall + 1),
                    dict(original, heartbeat=self.clock.wall + 1),
                    dict(original, last_attempt=self.clock.wall + 1),
                    dict(original, last_file='../escape.sqlite3'),
                    dict(original, interval_seconds=False),
                    dict(original, heartbeat=10 ** 500),
                    dict(original, last_success=self.clock.wall - 4000)]
        for state in variants:
            with self.subTest(state=state):
                (self.directory / 'status.json').write_text(json.dumps(state))
                instance = self.make_worker()
                with patch('shirasu.backup_worker.verify_backup', return_value={'backup_created_at': self.clock.wall - 5000}):
                    instance.recover_schedule()
                self.assertEqual(instance.deadline, self.clock.elapsed)
        (self.directory / 'status.json').write_text(json.dumps(original))
        with patch('shirasu.backup_worker.verify_backup', side_effect=ValueError('invalid signature')):
            instance = self.make_worker()
            instance.recover_schedule()
        self.assertIsNone(instance.state['last_success'])
        archive.unlink()
        instance = self.make_worker()
        instance.recover_schedule()
        self.assertEqual(instance.deadline, self.clock.elapsed)
        (self.directory / 'status.json').write_text('broken JSON')
        instance.recover_schedule()
        self.assertEqual(instance.deadline, self.clock.elapsed)

    def test_failure_retries_and_preserves_all_archives_then_recovers(self):
        instance, previous = self.saved_success()
        extra = self.archive(number=1)
        old_success = instance.state['last_success']
        with patch('shirasu.backup_worker.create_backup', side_effect=OSError('disk full\n\x1b[31m')), \
             patch.object(instance, 'prune') as prune:
            instance.run_backup()
        prune.assert_not_called()
        self.assertTrue(previous.exists())
        self.assertTrue(extra.exists())
        self.assertEqual(instance.state['last_success'], old_success)
        self.assertEqual(instance.deadline, self.clock.elapsed + 60)
        self.assertIn('disk full', instance.state['last_error'])
        self.assertNotIn('\n', instance.state['last_error'])
        self.assertNotIn('\x1b', instance.state['last_error'])
        self.assertFalse(worker.healthy(self.settings, self.clock.wall))

        def succeed(source, destination):
            destination.write_bytes(b'verified by create_backup')
            return {'events_verified': 2}

        self.clock.advance(60)
        with patch('shirasu.backup_worker.create_backup', side_effect=succeed):
            instance.run_backup()
        self.assertIsNone(instance.state['last_error'])
        self.assertTrue(worker.healthy(self.settings, self.clock.wall))
        self.assertEqual(instance.deadline, self.clock.elapsed + 3600)

    def test_pruning_only_targets_owned_regular_files_and_keeps_new_file_after_clock_rollback(self):
        current = self.archive('20250101T000000000000Z')
        newest = self.archive('20270101T000000000000Z')
        old = self.archive('20260101T000000000000Z')
        manual = self.directory / 'shirasu-manual.sqlite3'
        manual.write_bytes(b'manual')
        temporary = self.directory / '.auto-incomplete.tmp'
        temporary.write_bytes(b'incomplete')
        linked = self.directory / 'auto-20290101T000000000000Z-ffffffffffffffffffffffffffffffff.sqlite3'
        linked.symlink_to(manual)
        folder = self.directory / 'auto-20280101T000000000000Z-ffffffffffffffffffffffffffffffff.sqlite3'
        folder.mkdir()
        removed = self.make_worker().prune(current)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        for preserved in (current, newest, manual, temporary, linked, folder):
            self.assertTrue(preserved.exists())
        self.assertTrue(linked.is_symlink())

    def test_pruning_never_removes_source_even_if_it_looks_like_an_owned_archive(self):
        source = self.archive('20200101T000000000000Z')
        current = self.archive('20260101T000000000000Z')
        alias = self.root / 'state-alias.sqlite3'
        alias.symlink_to(source)
        for path in (source, alias):
            with self.subTest(source_path=path):
                settings = worker.Settings(str(path), str(self.directory), 3600, 1)
                self.assertEqual(self.make_worker(settings).prune(current), 0)
        self.assertEqual(source.read_bytes(), b'owned archive')

    def test_retention_failure_keeps_success_and_normal_schedule(self):
        instance = self.make_worker()

        def succeed(source, destination):
            destination.write_bytes(b'new verified archive')
            return {'events_verified': 1}

        with patch('shirasu.backup_worker.create_backup', side_effect=succeed), \
             patch.object(instance, 'prune', side_effect=PermissionError('cannot remove old file')):
            instance.run_backup()
        self.assertEqual(instance.state['last_success'], self.clock.wall)
        self.assertIsNone(instance.state['last_error'])
        self.assertIn('PermissionError', instance.state['retention_error'])
        self.assertTrue((self.directory / instance.state['last_file']).is_file())
        self.assertEqual(instance.deadline, self.clock.elapsed + 3600)
        self.assertFalse(worker.healthy(self.settings, self.clock.wall))

    def test_health_rejects_stale_future_failed_stopped_or_missing_archives_without_writes(self):
        instance, archive = self.saved_success()
        original = copy.deepcopy(instance.state)
        variants = [dict(original, running=False), dict(original, heartbeat=self.clock.wall - 91),
                    dict(original, heartbeat=self.clock.wall + 1),
                    dict(original, last_attempt=self.clock.wall + 1),
                    dict(original, heartbeat=10 ** 500),
                    dict(original, last_success=self.clock.wall + 1),
                    dict(original, last_success=self.clock.wall - 3721),
                    dict(original, last_error='backup failed'), dict(original, retention_error='delete failed'),
                    dict(original, last_file='/outside.sqlite3'), dict(original, keep_count=0)]
        for state in variants:
            with self.subTest(state=state):
                path = self.directory / 'status.json'
                path.write_text(json.dumps(state))
                before = path.read_bytes()
                self.assertFalse(worker.healthy(self.settings, self.clock.wall))
                self.assertEqual(path.read_bytes(), before)
        (self.directory / 'status.json').write_text(json.dumps(original))
        archive.unlink()
        self.assertFalse(worker.healthy(self.settings, self.clock.wall))
        archive.symlink_to(self.source)
        self.assertFalse(worker.healthy(self.settings, self.clock.wall))
        missing = self.root / 'absent'
        settings = worker.Settings(str(self.source), str(missing))
        self.assertFalse(worker.healthy(settings, self.clock.wall))
        self.assertFalse(missing.exists())

    def test_directory_lock_blocks_second_worker_without_changing_status(self):
        instance, _ = self.saved_success()
        before = (self.directory / 'status.json').read_bytes()
        with worker.worker_lock(self.directory):
            with self.assertRaisesRegex(RuntimeError, 'Another backup worker'):
                instance.run()
        self.assertEqual((self.directory / 'status.json').read_bytes(), before)
        with worker.worker_lock(self.directory):
            pass

    def test_waiting_worker_refreshes_heartbeat_and_stops_without_an_extra_backup(self):
        self.saved_success(age=10)
        stop = WaitingStop(self.clock, waits=2)
        instance = self.make_worker(stop_event=stop)
        with patch('shirasu.backup_worker.verify_backup', return_value={'backup_created_at': self.clock.wall - 20}), \
             patch('shirasu.backup_worker.create_backup') as create:
            instance.run()
        create.assert_not_called()
        self.assertEqual(stop.delays, [30, 30])
        saved = json.loads((self.directory / 'status.json').read_text())
        self.assertEqual(saved['heartbeat'], self.clock.wall)
        self.assertFalse(saved['running'])
        self.assertFalse(worker.healthy(self.settings, self.clock.wall))

    def test_stop_during_creation_finishes_one_backup_and_releases_lock(self):
        stop = threading.Event()
        instance = self.make_worker(stop_event=stop)

        def succeed(source, destination):
            stop.set()
            destination.write_bytes(b'finished despite stop')
            return {'events_verified': 1}

        with patch('shirasu.backup_worker.create_backup', side_effect=succeed) as create:
            instance.run()
        self.assertEqual(create.call_count, 1)
        saved = json.loads((self.directory / 'status.json').read_text())
        self.assertFalse(saved['running'])
        self.assertEqual(saved['last_success'], self.clock.wall)
        self.assertTrue((self.directory / saved['last_file']).is_file())
        with worker.worker_lock(self.directory):
            pass

    def test_wall_clock_changes_do_not_move_runtime_deadline(self):
        instance = self.make_worker()
        instance.schedule(3600)
        deadline = instance.deadline
        self.clock.wall -= 7200
        self.clock.elapsed += 100
        self.assertEqual(instance.deadline, deadline)
        self.assertEqual(instance.deadline - self.clock.elapsed, 3500)


if __name__ == '__main__':
    unittest.main()
