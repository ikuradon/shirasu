"""Plan offline posting units with the application's collectors and formatters.

This is a new-reception scenario, not a replay of historical delivery. Inputs
arrive in their given order at virtual time zero; ready posts are collected
after each input, then the clock advances to the default 180-second deadline.
The supplied receipt timestamps are decoder date context, never replay delays.
No keys, configuration, existing database, image URL or network are used.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import azarashi
from azarashi.reports import dcr, dcx

from image_protocol import MAX_TIMESTAMP_MS, _records as image_records
from message_format import report_message
from nankai import NankaiCollector, nankai_summary
from outbox import Outbox
from typhoon import TyphoonCollector, report_identity, typhoon_summary


WAIT_SECONDS = 180
MAX_INPUTS = 1000
# Collectors store opaque event strings. This marker cannot be mistaken for a
# signed Nostr event and exists only inside the newly opened in-memory database.
_PREVIEW_EVENT = '{"offline_post_plan":true}'


@dataclass(frozen=True)
class PlannedPost:
    text: str
    record_indexes: tuple[int, ...]
    kind: str
    image_enabled: bool


@contextmanager
def _isolated_decoder():
    """Keep azarashi's process-wide Nankai assembly out of caller state."""
    owner = dcr.NankaiTroughEarthquake
    saved = {name: getattr(owner, name) for name in ('completed', 'reports', 'announcement')}
    try:
        owner.completed, owner.reports, owner.announcement = False, {}, None
        yield
    finally:
        for name, value in saved.items():
            setattr(owner, name, value)


def _inputs(records):
    result = []
    for index, record in enumerate(records):
        if index >= MAX_INPUTS:
            raise ValueError('Too many offline posting inputs')
        if not isinstance(record, (tuple, list)) or len(record) != 2:
            raise ValueError('Posting inputs require (NMEA, received_ms) pairs')
        sentence, milliseconds = record
        if (not isinstance(sentence, str) or type(milliseconds) is not int
                or not 0 <= milliseconds <= MAX_TIMESTAMP_MS):
            raise ValueError('Invalid offline posting input')
        result.append((sentence, milliseconds))
    if not result:
        raise ValueError('At least one posting input is required')
    return tuple(result)


def _image_eligible(entries):
    # Validate the unsigned input using the shared URL protocol. This does not
    # render an image or promise that an external URL/render will succeed.
    try:
        image_records(entries)
    except ValueError:
        return False
    return True


def plan_posts(records):
    """Return actual formatter text and source indexes for a finite scenario.

    ``record_indexes`` follows each collector's entry order, not numeric index
    order. Duplicate receptions and superseded pending inputs need not appear
    in a post. Null/unknown DCX are excluded just as in the service; malformed
    decoding or collection failures raise instead of inventing a grouped post.
    Images are enabled for eligible non-Nankai posts, without producing URLs.
    """
    records = _inputs(records)
    posts, accepted = [], {}
    with _isolated_decoder(), Outbox(':memory:', 'offline-gallery-posts') as box:
        nankai = NankaiCollector(box, wait_seconds=WAIT_SECONDS)
        typhoons = TyphoonCollector(box, wait_seconds=WAIT_SECONDS, collect=True)

        def append_batch(collector, batch, kind, text, now):
            result = collector.complete(batch, _PREVIEW_EVENT, 'offline-' + batch.key, now)
            if result != 'queued':
                raise ValueError(f'Offline {kind} batch could not be queued: {result}')
            try:
                indexes = tuple(accepted[entry] for entry in batch.entries)
            except KeyError as error:
                raise ValueError('A collected post lost its original input') from error
            posts.append(PlannedPost(text, indexes, kind,
                                     kind != 'nankai' and _image_eligible(batch.entries)))

        def drain(now):
            # Match Service.publish_one's collection order before delivery.
            while True:
                nankai.maintain(now)
                typhoons.maintain(now)
                found = False
                batch = nankai.due(now)
                if batch is not None:
                    append_batch(nankai, batch, 'nankai', nankai_summary(batch), now)
                    found = True
                batch = typhoons.due(now)
                if batch is not None:
                    append_batch(typhoons, batch, 'typhoon',
                                 typhoon_summary(batch.reports, update=batch.update), now)
                    found = True
                if not found:
                    return

        for index, (sentence, milliseconds) in enumerate(records):
            try:
                report = azarashi.decode(sentence, timestamp=datetime.fromtimestamp(
                    milliseconds / 1000, timezone.utc))
            except azarashi.AzarashiException as error:
                raise ValueError(f'Cannot decode posting input {index}') from error
            if isinstance(report, (dcx.NullMsg, dcx.Unknown)):
                continue
            if not isinstance(report, (dcr.Base, dcx.AlertBase)):
                raise ValueError(f'Unsupported posting input {index}')
            nankai.maintain(0)
            typhoons.maintain(0)
            result = nankai.accept(report, 0)
            if result == 'immediate':
                result = typhoons.accept(report, 0)
            if result == 'buffered':
                # Only accepted records replace provenance. Repeated satellite
                # transmissions do not overwrite the collector's first entry.
                accepted[(report.nmea, round(report.timestamp.timestamp() * 1000))] = index
            elif result == 'immediate':
                text = report_message(report)
                key = report_identity(report)
                result = box.enqueue(key, _PREVIEW_EVENT, 'offline-' + key, 0)
                if result == 'queued':
                    entries = ((report.nmea, round(report.timestamp.timestamp() * 1000)),)
                    posts.append(PlannedPost(text, (index,), 'single',
                                             not isinstance(report, dcr.NankaiTroughEarthquake)
                                             and _image_eligible(entries)))
            if result not in ('buffered', 'duplicate', 'queued'):
                raise ValueError(f'Posting input {index} could not be planned: {result}')
            drain(0)
        drain(WAIT_SECONDS)
    return tuple(posts)
