import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import azarashi
from azarashi.reports import dcx
from nostr_sdk import Event, EventBuilder, Keys, Kind, RelayUrl

import main
from outbox import Outbox, read_status


# Valid synthetic null/unknown DCX frames and real DCR examples.
NULL_NMEA = '$QZQSM,55,53B0000DE000000000000000000000000000000000000000000000000E41D10*04'
UNKNOWN_NMEA = '$QZQSM,55,53B0000DFF00000000000000000000000000000000000000000000000B82B78*71'
DCR_NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
MARINE_NMEA = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'
RECEIVED_AT = datetime(2026, 11, 8, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fake_nostr():
    # Real local cryptography, mocked transport: no production key or network.
    keys = Keys.generate()
    return SimpleNamespace(
        identity=keys.public_key().to_hex(),
        build_event=Mock(side_effect=lambda text: EventBuilder(Kind(1), text).finalize(keys)),
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
        self.assertEqual(event.content(), f'{self.report}\n---\nNMEA: {DCR_NMEA}')
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
            f'---\nNMEA: {MARINE_NMEA}'
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
        self.assertTrue(main.healthy(snapshot, now))
        for change in ({'running': False}, {'gpsd_connected': False}, {'relay_connected': False},
                       {'heartbeat': now - 90}, {'heartbeat': now + 1}, {'last_received': now - 90},
                       {'last_delivery_error': 'relay rejected the event'}):
            with self.subTest(change=change):
                self.assertFalse(main.healthy(dict(snapshot, status=dict(state, **change)), now))
        self.assertFalse(main.healthy(dict(snapshot, oldest_pending_age_seconds=90), now))
        self.assertTrue(main.healthy(dict(snapshot, oldest_pending_age_seconds=89), now))

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
