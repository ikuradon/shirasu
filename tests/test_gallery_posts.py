"""Offline posting plans preserve collector decisions and formatter output."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import azarashi
from azarashi.reports import dcr, dcx

from message_format import report_message
from qzqsm_samples import decode_reports, expected_nankai_text
from scripts.gallery_catalog import _field_variant, load_cases
from scripts.gallery_posts import plan_posts
from typhoon import typhoon_summary


def decode(record):
    sentence, milliseconds = record
    return azarashi.decode(sentence, timestamp=datetime.fromtimestamp(milliseconds / 1000, timezone.utc))


def changed(record, fields):
    return _field_variant(record[0], fields), record[1]


class GalleryPostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def test_single_reports_use_the_exact_formatter_and_weather_frames_stay_separate(self):
        for slug in ('marine', 'weather-many', 'weather-cancel-area', 'tsunami-release',
                     'dcx-secondary-evacuation', 'dcx-mtinfo-leave'):
            records = self.cases[slug].records
            with self.subTest(slug=slug):
                posts = plan_posts(records)
                self.assertEqual(len(posts), len(records))
                for index, (post, record) in enumerate(zip(posts, records, strict=True)):
                    self.assertEqual(post.text, report_message(decode(record)))
                    self.assertEqual((post.record_indexes, post.kind, post.image_enabled),
                                     ((index,), 'single', True))
                    self.assertEqual(post.image_record_indexes, (index,))
        self.assertEqual(len(plan_posts(self.cases['weather-many'].records)), 3)

    def test_all_current_gallery_inputs_form_deterministic_posts_without_mutating_inputs(self):
        for case in self.cases.values():
            with self.subTest(slug=case.slug):
                records = [list(record) for record in case.records]
                before = deepcopy(records)
                posts = plan_posts(records)
                self.assertEqual(posts, plan_posts(records))
                self.assertEqual(records, before)
                self.assertTrue(posts)
                for post in posts:
                    self.assertTrue(post.text)
                    self.assertTrue(post.record_indexes)
                    self.assertTrue(all(0 <= index < len(records) for index in post.record_indexes))
                    self.assertIn(post.kind, ('single', 'typhoon', 'nankai'))
                    self.assertNotIn('https://images.', post.text)
                    self.assertTrue(set(post.image_record_indexes) <= set(post.record_indexes))
                    self.assertEqual(bool(post.image_record_indexes), post.image_enabled)

    def test_typhoon_batch_retains_forecast_order_and_first_received_provenance(self):
        original = self.cases['typhoon-cancel-reference'].cancel_reference
        records = tuple(reversed(original)) + (original[-1],)
        post, = plan_posts(records)
        self.assertEqual((post.kind, post.image_enabled), ('typhoon', True))
        self.assertEqual(post.record_indexes, tuple(reversed(range(6))))
        self.assertEqual(post.image_record_indexes, post.record_indexes)
        self.assertEqual(post.text, typhoon_summary(tuple(decode(record) for record in original)))
        self.assertEqual(post.text.count('NMEA: '), 6)

    def test_typhoon_correction_and_cancellation_replace_pending_points_like_the_collector(self):
        first, second, *_ = self.cases['typhoon-cancel-reference'].cancel_reference
        correction = changed(first, ((41, 2, 1),))
        post, = plan_posts((first, correction, second))
        self.assertEqual((post.kind, post.record_indexes), ('typhoon', (1,)))
        self.assertEqual(post.text, typhoon_summary((decode(correction),)))
        self.assertIn('【訂正】', post.text)
        cancelled = changed(first, ((41, 2, 2),))
        post, = plan_posts((first, cancelled, second, cancelled))
        self.assertEqual((post.kind, post.record_indexes), ('single', (1,)))
        self.assertEqual(post.text, report_message(decode(cancelled)))

    def test_unknown_typhoon_metadata_uses_single_text_instead_of_a_fabricated_batch(self):
        original = self.cases['typhoon-cancel-reference'].cancel_reference[0]
        unknown = changed(original, ((87, 7, 0),))
        post, = plan_posts((unknown, unknown))
        self.assertEqual((post.kind, post.record_indexes), ('single', (0,)))
        self.assertEqual(post.text, report_message(decode(unknown)))

    def test_an_immediate_post_precedes_the_waiting_typhoon_without_changing_input_order(self):
        typhoon = self.cases['typhoon-cancel-reference'].cancel_reference
        marine = self.cases['marine'].records[0]
        posts = plan_posts((typhoon[1], marine, typhoon[0]))
        self.assertEqual([(post.kind, post.record_indexes) for post in posts],
                         [('single', (1,)), ('typhoon', (2, 0))])

    def nankai_pages(self):
        return [report for report in decode_reports('dcr_receiver_20260924')
                if isinstance(report, dcr.NankaiTroughEarthquake)]

    def test_nankai_complete_text_uses_every_page_and_has_no_image(self):
        pages = list(reversed(self.nankai_pages()))
        records = tuple((report.nmea, round(report.timestamp.timestamp() * 1000)) for report in pages)
        with patch.object(dcr.NankaiTroughEarthquake, 'extract_text_information',
                          side_effect=AssertionError('Do not use decoder-global assembled text')):
            post, = plan_posts(records)
        self.assertEqual((post.kind, post.image_enabled), ('nankai', False))
        expected_order = tuple(sorted(range(len(pages)), key=lambda index: pages[index].page_number))
        self.assertEqual(post.record_indexes, expected_order)
        body, _, nmea = post.text.partition('\n---\n')
        self.assertEqual(body.split('\n\n', 1)[1], expected_nankai_text())
        self.assertEqual(nmea.splitlines(), ['NMEA: ' + records[index][0] for index in expected_order])

    def test_nankai_missing_pages_produce_metadata_only_after_the_virtual_deadline(self):
        page = self.nankai_pages()[0]
        record = page.nmea, round(page.timestamp.timestamp() * 1000)
        for nankai_topics in (False, True):
            with self.subTest(nankai_topics=nankai_topics):
                post, = plan_posts((record, record), nankai_topics=nankai_topics)
                self.assertEqual((post.kind, post.record_indexes, post.image_enabled), ('nankai', (0,), False))
                self.assertEqual(post.image_record_indexes, ())
                self.assertIn('【本文未受信】', post.text)
                self.assertIn('受信1/27ページ', post.text)
                self.assertNotIn(expected_nankai_text()[:30], post.text.partition('\n---\n')[0])

    def test_nankai_topics_select_one_original_page_without_shortening_post_input(self):
        for slug in ('nankai-advisory', 'nankai-ended', 'nankai-cancel'):
            original = self.cases[slug].records
            for records in (original, tuple(reversed(original))):
                with self.subTest(slug=slug, reversed=records is not original):
                    legacy, = plan_posts(records)
                    topic, = plan_posts(records, nankai_topics=True)
                    self.assertEqual(topic.text, legacy.text)
                    self.assertEqual(topic.record_indexes, legacy.record_indexes)
                    self.assertEqual(len(topic.record_indexes), len(original))
                    self.assertFalse(legacy.image_enabled)
                    self.assertEqual(legacy.image_record_indexes, ())
                    self.assertTrue(topic.image_enabled)
                    self.assertEqual(topic.image_record_indexes, (records.index(original[0]),))
                    self.assertEqual(topic.text.count('\nNMEA: '), len(original))
                    if slug == 'nankai-ended':
                        self.assertIn('調査終了', topic.text)
                        self.assertNotIn('【解除】', topic.text)
                    if slug == 'nankai-cancel':
                        self.assertIn('【取消】', topic.text)
                        self.assertNotIn('【本文未受信】', topic.text)

    def test_unreadable_complete_nankai_body_never_receives_a_topic_image(self):
        original = self.cases['nankai-advisory'].records
        bad = changed(original[0], ((57, 144, 255 << 136),))
        post, = plan_posts((bad, *original[1:]), nankai_topics=True)
        self.assertIn('本文の文字コードを読み取れません', post.text)
        self.assertEqual(len(post.record_indexes), len(original))
        self.assertFalse(post.image_enabled)
        self.assertEqual(post.image_record_indexes, ())

    def test_nankai_cancellation_suppresses_pending_pages_and_has_no_image_or_old_body(self):
        pages = self.nankai_pages()
        first, second = ((report.nmea, round(report.timestamp.timestamp() * 1000))
                         for report in pages[:2])
        cancelled = changed(first, ((41, 2, 2),))
        post, = plan_posts((first, cancelled, second, cancelled))
        self.assertEqual((post.kind, post.record_indexes, post.image_enabled), ('nankai', (1,), False))
        self.assertIn('【取消】', post.text)
        self.assertIn('この情報は取り消されました。', post.text)
        self.assertNotIn(expected_nankai_text()[:30], post.text.partition('\n---\n')[0])
        self.assertEqual(post.text.partition('\n---\n')[2], 'NMEA: ' + cancelled[0])

    def test_null_and_unknown_dcx_are_excluded_and_bad_decoding_is_not_salvaged(self):
        null = next(report for report in decode_reports('dcr_receiver_20260924')
                    if isinstance(report, dcx.NullMsg))
        record = null.nmea, round(null.timestamp.timestamp() * 1000)
        self.assertEqual(plan_posts((record,)), ())
        with patch('scripts.gallery_posts.azarashi.decode', return_value=object.__new__(dcx.Unknown)):
            self.assertEqual(plan_posts((record,)), ())
        ordinary = self.cases['marine'].records[0]
        with self.assertRaisesRegex(ValueError, 'Cannot decode posting input 1'):
            plan_posts((ordinary, ('not an NMEA message', ordinary[1])))
        for records in ((), ((ordinary[0], True),), ((ordinary[0], -1),), (('bad tuple',),)):
            with self.subTest(records=records), self.assertRaises(ValueError):
                plan_posts(records)

    def test_receipt_timestamps_are_decode_context_and_unsigned_image_limits_still_apply(self):
        first, second, *_ = self.cases['typhoon-cancel-reference'].cancel_reference
        # These still form one same-issue batch under the stated new-reception
        # scenario, but its original timestamps cannot fit a signed image input.
        delayed = second[0], first[1] + 86_400_001
        post, = plan_posts((first, delayed))
        self.assertEqual((post.kind, post.record_indexes, post.image_enabled), ('typhoon', (0, 1), False))

    def test_planning_uses_only_an_isolated_database_and_restores_decoder_state_on_failure(self):
        original_connect = sqlite3.connect
        state = {'sentinel': object()}
        owner = dcr.NankaiTroughEarthquake
        with patch.object(owner, 'reports', state), patch.object(owner, 'completed', True), \
                patch.object(owner, 'announcement', 'existing'), \
                patch('outbox.sqlite3.connect', wraps=original_connect) as connect, \
                patch('image_protocol.make_path', side_effect=AssertionError('No signing')), \
                patch('socket.socket', side_effect=AssertionError('No network')), \
                patch.object(Path, 'open', side_effect=AssertionError('No files')):
            plan_posts(self.cases['marine'].records)
            with self.assertRaises(ValueError):
                plan_posts((('invalid', 0),))
            self.assertIs(owner.reports, state)
            self.assertTrue(owner.completed)
            self.assertEqual(owner.announcement, 'existing')
        self.assertEqual(len(connect.call_args_list), 2)
        self.assertTrue(all(str(call.args[0]) == ':memory:' for call in connect.call_args_list))


if __name__ == '__main__':
    unittest.main()
