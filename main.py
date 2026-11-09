"""Receive QZSS reports and deliver them through a durable Nostr outbox."""

import argparse
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time

from azarashi.reports import dcx
from nostr_sdk import (
    ClientBuilder, Event, EventBuilder, Keys, Kind, NostrSdkError,
    RelayStatus, RelayUrl, SignerAuthenticator,
)

from outbox import Outbox, read_history, read_status
from receiver import GpsdReceiver
from message_format import report_message


logger = logging.getLogger('shirasu')


@dataclass
class Settings:
    secret_key: str
    relay_urls: str
    gpsd_host: str
    gpsd_port: int
    state_path: str = 'state/shirasu.sqlite3'
    retry_max_age: float = 900
    dedup_seconds: float = 86400
    max_pending: int = 1000
    read_timeout: float = 30

    @classmethod
    def load(cls, config):
        settings = cls(
            config.SECRET_KEY, config.RELAY_URLS, config.GPSD_HOST, config.GPSD_PORT,
            state_path=os.environ.get('SHIRASU_STATE_PATH', 'state/shirasu.sqlite3'),
            retry_max_age=float(getattr(config, 'RETRY_MAX_AGE_SECONDS', 900)),
            dedup_seconds=float(getattr(config, 'DEDUP_SECONDS', 86400)),
            max_pending=int(getattr(config, 'MAX_PENDING', 1000)),
            read_timeout=float(getattr(config, 'GPSD_READ_TIMEOUT', 30)),
        )
        if (not all(math.isfinite(v) for v in (settings.retry_max_age, settings.dedup_seconds, settings.read_timeout))
                or settings.retry_max_age < 0 or settings.dedup_seconds <= 0
                or settings.max_pending <= 0 or settings.read_timeout <= 0):
            raise ValueError('Invalid retry, retention, capacity, or read-timeout setting')
        if settings.dedup_seconds < settings.retry_max_age + 60:
            raise ValueError('DEDUP_SECONDS must exceed RETRY_MAX_AGE_SECONDS by at least 60 seconds')
        return settings


@contextmanager
def daemon_lock(path):
    lock = Path(str(path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another shirasu process is using this state database') from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class DeliveryError(Exception):
    """No relay confirmed delivery; retry the same signed event."""


class Nostr:
    def __init__(self, secret_key, relay_urls):
        self.keys = Keys.parse(secret_key)
        self.urls = [RelayUrl.parse(url.strip()) for url in relay_urls.split(',') if url.strip()]
        if not self.urls:
            raise ValueError('At least one Nostr relay is required')
        self.client = ClientBuilder().authenticator(SignerAuthenticator(self.keys)).build()

    @property
    def identity(self):
        return self.keys.public_key().to_hex()

    def build_event(self, message):
        return EventBuilder(Kind(1), message).finalize(self.keys)

    async def connect(self):
        for url in self.urls:
            await self.client.add_relay(url)
        await self.client.connect()

    async def connected(self):
        return any(relay.status() == RelayStatus.CONNECTED
                   for relay in (await self.client.relays()).values())

    async def send(self, event):
        async with asyncio.timeout(30):
            await self.client.connect()
            result = await self.client.send_event(
                event, ok_timeout=timedelta(seconds=10),
                authentication_timeout=timedelta(seconds=10),
            )
        for url, reason in result.failed.items():
            logger.warning('Relay rejected event=%s relay=%s reason=%s', event.id().to_hex(), url, reason)
        if not result.success:
            raise DeliveryError('No Nostr relay accepted the event')

    async def close(self):
        await self.client.shutdown()


def report_key(report):
    # Match decoder equality, independent of receipt time or satellite PRN.
    prefix = f'{report.message_type}:{type(report).__name__}:'.encode()
    return hashlib.sha256(prefix + bytes(report.raw)).hexdigest()


def is_publishable(report):
    return not isinstance(report, (dcx.NullMsg, dcx.Unknown))


class Service:
    def __init__(self, nostr, outbox, stop, clock=time.time):
        self.nostr = nostr
        self.outbox = outbox
        self.stop = stop
        self.clock = clock
        self.wake = asyncio.Event()
        self.state = outbox.snapshot(clock())['status']
        self.state.update(running=True, gpsd_connected=False, relay_connected=False)

    async def accept(self, report):
        now = self.clock()
        self.state['last_received'] = now
        if not is_publishable(report):
            return 'ignored'
        event = self.nostr.build_event(report_message(report))
        result = self.outbox.enqueue(report_key(report), event.as_json(), event.id().to_hex(), now)
        if result == 'queued':
            self.state['last_queued'] = now
            logger.info('Queued report=%s event=%s', type(report).__name__, event.id().to_hex())
            self.wake.set()
        elif result == 'full':
            self.state['dropped_full'] = self.state.get('dropped_full', 0) + 1
            self.state['last_delivery_error'] = 'Outbox full; received report could not be saved'
            logger.error('Outbox full; could not save report=%s', type(report).__name__)
        return result

    async def publish_one(self):
        maintenance = self.outbox.maintain(self.clock())
        if maintenance['expired']:
            logger.warning('Expired %s undelivered reports; retry deadline reached', maintenance['expired'])
        record = self.outbox.next_due(self.clock())
        if record is None:
            return False
        # Corrupt local state is not a transient network failure.
        event = Event.from_json(record['event_json'])
        if event.id().to_hex() != record['event_id'] or not event.verify():
            raise RuntimeError('Invalid signed event in outbox')
        try:
            await self.nostr.send(event)
        except (DeliveryError, NostrSdkError, TimeoutError, ConnectionError) as error:
            delay = min(60, 2 ** min(record['attempts'], 6))
            self.outbox.mark_failed(record['key'], str(error), self.clock(), delay)
            self.state['last_delivery_error'] = str(error)[:1000]
            logger.warning('Delivery failed event=%s attempt=%s retry_in=%ss reason=%s',
                           record['event_id'], record['attempts'] + 1, delay, error)
        else:
            self.outbox.mark_sent(record['key'], self.clock())
            self.state['last_published'] = self.clock()
            self.state['last_delivery_error'] = None
            logger.info('Published event=%s', record['event_id'])
        return True

    async def publisher(self):
        while not self.stop.is_set():
            self.wake.clear()
            if not await self.publish_one():
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1)
                except TimeoutError:
                    pass

    def receiver_state(self, connected, error):
        self.state['gpsd_connected'] = connected
        self.state['last_receiver_error'] = error

    def save_status(self):
        now = self.clock()
        for name, value in self.state.items():
            self.outbox.set_status(name, value, now)
        self.outbox.set_status('heartbeat', now, now)

    async def monitor(self):
        last_log = 0
        while not self.stop.is_set():
            self.state['relay_connected'] = await self.nostr.connected()
            self.save_status()
            now = self.clock()
            if now - last_log >= 60:
                snapshot = self.outbox.snapshot(now)
                logger.info('Status gpsd=%s relay=%s pending=%s expired=%s last_received=%s last_published=%s',
                            self.state['gpsd_connected'], self.state['relay_connected'],
                            snapshot['counts']['pending'], snapshot['counts']['expired'],
                            self.state.get('last_received'), self.state.get('last_published'))
                last_log = now
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=10)
            except TimeoutError:
                pass


