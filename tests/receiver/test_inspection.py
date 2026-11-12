import copy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import azarashi
from nostr_sdk import EventBuilder, Keys, Kind

from shirasu.inspection import format_event, format_history, format_status, prepare_event, prepare_history
from shirasu.reports.format import report_message


DCR_NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
MARINE_NMEA = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'
RECEIVED_AT = datetime(2026, 11, 8, tzinfo=timezone.utc)


class InspectionTests(unittest.TestCase):
    def snapshot(self, now):
        return {
            'healthy': True,
            'counts': {'pending': 2, 'sent': 12, 'expired': 1},
            'oldest_pending_age_seconds': 34,
            'status': {
                'running': True, 'gpsd_connected': True, 'relay_connected': True,
                'heartbeat': now - 1, 'last_received': now - 3,
                'last_published': now - 720,
            },
        }

    def history(self):
        report = azarashi.decode(DCR_NMEA, timestamp=RECEIVED_AT)
        event = EventBuilder(Kind(1), f'{report}\n---\nNMEA: {DCR_NMEA}').finalize(Keys.generate())
        return {'identity': event.author().to_hex(), 'total': 5, 'events': [{
            'key': 'report-key', 'event_id': event.id().to_hex(), 'event_json': event.as_json(),
            'state': 'pending', 'attempts': 2, 'created_at': RECEIVED_AT.timestamp(),
            'last_seen': RECEIVED_AT.timestamp(), 'next_attempt_at': RECEIVED_AT.timestamp() + 60,
            'sent_at': None, 'last_error': 'relay rejected event',
        }]}

    def detail(self):
        history = self.history()
        return {'identity': history['identity'], 'event': history['events'][0]}

    def test_status_shows_relative_times_and_counts(self):
        now = 1794148017.0343413
        snapshot = self.snapshot(now)
        before = copy.deepcopy(snapshot)
        text = format_status(snapshot, now)
        for expected in ('正常', '3秒前', '12分前', '1秒前', '34秒', '12件', '2件', '1件'):
            self.assertIn(expected, text)
        self.assertIn('接続中（最終記録）', text)
        self.assertEqual(snapshot, before)

    def test_status_missing_future_and_stale_timestamps_remain_explicit(self):
        now = 1794148017.0343413
        empty = {'healthy': False, 'counts': {}, 'status': {}}
        text = format_status(empty, now)
        for expected in ('要確認', '未受信', '未送信', '未記録'):
            self.assertIn(expected, text)

        for heartbeat in (now - 90, now + 60, float('nan')):
            with self.subTest(heartbeat=heartbeat):
                snapshot = self.snapshot(now)
                snapshot['status']['heartbeat'] = heartbeat
                text = format_status(snapshot, now)
                self.assertIn('要確認', text)
                self.assertIn('接続表示は現在の状態を保証しません', text)
                self.assertNotIn('正常', text)
        snapshot = self.snapshot(now)
        snapshot['status']['last_received'] = now + 60
        self.assertIn('1分後（時計を確認）', format_status(snapshot, now))

    def test_nankai_collection_status_is_visible_without_images(self):
        snapshot = self.snapshot(1000)
        snapshot['status']['nankai_buffered_reports'] = 2
        self.assertRegex(format_status(snapshot, 1000), r'南海トラフの集約待ち\s+2通')

    def test_preparation_decodes_real_signed_event_without_mutating_input(self):
        history = self.history()
        before = copy.deepcopy(history)
        prepared = prepare_history(history)
        self.assertEqual(prepared['events'][0]['report_type'], 'EarthquakeEarlyWarning')
        self.assertEqual(prepared['events'][0]['report_label'], '緊急地震速報')
        self.assertNotIn('event_json', prepared['events'][0])
        self.assertEqual(history, before)
        self.assertEqual(prepared['events'][0]['event_id'], history['events'][0]['event_id'])
        self.assertEqual(prepared['identity'], history['identity'])
        self.assertEqual(prepared['total'], 5)

    def test_corrupt_and_unsupported_history_rows_are_kept(self):
        history = self.history()
        for raw in ('broken JSON', '[]', '{"content":null}', '{"content":"old format"}',
                    '{"content":"text\\nNMEA: invalid"}'):
            with self.subTest(raw=raw):
                history['events'][0]['event_json'] = raw
                result = prepare_history(history)
                self.assertEqual(result['events'][0]['report_type'], '不明')
                self.assertEqual(result['events'][0]['report_label'], '不明')
                self.assertEqual(len(result['events']), 1)
        history = self.history()
        with patch('shirasu.inspection.azarashi.decode', side_effect=RuntimeError('unsupported historical format')):
            self.assertEqual(prepare_history(history)['events'][0]['report_type'], '不明')

    def test_missing_or_empty_disaster_label_falls_back_to_class_name(self):
        class CustomReport:
            pass

        history = self.history()
        report = CustomReport()
        for label in (None, '', '  ', 42):
            with self.subTest(label=label):
                report.disaster_category = label
                with patch('shirasu.inspection.azarashi.decode', return_value=report):
                    row = prepare_history(history)['events'][0]
                self.assertEqual(row['report_type'], 'CustomReport')
                self.assertEqual(row['report_label'], 'CustomReport')

    def test_history_has_jst_states_failures_and_short_event_id(self):
        history = prepare_history(self.history())
        text = format_history(history)
        for expected in ('JST', '2026-11-08 09:00:00', '緊急地震速報',
                         '送信待ち', '失敗回数', '表示 1件 / 対象 5件', 'relay rejected event'):
            self.assertIn(expected, text)
        self.assertNotIn('EarthquakeEarlyWarning', text)
        event_id = history['events'][0]['event_id']
        self.assertIn(event_id[:12], text)
        self.assertNotIn(event_id, text)
        self.assertNotIn(DCR_NMEA, text)

    def test_terminal_controls_and_line_breaks_cannot_create_extra_rows(self):
        error = 'rejected\n\x1b[31m\r\t\u202edanger'
        snapshot = self.snapshot(1000)
        snapshot['status']['last_delivery_error'] = error
        status = format_status(snapshot, 1000)
        history = prepare_history(self.history())
        history['events'][0]['last_error'] = error
        text = format_history(history)
        for output in (status, text):
            for control in ('\x1b', '\r', '\t', '\u202e'):
                self.assertNotIn(control, output)
            self.assertTrue(any('rejected' in line and 'danger' in line for line in output.splitlines()))
        self.assertEqual(history['events'][0]['last_error'], error)
        self.assertEqual(len(text.splitlines()), 5)

    def test_empty_history_is_understandable(self):
        text = format_history({'identity': 'test', 'total': 0, 'events': []})
        self.assertIn('配送履歴はありません', text)
        self.assertIn('表示 0件 / 対象 0件', text)

    def test_event_uses_the_saved_old_marine_body_even_when_decoding_is_unavailable(self):
        detail = self.detail()
        report = azarashi.decode(MARINE_NMEA, timestamp=RECEIVED_AT)
        content = f'{report}\n---\nNMEA: {MARINE_NMEA}'
        self.assertNotEqual(content, report_message(report))
        event = EventBuilder(Kind(1), content).finalize(Keys.generate())
        detail['identity'] = event.author().to_hex()
        detail['event'].update(event_id=event.id().to_hex(), event_json=event.as_json())
        before = copy.deepcopy(detail)

        with patch('shirasu.inspection.azarashi.decode', side_effect=RuntimeError('obsolete decoder')):
            prepared = prepare_event(detail)
            text = format_event(prepared)

        self.assertEqual(prepared['event']['content'], content)
        self.assertIsNone(prepared['event']['content_error'])
        self.assertEqual(prepared['event']['report_label'], '不明')
        self.assertNotIn('event_json', prepared['event'])
        self.assertEqual(prepared['identity'], event.author().to_hex())
        self.assertIn(event.id().to_hex(), text)
        self.assertTrue(text.endswith(content))
        self.assertEqual(detail, before)

    def test_event_displays_full_metadata_with_jst_times_and_no_truncation(self):
        detail = self.detail()
        detail['event'].update(state='sent', sent_at=RECEIVED_AT.timestamp() + 61,
                               attempts=123, last_error='長い理由' * 100)
        prepared = prepare_event(detail)
        before = copy.deepcopy(prepared)
        text = format_event(prepared)
        for expected in ('イベントID', detail['event']['event_id'], '緊急地震速報',
                         '送信済み', '受信日時(JST)', '送信日時(JST)',
                         '2026-11-08 09:00:00', '2026-11-08 09:01:01',
                         '失敗回数', '123', '長い理由' * 100, '投稿本文（保存当時）', DCR_NMEA):
            self.assertIn(expected, text)
        self.assertEqual(prepared, before)

    def test_unsent_event_states_do_not_invent_a_send_time(self):
        detail = self.detail()
        for state, label in (('pending', '送信待ち'), ('expired', '期限切れ')):
            with self.subTest(state=state):
                detail['event']['state'] = state
                text = format_event(prepare_event(detail))
                self.assertIn(label, text)
                self.assertIn('未送信', text)
                self.assertEqual(text.count('2026-11-08 09:00:00'), 1)

    def test_corrupt_event_content_keeps_delivery_metadata_visible(self):
        for raw in ('broken JSON', '[]', 'null', '{}', '{"content":null}', '{"content":7}', '[' * 2000):
            with self.subTest(raw=raw):
                detail = self.detail()
                detail['event']['event_json'] = raw
                prepared = prepare_event(detail)
                text = format_event(prepared)
                self.assertIsNone(prepared['event']['content'])
                self.assertTrue(prepared['event']['content_error'])
                self.assertIn(prepared['event']['content_error'], text)
                self.assertIn(detail['event']['event_id'], text)
                self.assertIn('送信待ち', text)
                self.assertIn('relay rejected event', text)

    def test_empty_saved_body_is_valid_and_explicit(self):
        detail = self.detail()
        detail['event']['event_json'] = json.dumps({'content': ''})
        prepared = prepare_event(detail)
        self.assertEqual(prepared['event']['content'], '')
        self.assertIsNone(prepared['event']['content_error'])
        self.assertIn('本文は空です', format_event(prepared))

    def test_saved_body_preserves_normal_whitespace_and_line_breaks(self):
        detail = self.detail()
        content = '  先頭  スペース\n\n　全角スペースも保持　\n末尾  \n'
        detail['event']['event_json'] = json.dumps({'content': content})
        prepared = prepare_event(detail)
        text = format_event(prepared)
        self.assertEqual(prepared['event']['content'], content)
        self.assertTrue(text.endswith(content))
        self.assertNotIn('エスケープ', text)

    def test_event_escapes_body_controls_but_keeps_json_content_exact(self):
        detail = self.detail()
        content = '本文\n\x1b[31m色\r復帰\tタブ\u202e方向\x00\u2028\ud800'
        detail['event']['event_json'] = json.dumps({'content': content})
        detail['event']['last_error'] = 'rejected\n\x1b[31m\r\t\u202edanger'
        prepared = prepare_event(detail)
        text = format_event(prepared)
        for control in ('\x1b', '\r', '\t', '\u202e', '\x00', '\u2028', '\ud800'):
            self.assertNotIn(control, text)
        for escaped in (r'\u001b', r'\u000d', r'\u0009', r'\u202e',
                        r'\u0000', r'\u2028', r'\ud800'):
            self.assertIn(escaped, text)
        self.assertIn('制御文字をエスケープ表示', text)
        self.assertIn('本文\n', text)
        self.assertTrue(any('rejected' in line and 'danger' in line for line in text.splitlines()))
        self.assertEqual(prepared['event']['content'], content)
        self.assertEqual(json.loads(json.dumps(prepared))['event']['content'], content)


if __name__ == '__main__':
    unittest.main()
