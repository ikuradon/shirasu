import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

import azarashi
from azarashi.reports import dcx
from nostr_sdk import Event, Keys, RelayUrl

from shirasu import service as main
from shirasu.health import healthy
from shirasu.reports.format import ATTRIBUTION
from shirasu.images.protocol import ProtocolError, parse_path
from shirasu.images.publication import ImagePublisher, image_entry
from shirasu.images.alt import DEFAULT_IMAGE_ALT, image_alt_text
from shirasu.outbox import Outbox, read_status


# Valid synthetic null/unknown DCX frames and real DCR examples.
NULL_NMEA = '$QZQSM,55,53B0000DE000000000000000000000000000000000000000000000000E41D10*04'
UNKNOWN_NMEA = '$QZQSM,55,53B0000DFF00000000000000000000000000000000000000000000000B82B78*71'
DCR_NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
MARINE_NMEA = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'
RECEIVED_AT = datetime(2026, 11, 8, tzinfo=timezone.utc)
TYPHOON_NMEA = (
    '$QZQSM,56,C6ADE5A5990002580100008004619010EC80F511B200000000000012D31A380*0C',
    '$QZQSM,57,53ADE5A59900028003001880046320113400F29437000000000000130F45394*7B',
)
IMAGE_KEY = bytes(range(32))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fake_nostr():
    # Real local cryptography, mocked transport: no production key or network.
    keys = Keys.generate()
    build_event = main.Nostr.build_event
    return SimpleNamespace(
        identity=keys.public_key().to_hex(),
        build_event=Mock(side_effect=lambda text, image_url=None, image_alt=None: build_event(
            SimpleNamespace(keys=keys), text, image_url=image_url, image_alt=image_alt)),
        send=AsyncMock(), connect=AsyncMock(), connected=AsyncMock(return_value=True),
        close=AsyncMock(),
    )


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.clock = Clock()
        self.nostr = fake_nostr()
        self.outbox = Outbox(self.path, self.nostr.identity)
        self.addCleanup(lambda: self.outbox.close())
        self.stop = asyncio.Event()
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock)
        self.report = azarashi.decode(DCR_NMEA, timestamp=RECEIVED_AT)
        self.marine = azarashi.decode(MARINE_NMEA, timestamp=RECEIVED_AT)
        self.enterContext(patch.object(main, 'logger'))

    def reopen(self, **options):
        self.outbox.close()
        self.outbox = Outbox(self.path, self.nostr.identity, **options)
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock)

    async def test_null_and_unknown_reports_update_reception_but_are_not_queued(self):
        for sentence, expected_type in ((NULL_NMEA, dcx.NullMsg), (UNKNOWN_NMEA, dcx.Unknown)):
            with self.subTest(report=expected_type.__name__):
                report = azarashi.decode(sentence, timestamp=RECEIVED_AT)
                self.assertIsInstance(report, expected_type)
                self.assertEqual(await self.service.accept(report), 'ignored')
        self.assertEqual(self.service.state['last_received'], self.clock())
        self.nostr.build_event.assert_not_called()
        self.assertEqual(self.outbox.snapshot(self.clock())['counts']['pending'], 0)

    async def test_real_report_is_durably_signed_and_delivered_with_original_nmea(self):
        self.assertEqual(await self.service.accept(self.report), 'queued')
        record = self.outbox.next_due(self.clock())
        event = Event.from_json(record['event_json'])
        self.assertTrue(event.verify())
        self.assertEqual(event.kind().as_u16(), 1)
        self.assertEqual(event.author().to_hex(), self.nostr.identity)
        self.assertEqual(event.content(), f'{self.report}\n\n{ATTRIBUTION}\n---\nNMEA: {DCR_NMEA}')
        self.assertEqual(event.id().to_hex(), record['event_id'])

        self.assertTrue(await self.service.publish_one())
        self.assertEqual(self.nostr.send.await_args.args[0].as_json(), record['event_json'])
        self.assertEqual(self.outbox.snapshot(self.clock())['counts'],
                         {'pending': 0, 'sent': 1, 'expired': 0})
        self.assertEqual(self.service.state['last_published'], self.clock())

    async def test_marine_groups_regions_in_the_durably_signed_publication(self):
        expected = (
            '【海上警報】発表（通常）\n'
            '発表時刻：11月8日 17:35 JST\n\n'
            '■ 海上着氷警報\n'
            '・サハリン西方海上\n'
            '・北海道西方海上\n\n'
            '■ 海上風警報\n'
            '・宗谷海峡\n'
            '・北海道西方海上\n'
            '・檜山津軽沖\n'
            '・関東海域南部\n'
            '・沿海州南部沖\n'
            '・能登沖\n'
            f'\n{ATTRIBUTION}\n---\nNMEA: {MARINE_NMEA}'
        )
        self.assertEqual(await self.service.accept(self.marine), 'queued')
        record = self.outbox.next_due(self.clock())
        event = Event.from_json(record['event_json'])
        self.assertTrue(event.verify())
        self.assertEqual(event.author().to_hex(), self.nostr.identity)
        self.assertEqual(event.content(), expected)
        self.assertEqual(event.id().to_hex(), record['event_id'])

        self.assertTrue(await self.service.publish_one())
        delivered = self.nostr.send.await_args.args[0]
        self.assertEqual(delivered.as_json(), record['event_json'])
        self.assertEqual(delivered.content(), expected)
        self.assertEqual(self.outbox.snapshot(self.clock())['counts'],
                         {'pending': 0, 'sent': 1, 'expired': 0})

    async def test_old_marine_pending_event_keeps_its_signature_through_retry_and_restart(self):
        old_content = f'{self.marine}\n---\nNMEA: {MARINE_NMEA}'
        self.assertNotEqual(main.report_message(self.marine), old_content)
        old_event = self.nostr.build_event(old_content)
        old_json = old_event.as_json()
        old_id = old_event.id().to_hex()
        self.outbox.enqueue(main.report_key(self.marine), old_json, old_id, self.clock())

        self.assertEqual(await self.service.accept(self.marine), 'duplicate')
        self.assertEqual(self.outbox.next_due(self.clock())['event_json'], old_json)
        self.nostr.send.side_effect = [main.DeliveryError('relay temporarily unavailable'), None]
        self.assertTrue(await self.service.publish_one())

        self.reopen()
        self.clock.now += 1
        self.assertEqual(await self.service.accept(self.marine), 'duplicate')
        record = self.outbox.next_due(self.clock())
        self.assertEqual(record['event_json'], old_json)
        self.assertEqual(record['event_id'], old_id)
        self.assertEqual(record['attempts'], 1)
        self.assertTrue(await self.service.publish_one())

        self.assertEqual(self.nostr.send.await_count, 2)
        for call in self.nostr.send.await_args_list:
            event = call.args[0]
            self.assertTrue(event.verify())
            self.assertEqual(event.as_json(), old_json)
            self.assertEqual(event.id().to_hex(), old_id)
            self.assertEqual(event.content(), old_content)
        self.assertEqual(self.outbox.snapshot(self.clock())['counts'],
                         {'pending': 0, 'sent': 1, 'expired': 0})

    async def test_seeded_old_marine_is_not_republished_with_the_new_format(self):
        old_content = f'{self.marine}\n---\nNMEA: {MARINE_NMEA}'
        old_event = self.nostr.build_event(old_content)
        old_json = old_event.as_json()
        old_id = old_event.id().to_hex()
        key = main.report_key(self.marine)
        self.outbox.seed_sent(key, old_json, old_id, self.clock() - 100, self.clock())

        self.reopen()
        self.clock.now += 5
        report = azarashi.decode(MARINE_NMEA, timestamp=datetime(2026, 11, 8, 1, tzinfo=timezone.utc))
        self.assertNotEqual(main.report_message(report), old_content)
        self.assertEqual(main.report_key(report), key)
        self.assertEqual(await self.service.accept(report), 'duplicate')
        self.assertFalse(await self.service.publish_one())
        self.nostr.send.assert_not_awaited()
        row = self.outbox.connection.execute(
            'SELECT event_json, event_id, state FROM events WHERE identity = ? AND key = ?',
            (self.nostr.identity, key),
        ).fetchone()
        self.assertEqual((row['event_json'], row['event_id'], row['state']),
                         (old_json, old_id, 'sent'))
        self.assertEqual(self.outbox.snapshot(self.clock())['counts'],
                         {'pending': 0, 'sent': 1, 'expired': 0})

    async def test_duplicates_stay_suppressed_after_database_reopen(self):
        self.assertEqual(await self.service.accept(self.report), 'queued')
        original = self.outbox.next_due(self.clock())['event_id']
        self.clock.now += 5
        self.assertEqual(await self.service.accept(self.report), 'duplicate')
        self.assertEqual(self.outbox.next_due(self.clock())['event_id'], original)
        await self.service.publish_one()

        self.reopen()
        later_report = azarashi.decode(DCR_NMEA, timestamp=datetime(2026, 11, 8, 1, tzinfo=timezone.utc))
        self.assertEqual(await self.service.accept(later_report), 'duplicate')
        self.assertFalse(await self.service.publish_one())
        self.nostr.send.assert_awaited_once()

    async def test_failed_delivery_waits_then_retries_the_same_event(self):
        await self.service.accept(self.report)
        self.nostr.send.side_effect = [main.DeliveryError('relay unavailable'), None]
        self.assertTrue(await self.service.publish_one())
        self.assertFalse(await self.service.publish_one())
        self.nostr.send.assert_awaited_once()
        self.assertEqual(self.service.state['last_delivery_error'], 'relay unavailable')

        self.clock.now += 1
        self.assertTrue(await self.service.publish_one())
        sent = [call.args[0] for call in self.nostr.send.await_args_list]
        self.assertEqual(sent[0].as_json(), sent[1].as_json())
        self.assertIsNone(self.service.state['last_delivery_error'])
        self.assertEqual(self.outbox.snapshot(self.clock())['counts']['sent'], 1)

    async def test_lost_ack_and_restart_reuse_the_already_attempted_event_id(self):
        await self.service.accept(self.report)
        self.nostr.send.side_effect = TimeoutError('relay ACK lost')
        await self.service.publish_one()
        attempted_json = self.nostr.send.await_args.args[0].as_json()

        self.reopen()
        self.clock.now += 1
        self.nostr.send.side_effect = None
        self.assertTrue(await self.service.publish_one())
        self.assertEqual(self.nostr.send.await_args.args[0].as_json(), attempted_json)
        self.assertEqual(await self.service.accept(self.report), 'duplicate')

    async def test_fifo_keeps_later_reports_behind_a_retrying_older_report(self):
        await self.service.accept(self.report)
        self.clock.now += 0.1
        await self.service.accept(self.marine)
        self.nostr.send.side_effect = [main.DeliveryError('temporarily blocked'), None, None]
        await self.service.publish_one()
        # The later report is already due, but cannot overtake the failed head.
        self.assertFalse(await self.service.publish_one())
        self.nostr.send.assert_awaited_once()
        self.clock.now += 1
        await self.service.publish_one()
        await self.service.publish_one()
        contents = [call.args[0].content() for call in self.nostr.send.await_args_list]
        self.assertEqual(contents, [main.report_message(self.report),
                                    main.report_message(self.report),
                                    main.report_message(self.marine)])

    async def test_capacity_failure_is_visible_and_can_be_queued_when_capacity_returns(self):
        self.reopen(max_pending=1)
        await self.service.accept(self.report)
        self.assertEqual(await self.service.accept(self.marine), 'full')
        self.service.save_status()
        status = read_status(self.path, now=self.clock())['status']
        self.assertEqual(status['dropped_full'], 1)
        self.assertIn('Outbox full', status['last_delivery_error'])
        await self.service.publish_one()
        self.assertEqual(await self.service.accept(self.marine), 'queued')

    async def test_expired_reports_are_not_delivered_or_revived_by_reception(self):
        await self.service.accept(self.report)
        self.clock.now += 900
        self.assertFalse(await self.service.publish_one())
        self.nostr.send.assert_not_awaited()
        self.assertEqual(self.outbox.snapshot(self.clock())['counts']['expired'], 1)
        self.assertEqual(await self.service.accept(self.report), 'duplicate')

    async def test_cancellation_preserves_pending_event_for_restart(self):
        await self.service.accept(self.report)
        original_json = self.outbox.next_due(self.clock())['event_json']
        started = asyncio.Event()

        async def blocked_send(event):
            started.set()
            await asyncio.Event().wait()

        self.nostr.send.side_effect = blocked_send
        task = asyncio.create_task(self.service.publish_one())
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.reopen()
        record = self.outbox.next_due(self.clock())
        self.assertEqual(record['event_json'], original_json)
        self.assertEqual(record['attempts'], 0)
        self.nostr.send.side_effect = None
        await self.service.publish_one()
        self.assertEqual(self.nostr.send.await_args.args[0].as_json(), original_json)


class ImageServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.clock = Clock()
        self.nostr = fake_nostr()
        self.outbox = Outbox(self.path, self.nostr.identity)
        self.addCleanup(lambda: self.outbox.close())
        self.stop = asyncio.Event()
        self.images = ImagePublisher('https://images.example.test', 'k1', IMAGE_KEY)
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock, images=self.images)
        self.report = azarashi.decode(DCR_NMEA, timestamp=RECEIVED_AT)
        self.marine = azarashi.decode(MARINE_NMEA, timestamp=RECEIVED_AT)
        self.typhoons = tuple(azarashi.decode(nmea, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
                              for nmea, ms in zip(TYPHOON_NMEA, (1794228531340, 1794228535353)))
        self.enterContext(patch.object(main, 'logger'))

    def reopen(self, *, images=True, nankai_wait_seconds=180, **outbox_options):
        self.outbox.close()
        self.outbox = Outbox(self.path, self.nostr.identity, **outbox_options)
        self.service = main.Service(self.nostr, self.outbox, self.stop, clock=self.clock,
                                    images=self.images if images else None,
                                    nankai_wait_seconds=nankai_wait_seconds)

    def image_url(self, event):
        tags = [tag.to_vec() for tag in event.tags() if tag.to_vec()[0] == 'imeta']
        self.assertEqual(len(tags), 1)
        url = next(value.removeprefix('url ') for value in tags[0] if value.startswith('url '))
        self.assertIn(url, event.content())
        self.assertIn('m image/png', tags[0])
        return url

    async def test_other_reports_publish_immediately_while_typhoon_points_wait(self):
        self.assertEqual(await self.service.accept(self.typhoons[0]), 'buffered')
        self.assertFalse(await self.service.publish_one())
        for report in (self.report, self.marine):
            self.assertEqual(await self.service.accept(report), 'queued')
            self.assertTrue(await self.service.publish_one())
            event = self.nostr.send.await_args.args[0]
            url = self.image_url(event)
            self.assertEqual(parse_path(urlsplit(url).path, {'k1': IMAGE_KEY}), (image_entry(report),))
            self.assertIn('alt ' + image_alt_text((image_entry(report),)), event.tags()[0].to_vec())
            self.assertTrue(event.content().endswith('NMEA: ' + report.nmea))
        self.assertEqual(self.service.typhoons.pending_count(), 1)
        self.assertEqual(self.nostr.send.await_count, 2)

    async def test_collection_survives_restart_and_publishes_one_signed_summary(self):
        self.assertEqual(await self.service.accept(self.typhoons[0]), 'buffered')
        self.clock.now += 4
        self.assertEqual(await self.service.accept(self.typhoons[1]), 'buffered')
        self.reopen()
        self.assertEqual(self.service.typhoons.pending_count(), 2)
        self.clock.now = 1179.9
        self.assertFalse(await self.service.publish_one())
        self.clock.now = 1180
        self.assertTrue(await self.service.publish_one())
        self.nostr.send.assert_awaited_once()
        event = self.nostr.send.await_args.args[0]
        self.assertTrue(event.verify())
        self.assertIn('受信した2時点', event.content())
        self.assertEqual(parse_path(urlsplit(self.image_url(event)).path, {'k1': IMAGE_KEY}),
                         tuple(image_entry(report) for report in self.typhoons))
        self.assertIn('alt ' + image_alt_text(tuple(image_entry(report) for report in self.typhoons)),
                      event.tags()[0].to_vec())
        self.assertEqual(self.service.typhoons.pending_count(), 0)
        self.assertEqual(await self.service.accept(self.typhoons[0]), 'duplicate')
        self.assertFalse(await self.service.publish_one())

    async def test_late_point_publishes_an_updated_complete_track_without_replacing_the_old_event(self):
        await self.service.accept(self.typhoons[0])
        self.clock.now += 180
        await self.service.publish_one()
        original = self.nostr.send.await_args.args[0]
        original_json = original.as_json()
        self.clock.now += 1
        self.assertEqual(await self.service.accept(self.typhoons[1]), 'buffered')
        self.clock.now += 180
        await self.service.publish_one()
        updated = self.nostr.send.await_args.args[0]
        self.assertIn('【追加更新】', updated.content())
        self.assertEqual(parse_path(urlsplit(self.image_url(updated)).path, {'k1': IMAGE_KEY}),
                         tuple(image_entry(report) for report in self.typhoons))
        self.assertEqual(parse_path(urlsplit(self.image_url(original)).path, {'k1': IMAGE_KEY}),
                         (image_entry(self.typhoons[0]),))
        saved = self.outbox.connection.execute('SELECT event_json FROM events WHERE event_id = ?',
                                                (original.id().to_hex(),)).fetchone()
        self.assertEqual(saved['event_json'], original_json)

    async def test_disabling_images_drains_existing_collection_as_text_and_stops_new_buffering(self):
        self.assertEqual(await self.service.accept(self.typhoons[0]), 'buffered')
        self.reopen(images=False)
        self.assertEqual(await self.service.accept(self.typhoons[0]), 'duplicate')
        self.assertEqual(await self.service.accept(self.typhoons[1]), 'queued')
        self.assertEqual(self.service.typhoons.pending_count(), 1)
        self.assertTrue(await self.service.publish_one())
        self.clock.now = 1180
        self.assertTrue(await self.service.publish_one())
        summary = self.nostr.send.await_args.args[0]
        self.assertIn('受信した1時点', summary.content())
        self.assertIn(TYPHOON_NMEA[0], summary.content())
        self.assertEqual(self.service.typhoons.pending_count(), 0)
        for call in self.nostr.send.await_args_list:
            self.assertEqual(call.args[0].tags(), [])
            self.assertNotIn('https://images.', call.args[0].content())
        self.service.save_status()
        self.assertFalse(read_status(self.path, now=self.clock())['status']['images_enabled'])

    async def test_disabling_images_can_reopen_with_a_shorter_delivery_lifetime(self):
        await self.service.accept(self.typhoons[0])
        self.reopen(images=False, max_age_seconds=120, nankai_wait_seconds=60)
        self.assertEqual(self.service.typhoons.pending_count(), 1)
        self.assertEqual(await self.service.accept(self.report), 'queued')

    async def test_capacity_combines_buffered_points_and_outbox_but_preserves_duplicate_refresh(self):
        self.reopen(max_pending=2)
        self.assertEqual(await self.service.accept(self.typhoons[0]), 'buffered')
        self.assertEqual(await self.service.accept(self.report), 'queued')
        original = self.outbox.next_due(self.clock())['event_json']
        self.assertEqual(await self.service.accept(self.marine), 'full')
        self.assertEqual(self.service.state['dropped_full'], 1)
        self.clock.now += 5
        self.assertEqual(await self.service.accept(self.report), 'duplicate')
        self.assertEqual(await self.service.accept(self.typhoons[0]), 'duplicate')
        row = self.outbox.connection.execute('SELECT event_json, last_seen FROM events WHERE key = ?',
                                              (main.report_key(self.report),)).fetchone()
        self.assertEqual(row['event_json'], original)
        self.assertEqual(row['last_seen'], self.clock())
        self.assertTrue(await self.service.publish_one())
        self.assertEqual(await self.service.accept(self.marine), 'queued')

    async def test_disabled_images_still_count_saved_raw_points_towards_capacity(self):
        self.reopen(max_pending=1)
        await self.service.accept(self.typhoons[0])
        self.reopen(images=False, max_pending=1)
        self.assertEqual(await self.service.accept(self.report), 'full')
        self.clock.now = 1180
        self.assertTrue(await self.service.publish_one())
        self.assertEqual(await self.service.accept(self.report), 'queued')

    async def test_expired_buffered_points_release_capacity_before_immediate_enqueue(self):
        self.reopen(max_pending=1)
        await self.service.accept(self.typhoons[0])
        self.clock.now += 900
        self.assertEqual(await self.service.accept(self.report), 'queued')
        self.assertEqual(self.service.typhoons.pending_count(), 0)

    async def test_image_encoding_failure_keeps_the_text_and_raw_record_deliverable(self):
        with patch.object(ImagePublisher, 'url', side_effect=ProtocolError('bad input')):
            self.assertEqual(await self.service.accept(self.report), 'queued')
        self.assertEqual(self.service.state['last_image_error'], 'ProtocolError')
        await self.service.publish_one()
        event = self.nostr.send.await_args.args[0]
        self.assertEqual(event.content(), main.report_message(self.report))
        self.assertEqual(event.tags(), [])
        self.assertTrue(event.verify())

    async def test_alt_failure_keeps_the_image_text_and_original_record_deliverable(self):
        with patch.object(main, 'image_alt_text', side_effect=RuntimeError('description unavailable')):
            self.assertEqual(await self.service.accept(self.report), 'queued')
        self.assertIsNone(self.service.state['last_image_error'])
        await self.service.publish_one()
        event = self.nostr.send.await_args.args[0]
        url = self.image_url(event)
        self.assertEqual(parse_path(urlsplit(url).path, {'k1': IMAGE_KEY}), (image_entry(self.report),))
        self.assertIn('alt ' + DEFAULT_IMAGE_ALT, event.tags()[0].to_vec())
        self.assertEqual(event.content().replace('\n\n' + url, '', 1), main.report_message(self.report))
        self.assertTrue(event.verify())

    async def test_invalid_receipt_context_falls_back_to_text_without_losing_report(self):
        with patch.object(main, 'image_entry', side_effect=ValueError('naive receipt timestamp')):
            self.assertEqual(await self.service.accept(self.report), 'queued')
        self.assertIsNotNone(self.service.state['last_image_error'])
        await self.service.publish_one()
        self.assertEqual(self.nostr.send.await_args.args[0].content(), main.report_message(self.report))

    async def test_summary_encoding_failure_also_publishes_text_and_finishes_collection(self):
        for report in self.typhoons:
            await self.service.accept(report)
        self.clock.now += 180
        with patch.object(ImagePublisher, 'url', side_effect=ProtocolError('bad input')):
            self.assertTrue(await self.service.publish_one())
        event = self.nostr.send.await_args.args[0]
        self.assertIn('受信した2時点', event.content())
        for nmea in TYPHOON_NMEA:
            self.assertIn(nmea, event.content())
        self.assertEqual(event.tags(), [])
        self.assertEqual(self.service.typhoons.pending_count(), 0)

    async def test_retry_and_restart_reuse_the_signed_url_after_key_rotation(self):
        await self.service.accept(self.report)
        self.nostr.send.side_effect = main.DeliveryError('relay unavailable')
        await self.service.publish_one()
        original = self.nostr.send.await_args.args[0]
        original_url = self.image_url(original)
        self.images = ImagePublisher('https://new-images.example.test', 'k2', b'x' * 32)
        self.reopen()
        self.clock.now += 1
        self.nostr.send.side_effect = None
        with patch.object(ImagePublisher, 'url', side_effect=AssertionError('retry must not regenerate URL')), \
                patch.object(main, 'image_alt_text', side_effect=AssertionError('retry must not regenerate ALT')):
            self.assertTrue(await self.service.publish_one())
        retried = self.nostr.send.await_args.args[0]
        self.assertEqual(retried.as_json(), original.as_json())
        self.assertEqual(self.image_url(retried), original_url)


class NostrTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.keys = Keys.generate()
        self.relay = RelayUrl.parse('wss://relay.example.test')
        self.client = SimpleNamespace(
            connect=AsyncMock(), add_relay=AsyncMock(), shutdown=AsyncMock(),
            send_event=AsyncMock(return_value=SimpleNamespace(success=[self.relay], failed={})),
        )
        builder = Mock()
        builder.authenticator.return_value = builder
        builder.build.return_value = self.client
        with patch.object(main, 'ClientBuilder', return_value=builder):
            self.nostr = main.Nostr(self.keys.secret_key().to_bech32(), 'wss://relay.example.test')
        self.event = self.nostr.build_event('通報本文\n---\nNMEA: sample')
        self.enterContext(patch.object(main, 'logger'))

    async def test_real_signature_and_reconnect_before_each_delivery(self):
        self.assertTrue(self.event.verify())
        self.assertEqual(self.event.kind().as_u16(), 1)
        self.assertEqual(self.event.author().to_hex(), self.keys.public_key().to_hex())
        self.assertEqual(self.event.content(), '通報本文\n---\nNMEA: sample')
        await self.nostr.connect()
        await self.nostr.send(self.event)
        await self.nostr.send(self.event)
        self.client.add_relay.assert_awaited_once_with(self.relay)
        self.assertEqual(self.client.connect.await_count, 3)
        self.assertEqual(self.client.send_event.await_count, 2)
        call = self.client.send_event.await_args
        self.assertIs(call.args[0], self.event)
        self.assertEqual(call.kwargs['ok_timeout'].total_seconds(), 10)
        self.assertEqual(call.kwargs['authentication_timeout'].total_seconds(), 10)

    async def test_image_metadata_uses_the_installed_sdk_and_is_covered_by_the_signature(self):
        url = 'https://images.example.test/v1/k1/payload.signature.png'
        event = self.nostr.build_event('通報本文\n\n' + url, image_url=url)
        self.assertTrue(event.verify())
        self.assertEqual([tag.to_vec() for tag in event.tags()],
                         [['imeta', 'url ' + url, 'm image/png', 'alt みちびき受信通報の図']])
        restored = Event.from_json(event.as_json())
        self.assertTrue(restored.verify())
        self.assertEqual([tag.to_vec() for tag in restored.tags()], [tag.to_vec() for tag in event.tags()])

    async def test_specific_image_alt_is_signed_and_text_only_events_have_no_image_metadata(self):
        url = 'https://images.example.test/v1/k1/payload.signature.png'
        alt = '【訓練・試験】【取消】気象警報・注意報。発表日時：2026年11月9日21時50分（JST）。'
        event = self.nostr.build_event('通報本文\n\n' + url, image_url=url, image_alt=alt)
        self.assertEqual([tag.to_vec() for tag in event.tags()],
                         [['imeta', 'url ' + url, 'm image/png', 'alt ' + alt]])
        self.assertTrue(Event.from_json(event.as_json()).verify())
        self.assertEqual(self.nostr.build_event('本文のみ', image_alt=alt).tags(), [])

    async def test_no_ack_is_a_delivery_failure(self):
        for failures in ({self.relay: 'blocked: test rejection'}, {}):
            with self.subTest(failures=bool(failures)):
                self.client.send_event.return_value = SimpleNamespace(success=[], failed=failures)
                with self.assertRaises(main.DeliveryError):
                    await self.nostr.send(self.event)

    async def test_hung_send_is_bounded_by_the_outer_timeout(self):
        async def blocked_send(*args, **kwargs):
            await asyncio.Event().wait()

        self.client.send_event.side_effect = blocked_send
        real_timeout = asyncio.timeout
        with patch.object(main.asyncio, 'timeout', side_effect=lambda seconds: real_timeout(0.01)) as timeout:
            with self.assertRaises(TimeoutError):
                await self.nostr.send(self.event)
        timeout.assert_called_once_with(30)


class OperationalTests(unittest.TestCase):
    def test_image_configuration_loads_explicitly_and_secrets_are_hidden_from_repr(self):
        config = SimpleNamespace(SECRET_KEY='private-nostr-test-key', RELAY_URLS='wss://relay.example.test',
                                 GPSD_HOST='gpsd.example.test', GPSD_PORT=2947)
        environment = {
            'SHIRASU_IMAGE_BASE_URL': 'https://images.example.test',
            'SHIRASU_IMAGE_KEY_ID': 'k1',
            'SHIRASU_IMAGE_SIGNING_KEY': IMAGE_KEY.hex(),
            'SHIRASU_TYPHOON_WAIT_SECONDS': '120',
        }
        with patch.dict(main.os.environ, environment, clear=True):
            settings = main.Settings.load(config)
        self.assertEqual(settings.images.base_url, environment['SHIRASU_IMAGE_BASE_URL'])
        self.assertEqual(settings.typhoon_wait_seconds, 120)
        self.assertNotIn(config.SECRET_KEY, repr(settings))
        self.assertNotIn(IMAGE_KEY.hex(), repr(settings))
        self.assertNotIn(repr(IMAGE_KEY), repr(settings))
        with patch.dict(main.os.environ, {'SHIRASU_NANKAI_WAIT_SECONDS': '60'}, clear=True):
            config.RETRY_MAX_AGE_SECONDS = 120
            settings = main.Settings.load(config)
            self.assertIsNone(settings.images)
        with patch.dict(main.os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, 'shorter than the retry lifetime'):
                main.Settings.load(config)

    def test_typhoon_wait_setting_has_a_finite_bounded_collection_window(self):
        config = SimpleNamespace(SECRET_KEY='unused-test-key', RELAY_URLS='wss://relay.example.test',
                                 GPSD_HOST='gpsd.example.test', GPSD_PORT=2947)
        for value in ('0', '-1', '301', 'nan', 'inf', 'invalid'):
            with self.subTest(value=value), patch.dict(main.os.environ, {'SHIRASU_TYPHOON_WAIT_SECONDS': value}, clear=True):
                with self.assertRaises(ValueError):
                    main.Settings.load(config)

    def test_nankai_wait_applies_without_images_and_precedes_collection_expiry(self):
        config = SimpleNamespace(SECRET_KEY='unused-test-key', RELAY_URLS='wss://relay.example.test',
                                 GPSD_HOST='gpsd.example.test', GPSD_PORT=2947,
                                 RETRY_MAX_AGE_SECONDS=120)
        with patch.dict(main.os.environ, {'SHIRASU_NANKAI_WAIT_SECONDS': '60'}, clear=True):
            settings = main.Settings.load(config)
            self.assertEqual(settings.nankai_wait_seconds, 60)
            self.assertIsNone(settings.images)
        for value in ('0', '-1', '120', '301', 'nan', 'inf', 'invalid'):
            with self.subTest(value=value), patch.dict(main.os.environ, {'SHIRASU_NANKAI_WAIT_SECONDS': value}, clear=True):
                with self.assertRaises(ValueError):
                    main.Settings.load(config)

    def test_settings_load_defaults_and_allow_a_safe_retention_boundary(self):
        config = SimpleNamespace(SECRET_KEY='unused-test-key', RELAY_URLS='wss://relay.example.test',
                                 GPSD_HOST='gpsd.example.test', GPSD_PORT=2947)
        with patch.dict(main.os.environ, {'SHIRASU_STATE_PATH': '/tmp/test-state.sqlite3'}):
            settings = main.Settings.load(config)
            self.assertEqual(settings.state_path, '/tmp/test-state.sqlite3')
            self.assertEqual(settings.retry_max_age, 900)
            self.assertEqual(settings.dedup_seconds, 86400)
            self.assertEqual(settings.max_pending, 1000)
            self.assertEqual(settings.read_timeout, 30)

            config.DEDUP_SECONDS = 960
            self.assertEqual(main.Settings.load(config).dedup_seconds, 960)
            config.RETRY_MAX_AGE_SECONDS = 0
            config.DEDUP_SECONDS = 60
            self.assertEqual(main.Settings.load(config).retry_max_age, 0)

    def test_settings_reject_invalid_limits_and_premature_dedup_expiry(self):
        base = dict(SECRET_KEY='unused-test-key', RELAY_URLS='wss://relay.example.test',
                    GPSD_HOST='gpsd.example.test', GPSD_PORT=2947)
        for name, value in (
            ('RETRY_MAX_AGE_SECONDS', -1), ('RETRY_MAX_AGE_SECONDS', float('nan')),
            ('RETRY_MAX_AGE_SECONDS', float('inf')), ('DEDUP_SECONDS', 0),
            ('DEDUP_SECONDS', 959.9), ('DEDUP_SECONDS', float('nan')),
            ('MAX_PENDING', 0), ('GPSD_READ_TIMEOUT', 0),
            ('GPSD_READ_TIMEOUT', float('inf')),
        ):
            with self.subTest(setting=name, value=value):
                with self.assertRaises(ValueError):
                    main.Settings.load(SimpleNamespace(**base, **{name: value}))

    def test_health_detects_stale_or_failed_processing(self):
        now = 1000
        state = {
            'running': True, 'gpsd_connected': True, 'relay_connected': True,
            'heartbeat': now, 'last_received': now, 'last_delivery_error': None,
        }
        snapshot = {'status': state, 'oldest_pending_age_seconds': None}
        self.assertTrue(healthy(snapshot, now))
        for change in ({'running': False}, {'gpsd_connected': False}, {'relay_connected': False},
                       {'heartbeat': now - 90}, {'heartbeat': now + 1}, {'last_received': now - 90},
                       {'last_delivery_error': 'relay rejected the event'}):
            with self.subTest(change=change):
                self.assertFalse(healthy(dict(snapshot, status=dict(state, **change)), now))
        self.assertFalse(healthy(dict(snapshot, oldest_pending_age_seconds=90), now))
        self.assertTrue(healthy(dict(snapshot, oldest_pending_age_seconds=89), now))

    def test_daemon_lock_prevents_two_writers_and_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            with main.daemon_lock(path):
                with self.assertRaisesRegex(RuntimeError, 'Another shirasu process'):
                    with main.daemon_lock(path):
                        self.fail('Second daemon obtained the lock')
            with main.daemon_lock(path):
                pass


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.settings = main.Settings('unused-test-key', 'wss://relay.example.test',
                                      'gpsd.example.test', 2947, state_path=str(self.path))
        self.nostr = fake_nostr()
        self.enterContext(patch.object(main, 'Nostr', return_value=self.nostr))
        self.enterContext(patch.object(main, 'logger'))
        self.loop = asyncio.get_running_loop()
        # Tests must not replace the runner's process-wide signal handlers.
        self.enterContext(patch.object(self.loop, 'add_signal_handler'))

    async def test_thread_handoff_and_shutdown_complete_without_deadlock(self):
        stop = asyncio.Event()
        report = azarashi.decode(DCR_NMEA, timestamp=RECEIVED_AT)
        loop = self.loop
        receivers = []
        callbacks = []

        class Receiver:
            def __init__(self, host, port, on_report, on_state, thread_stop, **kwargs):
                self.report = on_report
                self.state = on_state
                self.stop = thread_stop
                self.thread_id = None
                self.closed = False
                receivers.append(self)

            def run(self):
                self.thread_id = threading.get_ident()
                self.state(True, None)
                callbacks.append(self.report(report))
                loop.call_soon_threadsafe(stop.set)
                if not self.stop.wait(2):
                    raise RuntimeError('Worker was not stopped')

            def close(self):
                self.closed = True
                self.stop.set()

        with patch.object(main, 'GpsdReceiver', Receiver):
            await asyncio.wait_for(main.run(self.settings, stop), timeout=3)

        self.assertEqual(callbacks, ['queued'])
        self.assertNotEqual(receivers[0].thread_id, threading.get_ident())
        self.assertTrue(receivers[0].closed)
        self.nostr.close.assert_awaited_once()
        snapshot = read_status(self.path)
        self.assertEqual(snapshot['counts']['pending'] + snapshot['counts']['sent'], 1)
        self.assertFalse(snapshot['status']['running'])

    async def test_worker_failure_terminates_run_and_closes_resources(self):
        receiver = SimpleNamespace(run=Mock(side_effect=RuntimeError('receiver failed')), close=Mock())
        with patch.object(main, 'GpsdReceiver', return_value=receiver):
            with self.assertRaisesRegex(RuntimeError, 'receiver failed'):
                await asyncio.wait_for(main.run(self.settings, asyncio.Event()), timeout=3)
        receiver.close.assert_called_once()
        self.nostr.close.assert_awaited_once()
        self.assertFalse(read_status(self.path)['status']['running'])

    async def test_callback_timeout_error_propagates_instead_of_becoming_a_wait_timeout(self):
        report = azarashi.decode(DCR_NMEA, timestamp=RECEIVED_AT)

        class Receiver:
            def __init__(self, host, port, on_report, on_state, thread_stop, **kwargs):
                self.report = on_report
                self.stop = thread_stop

            def run(self):
                self.report(report)

            def close(self):
                self.stop.set()

        with patch.object(main, 'GpsdReceiver', Receiver), \
             patch.object(main.Service, 'accept', new=AsyncMock(side_effect=TimeoutError('database callback failed'))):
            with self.assertRaisesRegex(TimeoutError, 'database callback failed'):
                await asyncio.wait_for(main.run(self.settings, asyncio.Event()), timeout=3)
        self.nostr.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
