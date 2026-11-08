import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from outbox import Outbox, read_status


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


if __name__ == '__main__':
    unittest.main()
