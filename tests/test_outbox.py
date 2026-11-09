import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from outbox import Outbox, event_id_prefix, read_event, read_history, read_status


def signed_note(event_id):
    # Storage treats already-signed events as opaque JSON; signing is tested
    # separately against the real SDK by the application tests.
    return json.dumps({'id': event_id, 'content': '通報', 'sig': 'test-signature'})


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state' / 'outbox.sqlite3'

    def open(self, identity='account-a', **options):
        box = Outbox(self.path, identity, **options)
        self.addCleanup(box.close)
        return box

    def enqueue(self, box, key, now, event_id=None):
        event_id = event_id or key + '-event'
        return box.enqueue(key, signed_note(event_id), event_id, now)

    def test_unacknowledged_event_survives_restart_with_same_signature_and_id(self):
        box = self.open()
        self.assertEqual(self.enqueue(box, 'warning', 10), 'queued')
        attempted = box.next_due(10)
        # The remote side may have accepted this attempt.  Simulate a crash
        # before the ACK was recorded, without marking it failed or sent.
        box.close()

        restarted = self.open()
        retry = restarted.next_due(12)
        self.assertEqual(retry['event_id'], attempted['event_id'])
        self.assertEqual(retry['event_json'], attempted['event_json'])
        self.assertEqual(retry['state'], 'pending')
        restarted.mark_sent('warning', 13)
        restarted.close()

        acknowledged = self.open()
        self.assertIsNone(acknowledged.next_due(14))
        self.assertEqual(self.enqueue(acknowledged, 'warning', 14, 'different-event'), 'duplicate')
        self.assertEqual(acknowledged.snapshot(14)['counts'], {'pending': 0, 'sent': 1, 'expired': 0})

    def test_failed_attempt_and_retry_deadline_survive_restart(self):
        box = self.open()
        self.enqueue(box, 'warning', 10)
        box.mark_failed('warning', 'relay offline', 11, retry_delay=5)
        self.assertEqual(self.enqueue(box, 'warning', 12, 'replacement'), 'duplicate')
        box.close()

        restarted = self.open()
        self.assertIsNone(restarted.next_due(15))
        retry = restarted.next_due(16)
        self.assertEqual(retry['attempts'], 1)
        self.assertEqual(retry['event_id'], 'warning-event')
        self.assertEqual(retry['event_json'], signed_note('warning-event'))
        self.assertEqual(retry['created_at'], 10)
        self.assertEqual(retry['last_seen'], 12)
        self.assertEqual(retry['last_error'], 'relay offline')
        restarted.mark_failed('warning', 'x' * 2000, 16, retry_delay=1)
        retry = restarted.next_due(17)
        self.assertEqual(retry['attempts'], 2)
        self.assertEqual(len(retry['last_error']), 1000)

    def test_successful_deduplication_retention_tracks_last_receipt(self):
        box = self.open(dedup_seconds=10)
        self.enqueue(box, 'warning', 0)
        box.mark_sent('warning', 1)
        self.assertEqual(self.enqueue(box, 'warning', 9, 'replacement'), 'duplicate')
        self.assertEqual(box.maintain(18)['purged'], 0)
        self.assertEqual(box.maintain(19)['purged'], 1)
        self.assertEqual(self.enqueue(box, 'warning', 19, 'fresh-event'), 'queued')
        self.assertEqual(box.next_due(19)['event_id'], 'fresh-event')

    def test_repeated_receipt_does_not_extend_delivery_expiration(self):
        box = self.open(max_age_seconds=15)
        self.enqueue(box, 'warning', 10)
        self.assertEqual(self.enqueue(box, 'warning', 24, 'replacement'), 'duplicate')
        self.assertIsNone(box.next_due(25))
        self.assertEqual(box.snapshot(25)['counts']['expired'], 1)
        self.assertEqual(self.enqueue(box, 'warning', 26, 'replacement'), 'duplicate')
        self.assertIsNone(box.next_due(26))

    def test_late_acknowledgment_is_recorded_even_after_expiration(self):
        box = self.open(max_age_seconds=10)
        self.enqueue(box, 'warning', 0)
        self.assertIsNotNone(box.next_due(9))
        box.maintain(10)
        box.mark_sent('warning', 11)
        self.assertEqual(box.snapshot(11)['counts'], {'pending': 0, 'sent': 1, 'expired': 0})
        box.mark_failed('warning', 'late failed callback', 12, retry_delay=1)
        self.assertEqual(box.snapshot(12)['counts']['sent'], 1)
        self.assertIsNone(box.next_due(13))

    def test_unlimited_delivery_age_keeps_pending_events_despite_dedup_cleanup(self):
        box = self.open(max_age_seconds=0, dedup_seconds=10)
        self.enqueue(box, 'warning', 0)
        self.assertEqual(box.maintain(1_000_000), {'expired': 0, 'purged': 0})
        self.assertEqual(box.next_due(1_000_000)['event_id'], 'warning-event')
        box.mark_sent('warning', 1_000_000)
        self.assertEqual(box.maintain(1_000_001)['purged'], 0)
        self.assertEqual(self.enqueue(box, 'warning', 1_000_001, 'replacement'), 'duplicate')

    def test_retrying_old_warning_blocks_newer_updates_until_acknowledged(self):
        box = self.open()
        self.enqueue(box, 'warning', 1)
        self.enqueue(box, 'all-clear', 2)
        box.mark_failed('warning', 'offline', 3, retry_delay=10)
        self.assertIsNone(box.next_due(4))
        self.assertEqual(box.snapshot(4)['next_attempt_at'], 13)
        self.assertEqual(box.next_due(13)['key'], 'warning')
        box.mark_sent('warning', 13)
        self.assertEqual(box.next_due(13)['key'], 'all-clear')

    def test_fifo_uses_insertion_order_when_receipt_times_match(self):
        box = self.open()
        self.enqueue(box, 'z-first', 10)
        self.enqueue(box, 'a-second', 10)
        self.assertEqual(box.next_due(10)['key'], 'z-first')
        box.mark_sent('z-first', 10)
        self.assertEqual(box.next_due(10)['key'], 'a-second')

    def test_expired_fifo_head_does_not_block_newer_warning(self):
        box = self.open(max_age_seconds=10)
        self.enqueue(box, 'old-warning', 10)
        self.enqueue(box, 'new-warning', 15)
        box.mark_failed('old-warning', 'offline', 16, retry_delay=30)
        self.assertIsNone(box.next_due(19))
        self.assertEqual(box.next_due(20)['key'], 'new-warning')
        self.assertEqual(box.snapshot(20)['counts'], {'pending': 1, 'sent': 0, 'expired': 1})

    def test_full_queue_does_not_silently_count_an_unstored_event_as_delivered(self):
        box = self.open(max_pending=1)
        self.enqueue(box, 'first', 0)
        self.assertEqual(self.enqueue(box, 'first', 1, 'replacement'), 'duplicate')
        self.assertEqual(self.enqueue(box, 'second', 1), 'full')
        box.mark_sent('first', 2)
        self.assertEqual(self.enqueue(box, 'second', 2), 'queued')
        self.assertEqual(box.next_due(2)['event_id'], 'second-event')

    def test_accounts_have_separate_delivery_and_status_state(self):
        first = self.open('account-a')
        self.enqueue(first, 'same-report', 0, 'account-a-event')
        first.set_status('gpsd_connected', True, 1)
        second = self.open('account-b')
        self.assertIsNone(second.next_due(1))
        self.assertEqual(self.enqueue(second, 'same-report', 1, 'account-b-event'), 'queued')
        second.mark_sent('same-report', 2)
        self.assertEqual(first.next_due(2)['event_id'], 'account-a-event')
        self.assertEqual(read_status(self.path, now=2)['identity'], 'account-b')
        self.assertEqual(read_status(self.path, now=2)['status'], {})
        first_status = read_status(self.path, now=2, identity='account-a')
        self.assertEqual(first_status['status'], {'gpsd_connected': True})
        self.assertEqual(first_status['counts']['pending'], 1)

    def test_status_can_be_read_while_writer_is_running_and_after_restart(self):
        box = self.open()
        self.enqueue(box, 'warning', 10)
        box.set_status('last_error', {'message': '接続待ち'}, 12)
        box.set_status('gpsd_connected', False, 13)
        expected = box.snapshot(15)
        self.assertEqual(expected['oldest_pending_at'], 10)
        self.assertEqual(expected['oldest_pending_age_seconds'], 5)
        self.assertEqual(expected['status_updated_at']['gpsd_connected'], 13)
        self.assertEqual(read_status(self.path, now=15), expected)
        box.close()
        self.assertEqual(read_status(self.path, now=15), expected)

    def test_read_status_never_creates_missing_database(self):
        self.path.parent.mkdir()
        with self.assertRaises(sqlite3.OperationalError):
            read_status(self.path)
        self.assertFalse(self.path.exists())

    def test_seeded_history_is_deduplicated_and_does_not_replace_local_queue(self):
        box = self.open(max_age_seconds=10)
        self.assertTrue(box.seed_sent('published', signed_note('historical'), 'historical', 0, 100))
        self.assertFalse(box.seed_sent('published', signed_note('replacement'), 'replacement', 50, 100))
        self.assertEqual(self.enqueue(box, 'published', 101, 'newly-signed'), 'duplicate')
        self.assertIsNone(box.next_due(101))
        self.enqueue(box, 'local', 101)
        self.assertFalse(box.seed_sent('local', signed_note('remote'), 'remote', 50, 102))
        self.assertEqual(box.next_due(102)['event_id'], 'local-event')
        box.close()
        restarted = self.open(max_age_seconds=10)
        self.assertEqual(restarted.snapshot(103)['counts'], {'pending': 1, 'sent': 1, 'expired': 0})

    def test_history_returns_newest_receipts_first_with_stable_tie_order(self):
        box = self.open()
        for key, timestamp in [('old', 10), ('same-first', 20), ('same-second', 20), ('new', 30)]:
            self.enqueue(box, key, timestamp)
        history = read_history(self.path, limit=3)
        self.assertEqual(history['identity'], 'account-a')
        self.assertEqual(history['total'], 4)
        self.assertEqual([row['key'] for row in history['events']], ['new', 'same-second', 'same-first'])

    def test_history_filters_states_and_preserves_delivery_details(self):
        box = self.open(max_age_seconds=10)
        self.enqueue(box, 'sent', 10)
        box.mark_sent('sent', 11)
        self.enqueue(box, 'expired', 20)
        box.maintain(30)
        self.enqueue(box, 'retry', 31)
        box.mark_failed('retry', 'relay offline', 32, retry_delay=5)
        self.enqueue(box, 'new', 33)

        pending = read_history(self.path, state='pending', limit=1)
        self.assertEqual(pending['total'], 2)
        self.assertEqual(pending['events'][0]['key'], 'new')
        retry = read_history(self.path, state='pending')['events'][1]
        self.assertEqual(retry, {
            'key': 'retry', 'event_id': 'retry-event', 'event_json': signed_note('retry-event'),
            'state': 'pending', 'attempts': 1, 'created_at': 31, 'last_seen': 31,
            'next_attempt_at': 37, 'sent_at': None, 'last_error': 'relay offline',
        })
        sent = read_history(self.path, state='sent')
        self.assertEqual(sent['total'], 1)
        self.assertEqual(sent['events'][0]['sent_at'], 11)
        expired = read_history(self.path, state='expired')
        self.assertEqual(expired['total'], 1)
        self.assertEqual(expired['events'][0]['key'], 'expired')

    def test_history_default_limit_and_upper_boundary_do_not_hide_total(self):
        box = self.open()
        for index in range(25):
            self.enqueue(box, f'report-{index}', index)
        history = read_history(self.path)
        self.assertEqual(history['total'], 25)
        self.assertEqual(len(history['events']), 20)
        self.assertEqual(len(read_history(self.path, limit=1000)['events']), 25)

    def test_history_uses_current_account_and_binds_explicit_identity(self):
        first = self.open('account-a')
        self.enqueue(first, 'same-report', 10, 'first-event')
        second = self.open('account-b')
        self.enqueue(second, 'same-report', 11, 'second-event')
        self.assertEqual(read_history(self.path)['events'][0]['event_id'], 'second-event')
        self.assertEqual(read_history(self.path, identity='account-a')['events'][0]['event_id'], 'first-event')
        self.assertEqual(read_history(self.path, identity="' OR 1=1 --")['total'], 0)

    def test_history_never_runs_maintenance_or_modifies_saved_state(self):
        box = self.open(max_age_seconds=10, dedup_seconds=20)
        self.enqueue(box, 'sent', 0)
        box.mark_sent('sent', 1)
        self.enqueue(box, 'pending', 2)
        box.set_status('running', True, 3)
        before = read_status(self.path, now=1000)
        records_before = list(box.connection.iterdump())
        history = read_history(self.path)
        self.assertEqual(history['total'], 2)
        self.assertEqual(history['events'][0]['state'], 'pending')
        self.assertEqual(read_status(self.path, now=1000), before)
        self.assertEqual(list(box.connection.iterdump()), records_before)
        box.close()
        self.assertEqual(read_history(self.path), history)

    def test_history_count_and_rows_share_a_snapshot_during_concurrent_write(self):
        box = self.open()
        self.enqueue(box, 'first', 10)
        connect = sqlite3.connect

        def open_reader(*args, **kwargs):
            connection = connect(*args, **kwargs)

            def concurrent_write(statement):
                if statement.lstrip().startswith('SELECT key,'):
                    self.enqueue(box, 'concurrent', 11)

            connection.set_trace_callback(concurrent_write)
            return connection

        with patch('outbox.sqlite3.connect', side_effect=open_reader):
            history = read_history(self.path)
        self.assertEqual(history['total'], 1)
        self.assertEqual([row['key'] for row in history['events']], ['first'])
        self.assertEqual(read_history(self.path)['total'], 2)

    def test_history_missing_database_and_invalid_filters_create_nothing(self):
        for options in ({'state': 'unknown'}, {'state': "pending' OR 1=1 --"},
                        *({'limit': value} for value in (0, -1, 1001, True, 1.5, '20'))):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    read_history(self.path, **options)
        with self.assertRaises(sqlite3.OperationalError):
            read_history(self.path)
        self.assertFalse(self.path.parent.exists())

    def test_history_rejects_unknown_schema_and_uninitialized_identity(self):
        box = self.open()
        box.connection.execute('PRAGMA user_version = 99')
        with self.assertRaisesRegex(RuntimeError, 'Unsupported outbox database version'):
            read_history(self.path)
        box.connection.execute('PRAGMA user_version = 1')
        with box.connection:
            box.connection.execute("DELETE FROM metadata WHERE name = 'current_identity'")
        with self.assertRaisesRegex(RuntimeError, 'identity has not been initialized'):
            read_history(self.path)

    def test_event_lookup_accepts_full_id_and_unique_history_prefix_in_either_case(self):
        box = self.open()
        event_id = 'a1b2c3d4e5f6' + '7' * 52
        self.enqueue(box, 'warning', 10, event_id)
        box.mark_failed('warning', 'relay offline', 11, 5)
        expected = {'identity': box.identity, 'event': read_history(self.path)['events'][0]}
        for selector in (event_id, event_id.upper(), event_id[:12], event_id[:12].upper(), event_id[:63]):
            with self.subTest(selector=selector):
                self.assertEqual(read_event(self.path, selector), expected)
        self.assertEqual(event_id_prefix(event_id[:12].upper()), event_id[:12])

    def test_event_lookup_rejects_missing_and_ambiguous_ids_without_selecting_a_row(self):
        box = self.open()
        prefix = 'abcdef012345'
        first_id = prefix + 'a' * 52
        second_id = prefix + 'b' * 52
        self.enqueue(box, 'first', 10, first_id)
        self.enqueue(box, 'second', 11, second_id)
        with self.assertRaisesRegex(ValueError, '複数.*完全なイベントID'):
            read_event(self.path, prefix)
        with self.assertRaisesRegex(ValueError, '保存済みの記録がありません'):
            read_event(self.path, 'f' * 64)
        self.assertEqual(read_event(self.path, first_id)['event']['key'], 'first')
        self.assertEqual(read_event(self.path, second_id[:13])['event']['key'], 'second')
        # Even a malformed stored ID cannot turn a complete-ID lookup into a
        # prefix lookup that returns a different event.
        self.enqueue(box, 'long-id', 12, 'c' * 65)
        with self.assertRaisesRegex(ValueError, '保存済みの記録がありません'):
            read_event(self.path, 'c' * 64)

    def test_event_lookup_uses_current_identity_and_keeps_other_accounts_separate(self):
        first = self.open('account-a')
        first_id = 'abcdef012345' + 'a' * 52
        second_id = 'abcdef012345' + 'b' * 52
        self.enqueue(first, 'first', 10, first_id)
        second = self.open('account-b')
        self.enqueue(second, 'second', 11, second_id)
        self.assertEqual(read_event(self.path, first_id[:12]), {
            'identity': 'account-b', 'event': read_history(self.path)['events'][0],
        })
        self.assertEqual(read_event(self.path, first_id[:12], identity='account-a')['event']['event_id'], first_id)
        for identity in (None, "' OR 1=1 --"):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(ValueError, '保存済みの記録がありません'):
                    read_event(self.path, first_id, identity=identity)

    def test_event_lookup_preserves_overdue_pending_and_old_sent_records_without_writes(self):
        box = self.open(max_age_seconds=10, dedup_seconds=20)
        sent_id, pending_id = 'a' * 64, 'b' * 64
        self.enqueue(box, 'sent', 0, sent_id)
        box.mark_sent('sent', 1)
        self.enqueue(box, 'pending', 2, pending_id)
        box.mark_failed('pending', 'relay offline', 3, 5)
        box.set_status('running', True, 3)
        before = read_status(self.path, now=1000)
        records_before = list(box.connection.iterdump())
        sent = read_event(self.path, sent_id)
        pending = read_event(self.path, pending_id)
        self.assertEqual(sent['event']['state'], 'sent')
        self.assertEqual(pending['event']['state'], 'pending')
        self.assertEqual(pending['event']['event_json'], signed_note(pending_id))
        self.assertEqual(pending['event']['attempts'], 1)
        self.assertEqual(read_status(self.path, now=1000), before)
        self.assertEqual(list(box.connection.iterdump()), records_before)
        box.close()
        self.assertEqual(read_event(self.path, sent_id), sent)
        self.assertEqual(read_event(self.path, pending_id), pending)

    def test_event_lookup_validates_ids_before_opening_storage_and_never_creates_missing_database(self):
        for value in ('', 'a' * 11, 'a' * 65, 'g' * 12, 'a' * 11 + '%',
                      'a' * 11 + '_', "' OR 1=1 --", 'a' * 12 + '\n', None, 12, True):
            with self.subTest(value=value):
                with patch('outbox.sqlite3.connect') as connect:
                    with self.assertRaisesRegex(ValueError, '12〜64文字の16進数'):
                        read_event(self.path, value)
                    connect.assert_not_called()
        with self.assertRaises(sqlite3.OperationalError):
            read_event(self.path, 'a' * 64)
        self.assertFalse(self.path.parent.exists())

    def test_event_lookup_rejects_unknown_schema_and_uninitialized_identity(self):
        box = self.open()
        event_id = 'a' * 64
        self.enqueue(box, 'warning', 10, event_id)
        box.connection.execute('PRAGMA user_version = 99')
        with self.assertRaisesRegex(RuntimeError, 'Unsupported outbox database version'):
            read_event(self.path, event_id)
        box.connection.execute('PRAGMA user_version = 1')
        with box.connection:
            box.connection.execute("DELETE FROM metadata WHERE name = 'current_identity'")
        with self.assertRaisesRegex(RuntimeError, 'identity has not been initialized'):
            read_event(self.path, event_id)


if __name__ == '__main__':
    unittest.main()