async def run(settings, stop=None):
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    with daemon_lock(settings.state_path):
        nostr = Nostr(settings.secret_key, settings.relay_urls)
        with Outbox(settings.state_path, nostr.identity, settings.dedup_seconds,
                    settings.retry_max_age, settings.max_pending) as outbox:
            service = Service(nostr, outbox, stop)
            thread_stop = threading.Event()

            def receive(report):
                future = asyncio.run_coroutine_threadsafe(service.accept(report), loop)
                while True:
                    try:
                        return future.result(timeout=0.5)
                    except FutureTimeout:
                        if future.done():
                            return future.result()
                        if thread_stop.is_set():
                            future.cancel()
                            return

            receiver = GpsdReceiver(
                settings.gpsd_host, settings.gpsd_port, receive,
                lambda connected, error: loop.call_soon_threadsafe(service.receiver_state, connected, error),
                thread_stop, read_timeout=settings.read_timeout,
            )
            tasks = []
            receiver_task = None
            try:
                await nostr.connect()
                logger.info('Starting state=%s retry_max_age=%ss dedup=%ss',
                            settings.state_path, settings.retry_max_age, settings.dedup_seconds)
                service.save_status()
                receiver_task = asyncio.create_task(asyncio.to_thread(receiver.run), name='gpsd')
                tasks = [receiver_task, asyncio.create_task(service.publisher(), name='publisher'),
                         asyncio.create_task(service.monitor(), name='monitor'),
                         asyncio.create_task(stop.wait(), name='stop')]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if not stop.is_set():
                    raise RuntimeError('A background worker stopped unexpectedly')
            finally:
                stop.set()
                receiver.close()
                # The event loop must stay alive until receiver callbacks return.
                for task in tasks:
                    if task is not receiver_task:
                        task.cancel()
                await asyncio.gather(*(t for t in tasks if t is not receiver_task), return_exceptions=True)
                if receiver_task is not None:
                    await asyncio.gather(receiver_task, return_exceptions=True)
                service.state.update(running=False, gpsd_connected=False, relay_connected=False)
                service.save_status()
                await nostr.close()
                logger.info('Stopped; undelivered reports remain in the outbox')


