"""Virtual reception timing preserves real collector rules and provenance."""

from copy import deepcopy
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from azarashi.reports import dcr, dcx

from scripts.gallery_catalog import _field_variant, load_cases
from scripts.gallery_posts import plan_posts
from scripts.gallery_timeline import MAX_ELAPSED_SECONDS, plan_timeline


def published(steps):
    return [(step.elapsed_seconds, post) for step in steps for post in step.posts]


def variant(record, fields):
    return _field_variant(record[0], fields), record[1]


class GalleryTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}
        cls.nankai = cls.cases['nankai-advisory'].records
        cls.typhoon = cls.cases['typhoon-cancel-reference'].cancel_reference

    def test_zero_arrivals_preserve_every_existing_gallery_post(self):
        for case in self.cases.values():
            with self.subTest(case=case.slug):
                steps = plan_timeline(case.records, [0] * len(case.records))
                self.assertEqual(tuple(post for _, post in published(steps)),
                                 plan_posts(case.records, nankai_topics=True))
                self.assertEqual(sum(len(step.received_indexes) for step in steps), len(case.records))

    def test_missing_notice_precedes_late_full_text_with_one_image(self):
        records = self.nankai
        steps = plan_timeline(records, (0, 200, 210, 220, 230))
        (first_at, missing), (last_at, complete) = published(steps)
        self.assertEqual((first_at, last_at), (180, 230))
        self.assertIn('【本文未受信】', missing.text)
        self.assertFalse(missing.image_enabled)
        self.assertEqual(missing.record_indexes, (0,))
        self.assertIn('【追加更新】', complete.text)
        self.assertNotIn('【本文未受信】', complete.text)
        self.assertEqual(complete.record_indexes, tuple(range(5)))
        self.assertEqual(complete.image_record_indexes, (0,))
        self.assertEqual(steps[1].pending_nankai, 1)
        self.assertEqual(steps[-1].pending_nankai, 0)

    def test_equal_time_runs_timer_then_arrivals_in_input_order(self):
        steps = plan_timeline(self.nankai, (0, 180, 180, 180, 180))
        self.assertEqual([at for at, _ in published(steps)], [180, 180])
        at_deadline = [step for step in steps if step.elapsed_seconds == 180]
        self.assertEqual([step.received_indexes for step in at_deadline],
                         [(), (1,), (2,), (3,), (4,)])
        self.assertIn('【本文未受信】', at_deadline[0].posts[0].text)
        self.assertIn('【追加更新】', at_deadline[-1].posts[0].text)

    def test_completion_before_deadline_cancels_missing_notice(self):
        steps = plan_timeline(self.nankai, (0, 30, 60, 90, 179))
        [(at, post)] = published(steps)
        self.assertEqual(at, 179)
        self.assertNotIn('【本文未受信】', post.text)
        self.assertNotIn('【追加更新】', post.text)

    def test_correction_replaces_pending_original_and_ignores_late_original(self):
        correction = tuple(variant(record, ((41, 2, 1),)) for record in self.nankai)
        records = (self.nankai[0], *correction, self.nankai[1])
        steps = plan_timeline(records, (0, 50, 60, 70, 80, 90, 200))
        [(at, post)] = published(steps)
        self.assertEqual(at, 90)
        self.assertIn('（訂正）', post.text)
        self.assertEqual(post.record_indexes, (1, 2, 3, 4, 5))
        self.assertEqual([step.outcomes for step in steps if step.received_indexes == (6,)],
                         [('duplicate',)])

    def test_cancellation_publishes_immediately_and_suppresses_pending_notice(self):
        cancelled = variant(self.nankai[0], ((41, 2, 2),))
        records = (self.nankai[0], cancelled, self.nankai[1], cancelled)
        steps = plan_timeline(records, (0, 70, 200, 220))
        [(at, post)] = published(steps)
        self.assertEqual(at, 70)
        self.assertIn('【取消】', post.text)
        self.assertEqual(post.record_indexes, (1,))
        self.assertEqual(post.image_record_indexes, (1,))
        self.assertEqual([step.outcomes for step in steps if step.received_indexes],
                         [('buffered',), ('buffered',), ('duplicate',), ('duplicate',)])
        self.assertEqual(steps[-1].pending_nankai, 0)

    def test_conflicting_page_returning_to_published_body_suppresses_duplicate_batch(self):
        changed_first = variant(self.nankai[0], ((57, 8, 65),))
        records = (*self.nankai, changed_first, *self.nankai)
        steps = plan_timeline(records, (0, 0, 0, 0, 0, 20, 30, 40, 40, 40, 40))
        [(at, post)] = published(steps)
        self.assertEqual(at, 0)
        self.assertEqual(post.record_indexes, tuple(range(5)))
        self.assertEqual(post.image_record_indexes, (0,))
        self.assertEqual(steps[-1].pending_nankai, 0)
        self.assertFalse(any(step.posts for step in steps if step.elapsed_seconds > 0))

    def test_suppressed_missing_batch_leaves_other_group_for_next_idle_poll(self):
        first = self.nankai[0]
        changed = variant(first, ((57, 8, 65),))
        other_group = variant(first, ((53, 4, 3),))
        records, arrivals = (first, changed, first, other_group), (0, 200, 210, 210)
        before = plan_timeline(records, arrivals, until_seconds=390)
        after = plan_timeline(records, arrivals, until_seconds=391)
        self.assertEqual([at for at, _ in published(before)], [180])
        self.assertEqual([at for at, _ in published(after)], [180, 391])
        self.assertEqual(published(after)[1][1].record_indexes, (3,))
        self.assertEqual(before[-1].elapsed_seconds, 390)
        self.assertEqual(before[-1].pending_nankai, 2)

    def test_receipt_wakes_other_due_group_after_suppressed_missing_batch(self):
        first = self.nankai[0]
        changed = variant(first, ((57, 8, 65),))
        other_group = variant(first, ((53, 4, 3),))
        marine = self.cases['marine'].records[0]
        steps = plan_timeline((first, changed, first, other_group, marine),
                              (0, 200, 210, 210, 390), until_seconds=390)
        self.assertEqual([(at, post.kind) for at, post in published(steps)],
                         [(180, 'nankai'), (390, 'single'), (390, 'nankai')])

    def test_duplicate_receipt_does_not_wake_group_waiting_for_next_poll(self):
        first = self.nankai[0]
        changed = variant(first, ((57, 8, 65),))
        other_group = variant(first, ((53, 4, 3),))
        records = (first, changed, first, other_group, first)
        arrivals = (0, 200, 210, 210, 390)
        for until, expected_times in ((390, [180]), (391, [180, 391])):
            with self.subTest(until=until):
                steps = plan_timeline(records, arrivals, until_seconds=until)
                self.assertEqual([at for at, _ in published(steps)], expected_times)
                duplicate, = [step for step in steps if step.received_indexes == (4,)]
                self.assertEqual(duplicate.outcomes, ('duplicate',))
                self.assertEqual(duplicate.posts, ())

    def test_later_typhoon_points_create_updated_batch_with_original_provenance(self):
        records = (*self.typhoon, self.typhoon[0])
        steps = plan_timeline(records, (0, 20, 40, 200, 220, 240, 300))
        (first_at, first), (update_at, update) = published(steps)
        self.assertEqual((first_at, update_at), (180, 380))
        self.assertEqual(first.record_indexes, (0, 1, 2))
        self.assertEqual(update.record_indexes, tuple(range(6)))
        self.assertEqual(update.image_record_indexes, tuple(range(6)))
        self.assertIn('【追加更新】', update.text)
        self.assertEqual([step.outcomes for step in steps if step.received_indexes == (6,)],
                         [('duplicate',)])

    def test_mixed_collectors_use_service_priority_before_same_time_receipt(self):
        marine = self.cases['marine'].records[0]
        records = (self.typhoon[0], self.nankai[0], marine)
        posts = published(plan_timeline(records, (0, 0, 180)))
        self.assertEqual([(at, post.kind) for at, post in posts],
                         [(180, 'nankai'), (180, 'typhoon'), (180, 'single')])

    def test_expired_missing_group_does_not_complete_from_very_late_pages(self):
        steps = plan_timeline(self.nankai, (0, 901, 902, 903, 904))
        self.assertEqual([at for at, _ in published(steps)], [180])
        expiry, = [step for step in steps if step.elapsed_seconds == 900]
        self.assertEqual((expiry.posts, expiry.pending_nankai), ((), 0))
        self.assertTrue(all(step.outcomes == ('duplicate',)
                            for step in steps if step.received_indexes and step.elapsed_seconds > 900))

    def test_horizon_can_stop_before_collection_deadline(self):
        steps = plan_timeline((self.nankai[0],), (10,), until_seconds=100)
        self.assertFalse(published(steps))
        self.assertEqual(steps[-1].elapsed_seconds, 100)
        self.assertEqual(steps[-1].pending_nankai, 1)

    def test_ignored_dcx_is_recorded_without_a_post(self):
        with patch('scripts.gallery_timeline.azarashi.decode',
                   return_value=object.__new__(dcx.Unknown)):
            steps = plan_timeline((self.nankai[0],), (10,))
        self.assertEqual(steps[0].received_indexes, (0,))
        self.assertEqual(steps[0].outcomes, ('ignored',))
        self.assertFalse(published(steps))
        self.assertEqual(steps[-1].pending_nankai, 0)

    def test_nankai_image_capability_changes_only_image_fields(self):
        records = self.nankai
        with_images = published(plan_timeline(records, (0, 200, 210, 220, 230)))
        without_images = published(plan_timeline(records, (0, 200, 210, 220, 230), nankai_topics=False))
        self.assertEqual([(at, post.text, post.record_indexes) for at, post in with_images],
                         [(at, post.text, post.record_indexes) for at, post in without_images])
        self.assertTrue(all(not post.image_enabled for _, post in without_images))

    def test_explicit_offsets_do_not_come_from_decoder_date_anchors(self):
        records = [list(record) for record in self.nankai]
        before = deepcopy(records)
        steps = plan_timeline(records, (7, 8, 9, 10, 11))
        self.assertEqual(records, before)
        self.assertEqual([at for at, _ in published(steps)], [11])
        self.assertEqual(steps[-1].elapsed_seconds, 191)

    def test_invalid_arrival_sequences_and_horizons_are_rejected(self):
        record = self.nankai[0]
        for arrivals, until in (((), None), ((True,), None), ((-1,), None),
                                ((1.5,), None), ((0, 1), None), ((2,), 1),
                                ((0,), True), ((0,), MAX_ELAPSED_SECONDS + 1),
                                ((MAX_ELAPSED_SECONDS,), None)):
            with self.subTest(arrivals=arrivals, until=until), self.assertRaises(ValueError):
                plan_timeline((record,), arrivals, until_seconds=until)
        with self.assertRaises(ValueError):
            plan_timeline((record, record), (20, 10))

    def test_no_files_network_or_signing_and_decoder_is_restored_on_error(self):
        original_connect = sqlite3.connect
        marker = {'sentinel': object()}
        owner = dcr.NankaiTroughEarthquake
        with patch.object(owner, 'reports', marker), patch.object(owner, 'completed', True), \
                patch.object(owner, 'announcement', 'saved'), \
                patch('outbox.sqlite3.connect', wraps=original_connect) as connect, \
                patch('image_protocol.make_path', side_effect=AssertionError('No signing')), \
                patch('socket.socket', side_effect=AssertionError('No network')), \
                patch.object(Path, 'open', side_effect=AssertionError('No files')):
            plan_timeline(self.nankai, (0, 200, 210, 220, 230))
            with self.assertRaisesRegex(ValueError, 'Cannot decode posting input 1'):
                plan_timeline((self.nankai[0], ('invalid', self.nankai[0][1])), (0, 10))
            self.assertIs(owner.reports, marker)
            self.assertTrue(owner.completed)
            self.assertEqual(owner.announcement, 'saved')
        self.assertEqual(len(connect.call_args_list), 2)
        self.assertTrue(all(str(call.args[0]) == ':memory:' for call in connect.call_args_list))


if __name__ == '__main__':
    unittest.main()
