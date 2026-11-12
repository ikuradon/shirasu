"""Read backup health independently of delivery without doing backup work."""

import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from shirasu import backup_worker
from shirasu.metrics import Collector, MetricsServer
from shirasu.outbox import Outbox


ARCHIVE = 'auto-20261109T123653016941Z-4c2fd024113a48cfbf27b388fc4525bd.sqlite3'


class BackupMetricsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-backup-metrics-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'state.sqlite3'
        self.backups = self.root / 'backups'
        self.now = 1000
        self.collector = Collector(self.path, clock=lambda: self.now, backup_dir=self.backups)
        self.box = Outbox(self.path, 'private-account-identifier')
        self.addCleanup(self.box.close)
        for name, value in {
            'running': True, 'gpsd_connected': True, 'relay_connected': True,
            'heartbeat': self.now, 'last_received': self.now,
        }.items():
            self.box.set_status(name, value, self.now)

    def state(self):
        return {'version': 1, 'running': True, 'heartbeat': self.now,
                'last_attempt': self.now - 20, 'last_success': self.now - 10,
                'last_file': ARCHIVE, 'last_error': None, 'retention_error': None,
                'interval_seconds': 3600, 'keep_count': 168,
                'next_attempt_at': self.now + 3590}

    def save(self, state=None, *, archive=True):
        self.backups.mkdir(exist_ok=True)
        state = self.state() if state is None else state
        (self.backups / 'status.json').write_text(json.dumps(state), encoding='utf-8')
        if archive:
            # Deliberately not a valid SQLite file: a scrape must only lstat it,
            # never parse the archive or perform another signature verification.
            (self.backups / ARCHIVE).write_bytes(b'private archive body')
        return state

    def backup_values(self):
        return {key: value for key, value in self.collector.collect().items()
                if key.startswith('backup_')}

    def test_normal_state_exports_numeric_gauges_without_saved_strings(self):
        state = self.save()
        values = self.backup_values()
        self.assertEqual(values, {
            'backup_state_readable': 1, 'backup_healthy': 1,
            'backup_process_running': 1, 'backup_heartbeat_timestamp_seconds': 1000,
            'backup_last_attempt_timestamp_seconds': 980,
            'backup_last_success_timestamp_seconds': 990, 'backup_has_success': 1,
            'backup_interval_seconds': 3600, 'backup_failed': 0,
            'backup_retention_failed': 0, 'backup_last_file_present': 1,
        })
        text = self.collector.render().decode()
        for key in values:
            self.assertIn(f'# TYPE shirasu_{key} gauge\n', text)
        for private in (str(self.backups), ARCHIVE, 'private archive body', 'private-account-identifier'):
            self.assertNotIn(private, text)
        self.assertEqual(json.loads((self.backups / 'status.json').read_text()), state)

    def test_missing_backup_state_does_not_hide_delivery_or_create_directory(self):
        with self.assertLogs('shirasu.metrics', level='WARNING') as captured:
            values = self.collector.collect()
        self.assertEqual(values['state_readable'], 1)
        self.assertEqual(values['healthy'], 1)
        self.assertEqual({k: v for k, v in values.items() if k.startswith('backup_')},
                         {'backup_state_readable': 0, 'backup_healthy': 0})
        self.assertFalse(self.backups.exists())
        self.assertNotIn(str(self.backups), '\n'.join(captured.output))

    def test_missing_or_corrupt_delivery_does_not_hide_backup(self):
        self.save()
        for name, contents in (('missing.sqlite3', None), ('corrupt.sqlite3', b'broken DB')):
            with self.subTest(name=name):
                path = self.root / name
                if contents is not None:
                    path.write_bytes(contents)
                collector = Collector(path, clock=lambda: self.now, backup_dir=self.backups)
                with self.assertLogs('shirasu.metrics', level='WARNING'):
                    values = collector.collect()
                self.assertEqual(values['state_readable'], 0)
                self.assertEqual(values['healthy'], 0)
                self.assertEqual(values['backup_state_readable'], 1)
                self.assertEqual(values['backup_healthy'], 1)
                if contents is None:
                    self.assertFalse(path.exists())

    def test_corrupt_missing_and_overflowed_state_is_not_reported_as_healthy(self):
        self.save()
        states = ['broken JSON', '[]', 'null', '[' * 2000]
        for change in ({'version': 2}, {'running': 'yes'}, {'heartbeat': float('nan')},
                       {'last_success': float('inf')}, {'last_attempt': True},
                       {'interval_seconds': 0}, {'interval_seconds': 10 ** 400},
                       {'keep_count': 10 ** 400}, {'last_file': 42}):
            states.append(json.dumps(dict(self.state(), **change)))
        for raw in states:
            with self.subTest(raw=raw[:80]):
                (self.backups / 'status.json').write_text(raw, encoding='utf-8')
                values = self.backup_values()
                self.assertEqual(values, {'backup_state_readable': 0, 'backup_healthy': 0})
                self.assertEqual((self.backups / 'status.json').read_text(), raw)

    def test_read_failure_and_recovery_are_logged_once_without_details(self):
        self.save()
        state_path = self.backups / 'status.json'
        state_path.write_text('secret-webhook-token', encoding='utf-8')
        with self.assertLogs('shirasu.metrics', level='WARNING') as captured:
            self.collector.collect()
            self.collector.collect()
            state_path.write_text(json.dumps(dict(self.state(), interval_seconds=10 ** 400)))
            self.collector.collect()
        self.assertEqual(len(captured.output), 1)
        self.assertNotIn('secret-webhook-token', captured.output[0])
        self.assertNotIn(str(state_path), captured.output[0])
        self.save()
        with self.assertLogs('shirasu.metrics', level='INFO') as recovered:
            self.assertEqual(self.collector.collect()['backup_healthy'], 1)
            self.collector.collect()
        self.assertEqual(len(recovered.output), 1)

    def test_stopped_stale_future_failed_and_expired_health_matches_worker(self):
        changes = [
            {}, {'running': False}, {'heartbeat': self.now - 90},
            {'heartbeat': self.now - 91}, {'heartbeat': self.now + 1},
            {'last_attempt': self.now + 1}, {'last_success': self.now + 1},
            {'last_error': 'secret-backup-error'}, {'retention_error': 'private-retention-error'},
            {'interval_seconds': 1, 'last_success': self.now - 122},
            {'interval_seconds': 7200, 'last_success': 0},
        ]
        for change in changes:
            with self.subTest(change=change):
                state = self.save(dict(self.state(), **change))
                settings = backup_worker.Settings(backup_dir=str(self.backups),
                                                  interval_seconds=state['interval_seconds'])
                values = self.backup_values()
                self.assertEqual(values['backup_state_readable'], 1)
                self.assertEqual(values['backup_healthy'], int(backup_worker.healthy(settings, self.now)))
                self.assertEqual(values['backup_heartbeat_timestamp_seconds'], state['heartbeat'])
                self.assertEqual(values['backup_last_success_timestamp_seconds'], state['last_success'])
                self.assertEqual(values['backup_failed'], int(bool(state['last_error'])))
                self.assertEqual(values['backup_retention_failed'], int(bool(state['retention_error'])))
                text = self.collector.render().decode()
                self.assertNotIn('secret-backup-error', text)
                self.assertNotIn('private-retention-error', text)

    def test_never_succeeded_is_distinct_from_a_success_at_epoch_zero(self):
        for success, has_success in ((None, 0), (0, 1)):
            with self.subTest(success=success):
                self.save(dict(self.state(), last_attempt=None, last_success=success))
                values = self.backup_values()
                self.assertEqual(values['backup_has_success'], has_success)
                self.assertEqual(values['backup_last_success_timestamp_seconds'], 0)
                self.assertEqual(values['backup_last_attempt_timestamp_seconds'], 0)
                self.assertEqual(values['backup_healthy'], has_success)

    def test_latest_file_missing_symlink_directory_or_unowned_path_is_not_present(self):
        self.save(archive=False)
        self.assertEqual(self.backup_values()['backup_last_file_present'], 0)
        target = self.root / 'operator-file'
        target.write_bytes(b'keep operator file')
        archive = self.backups / ARCHIVE
        archive.symlink_to(target)
        self.assertEqual(self.backup_values()['backup_last_file_present'], 0)
        archive.unlink()
        archive.mkdir()
        self.assertEqual(self.backup_values()['backup_last_file_present'], 0)
        archive.rmdir()
        for name in ('../operator-file', str(target), 'manual.sqlite3', None):
            with self.subTest(name=name):
                self.save(dict(self.state(), last_file=name), archive=False)
                values = self.backup_values()
                self.assertEqual(values['backup_state_readable'], 1)
                self.assertEqual(values['backup_last_file_present'], 0)
                self.assertEqual(values['backup_healthy'], 0)
        self.assertEqual(target.read_bytes(), b'keep operator file')

    def test_one_status_read_per_scrape_and_no_backup_or_filesystem_mutation(self):
        state = self.save()
        before = {path.name: path.read_bytes() for path in self.backups.iterdir()}
        database_before = list(self.box.connection.iterdump())
        with patch('shirasu.metrics.read_backup_status', return_value=copy.deepcopy(state)) as reader, \
             patch('shirasu.backup_worker.create_backup', side_effect=AssertionError('must not create')), \
             patch('shirasu.backup_worker.verify_backup', side_effect=AssertionError('must not verify')), \
             patch('shirasu.backup_worker.worker_lock', side_effect=AssertionError('must not lock')), \
             patch('shirasu.backup_worker.Worker', side_effect=AssertionError('must not run')):
            self.assertEqual(self.collector.collect()['backup_healthy'], 1)
            reader.assert_called_once_with(self.backups)
        self.assertEqual({path.name: path.read_bytes() for path in self.backups.iterdir()}, before)
        self.assertEqual(list(self.box.connection.iterdump()), database_before)

    def test_fractional_epoch_timestamps_keep_precision(self):
        self.now = 1794226798.667286
        self.save()
        samples = dict(line.split() for line in self.collector.render().decode().splitlines()
                       if not line.startswith('#'))
        self.assertEqual(float(samples['shirasu_backup_heartbeat_timestamp_seconds']), self.now)
        self.assertEqual(float(samples['shirasu_backup_last_success_timestamp_seconds']), self.now - 10)

    def test_http_keeps_delivery_metrics_and_exporter_health_when_backup_status_is_bad(self):
        self.save()
        (self.backups / 'status.json').write_text('private-broken-status', encoding='utf-8')
        with MetricsServer(('127.0.0.1', 0), self.collector) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path in ('/metrics', '/healthz'):
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                    try:
                        connection.request('GET', path)
                        response = connection.getresponse()
                        body = response.read()
                        self.assertEqual(response.status, 200)
                        if path == '/metrics':
                            self.assertIn(b'shirasu_state_readable 1\n', body)
                            self.assertIn(b'shirasu_backup_state_readable 0\n', body)
                            self.assertNotIn(b'private-broken-status', body)
                        else:
                            self.assertEqual(body, b'ok\n')
                    finally:
                        connection.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
