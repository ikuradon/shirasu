"""Durable Nankai assembly, with no partial body publication or shared state."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from azarashi.reports import dcr

from shirasu.reports.nankai import NankaiCollector, nankai_summary, validate_storage
from shirasu.outbox import Outbox
from shirasu.reports.typhoon import TyphoonCollector, report_identity
from tests.receiver import test_nankai_publication as fixtures
from tests.receiver import test_typhoon as typhoon_fixtures


class NankaiCollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'shirasu.outbox.sqlite3'
        fixtures.isolate_assembly(self)

    def open(self, identity='test-account', wait=3, **options):
        box = Outbox(self.path, identity, **options)
        self.addCleanup(box.close)
        return box, NankaiCollector(box, wait_seconds=wait)

    def finish(self, collector, now, identity='event'):
        batch = collector.due(now)
        self.assertIsNotNone(batch)
        self.assertEqual(collector.complete(batch, '{"signed":"' + identity + '"}', identity, now), 'queued')
        return batch

    def test_pages_survive_restart_and_complete_immediately_in_page_order(self):
        body = 'A訓練用の架空の情報です。\n全文を保持します。'
        encoded = body.encode()
        chunks = [encoded[offset:offset + 18] for offset in range(0, len(encoded), 18)]
        box, collector = self.open()
        pages = {}
        for index in reversed(range(len(chunks))):
            report = fixtures.nankai_page(chunks[index], number=index + 1, total=len(chunks))
            pages[index + 1] = report.nmea
            self.assertEqual(collector.accept(report, 0), 'buffered')
            if index:
                self.assertIsNone(collector.due(0))
                box.close()
                box, collector = self.open()
        with patch.object(dcr.NankaiTroughEarthquake, 'extract_text_information',
                          side_effect=AssertionError('No decoder shared state')):
            batch = self.finish(collector, 0)
            text = nankai_summary(batch)
            self.assertIn('\n\n' + body + '\n---\n', text)
        self.assertEqual([nmea for nmea, _ in batch.entries], [pages[index] for index in sorted(pages)])
        self.assertEqual(collector.pending_count(), 0)
        self.assertIsNone(collector.due(0))
        validate_storage(box.connection)

    def test_timeout_is_once_and_late_complete_contains_no_missing_page_gap(self):
        box, collector = self.open(wait=30)
        first = fixtures.nankai_page(b'first ', number=1, total=2)
        second = fixtures.nankai_page(b'second', number=2, total=2)
        self.assertEqual(collector.accept(first, 0), 'buffered')
        self.assertEqual(collector.accept(first, 29), 'duplicate')
        self.assertIsNone(collector.due(29))
        partial = self.finish(collector, 30, 'incomplete')
        self.assertEqual(partial.mode, 'missing')
        self.assertNotIn('first ', nankai_summary(partial).partition('\n---\n')[0])
        self.assertIn('受信1/2ページ', nankai_summary(partial))
        self.assertIn('不足ページ：2', nankai_summary(partial))
        self.assertEqual(collector.pending_count(), 0)
        self.assertEqual(collector.waiting_count(), 1)
        self.assertIsNone(collector.due(50))
        self.assertEqual(collector.accept(first, 50), 'duplicate')
        self.assertEqual(collector.accept(second, 51), 'buffered')
        complete = self.finish(collector, 51, 'complete')
        self.assertEqual(complete.mode, 'complete')
        self.assertIn('【追加更新】', nankai_summary(complete))
        self.assertIn('\n\nfirst second\n---\n', nankai_summary(complete))
        self.assertEqual(collector.waiting_count(), 0)
        self.assertEqual(box.pending_count(), 2)
        validate_storage(box.connection)

    def test_timeout_batch_is_stale_if_missing_page_arrives_before_commit(self):
        box, collector = self.open()
        collector.accept(fixtures.nankai_page(b'one', total=2), 0)
        partial = collector.due(3)
        collector.accept(fixtures.nankai_page(b'two', number=2, total=2), 3)
        self.assertEqual(collector.complete(partial, '{}', 'stale', 3), 'stale')
        self.assertEqual(box.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        self.assertEqual(self.finish(collector, 3).mode, 'complete')

    def test_conflicting_page_restarts_the_generation_and_cannot_mix_old_fragments(self):
        box, collector = self.open()
        first = fixtures.nankai_page(b'old ', total=2)
        second = fixtures.nankai_page(b'tail', number=2, total=2)
        collector.accept(first, 0)
        collector.accept(second, 1)
        stale = collector.due(1)
        changed = fixtures.nankai_page(b'new ', total=2)
        self.assertEqual(collector.accept(changed, 2), 'buffered')
        self.assertEqual(collector.pending_count(), 1)
        self.assertIsNone(collector.due(2))
        self.assertEqual(collector.complete(stale, '{}', 'stale', 2), 'stale')
        # A repeated unchanged page can join the fresh generation after it is
        # received again, but is not silently reused at the conflicting update.
        self.assertEqual(collector.accept(second, 2), 'buffered')
        current = self.finish(collector, 2)
        self.assertIn('\n\nnew tail\n---\n', nankai_summary(current))
        self.assertNotIn(first.nmea, nankai_summary(current))
        validate_storage(box.connection)

    def test_correction_and_cancellation_supersede_only_unpublished_same_classification(self):
        box, collector = self.open()
        normal = fixtures.nankai_page(b'ordinary', total=2, classification=2)
        training = fixtures.nankai_page(b'training', total=2)
        collector.accept(normal, 0)
        collector.accept(training, 0)
        corrected = fixtures.nankai_page(b'corrected', total=2, information=1)
        self.assertEqual(collector.accept(corrected, 1), 'buffered')
        self.assertEqual(collector.pending_count(), 2)
        self.assertEqual(collector.accept(training, 1), 'duplicate')
        cancelled = fixtures.nankai_page(b'not a full body', total=2, information=2)
        self.assertEqual(collector.accept(cancelled, 2), 'buffered')
        batch = self.finish(collector, 2)
        self.assertEqual(batch.mode, 'cancelled')
        self.assertIn('【訓練・試験】【取消】', nankai_summary(batch))
        self.assertIn('この情報は取り消されました。', nankai_summary(batch))
        self.assertNotIn('not a full body', nankai_summary(batch).partition('\n---\n')[0])
        self.assertEqual(collector.pending_count(), 1)
        self.assertEqual(collector.accept(corrected, 2), 'duplicate')
        validate_storage(box.connection)

    def test_conflict_after_completion_gets_a_new_timeout_then_a_complete_update(self):
        box, collector = self.open(wait=3, max_age_seconds=0)
        original = fixtures.nankai_page(b'old ', total=2)
        tail = fixtures.nankai_page(b'tail', number=2, total=2)
        collector.accept(original, 0)
        collector.accept(tail, 0)
        old_batch = self.finish(collector, 0, 'original')
        old_json = box.connection.execute('SELECT event_json FROM events WHERE key=?',
                                          (old_batch.key,)).fetchone()[0]

        changed = fixtures.nankai_page(b'new ', total=2)
        self.assertEqual(collector.accept(changed, 5), 'buffered')
        self.assertEqual(collector.accept(changed, 7), 'duplicate')
        self.assertIsNone(collector.due(7))
        partial = self.finish(collector, 8, 'new-missing')
        self.assertEqual(partial.mode, 'missing')
        self.assertEqual(partial.created_at, 5)
        self.assertIn('不足ページ：2', nankai_summary(partial))
        self.assertNotIn('new ', nankai_summary(partial).partition('\n---\n')[0])
        self.assertIsNone(collector.due(8))

        self.assertEqual(collector.accept(tail, 9), 'buffered')
        updated = self.finish(collector, 9, 'new-complete')
        self.assertIn('【追加更新】', nankai_summary(updated))
        self.assertIn('\n\nnew tail\n---\n', nankai_summary(updated))
        self.assertNotIn(original.nmea, nankai_summary(updated))
        self.assertEqual(box.connection.execute('SELECT event_json FROM events WHERE key=?',
                                               (old_batch.key,)).fetchone()[0], old_json)
        self.assertEqual(box.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], 3)
        validate_storage(box.connection)

    def test_new_issuance_supersedes_older_pages_and_includes_the_year(self):
        box, collector = self.open()
        old_time = datetime(2026, 12, 31, 23, tzinfo=timezone.utc)
        old = fixtures.nankai_page(b'old', total=2, issued_at=old_time)
        new = fixtures.nankai_page(b'new', issued_at=old_time + timedelta(hours=2))
        collector.accept(old, 0)
        self.assertEqual(collector.accept(new, 1), 'buffered')
        self.assertEqual(collector.pending_count(), 1)
        self.assertEqual(collector.accept(old, 1), 'duplicate')
        batch = self.finish(collector, 1)
        self.assertEqual(datetime.fromtimestamp(batch.issued_at, timezone.utc).year, 2027)
        self.assertIn('\n\nnew\n---\n', nankai_summary(batch))
        validate_storage(box.connection)

    def test_serial_total_and_account_never_mix(self):
        box, collector = self.open()
        other_box, other = self.open(identity='another-account')
        one = fixtures.nankai_page(b'one', total=2, serial=4)
        two = fixtures.nankai_page(b'two', number=2, total=2, serial=5)
        three = fixtures.nankai_page(b'three', number=2, total=3, serial=4)
        for report in (one, two, three):
            self.assertEqual(collector.accept(report, 0), 'buffered')
        self.assertEqual(other.accept(one, 0), 'buffered')
        self.assertEqual(collector.pending_count(), 3)
        self.assertEqual(other.pending_count(), 1)
        self.assertIsNone(collector.due(0))
        self.assertIsNone(other.due(0))
        validate_storage(box.connection)

    def test_same_raw_page_in_a_new_year_moves_to_the_new_announcement(self):
        box, collector = self.open(max_age_seconds=0)
        old = fixtures.nankai_page(b'one', total=2)
        new_time = fixtures.ISSUED_AT.replace(year=fixtures.ISSUED_AT.year + 1)
        repeated = fixtures.nankai_page(b'one', total=2, issued_at=new_time)
        self.assertEqual(report_identity(old), report_identity(repeated), 'QZQSM carries no year')
        collector.accept(old, 0)
        later = 366 * 86400
        self.assertEqual(collector.accept(repeated, later), 'buffered')
        self.assertEqual(collector.pending_count(), 1)
        validate_storage(box.connection)
        row = box.connection.execute('SELECT * FROM nankai_reports').fetchone()
        self.assertEqual(row['received_ms'], round(new_time.timestamp() * 1000))
        self.assertEqual(collector.accept(fixtures.nankai_page(b'two', number=2, total=2,
                                                              issued_at=new_time), later), 'buffered')
        batch = self.finish(collector, later)
        self.assertEqual(datetime.fromtimestamp(batch.issued_at, timezone.utc).year, new_time.year)
        self.assertIn('\n\nonetwo\n---\n', nankai_summary(batch))
        validate_storage(box.connection)

    def test_new_year_existing_group_does_not_deduplicate_a_completed_old_year_page(self):
        box, collector = self.open(max_age_seconds=0, dedup_seconds=800 * 86400)
        first = fixtures.nankai_page(b'one', total=2)
        second = fixtures.nankai_page(b'two', number=2, total=2)
        collector.accept(first, 0)
        collector.accept(second, 0)
        original = self.finish(collector, 0)
        new_time = fixtures.ISSUED_AT.replace(year=fixtures.ISSUED_AT.year + 1)
        later = 366 * 86400
        # A changed first page creates the new group while the unchanged second
        # page still exists as done data belonging to last year's group.
        self.assertEqual(collector.accept(fixtures.nankai_page(b'new', total=2,
                                                              issued_at=new_time), later), 'buffered')
        self.assertEqual(collector.accept(fixtures.nankai_page(b'two', number=2, total=2,
                                                              issued_at=new_time), later), 'buffered')
        batch = self.finish(collector, later, 'new-year')
        self.assertNotEqual(batch.group_key, original.group_key)
        self.assertIn('\n\nnewtwo\n---\n', nankai_summary(batch))
        validate_storage(box.connection)

    def test_legacy_page_suppresses_the_whole_group_without_rewriting_old_events(self):
        box, collector = self.open()
        first = fixtures.nankai_page(b'one', total=2)
        second = fixtures.nankai_page(b'two', number=2, total=2)
        box.enqueue(report_identity(first), '{"old":"image-event"}', 'old-event', 0)
        self.assertEqual(collector.accept(second, 1), 'buffered')
        self.assertEqual(collector.accept(first, 2), 'duplicate')
        self.assertEqual(collector.pending_count(), 0)
        self.assertIsNone(collector.due(100))
        self.assertEqual(collector.accept(second, 100), 'duplicate')
        self.assertEqual(box.next_due(100)['event_json'], '{"old":"image-event"}')
        validate_storage(box.connection)

    def test_fixed_expiry_frees_capacity_even_after_timeout_and_duplicate(self):
        box, collector = self.open(wait=3, max_age_seconds=10)
        first = fixtures.nankai_page(b'one', total=2)
        collector.accept(first, 0)
        self.finish(collector, 3)
        collector.accept(first, 9)
        self.assertEqual(collector.waiting_count(), 1)
        collector.maintain(10)
        self.assertEqual(collector.waiting_count(), 0)
        self.assertEqual(collector.pending_count(), 0)
        self.assertEqual(collector.accept(fixtures.nankai_page(b'two', number=2, total=2), 10), 'duplicate')
        self.assertIsNone(collector.due(10))
        validate_storage(box.connection)

    def test_repeated_legacy_issuance_and_correction_suppress_older_unfinished_groups(self):
        for information in (0, 1):
            with self.subTest(information=information):
                box, collector = self.open(identity=f'legacy-{information}')
                original = fixtures.nankai_page(b'old', total=2)
                newer = fixtures.nankai_page(b'new', total=2, information=information,
                                             issued_at=fixtures.ISSUED_AT + timedelta(minutes=1))
                box.enqueue(report_identity(newer), '{"old":"signed-event"}', 'old-event', 0)
                collector.accept(original, 0)
                self.assertEqual(collector.accept(newer, 1), 'duplicate')
                self.assertEqual(collector.pending_count(), 0)
                self.assertEqual(collector.waiting_count(), 0)
                self.assertIsNone(collector.due(3))
                self.assertEqual(box.next_due(3)['event_json'], '{"old":"signed-event"}')
                validate_storage(box.connection)

    def test_shared_capacity_counts_other_buffers_and_reclaims_superseded_pages(self):
        box, collector = self.open(max_pending=2)
        typhoons = TyphoonCollector(box, wait_seconds=3)
        self.assertEqual(typhoons.accept(typhoon_fixtures.report(), 0), 'buffered')
        first = fixtures.nankai_page(b'one', total=2)
        self.assertEqual(collector.accept(first, 0), 'buffered')
        self.assertEqual(collector.accept(fixtures.nankai_page(b'two', number=2, total=2), 0), 'full')
        self.assertEqual(box.enqueue('other', '{}', 'other', 0), 'full')
        corrected = fixtures.nankai_page(b'correction', information=1)
        self.assertEqual(collector.accept(corrected, 1), 'buffered')
        self.finish(collector, 1)
        self.assertEqual(box.pending_count(), 2)

    def test_completed_group_retains_all_pages_when_only_one_page_is_refreshed(self):
        box, collector = self.open(dedup_seconds=10)
        first = fixtures.nankai_page(b'one', total=2)
        second = fixtures.nankai_page(b'two', number=2, total=2)
        collector.accept(first, 0)
        collector.accept(second, 0)
        self.finish(collector, 0)
        self.assertEqual(collector.accept(first, 9), 'duplicate')
        collector.maintain(11)
        self.assertEqual(collector.accept(second, 11), 'duplicate')
        self.assertEqual(collector.pending_count(), 0)
        self.assertIsNone(collector.due(11))
        validate_storage(box.connection)
        collector.maintain(22)
        self.assertEqual(box.connection.execute('SELECT COUNT(*) FROM nankai_groups').fetchone()[0], 0)

    def test_unlimited_lifetime_retains_missing_pages_after_timeout_and_dedup_window(self):
        box, collector = self.open(max_age_seconds=0, dedup_seconds=10)
        collector.accept(fixtures.nankai_page(b'one', total=2), 0)
        self.finish(collector, 3)
        collector.maintain(100)
        self.assertEqual(collector.waiting_count(), 1)
        self.assertEqual(collector.accept(fixtures.nankai_page(b'two', number=2, total=2), 101), 'buffered')
        batch = self.finish(collector, 101, 'late')
        self.assertIn('\n\nonetwo\n---\n', nankai_summary(batch))
        validate_storage(box.connection)

    def test_commit_failure_rolls_back_both_summary_and_page_completion(self):
        box, collector = self.open()
        collector.accept(fixtures.nankai_page(b'body'), 0)
        batch = collector.due(0)
        box.connection.execute("CREATE TRIGGER fail_close BEFORE UPDATE OF state ON nankai_reports WHEN NEW.state='done' BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            collector.complete(batch, '{}', 'event', 0)
        self.assertEqual(collector.pending_count(), 1)
        self.assertEqual(box.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        box.connection.execute('DROP TRIGGER fail_close')
        self.finish(collector, 0)

    def test_invalid_input_does_not_create_a_collection_and_invalid_utf8_is_not_silently_dropped(self):
        box, collector = self.open()
        for report in (typhoon_fixtures.report(), fixtures.nankai_page(number=0),
                       fixtures.nankai_page(serial=7), fixtures.nankai_page(information=3)):
            self.assertEqual(collector.accept(report, 0), 'immediate')
        self.assertEqual(collector.pending_count(), 0)
        collector.accept(fixtures.nankai_page(b'A\xffB'), 0)
        text = nankai_summary(self.finish(collector, 0))
        self.assertIn('本文の文字コードを読み取れませんでした', text)
        self.assertNotIn('\n\nAB', text)

    def test_configuration_rejects_wait_beyond_delivery_lifetime(self):
        box, collector = self.open()
        for wait in (0, -1, 301, float('nan'), True):
            with self.subTest(wait=wait), self.assertRaises(ValueError):
                NankaiCollector(box, wait_seconds=wait)
        short = Outbox(Path(self.directory.name) / 'short.sqlite3', 'short', max_age_seconds=2)
        self.addCleanup(short.close)
        with self.assertRaises(ValueError):
            NankaiCollector(short, wait_seconds=2)

    def test_backup_validation_checks_raw_bytes_without_touching_decoder_assembly(self):
        box, collector = self.open()
        collector.accept(fixtures.nankai_page(b'one', total=2), 0)
        unrelated = fixtures.nankai_page(b'unrelated', issued_at=fixtures.ISSUED_AT + timedelta(hours=1))
        before = dcr.NankaiTroughEarthquake.reports
        announcement = dcr.NankaiTroughEarthquake.announcement
        validate_storage(box.connection)
        self.assertIs(dcr.NankaiTroughEarthquake.reports, before)
        self.assertEqual(dcr.NankaiTroughEarthquake.announcement, announcement)
        self.assertEqual(unrelated.extract_text_information(), 'unrelated')
        mutations = (
            "UPDATE nankai_reports SET text=X'00'",
            "UPDATE nankai_reports SET report_key='wrong'",
            "UPDATE nankai_reports SET nmea='invalid'",
            "UPDATE nankai_reports SET received_ms=received_ms+366*86400000",
            "UPDATE nankai_groups SET deadline=NULL",
            "UPDATE nankai_groups SET state='done'",
            "DELETE FROM nankai_reports",
            "UPDATE metadata SET value='999' WHERE name='nankai_schema_version'",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                box.connection.execute('SAVEPOINT corrupt')
                box.connection.execute(mutation)
                with self.assertRaises(ValueError):
                    validate_storage(box.connection)
                box.connection.execute('ROLLBACK TO corrupt')
                box.connection.execute('RELEASE corrupt')


if __name__ == '__main__':
    unittest.main()
