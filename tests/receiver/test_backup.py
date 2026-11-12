"""Exercise real SQLite snapshots and signed events without production state."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from nostr_sdk import Event, EventBuilder, Keys, Kind

from shirasu import backup
from shirasu.outbox import Outbox, read_history


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-backup-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'live' / 'state.sqlite3'
        self.destination = self.root / 'snapshot.sqlite3'
        self.keys = Keys.generate()
        self.identity = self.keys.public_key().to_hex()
        self.box = Outbox(self.source, self.identity)
        self.addCleanup(self.box.close)

    def enqueue(self, key, *, box=None, keys=None, now=1000, content=None):
        box = self.box if box is None else box
        keys = self.keys if keys is None else keys
        event = EventBuilder(Kind(1), key if content is None else content).finalize(keys)
        box.enqueue(key, event.as_json(), event.id().to_hex(), now)
        return event

    def mutate_archive(self, statement, arguments=()):
        with sqlite3.connect(self.destination) as connection:
            connection.execute(statement, arguments)

    def test_online_backup_preserves_wal_records_and_all_accounts_without_source_changes(self):
        pending = self.enqueue('pending')
        self.box.mark_failed('pending', 'relay offline', 1001, 5)
        self.enqueue('sent')
        self.box.mark_sent('sent', 1002)
        self.box.set_status('running', True, 1003)
        self.box.set_status('heartbeat', 1003, 1003)
        other_keys = Keys.generate()
        other_identity = other_keys.public_key().to_hex()
        with Outbox(self.source, other_identity) as other:
            self.enqueue('expired', box=other, keys=other_keys)
            other.maintain(1900)
            other.set_status('running', False, 1901)
            before = list(self.box.connection.iterdump())
            self.assertGreater(Path(str(self.source) + '-wal').stat().st_size, 0)
            result = backup.create_backup(self.source, self.destination)
            self.assertEqual(list(self.box.connection.iterdump()), before)

        self.assertEqual(result['identity'], other_identity)
        self.assertEqual(result['counts'], {'pending': 1, 'sent': 1, 'expired': 1})
        self.assertEqual(result['events_verified'], 3)
        self.assertEqual(result['schema_version'], 1)
        self.assertGreater(result['backup_created_at'], 1901)
        self.assertEqual(result['status_updated_at'], 1901)
        accounts = {row['identity']: row for row in result['accounts']}
        self.assertEqual(accounts[self.identity]['counts'], {'pending': 1, 'sent': 1, 'expired': 0})
        self.assertEqual(accounts[self.identity]['status_updated_at'], 1003)
        self.assertEqual(accounts[other_identity]['status_updated_at'], 1901)
        self.assertEqual(os.stat(self.destination).st_mode & 0o777, 0o600)
        self.assertEqual(backup.verify_backup(self.destination), result)
        with sqlite3.connect(self.destination) as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(connection.execute('SELECT event_json FROM events WHERE key = ?',
                                                ('pending',)).fetchone()[0], pending.as_json())
        for path in backup._sidecars(self.destination):
            self.assertFalse(path.exists())

    def test_snapshot_remains_consistent_when_writer_commits_during_copy(self):
        self.enqueue('before', content='large report ' * 200000)
        original_reader = backup._read_connection
        committed = []

        @contextmanager
        def reader(path, identity):
            with original_reader(path, identity) as (source, account):
                if Path(path) != self.source:
                    yield source, account
                    return

                class Source:
                    def backup(inner, target, **options):
                        progress = options['progress']

                        def step(status, remaining, total):
                            if remaining and not committed:
                                self.enqueue('during', now=1001)
                                committed.append(True)
                            progress(status, remaining, total)

                        source.backup(target, **dict(options, progress=step))

                yield Source(), account

        with patch('shirasu.backup._read_connection', side_effect=reader):
            result = backup.create_backup(self.source, self.destination)
        self.assertTrue(committed)
        self.assertEqual(result['events_verified'], 1)
        self.assertEqual(read_history(self.source)['total'], 2)
        self.assertEqual([row['key'] for row in read_history(self.destination)['events']], ['before'])

    def test_single_file_can_restore_pending_signature_and_sent_dedup_without_original_directory(self):
        pending = self.enqueue('pending')
        sent = self.enqueue('sent')
        self.box.mark_sent('sent', 1001)
        self.box.mark_failed('pending', 'retry later', 1001, 1)
        result = backup.create_backup(self.source, self.destination)
        self.box.close()
        self.source.parent.rename(self.root / 'unavailable-original')
        self.assertEqual(backup.verify_backup(self.destination), result)

        restored_path = self.root / 'restored' / 'state.sqlite3'
        restored_path.parent.mkdir()
        shutil.copyfile(self.destination, restored_path)
        with Outbox(restored_path, self.identity) as restored:
            due = restored.next_due(1002)
            self.assertEqual(due['event_id'], pending.id().to_hex())
            self.assertEqual(due['event_json'], pending.as_json())
            self.assertTrue(Event.from_json(due['event_json']).verify())
            self.assertEqual(due['attempts'], 1)
            replacement = EventBuilder(Kind(1), 'would be a duplicate').finalize(self.keys)
            self.assertEqual(restored.enqueue('sent', replacement.as_json(), replacement.id().to_hex(), 1002),
                             'duplicate')
            self.assertEqual(restored.connection.execute('SELECT event_json FROM events WHERE key = ?',
                                                        ('sent',)).fetchone()[0], sent.as_json())
            restored.mark_sent('pending', 1003)
            self.assertEqual(restored.snapshot(1003)['counts'], {'pending': 0, 'sent': 2, 'expired': 0})

    def test_nankai_pages_survive_verified_backup_and_finish_after_restore(self):
        from azarashi.reports import dcr
        from shirasu.reports.nankai import NankaiCollector, nankai_summary
        from tests.receiver.test_nankai_publication import nankai_page
        for name, value in (('completed', False), ('reports', {}), ('announcement', None)):
            self.enterContext(patch.object(dcr.NankaiTroughEarthquake, name, value))
        body = 'A訓練用の架空の情報です。'
        data = body.encode()
        chunks = [data[start:start + 18] for start in range(0, len(data), 18)]
        collector = NankaiCollector(self.box)
        collector.accept(nankai_page(chunks[0], number=1, total=len(chunks)), 1000)
        before = list(self.box.connection.iterdump())
        result = backup.create_backup(self.source, self.destination)
        self.assertEqual(list(self.box.connection.iterdump()), before)
        self.assertEqual(backup.verify_backup(self.destination), result)
        restored_path = self.root / 'restored.sqlite3'
        shutil.copyfile(self.destination, restored_path)
        with Outbox(restored_path, self.identity) as restored:
            resumed = NankaiCollector(restored)
            for number, chunk in enumerate(chunks[1:], 2):
                resumed.accept(nankai_page(chunk, number=number, total=len(chunks)), 1001)
            batch = resumed.due(1001)
            self.assertIsNotNone(batch)
            text = nankai_summary(batch)
            self.assertIn(body, text)
            self.assertNotIn('受信中', text)
            self.assertEqual(text.count('\nNMEA: '), len(chunks))
            event = EventBuilder(Kind(1), text).finalize(self.keys)
            self.assertEqual(resumed.complete(batch, event.as_json(), event.id().to_hex(), 1001), 'queued')
            self.assertEqual(restored.next_due(1001)['event_json'], event.as_json())
        self.mutate_archive("UPDATE nankai_reports SET nmea = 'invalid'")
        with self.assertRaises(ValueError):
            backup.verify_backup(self.destination)

    def test_empty_initialized_database_is_a_valid_backup(self):
        result = backup.create_backup(self.source, self.destination)
        self.assertEqual(result['counts'], {'pending': 0, 'sent': 0, 'expired': 0})
        self.assertEqual(result['events_verified'], 0)
        self.assertIsNone(result['status_updated_at'])
        self.assertEqual(result['accounts'], [{'identity': self.identity,
                                              'counts': result['counts'], 'status_updated_at': None}])

    def test_existing_files_links_source_and_sidecars_are_never_overwritten(self):
        self.enqueue('pending')
        self.destination.write_bytes(b'keep existing archive')
        linked = self.root / 'linked.sqlite3'
        linked.symlink_to(self.source)
        dangling = self.root / 'dangling.sqlite3'
        dangling.symlink_to(self.root / 'missing-target')
        sidecar_destination = self.root / 'sidecars.sqlite3'
        sidecar = Path(str(sidecar_destination) + '-wal')
        sidecar.write_bytes(b'keep WAL')
        source_before = list(self.box.connection.iterdump())
        for destination in (self.destination, linked, dangling, self.source, sidecar_destination):
            with self.subTest(destination=destination):
                with self.assertRaises(FileExistsError):
                    backup.create_backup(self.source, destination)
        self.assertEqual(self.destination.read_bytes(), b'keep existing archive')
        self.assertEqual(sidecar.read_bytes(), b'keep WAL')
        self.assertTrue(linked.is_symlink())
        self.assertTrue(dangling.is_symlink())
        self.assertFalse(sidecar_destination.exists())
        self.assertEqual(list(self.box.connection.iterdump()), source_before)

    def test_destination_created_during_backup_is_preserved(self):
        real_verify = backup.verify_backup

        def verify(path):
            result = real_verify(path)
            self.destination.write_bytes(b'created by another process')
            return result

        with patch('shirasu.backup.verify_backup', side_effect=verify):
            with self.assertRaises(FileExistsError):
                backup.create_backup(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b'created by another process')
        self.assertEqual(list(self.root.glob('.snapshot.sqlite3.tmp-*')), [])

    def test_absent_source_sidecar_paths_cannot_be_used_as_destinations(self):
        self.box.close()
        for destination in backup._sidecars(self.source):
            with self.subTest(destination=destination):
                self.assertFalse(destination.exists())
                with self.assertRaisesRegex(ValueError, '元DBまたはその付随ファイル'):
                    backup.create_backup(self.source, destination)
                self.assertFalse(destination.exists())

    def test_missing_source_or_directory_and_failed_validation_leave_no_backup(self):
        missing = self.root / 'missing' / 'state.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            backup.create_backup(missing, self.destination)
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(FileNotFoundError):
            backup.create_backup(self.source, missing)
        self.assertFalse(missing.parent.exists())
        with patch('shirasu.backup.verify_backup', side_effect=ValueError('invalid copied signature')):
            with self.assertRaisesRegex(ValueError, 'invalid copied signature'):
                backup.create_backup(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.snapshot.sqlite3.tmp-*')), [])

    def test_backup_timeout_and_publication_error_clean_temporary_files(self):
        with patch('shirasu.backup.BACKUP_TIMEOUT_SECONDS', 0):
            with self.assertRaises(TimeoutError):
                backup.create_backup(self.source, self.destination)
        with patch('shirasu.backup.os.link', side_effect=OSError('publication failed')):
            with self.assertRaisesRegex(OSError, 'publication failed'):
                backup.create_backup(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.snapshot.sqlite3.tmp-*')), [])

    def test_verification_rejects_missing_corrupt_wal_and_unmarked_databases_without_modification(self):
        with self.assertRaises(FileNotFoundError):
            backup.verify_backup(self.destination)
        self.assertFalse(self.destination.exists())
        self.destination.write_bytes(b'not a database')
        with self.assertRaises(ValueError):
            backup.verify_backup(self.destination)
        self.assertEqual(self.destination.read_bytes(), b'not a database')
        before = list(self.box.connection.iterdump())
        with self.assertRaisesRegex(ValueError, '付随|WAL'):
            backup.verify_backup(self.source)
        self.assertEqual(list(self.box.connection.iterdump()), before)
        self.destination.unlink()
        backup.create_backup(self.source, self.destination)
        self.mutate_archive("DELETE FROM metadata WHERE name = 'backup_format_version'")
        before = self.destination.read_bytes()
        with self.assertRaisesRegex(ValueError, '形式'):
            backup.verify_backup(self.destination)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_verification_rejects_unsupported_version_missing_columns_and_bad_metadata(self):
        backup.create_backup(self.source, self.destination)
        original = self.destination.read_bytes()
        for statement in ('PRAGMA user_version = 99', 'ALTER TABLE events RENAME COLUMN event_json TO wrong',
                          "UPDATE metadata SET value = 'NaN' WHERE name = 'backup_created_at'",
                          "UPDATE metadata SET value = 'invalid identity' WHERE name = 'current_identity'"):
            with self.subTest(statement=statement):
                self.destination.write_bytes(original)
                self.mutate_archive(statement)
                with self.assertRaises((RuntimeError, ValueError)):
                    backup.verify_backup(self.destination)

    def test_verification_rejects_invalid_event_signature_id_and_author(self):
        event = self.enqueue('pending')
        backup.create_backup(self.source, self.destination)
        original = self.destination.read_bytes()
        tampered = json.loads(event.as_json())
        tampered['content'] = 'modified after signing'
        for column, value in (('event_json', json.dumps(tampered)), ('event_json', 'broken JSON'),
                              ('event_id', '0' * 64), ('identity', '1' * 64)):
            with self.subTest(column=column, value=value):
                self.destination.write_bytes(original)
                self.mutate_archive(f'UPDATE events SET {column} = ?', (value,))
                with self.assertRaisesRegex(ValueError, '署名イベント'):
                    backup.verify_backup(self.destination)

    def test_verification_rejects_invalid_delivery_fields_and_status_json(self):
        self.enqueue('pending')
        self.box.set_status('heartbeat', 1000, 1000)
        backup.create_backup(self.source, self.destination)
        original = self.destination.read_bytes()
        for statement in ('UPDATE events SET attempts = -1', "UPDATE events SET created_at = 'not a time'",
                          'UPDATE events SET sent_at = 1000', "UPDATE events SET key = ''",
                          "UPDATE status SET value = 'NaN'", "UPDATE status SET value = 'broken JSON'"):
            with self.subTest(statement=statement):
                self.destination.write_bytes(original)
                self.mutate_archive(statement)
                with self.assertRaises(ValueError):
                    backup.verify_backup(self.destination)

    def test_invalid_live_signature_is_not_published_as_a_successful_backup(self):
        self.enqueue('pending')
        with self.box.connection:
            self.box.connection.execute("UPDATE events SET event_id = ?", ('0' * 64,))
        before = list(self.box.connection.iterdump())
        with self.assertRaisesRegex(ValueError, '署名イベント'):
            backup.create_backup(self.source, self.destination)
        self.assertEqual(list(self.box.connection.iterdump()), before)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.snapshot.sqlite3.tmp-*')), [])


if __name__ == '__main__':
    unittest.main()
