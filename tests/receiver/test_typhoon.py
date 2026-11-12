import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import azarashi

from shirasu.outbox import Outbox
from shirasu.reports.typhoon import TyphoonCollector, report_identity, typhoon_summary, validate_storage


# One real six-point issuance. The separate receive timestamps matter because
# QZQSM omits the year and the decoder infers it from reception time.
FRAMES = (
    (1794228531340, '$QZQSM,56,C6ADE5A5990002580100008004619010EC80F511B200000000000012D31A380*0C'),
    (1794228535353, '$QZQSM,57,53ADE5A59900028003001880046320113400F29437000000000000130F45394*7B'),
    (1794228539354, '$QZQSM,57,9AADE5A599000298030030800465E011AA00F3D1B20000000000001373573A0*70'),
    (1794228543351, '$QZQSM,56,C6ADE5A5990002D803006080006D9012CA00F78F2800000000000013A185404*70'),
    (1794228547341, '$QZQSM,57,53ADE5A59900031803009080006C0013CA00FA8A1E00000000000013E0A1018*08'),
    (1794228551356, '$QZQSM,57,9AADE5A5990003580300C08000677013EA00FC00000000000000001030538B8*0A'),
)


def report(index=0, changes=(), received_ms=None, satellite=None):
    """Mutate actual encoded fields and recalculate both protocol checksums."""
    ms, nmea = FRAMES[index]
    if changes or satellite is not None:
        _, original_satellite, encoded = nmea.split('*')[0].split(',')
        value = int(encoded + '0', 16)

        def set_bits(offset, width, field):
            nonlocal value
            shift = 256 - offset - width
            mask = ((1 << width) - 1) << shift
            value = (value & ~mask) | (field << shift)

        for offset, width, field in changes:
            set_bits(offset, width, field)
        data = value.to_bytes(32, 'big')
        crc = 0
        remaining = 226
        for byte in data[:28] + bytes((data[28] & 0xC0,)):
            crc ^= byte << 16
            for _ in range(8):
                crc <<= 1
                if crc & 0x1000000:
                    crc ^= 0x1864CFB
                remaining -= 1
                if not remaining:
                    break
        set_bits(226, 24, crc & 0xFFFFFF)
        body = f'QZQSM,{satellite or original_satellite},{value.to_bytes(32, "big").hex()[:-1].upper()}'
        checksum = 0
        for char in body:
            checksum ^= ord(char)
        nmea = f'${body}*{checksum:02X}'
    return azarashi.decode(nmea, timestamp=datetime.fromtimestamp((ms if received_ms is None else received_ms) / 1000, timezone.utc))


def signed_note(event_id='summary-event'):
    # Event signatures are validated by the application and backup tests. This
    # layer stores opaque signed JSON and guarantees transactional durability.
    return json.dumps({'id': event_id, 'content': '台風まとめ', 'sig': 'test-signature'})


class TyphoonCollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'shirasu.outbox.sqlite3'

    def open(self, identity='account-a', wait=3, collect=True, **options):
        box = Outbox(self.path, identity, **options)
        self.addCleanup(box.close)
        return box, TyphoonCollector(box, wait, collect=collect)

    def complete(self, collector, now, event_id='summary-event'):
        batch = collector.due(now)
        self.assertIsNotNone(batch)
        self.assertEqual(collector.complete(batch, signed_note(event_id), event_id, now), 'queued')
        return batch

    def test_six_points_share_first_deadline_and_preserve_received_timestamps(self):
        box, collector = self.open(wait=30)
        for index in range(6):
            self.assertEqual(collector.accept(report(index), index * 4), 'buffered')
        self.assertIsNone(collector.due(29))
        batch = collector.due(30)
        self.assertEqual(len(batch.reports), 6)
        self.assertEqual(batch.entries, tuple((nmea, ms) for ms, nmea in FRAMES))
        self.assertEqual([int(item.timestamp.timestamp() * 1000) for item in batch.reports], [ms for ms, _ in FRAMES])
        self.assertEqual(batch.created_at, 0)
        self.assertEqual(batch.deadline, 30)
        self.assertFalse(batch.update)
        self.assertEqual(collector.complete(batch, signed_note(), 'summary-event', 30), 'queued')
        self.assertEqual(box.next_due(30)['created_at'], 30)
        self.assertFalse(collector.has_pending())
        validate_storage(box.connection)

    def test_partial_collection_publishes_without_waiting_for_six_points(self):
        _, collector = self.open()
        collector.accept(report(3), 0)
        batch = self.complete(collector, 3)
        self.assertEqual(len(batch.reports), 1)
        self.assertEqual(batch.reports[0].reference_time, report(3).reference_time)

    def test_repeated_satellite_does_not_extend_deadline_or_replace_provenance(self):
        _, collector = self.open()
        original = report()
        repeated = report(changes=((0, 8, 0x53),), satellite=57, received_ms=FRAMES[0][0] + 60_000)
        self.assertEqual(report_identity(original), report_identity(repeated))
        collector.accept(original, 0)
        self.assertEqual(collector.accept(repeated, 2), 'duplicate')
        batch = collector.due(3)
        self.assertEqual(batch.entries, ((original.nmea, FRAMES[0][0]),))
        self.assertEqual(batch.deadline, 3)

    def test_restart_preserves_batch_and_signed_retry(self):
        box, collector = self.open()
        collector.accept(report(2), 0)
        collector.accept(report(), 1)
        before = collector.due(3)
        box.close()
        restarted, collector = self.open()
        after = collector.due(4)
        self.assertEqual((before.key, before.entries, before.deadline), (after.key, after.entries, after.deadline))
        self.complete(collector, 4)
        event = restarted.next_due(4)
        restarted.mark_failed(before.key, 'offline', 4, retry_delay=10)
        restarted.close()
        retried, collector = self.open()
        self.assertIsNone(collector.due(20))
        self.assertEqual(collector.accept(report(), 20), 'duplicate')
        self.assertEqual(retried.next_due(20)['event_json'], event['event_json'])
        self.assertEqual(retried.next_due(20)['event_id'], event['event_id'])

    def test_late_unique_point_becomes_additional_batch(self):
        box, collector = self.open()
        collector.accept(report(), 0)
        first = self.complete(collector, 3)
        box.mark_sent(first.key, 3)
        self.assertEqual(collector.accept(report(), 10), 'duplicate')
        self.assertEqual(collector.accept(report(1), 11), 'buffered')
        self.assertIsNone(collector.due(13))
        late = collector.due(14)
        self.assertTrue(late.update)
        self.assertNotEqual(late.key, first.key)
        self.assertEqual(late.entries, tuple((nmea, ms) for ms, nmea in FRAMES[:2]))
        self.assertEqual(late.report_keys, (report_identity(report(1)),))
        self.assertEqual(late.created_at, 11)
        self.complete(collector, 14, 'late-event')
        self.assertEqual(box.snapshot(14)['counts'], {'pending': 1, 'sent': 1, 'expired': 0})
        self.assertEqual(box.connection.execute('SELECT event_json FROM events WHERE key = ?', (first.key,)).fetchone()[0], signed_note())
        validate_storage(box.connection)

    def test_late_changed_slot_replaces_old_point_in_full_track(self):
        box, collector = self.open()
        collector.accept(report(), 0)
        collector.accept(report(1), 1)
        first = self.complete(collector, 3)
        changed = report(changes=((143, 11, 975),))
        self.assertEqual(collector.accept(changed, 4), 'buffered')
        batch = collector.due(7)
        self.assertEqual(batch.entries, ((changed.nmea, FRAMES[0][0]), (FRAMES[1][1], FRAMES[1][0])))
        self.assertEqual(batch.report_keys, (report_identity(changed),))
        self.assertEqual(collector.accept(report(), 7), 'duplicate')
        self.complete(collector, 7, 'changed')
        self.assertEqual(box.connection.execute('SELECT event_json FROM events WHERE key = ?', (first.key,)).fetchone()[0], signed_note())
        validate_storage(box.connection)

    def test_maximum_slots_includes_previously_completed_points(self):
        box, collector = self.open(wait=100, max_pending=100)
        for minute in range(32):
            self.assertEqual(collector.accept(report(changes=((63, 6, minute),)), minute), 'buffered')
        self.assertEqual(collector.accept(report(changes=((63, 6, 32),)), 32), 'full')
        batch = self.complete(collector, 100)
        self.assertEqual(len(batch.entries), 32)
        self.assertEqual(collector.accept(report(changes=((63, 6, 32),)), 101), 'full')
        changed = report(changes=((63, 6, 31), (143, 11, 975)))
        self.assertEqual(collector.accept(changed, 102), 'buffered')
        late = collector.due(202)
        self.assertEqual(len(late.entries), 32)
        self.assertEqual(late.report_keys, (report_identity(changed),))
        validate_storage(box.connection)

    def test_new_version_replaces_pending_same_slot_and_old_batch_is_stale(self):
        box, collector = self.open()
        original = report()
        changed = report(changes=((143, 11, 975),))
        collector.accept(original, 0)
        before = collector.due(3)
        self.assertEqual(collector.accept(changed, 4), 'buffered')
        self.assertEqual(collector.pending_count(), 1)
        self.assertEqual(collector.accept(original, 5), 'duplicate')
        self.assertEqual(collector.complete(before, signed_note(), 'old-event', 5), 'stale')
        self.assertIsNone(box.next_due(5))
        after = collector.due(5)
        self.assertEqual(after.deadline, 3)
        self.assertEqual(after.entries[0][0], changed.nmea)
        self.assertEqual(after.created_at, 4)
        validate_storage(box.connection)

    def test_correction_supersedes_old_pending_and_does_not_accept_late_original(self):
        box, collector = self.open(max_pending=1)
        collector.accept(report(), 0)
        corrected = report(changes=((41, 2, 1),))
        self.assertEqual(collector.accept(corrected, 1), 'buffered')
        self.assertEqual(collector.pending_count(), 1)
        self.assertEqual(collector.accept(report(1), 2), 'duplicate')
        batch = collector.due(4)
        self.assertEqual(batch.reports[0].information_type_no, 1)
        self.assertIn('【訂正】', typhoon_summary(batch.reports))
        validate_storage(box.connection)

    def test_new_issue_supersedes_old_pending_and_older_cancel_cannot_clear_newer(self):
        box, collector = self.open(max_pending=1)
        collector.accept(report(), 0)
        newer = report(changes=((35, 6, 51),))
        self.assertEqual(collector.accept(newer, 1), 'buffered')
        self.assertEqual(collector.accept(report(changes=((41, 2, 2),)), 2), 'immediate')
        self.assertEqual(collector.pending_count(), 1)
        self.assertEqual(collector.accept(report(1), 2), 'duplicate')
        self.assertEqual(collector.due(4).entries[0][0], newer.nmea)
        validate_storage(box.connection)

    def test_cancel_is_immediate_clears_pending_and_blocks_unknown_older_points(self):
        box, collector = self.open()
        collector.accept(report(), 0)
        cancel = report(changes=((41, 2, 2),))
        self.assertEqual(collector.accept(cancel, 1), 'immediate')
        self.assertFalse(collector.has_pending())
        self.assertIsNone(collector.due(10))
        self.assertEqual(collector.accept(report(2), 10), 'duplicate')
        self.assertEqual(collector.accept(report(changes=((35, 6, 51),)), 10), 'buffered')
        validate_storage(box.connection)

    def test_previously_posted_cancel_still_clears_collector_when_repeated(self):
        box, collector = self.open()
        cancel = report(changes=((41, 2, 2),))
        box.enqueue(report_identity(cancel), signed_note('cancel'), 'cancel', 0)
        box.mark_sent(report_identity(cancel), 0)
        collector.accept(report(), 1)
        self.assertEqual(collector.accept(cancel, 2), 'duplicate')
        self.assertFalse(collector.has_pending())
        self.assertEqual(collector.accept(report(1), 3), 'duplicate')

    def test_new_year_cancellation_supersedes_previous_year_issue(self):
        box, collector = self.open()
        december = int(datetime(2026, 12, 31, 23, 55, tzinfo=timezone.utc).timestamp() * 1000)
        january = int(datetime(2027, 1, 1, 0, 5, tzinfo=timezone.utc).timestamp() * 1000)
        original = report(changes=((21, 4, 12), (25, 5, 31), (30, 5, 23),
                                   (53, 5, 31), (58, 5, 23)), received_ms=december)
        cancel = report(changes=((21, 4, 1), (25, 5, 1), (30, 5, 0),
                                 (35, 6, 0), (41, 2, 2)), received_ms=january)
        self.assertEqual(original.report_time.year, 2026)
        self.assertEqual(cancel.report_time.year, 2027)
        self.assertIn('2027/01/01 08:50 JST', typhoon_summary((original,)))
        collector.accept(original, 0)
        self.assertEqual(collector.accept(cancel, 1), 'immediate')
        self.assertFalse(collector.has_pending())
        validate_storage(box.connection)

    def test_training_and_live_collections_are_isolated(self):
        box, collector = self.open()
        collector.accept(report(), 0)
        training = report(changes=((14, 3, 7),))
        collector.accept(training, 0)
        self.assertEqual(collector.pending_count(), 2)
        cancel = report(changes=((14, 3, 7), (41, 2, 2)))
        collector.accept(cancel, 1)
        self.assertEqual(collector.pending_count(), 1)
        self.assertEqual(collector.due(3).reports[0].report_classification_no, 3)
        self.assertIn('【訓練・試験】', typhoon_summary((training,)))
        validate_storage(box.connection)

    def test_unknown_metadata_falls_back_to_single_report(self):
        _, collector = self.open()
        for field in (((41, 2, 3),), ((69, 3, 0),), ((87, 7, 0),), ((14, 3, 0),)):
            with self.subTest(field=field):
                self.assertEqual(collector.accept(report(changes=field), 0), 'immediate')
        missing = copy.copy(report())
        missing.reference_time = None
        self.assertEqual(collector.accept(missing, 0), 'immediate')
        self.assertFalse(collector.has_pending())

    def test_existing_single_report_is_not_republished_after_enabling_collection(self):
        box, collector = self.open(dedup_seconds=10)
        key = report_identity(report())
        box.enqueue(key, signed_note(), 'single-event', 0)
        box.mark_sent(key, 0)
        self.assertEqual(collector.accept(report(), 9), 'duplicate')
        self.assertFalse(collector.has_pending())
        self.assertEqual(box.maintain(18)['purged'], 0)

    def test_capacity_counts_raw_rows_and_outbox_and_allows_replacements(self):
        box, collector = self.open(max_pending=2)
        box.enqueue('other', signed_note('other'), 'other', 0)
        self.assertEqual(collector.accept(report(), 0), 'buffered')
        self.assertEqual(collector.accept(report(1), 1), 'full')
        changed = report(changes=((143, 11, 975),))
        self.assertEqual(collector.accept(changed, 1), 'buffered')
        self.assertEqual(collector.pending_count(), 1)
        box.mark_sent('other', 2)
        self.assertEqual(collector.accept(report(1), 2), 'buffered')
        self.assertEqual(collector.pending_count(), 2)
        self.complete(collector, 3)
        self.assertEqual(box.snapshot(3)['counts']['pending'], 1)

    def test_enqueue_counts_raw_points_and_complete_rechecks_a_lowered_limit(self):
        box, collector = self.open(max_pending=1)
        collector.accept(report(), 0)
        batch = collector.due(3)
        self.assertEqual(box.enqueue('other', signed_note('other'), 'other', 3), 'full')
        box.max_pending = 2
        self.assertEqual(box.enqueue('other', signed_note('other'), 'other', 3), 'queued')
        box.max_pending = 1
        self.assertEqual(collector.complete(batch, signed_note(), 'summary', 3), 'full')
        self.assertEqual(collector.pending_count(), 1)
        box.mark_sent('other', 4)
        self.assertEqual(collector.complete(batch, signed_note(), 'summary', 4), 'queued')

    def test_atomic_rollback_keeps_raw_points_if_closing_batch_fails(self):
        box, collector = self.open()
        collector.accept(report(), 0)
        batch = collector.due(3)
        box.connection.execute("CREATE TRIGGER fail_close BEFORE UPDATE OF state ON typhoon_reports WHEN NEW.state = 'done' BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            collector.complete(batch, signed_note(), 'summary', 3)
        self.assertIsNone(box.next_due(3))
        self.assertEqual(collector.pending_count(), 1)
        box.connection.execute('DROP TRIGGER fail_close')
        self.assertEqual(collector.complete(batch, signed_note(), 'summary', 4), 'queued')
        self.assertFalse(collector.has_pending())

    def test_old_unsent_collection_expires_after_downtime(self):
        box, collector = self.open(max_age_seconds=10)
        collector.accept(report(), 0)
        batch = collector.due(3)
        self.assertEqual(collector.accept(report(), 9), 'duplicate')
        self.assertIsNone(collector.due(10))
        self.assertEqual(collector.complete(batch, signed_note(), 'stale', 10), 'stale')
        self.assertEqual(collector.accept(report(), 11), 'duplicate')
        self.assertFalse(collector.has_pending())
        self.assertIsNone(box.next_due(11))
        validate_storage(box.connection)

    def test_unlimited_delivery_age_preserves_pending_collection(self):
        _, collector = self.open(max_age_seconds=0, dedup_seconds=10)
        collector.accept(report(), 0)
        self.assertIsNotNone(collector.due(100000))

    def test_completed_dedup_retention_refreshes_on_each_repetition(self):
        box, collector = self.open(dedup_seconds=10)
        collector.accept(report(), 0)
        batch = self.complete(collector, 3)
        box.mark_sent(batch.key, 3)
        self.assertEqual(collector.accept(report(), 9), 'duplicate')
        collector.maintain(18)
        self.assertEqual(collector.accept(report(), 18), 'duplicate')
        collector.maintain(28)
        self.assertEqual(collector.accept(report(), 28), 'buffered')

    def test_account_isolation(self):
        _, first = self.open('a')
        _, second = self.open('b')
        first.accept(report(), 0)
        second.accept(report(), 1)
        first.accept(report(changes=((41, 2, 2),)), 2)
        self.assertFalse(first.has_pending())
        self.assertEqual(second.pending_count(), 1)
        self.assertIsNotNone(second.due(4))

    def test_disable_images_drains_existing_and_immediately_handles_new_reports(self):
        box, collector = self.open()
        collector.accept(report(), 0)
        box.close()
        box, draining = self.open(collect=False)
        self.assertEqual(draining.accept(report(), 1), 'duplicate')
        self.assertEqual(draining.accept(report(1), 1), 'immediate')
        self.assertEqual(draining.pending_count(), 1)
        self.complete(draining, 3)
        self.assertFalse(draining.has_pending())

    def test_disabled_collector_accepts_shorter_retry_age_and_clears_obsolete_pending(self):
        box, collector = self.open(wait=30)
        collector.accept(report(), 0)
        box.close()
        box, draining = self.open(wait=30, collect=False, max_age_seconds=10)
        self.assertEqual(draining.accept(report(changes=((35, 6, 51),)), 1), 'immediate')
        self.assertFalse(draining.has_pending())

    def test_invalid_wait_and_nonfinite_scheduling_are_rejected(self):
        box, collector = self.open()
        for value in (0, -1, 301, float('nan'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TyphoonCollector(box, value)
        box.max_age_seconds = 3
        with self.assertRaises(ValueError):
            TyphoonCollector(box, 3)
        with self.assertRaises(ValueError):
            collector.accept(report(), float('inf'))

    def test_backup_copy_contains_pending_reports_and_can_resume(self):
        box, collector = self.open()
        collector.accept(report(), 0)
        collector.accept(report(2), 1)
        backup_path = Path(self.directory.name) / 'copy.sqlite3'
        with sqlite3.connect(backup_path) as destination:
            box.connection.backup(destination)
            destination.row_factory = sqlite3.Row
            validate_storage(destination)
        restored = Outbox(backup_path, 'account-a')
        self.addCleanup(restored.close)
        batch = TyphoonCollector(restored, 3).due(3)
        self.assertEqual(len(batch.reports), 2)
        self.assertEqual(batch.entries[0], (FRAMES[0][1], FRAMES[0][0]))

    def test_validation_rejects_corrupt_nmea_identity_deadline_and_group(self):
        box, collector = self.open()
        collector.accept(report(), 0)
        validate_storage(box.connection)
        for statement in (
            "UPDATE typhoon_reports SET nmea = 'invalid'",
            "UPDATE typhoon_reports SET report_key = 'incorrect'",
            'UPDATE typhoon_groups SET deadline = NULL',
            "UPDATE typhoon_groups SET storm_key = '99:3'",
            "UPDATE typhoon_groups SET state = 'cancelled'",
        ):
            with self.subTest(statement=statement):
                box.connection.execute('SAVEPOINT bad_copy')
                box.connection.execute(statement)
                with self.assertRaises(ValueError):
                    validate_storage(box.connection)
                box.connection.execute('ROLLBACK TO bad_copy')
                box.connection.execute('RELEASE bad_copy')

    def test_validation_accepts_older_outbox_and_rejects_incomplete_schema(self):
        box = Outbox(self.path, 'a')
        self.addCleanup(box.close)
        validate_storage(box.connection)
        collector = TyphoonCollector(box, 3)
        collector.accept(report(), 0)
        with box.connection:
            box.connection.execute("UPDATE metadata SET value = '999' WHERE name = 'typhoon_schema_version'")
        with self.assertRaises(ValueError):
            validate_storage(box.connection)
        with self.assertRaises(RuntimeError):
            TyphoonCollector(box, 3)


class TyphoonSummaryTests(unittest.TestCase):
    def test_text_keeps_metadata_jst_times_unknown_values_and_every_original_frame(self):
        reports = tuple(report(index) for index in reversed(range(6)))
        text = typhoon_summary(reports)
        self.assertIn('【台風32号】', text)
        self.assertIn('2026/11/09 21:50 JST', text)
        self.assertIn('受信した6時点', text)
        self.assertIn('11/09 21:00 JST', text)
        self.assertIn('最大風速 不明', text)
        self.assertIn('中心気圧', text)
        self.assertEqual(text.count('NMEA: '), 6)
        self.assertTrue(text.endswith(FRAMES[-1][1]))
        self.assertIn('【追加更新】', typhoon_summary((report(1),), update=True))

    def test_mixed_issuance_or_cancellation_cannot_be_formatted_as_one_summary(self):
        for reports in ((), (report(), report(changes=((41, 2, 1),))),
                        (report(changes=((41, 2, 2),)),)):
            with self.subTest(reports=reports), self.assertRaises(ValueError):
                typhoon_summary(reports)

    def test_identity_matches_original_single_report_hash(self):
        item = report()
        expected = hashlib.sha256(f'{item.message_type}:{type(item).__name__}:'.encode() + bytes(item.raw)).hexdigest()
        self.assertEqual(report_identity(item), expected)


if __name__ == '__main__':
    unittest.main()