def healthy(snapshot, now):
    state = snapshot['status']
    heartbeat = state.get('heartbeat', 0)
    pending_age = snapshot.get('oldest_pending_age_seconds')
    return bool(state.get('running') and state.get('gpsd_connected')
                and state.get('relay_connected') and 0 <= now - heartbeat < 90
                and 0 <= now - state.get('last_received', 0) < 90
                and not state.get('last_delivery_error')
                and (pending_age is None or pending_age < 90))


def received_at(value):
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timestamp.utcoffset() is None:
            raise ValueError('timezone is required')
        return timestamp.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError('use an ISO 8601 date/time with a timezone, e.g. 2026-11-08T12:00:00+09:00') from error


def run_preview(args):
    from preview import preview_stream

    timestamp = args.received_at or datetime.now(timezone.utc)
    total = dict(decoded=0, shown=0, ignored=0, errors=0)
    for filename in args.preview:
        try:
            # Standard input belongs to the caller; only close files opened here.
            source = nullcontext(sys.stdin.buffer) if filename == '-' else open(filename, 'rb')
            with source as stream:
                counts = preview_stream(
                    stream, input_format=args.input_format or 'nmea', received_at=timestamp,
                    message_formatter=report_message, report_filter=is_publishable,
                    stdout=sys.stdout, stderr=sys.stderr, source_name=filename,
                )
            for name in total:
                total[name] += counts[name]
        except (OSError, ValueError) as error:
            total['errors'] += 1
            message = ' '.join(str(error).split())
            message = ''.join(char for char in message if char.isprintable())
            print(f'入力を読み取れません: {message}', file=sys.stderr)
    print(f"プレビュー: 読取 {total['decoded']}件 / 表示 {total['shown']}件 / "
          f"除外 {total['ignored']}件 / エラー {total['errors']}件", file=sys.stderr)
    if not total['decoded']:
        print('通報データが見つかりません。入力ファイルと --input-format を確認してください。', file=sys.stderr)
    return 1 if total['errors'] or not total['decoded'] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--status', action='store_true', help='print saved runtime state (default: JSON)')
    group.add_argument('--health', action='store_true', help='exit successfully only when healthy')
    group.add_argument('--history', action='store_true', help='list saved delivery history (default: text)')
    group.add_argument('--preview', nargs='+', metavar='FILE', help='preview reports from files; use - for standard input')
    parser.add_argument('--format', choices=('text', 'json'), help='output format for --status or --history')
    parser.add_argument('--state', choices=('pending', 'sent', 'expired'), help='filter --history by delivery state')
    parser.add_argument('--limit', type=int, help='maximum history rows (default: 20, maximum: 1000)')
    parser.add_argument('--input-format', choices=('nmea', 'ubx'), help='preview input format (default: nmea)')
    parser.add_argument('--received-at', type=received_at, help='preview reference date/time with timezone (default: current UTC time)')
    args = parser.parse_args(argv)
    if args.format and not (args.status or args.history):
        parser.error('--format requires --status or --history')
    if (args.state is not None or args.limit is not None) and not args.history:
        parser.error('--state and --limit require --history')
    if args.limit is not None and not 1 <= args.limit <= 1000:
        parser.error('--limit must be between 1 and 1000')
    if (args.input_format is not None or args.received_at is not None) and not args.preview:
        parser.error('--input-format and --received-at require --preview')
    if args.preview and args.preview.count('-') > 1:
        parser.error('standard input (-) can only be used once')
    logging.basicConfig(level=os.environ.get('SHIRASU_LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    if args.preview:
        return run_preview(args)
    if args.status or args.health or args.history:
        # Inspection commands never load config, start workers, or maintain state.
        try:
            path = os.environ.get('SHIRASU_STATE_PATH', 'state/shirasu.sqlite3')
            if args.history:
                from inspection import format_history, prepare_history
                history = prepare_history(read_history(path, state=args.state, limit=args.limit or 20))
                if args.format == 'json':
                    print(json.dumps(history, ensure_ascii=False, indent=2))
                else:
                    print(format_history(history))
                return 0
            now = time.time()
            snapshot = read_status(path, now=now)
            ok = healthy(snapshot, now)
            if args.status:
                snapshot = dict(snapshot, healthy=ok)
                if args.format == 'text':
                    from inspection import format_status
                    print(format_status(snapshot, now))
                else:
                    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            return 0 if args.status or ok else 1
        except Exception as error:
            print(f'保存状態を読み取れません: {error}', file=sys.stderr)
            return 1
    try:
        import config
        asyncio.run(run(Settings.load(config)))
        return 0
    except Exception:
        logger.exception('Shirasu stopped with an error')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
