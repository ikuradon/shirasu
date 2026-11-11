"""Exercise the real daemon, decoder, SQLite database and Nostr SDK locally.

Run separately from unit tests:
    python -B -m unittest discover -s tests/integration -v

Every socket is bound to loopback and every daemon uses a generated test key and
temporary state. These tests also work inside a Docker --network none container.
"""

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import struct
import sys
import tempfile
import unittest

import azarashi
from nostr_sdk import Event, Keys
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from backup import create_backup, verify_backup


ROOT = Path(__file__).resolve().parents[2]
DCR_NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
MARINE_NMEA = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'


def ubx_frame(sentence):
    """Encode a valid UBX-RXM-SFRBX frame around the QZSS L1S data words."""
    data = bytes.fromhex(sentence.split(',')[2].split('*')[0] + '0')
    # gnssId=QZSS, svId=1 (PRN183), sigId=L1S, eight little-endian words.
    payload = bytes((5, 1, 1, 0, 8, 0, 2, 0))
    payload += b''.join(data[offset:offset + 4][::-1] for offset in range(0, 32, 4))
    message = b'\xb5\x62\x02\x13' + struct.pack('<H', len(payload)) + payload
    a = b = 0
    for value in message[2:]:
        a = (a + value) & 255
        b = (b + a) & 255
    frame = message + bytes((a, b))
    # Fail immediately if a future decoder no longer accepts the test fixture.
    assert azarashi.decode(frame, msg_type='ublox').raw == azarashi.decode(sentence).raw
    return frame


class Gpsd:
    def __init__(self):
        self.accepted = asyncio.Queue()
        self.connections = set()
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer):
        self.connections.add(writer)
        self.tasks.add(asyncio.current_task())
        try:
            watch = await asyncio.wait_for(reader.readline(), timeout=5)
            if watch != b'?WATCH={"enable":true,"raw":2};\n':
                raise AssertionError(f'Unexpected GPSD command: {watch!r}')
            await self.accepted.put(writer)
            await reader.read()
        except (ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.connections.discard(writer)
            self.tasks.discard(asyncio.current_task())

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.connections):
            writer.close()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


class Relay:
    def __init__(self):
        self.mode = 'ack'
        self.received = asyncio.Queue()
        self.accepted = asyncio.Queue()
        self.events = []

    async def start(self):
        self.server = await serve(self.handle, '127.0.0.1', 0,
                                  ping_interval=None, close_timeout=1)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, websocket):
        await self.accepted.put(websocket)
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if message[0] != 'EVENT':
                    continue
                event = message[1]
                self.events.append(event)
                await self.received.put(event)
                if self.mode == 'disconnect_once':
                    self.mode = 'ack'
                    # The relay received the event, but the sender never gets its ACK.
                    websocket.transport.abort()
                    return
                if self.mode == 'ack':
                    await websocket.send(json.dumps(['OK', event['id'], True, '']))
                # "hold" deliberately leaves a received event unacknowledged.
        except ConnectionClosed:
            pass

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


class DeliveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-fault-')
        self.addCleanup(self.directory.cleanup)
        self.directory_path = Path(self.directory.name)
        self.path = self.directory_path / 'state.sqlite3'
        self.log_path = self.directory_path / 'daemon.log'
        self.process = None
        self.gpsd = Gpsd()
        await self.gpsd.start()
        self.addAsyncCleanup(self.gpsd.close)
        self.relay = Relay()
        await self.relay.start()
        self.addAsyncCleanup(self.relay.close)
        self.addAsyncCleanup(self.stop_daemon)
        for filename in ('main.py', 'receiver.py', 'outbox.py', 'message_format.py', 'dcx_instructions.py',
                         'image_publication.py', 'image_protocol.py', 'typhoon.py', 'nankai.py'):
            shutil.copyfile(ROOT / filename, self.directory_path / filename)
        self.keys = Keys.generate()
        (self.directory_path / 'config.py').write_text(
            f'SECRET_KEY = {self.keys.secret_key().to_bech32()!r}\n'
            f'RELAY_URLS = "ws://127.0.0.1:{self.relay.port}"\n'
            f'GPSD_HOST = "127.0.0.1"\nGPSD_PORT = {self.gpsd.port}\n'
            'GPSD_READ_TIMEOUT = 60\n', encoding='utf-8',
        )

    def records(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM events ORDER BY rowid')]

    async def wait_for(self, predicate, label, timeout=30):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            result = predicate()
            if result:
                return result
            if self.process is not None and self.process.returncode is not None:
                self.fail(f'Daemon exited with {self.process.returncode}:\n{self.log_path.read_text()}')
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(f'Timed out waiting for {label}:\n{self.log_path.read_text()}')
            await asyncio.sleep(0.05)

    async def next_event(self):
        try:
            event = await asyncio.wait_for(self.relay.received.get(), timeout=30)
        except TimeoutError:
            self.fail(f'Timed out waiting for a relay event:\n{self.log_path.read_text()}')
        parsed = Event.from_json(json.dumps(event))
        self.assertTrue(parsed.verify())
        self.assertEqual(parsed.author().to_hex(), self.keys.public_key().to_hex())
        return event

    async def start_daemon(self):
        # Execute a copy: Python cannot import the production config.py by accident.
        env = {'PATH': os.environ.get('PATH', ''), 'PYTHONUNBUFFERED': '1',
               'PYTHONDONTWRITEBYTECODE': '1', 'SHIRASU_STATE_PATH': str(self.path)}
        with self.log_path.open('ab') as output:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, '-B', 'main.py', cwd=self.directory_path, env=env,
                stdout=output, stderr=asyncio.subprocess.STDOUT,
            )
        writer = await asyncio.wait_for(self.gpsd.accepted.get(), timeout=10)
        await asyncio.wait_for(self.relay.accepted.get(), timeout=10)
        return writer

    async def stop_daemon(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
                self.fail(f'Daemon did not stop after SIGTERM:\n{self.log_path.read_text()}')
            self.assertEqual(self.process.returncode, 0, self.log_path.read_text())
        self.process = None

    async def send_report(self, writer, sentence):
        writer.write(ubx_frame(sentence))
        await writer.drain()

    async def assert_duplicate_is_suppressed(self, writer, sentence, sent_count):
        before = self.records()[0]['last_seen']
        await self.send_report(writer, sentence)
        await self.wait_for(lambda: self.records()[0]['last_seen'] > before, 'duplicate reception')
        self.assertEqual(len(self.records()), sent_count)
        self.assertTrue(all(row['state'] == 'sent' for row in self.records()))

    async def test_gpsd_disconnect_reconnects_and_delivers_later_report(self):
        writer = await self.start_daemon()
        await self.send_report(writer, DCR_NMEA)
        first = await self.next_event()
        await self.wait_for(lambda: self.records()[0]['state'] == 'sent', 'first ACK commit')

        writer.close()
        await writer.wait_closed()
        reconnected = await asyncio.wait_for(self.gpsd.accepted.get(), timeout=10)
        await self.send_report(reconnected, MARINE_NMEA)
        second = await self.next_event()
        await self.wait_for(lambda: len(self.records()) == 2
                            and all(row['state'] == 'sent' for row in self.records()), 'second ACK commit')

        self.assertNotEqual(first['id'], second['id'])
        await self.assert_duplicate_is_suppressed(reconnected, DCR_NMEA, 2)
        self.assertEqual(len(self.relay.events), 2)

    async def test_relay_disconnect_before_ack_retries_the_same_signed_event(self):
        self.relay.mode = 'disconnect_once'
        writer = await self.start_daemon()
        await self.send_report(writer, DCR_NMEA)
        first = await self.next_event()
        # Intake remains live while the first event has no acknowledgment.
        await self.send_report(writer, MARINE_NMEA)
        await self.wait_for(lambda: len(self.records()) == 2, 'second durable report')

        async with asyncio.timeout(30):
            retried = await self.next_event()
            self.assertEqual(retried, first)
            # The SDK can flush an already buffered retry when reconnecting. Every
            # copy must retain the exact same event, regardless of retry count.
            later = await self.next_event()
            while later['id'] == first['id']:
                self.assertEqual(later, first)
                later = await self.next_event()
        self.assertNotEqual(later['id'], first['id'])
        await self.wait_for(lambda: all(row['state'] == 'sent' for row in self.records()), 'recovery ACKs')
        self.assertGreaterEqual(self.records()[0]['attempts'], 1)
        self.assertTrue(all(event == first for event in self.relay.events[:-1]))
        self.assertEqual(self.relay.events[-1], later)

    async def test_sigkill_before_ack_restores_same_event_and_suppresses_reception(self):
        self.relay.mode = 'hold'
        writer = await self.start_daemon()
        await self.send_report(writer, DCR_NMEA)
        first = await self.next_event()
        record = self.records()[0]
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(json.loads(record['event_json']), first)

        self.process.kill()
        await asyncio.wait_for(self.process.wait(), timeout=5)
        self.assertEqual(self.process.returncode, -signal.SIGKILL)
        self.assertEqual(self.records()[0]['event_json'], record['event_json'])
        self.assertEqual(self.records()[0]['state'], 'pending')

        self.relay.mode = 'ack'
        writer = await self.start_daemon()
        retried = await self.next_event()
        self.assertEqual(retried, first)
        await self.wait_for(lambda: self.records()[0]['state'] == 'sent', 'restart ACK commit')
        await self.assert_duplicate_is_suppressed(writer, DCR_NMEA, 1)
        self.assertEqual(len(self.relay.events), 2)

    async def test_new_database_restored_from_backup_preserves_pending_and_sent_events(self):
        writer = await self.start_daemon()
        await self.send_report(writer, DCR_NMEA)
        sent = await self.next_event()
        await self.wait_for(lambda: self.records()[0]['state'] == 'sent', 'sent event before backup')
        self.relay.mode = 'hold'
        await self.send_report(writer, MARINE_NMEA)
        pending = await self.next_event()
        records = self.records()
        self.assertEqual([row['state'] for row in records], ['sent', 'pending'])

        archive = self.directory_path / 'backup.sqlite3'
        summary = create_backup(self.path, archive)
        self.assertEqual(summary['counts'], {'pending': 1, 'sent': 1, 'expired': 0})
        await self.stop_daemon()
        self.assertEqual(verify_backup(archive), summary)
        original_path = self.path
        self.path = self.directory_path / 'restored.sqlite3'
        shutil.copyfile(archive, self.path)
        self.assertEqual([row['event_json'] for row in self.records()],
                         [row['event_json'] for row in records])

        self.relay.mode = 'ack'
        writer = await self.start_daemon()
        self.assertEqual(await self.next_event(), pending)
        await self.wait_for(lambda: all(row['state'] == 'sent' for row in self.records()),
                            'restored pending ACK commit')
        await self.assert_duplicate_is_suppressed(writer, DCR_NMEA, 2)
        self.assertEqual(self.relay.events, [sent, pending, pending])
        self.assertTrue(original_path.exists())


if __name__ == '__main__':
    unittest.main()
