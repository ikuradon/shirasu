"""Receive QZSS reports and deliver them through a durable Nostr outbox."""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
import fcntl
import hashlib
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time

from azarashi.reports import dcr, dcx
from nostr_sdk import (
    ClientBuilder, Event, EventBuilder, Keys, Kind, NostrSdkError,
    RelayStatus, RelayUrl, SignerAuthenticator, Tag,
)

from shirasu.outbox import Outbox
from shirasu.receiver import GpsdReceiver
from shirasu.reports.format import report_message
from shirasu.images.publication import ImagePublisher, image_entry, with_image
from shirasu.reports.publication import message_hashtags
from shirasu.images.alt import DEFAULT_IMAGE_ALT, image_alt_text
from shirasu.images.nankai import nankai_topic_entries


logger = logging.getLogger('shirasu')


@dataclass
class Settings:
    secret_key: str = field(repr=False)
    relay_urls: str
    gpsd_host: str
    gpsd_port: int
    state_path: str = 'state/shirasu.sqlite3'
    retry_max_age: float = 900
    dedup_seconds: float = 86400
    max_pending: int = 1000
    read_timeout: float = 30
    images: ImagePublisher | None = None
    typhoon_wait_seconds: float = 180
    nankai_wait_seconds: float = 180

    @classmethod
    def load(cls, config):
        settings = cls(
            config.SECRET_KEY, config.RELAY_URLS, config.GPSD_HOST, config.GPSD_PORT,
            state_path=os.environ.get('SHIRASU_STATE_PATH', 'state/shirasu.sqlite3'),
            retry_max_age=float(getattr(config, 'RETRY_MAX_AGE_SECONDS', 900)),
            dedup_seconds=float(getattr(config, 'DEDUP_SECONDS', 86400)),
            max_pending=int(getattr(config, 'MAX_PENDING', 1000)),
            read_timeout=float(getattr(config, 'GPSD_READ_TIMEOUT', 30)),
            images=ImagePublisher.from_environment(os.environ),
            typhoon_wait_seconds=float(os.environ.get('SHIRASU_TYPHOON_WAIT_SECONDS', '180')),
            nankai_wait_seconds=float(os.environ.get('SHIRASU_NANKAI_WAIT_SECONDS', '180')),
        )
        if (not all(math.isfinite(v) for v in (settings.retry_max_age, settings.dedup_seconds, settings.read_timeout))
                or settings.retry_max_age < 0 or settings.dedup_seconds <= 0
                or settings.max_pending <= 0 or settings.read_timeout <= 0):
            raise ValueError('Invalid retry, retention, capacity, or read-timeout setting')
        if settings.dedup_seconds < settings.retry_max_age + 60:
            raise ValueError('DEDUP_SECONDS must exceed RETRY_MAX_AGE_SECONDS by at least 60 seconds')
        if (not math.isfinite(settings.typhoon_wait_seconds)
                or not 1 <= settings.typhoon_wait_seconds <= 300
                or (settings.images and settings.retry_max_age
                    and settings.typhoon_wait_seconds >= settings.retry_max_age)):
            raise ValueError('Typhoon wait must be 1..300 seconds and shorter than the retry lifetime')
        if (not math.isfinite(settings.nankai_wait_seconds)
                or not 1 <= settings.nankai_wait_seconds <= 300
                or (settings.retry_max_age
                    and settings.nankai_wait_seconds >= settings.retry_max_age)):
            raise ValueError('Nankai wait must be 1..300 seconds and shorter than the retry lifetime')
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

    def build_event(self, message, image_url=None, image_alt=None, image_mime='image/png'):
        builder = EventBuilder(Kind(1), message)
        tags = [Tag.parse(['t', hashtag]) for hashtag in message_hashtags(message)]
        if image_url is not None:
            if image_mime not in ('image/png', 'image/webp'):
                raise ValueError('Unsupported image MIME type')
            alt = image_alt if isinstance(image_alt, str) and image_alt.strip() else DEFAULT_IMAGE_ALT
            tags.append(Tag.parse(['imeta', f'url {image_url}',
                                   'm ' + image_mime, 'alt ' + alt]))
        return builder.tags(tags).finalize(self.keys)

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
    def __init__(self, nostr, outbox, stop, clock=time.time, *, images=None,
                 typhoon_wait_seconds=180, nankai_wait_seconds=180):
        self.nostr = nostr
        self.outbox = outbox
        self.stop = stop
        self.clock = clock
        self.images = images
        from shirasu.reports.nankai import NankaiCollector
        self.nankai = NankaiCollector(outbox, wait_seconds=nankai_wait_seconds)
        self.typhoons = None
        saved_collection = outbox.connection.execute('''
            SELECT 1 FROM metadata WHERE name = 'typhoon_schema_version'
            UNION ALL SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'typhoon_reports' LIMIT 1
        ''').fetchone()
        if images is not None or saved_collection is not None:
            from shirasu.reports.typhoon import TyphoonCollector
            self.typhoons = TyphoonCollector(outbox, wait_seconds=typhoon_wait_seconds,
                                             collect=images is not None)
        self.wake = asyncio.Event()
        self.state = outbox.snapshot(clock())['status']
        self.state.update(running=True, gpsd_connected=False, relay_connected=False)
        self.state['images_enabled'] = images is not None

    def _build_event(self, message, entries, *, include_image=True):
        if self.images is not None and include_image:
            try:
                image_url = self.images.url(entries)
            except (ValueError, TypeError, OverflowError) as error:
                # Invalid image input must never prevent the readable report.
                self.state['last_image_error'] = type(error).__name__
                logger.warning('Image URL unavailable: %s; publishing text', type(error).__name__)
            else:
                self.state['last_image_error'] = None
                try:
                    alt = image_alt_text(entries)
                except Exception as error:
                    # A description failure must not suppress a valid image or
                    # the readable report; retries reuse the signed fallback.
                    logger.warning('Image description unavailable: %s; using default', type(error).__name__)
                    alt = DEFAULT_IMAGE_ALT
                # Keep the existing PNG call contract, while signing the MIME
                # selected by the WebP URL profile for newly created events.
                image_options = {'image_url': image_url, 'image_alt': alt}
                image_mime = getattr(self.images, 'mime', 'image/png')
                if image_mime != 'image/png':
                    image_options['image_mime'] = image_mime
                return self.nostr.build_event(with_image(message, image_url), **image_options)
        return self.nostr.build_event(message)

    def _full(self):
        self.state['dropped_full'] = self.state.get('dropped_full', 0) + 1
        self.state['last_delivery_error'] = 'Outbox full; received report could not be saved'
        logger.error('Outbox full; received report could not be saved')

    def _enqueue(self, key, event, now):
        self.nankai.maintain(now)
        if self.typhoons is not None:
            self.typhoons.maintain(now)
        return self.outbox.enqueue(key, event.as_json(), event.id().to_hex(), now)

    async def accept(self, report):
        now = self.clock()
        self.state['last_received'] = now
        if not is_publishable(report):
            return 'ignored'
        # Capacity is shared by signed events and both durable collectors.
        self.nankai.maintain(now)
        if self.typhoons is not None:
            self.typhoons.maintain(now)
        result = self.nankai.accept(report, now)
        if result != 'immediate':
            if result == 'full':
                self._full()
            elif result == 'buffered':
                self.wake.set()
            return result
        if self.typhoons is not None:
            result = self.typhoons.accept(report, now)
            if result != 'immediate':
                if result == 'full':
                    self._full()
                elif result == 'buffered':
                    self.wake.set()
                return result
        # Invalid/uncollected Nankai input must remain text-only. Topic images
        # are selected only from validated complete/cancellation batches.
        include_image = not isinstance(report, dcr.NankaiTroughEarthquake)
        entries = ()
        if self.images is not None and include_image:
            try:
                entries = (image_entry(report),)
            except (ValueError, TypeError, OverflowError, AttributeError):
                pass  # _build_event records the error and preserves the text.
        event = self._build_event(report_message(report), entries, include_image=include_image)
        result = self._enqueue(report_key(report), event, now)
        if result == 'queued':
            self.state['last_queued'] = now
            logger.info('Queued report=%s event=%s', type(report).__name__, event.id().to_hex())
            self.wake.set()
        elif result == 'full':
            self._full()
        return result

    async def publish_one(self):
        from shirasu.reports.nankai import nankai_summary
        now = self.clock()
        self.nankai.maintain(now)
        if self.typhoons is not None:
            self.typhoons.maintain(now)
        batch = self.nankai.due(now)
        if batch is not None:
            entries = ()
            if self.images is not None and getattr(self.images, 'supports_nankai_topic', False) is True:
                try:
                    entries = nankai_topic_entries(batch)
                except (ValueError, TypeError, OverflowError) as error:
                    logger.warning('Nankai image input unavailable: %s; publishing text', type(error).__name__)
            event = self._build_event(nankai_summary(batch), entries, include_image=bool(entries))
            result = self.nankai.complete(batch, event.as_json(), event.id().to_hex(), now)
            if result == 'queued':
                self.state['last_queued'] = now
                logger.info('Queued Nankai summary event=%s', event.id().to_hex())
        if self.typhoons is not None:
            from shirasu.reports.typhoon import typhoon_summary
            batch = self.typhoons.due(self.clock())
            if batch is not None:
                event = self._build_event(typhoon_summary(batch.reports, update=batch.update), batch.entries)
                result = self.typhoons.complete(batch, event.as_json(), event.id().to_hex(), self.clock())
                if result == 'queued':
                    self.state['last_queued'] = self.clock()
                    logger.info('Queued typhoon summary reports=%s event=%s',
                                len(batch.entries), event.id().to_hex())
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
        self.state['nankai_buffered_reports'] = self.nankai.waiting_count()
        if self.typhoons is not None:
            self.state['typhoon_buffered_reports'] = self.typhoons.pending_count()
        else:
            self.state['typhoon_buffered_reports'] = 0
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
            service = Service(nostr, outbox, stop, images=settings.images,
                              typhoon_wait_seconds=settings.typhoon_wait_seconds,
                              nankai_wait_seconds=settings.nankai_wait_seconds)
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
