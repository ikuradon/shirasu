import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import azarashi
from azarashi.reports import dcx
from nostr_sdk import Keys, RelayUrl


# Load the application with a fake config so tests never read production keys.
config = ModuleType('config')
config.SECRET_KEY = 'unused-test-key'
config.RELAY_URLS = 'wss://relay.example.test'
config.GPSD_HOST = 'gpsd.example.test'
config.GPSD_PORT = 2947
spec = importlib.util.spec_from_file_location(
    'shirasu_under_test', Path(__file__).resolve().parents[1] / 'main.py',
)
main = importlib.util.module_from_spec(spec)
with patch.dict('sys.modules', {'config': config}):
    spec.loader.exec_module(main)


# The first two are valid synthetic DCX frames (Japan, null/unknown provider).
# The DCR frame is the upstream earthquake-early-warning example.
NULL_NMEA = '$QZQSM,55,53B0000DE000000000000000000000000000000000000000000000000E41D10*04'
UNKNOWN_NMEA = '$QZQSM,55,53B0000DFF00000000000000000000000000000000000000000000000B82B78*71'
DCR_NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
RECEIVED_AT = datetime(2026, 11, 8, tzinfo=timezone.utc)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.nostr = SimpleNamespace(post=AsyncMock())

    def decode_stream(self, stream):
        return azarashi.decode_stream(
            stream, msg_type='nmea', callback=main.shirasu,
            callback_args=(self.loop, self.nostr), unique=True,
            ignore_dcx=False, timestamp=RECEIVED_AT,
        )

    def test_null_and_unknown_dcx_reports_are_not_published(self):
        for sentence, expected_type in ((NULL_NMEA, dcx.NullMsg), (UNKNOWN_NMEA, dcx.Unknown)):
            with self.subTest(report=expected_type.__name__):
                report = azarashi.decode(sentence, timestamp=RECEIVED_AT)
                self.assertIsInstance(report, expected_type)
                with redirect_stdout(io.StringIO()):
                    main.shirasu(report, self.loop, self.nostr)
        self.nostr.post.assert_not_awaited()

    def test_repeated_dcr_is_published_once_with_text_and_original_nmea(self):
        report = azarashi.decode(DCR_NMEA, timestamp=RECEIVED_AT)
        with io.BytesIO(((DCR_NMEA + '\n') * 2).encode()) as stream:
            with redirect_stdout(io.StringIO()), self.assertRaises(azarashi.AzarashiNoMoreData):
                self.decode_stream(stream)
        self.nostr.post.assert_awaited_once_with(f'{report}\n---\nNMEA: {DCR_NMEA}')

    def test_failed_delivery_can_retry_the_next_identical_report(self):
        self.nostr.post.side_effect = [RuntimeError('relay unavailable'), None]
        with io.BytesIO(((DCR_NMEA + '\n') * 2).encode()) as stream:
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'relay unavailable'):
                    self.decode_stream(stream)
                with self.assertRaises(azarashi.AzarashiNoMoreData):
                    self.decode_stream(stream)
        self.assertEqual(self.nostr.post.await_count, 2)


class NostrTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_signs_a_kind_one_event_with_the_configured_key(self):
        nostr = main.Nostr()
        nostr.keys = Keys.generate()
        output = SimpleNamespace(success=[RelayUrl.parse('wss://relay.example.test')], failed={})
        nostr.client = SimpleNamespace(send_event=AsyncMock(return_value=output))

        result = await nostr.post('通報本文\n---\nNMEA: sample')

        self.assertIs(result, output)
        nostr.client.send_event.assert_awaited_once()
        event = nostr.client.send_event.await_args.args[0]
        self.assertEqual(event.kind().as_u16(), 1)
        self.assertEqual(event.content(), '通報本文\n---\nNMEA: sample')
        self.assertEqual(event.author().to_hex(), nostr.keys.public_key().to_hex())
        self.assertTrue(event.verify())

    async def test_no_relay_acknowledgment_is_a_publication_failure(self):
        nostr = main.Nostr()
        nostr.keys = Keys.generate()
        relay = RelayUrl.parse('wss://relay.example.test')
        for failures in ({relay: 'blocked: test rejection'}, {}):
            with self.subTest(failures=bool(failures)):
                nostr.client = SimpleNamespace(send_event=AsyncMock(
                    return_value=SimpleNamespace(success=[], failed=failures),
                ))
                with redirect_stderr(io.StringIO()), self.assertRaisesRegex(
                    RuntimeError, 'No Nostr relay accepted',
                ):
                    await nostr.post('must not count as delivered')


class ReceiverLoopTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.nostr = SimpleNamespace(init=AsyncMock(), post=AsyncMock(), close=AsyncMock())
        self.stream = io.BytesIO()
        self.sock = MagicMock()
        self.sock.__enter__.return_value = self.sock
        self.sock.makefile.return_value = self.stream
        self.factory = self.enterContext(patch.object(main, 'Nostr', return_value=self.nostr))
        self.connect = self.enterContext(patch.object(main.socket, 'create_connection', return_value=self.sock))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))

    def test_invalid_frame_is_skipped_and_following_report_is_delivered(self):
        report = azarashi.decode(DCR_NMEA, timestamp=RECEIVED_AT)
        calls = 0

        def decode(stream, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise azarashi.AzarashiInvalidMessageError('damaged frame')
            kwargs['callback'](report, *kwargs['callback_args'])
            raise azarashi.AzarashiNoMoreData('end of stream')

        with patch.object(main.azarashi, 'decode_stream', side_effect=decode):
            self.assertEqual(main.main(self.loop), 0)

        self.assertEqual(calls, 2)
        self.assertEqual(self.nostr.post.await_args_list[0].args, ('booting...',))
        self.assertIn(DCR_NMEA, self.nostr.post.await_args_list[1].args[0])
        self.assertEqual(self.nostr.post.await_count, 2)
        self.nostr.close.assert_awaited_once()
        self.assertTrue(self.stream.closed)
        self.sock.__exit__.assert_called_once()

    def test_broken_stream_exits_for_restart_and_closes_resources(self):
        with patch.object(main.azarashi, 'decode_stream', side_effect=azarashi.AzarashiDisconnectedError('gone')):
            self.assertEqual(main.main(self.loop), 1)
        self.nostr.close.assert_awaited_once()
        self.assertTrue(self.stream.closed)
        self.sock.__exit__.assert_called_once()

    def test_initialization_failure_exits_with_failure_and_closes_client(self):
        self.nostr.init.side_effect = RuntimeError('cannot initialize relay')
        self.assertEqual(main.main(self.loop), 1)
        self.connect.assert_not_called()
        self.nostr.post.assert_not_awaited()
        self.nostr.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
