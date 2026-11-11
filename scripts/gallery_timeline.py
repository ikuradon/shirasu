"""Replay explicit reception delays without reading live receiver state.

The clock is virtual. Decoder timestamps remain date anchors, never arrival
delays. At an equal second the timer runs before a reception; simultaneous
receptions run in input order. Accepted inputs wake the publisher; duplicates
and ignored inputs do not interrupt its one-second idle wait. Delivery is
assumed to succeed immediately, so displayed times are planned publication
times rather than a claim about relay acknowledgements or historical reception.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import azarashi
from azarashi.reports import dcr, dcx

from message_format import report_message
from nankai import NankaiCollector, nankai_summary
from nankai_image import nankai_topic_entries
from outbox import Outbox
from scripts.gallery_posts import (
    WAIT_SECONDS, PlannedPost, _PREVIEW_EVENT, _image_eligible, _inputs,
    _isolated_decoder,
)
from typhoon import TyphoonCollector, report_identity, typhoon_summary


MAX_ELAPSED_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class TimelineStep:
    elapsed_seconds: int
    received_indexes: tuple[int, ...]
    outcomes: tuple[str, ...]
    posts: tuple[PlannedPost, ...]
    # Match Service's status counts: Nankai includes already-notified pages
    # retained while waiting for a complete body; typhoon counts pending points.
    pending_nankai: int
    pending_typhoon: int


def _timing(arrivals, count, until):
    arrivals = tuple(arrivals)
    if len(arrivals) != count:
        raise ValueError('One arrival second is required per input')
    if any(type(value) is not int or not 0 <= value <= MAX_ELAPSED_SECONDS
           for value in arrivals):
        raise ValueError('Arrival seconds must be nonnegative integers within seven days')
    if any(before > after for before, after in zip(arrivals, arrivals[1:])):
        raise ValueError('Arrival seconds must be nondecreasing')
    until = arrivals[-1] + WAIT_SECONDS if until is None else until
    if type(until) is not int or not arrivals[-1] <= until <= MAX_ELAPSED_SECONDS:
        raise ValueError('End time must include all arrivals and be within seven days')
    return arrivals, until


def plan_timeline(records, arrival_seconds, *, until_seconds=None, nankai_topics=True):
    """Return reception/timer steps and actual collector/formatter output.

    Inputs use the same ``(NMEA, received_ms)`` pairs as ``plan_posts``; a
    separate integer sequence explicitly defines their arrival offsets. The
    default horizon is 180 seconds after the last receipt. Every receipt gets
    a step, even a duplicate or ignored message. Timer steps record a posting
    or expiration; a final step records the end of the observation window.

    All state lives in a fresh in-memory Outbox. No signing, URL creation,
    config, existing database, file or network access is involved. Collection
    and deduplication use default receiver settings (180 second wait, 900
    second expiry, 86400 second retention), with immediate successful delivery.
    """
    records = _inputs(records)
    arrivals, until = _timing(arrival_seconds, len(records), until_seconds)
    accepted, planned, steps = {}, {}, []
    with _isolated_decoder(), Outbox(':memory:', 'offline-gallery-timeline') as box:
        nankai = NankaiCollector(box, wait_seconds=WAIT_SECONDS)
        typhoons = TyphoonCollector(box, wait_seconds=WAIT_SECONDS, collect=True)
        next_poll = 0

        def counts():
            return nankai.waiting_count(), typhoons.pending_count()

        def batch_post(collector, batch, kind, text, now):
            result = collector.complete(batch, _PREVIEW_EVENT, 'offline-' + batch.key, now)
            if result == 'duplicate':
                # A conflicting Nankai page can start a fresh generation and
                # then return to an already-published complete body. complete
                # closes that generation without enqueueing the same event.
                return
            if result != 'queued':
                raise ValueError(f'Offline {kind} batch could not be queued: {result}')
            try:
                indexes = tuple(accepted[entry] for entry in batch.entries)
                entries = batch.entries if kind != 'nankai' else ()
                if kind == 'nankai' and nankai_topics:
                    try:
                        entries = nankai_topic_entries(batch)
                    except (ValueError, TypeError, OverflowError):
                        entries = ()
                enabled = bool(entries) and _image_eligible(entries)
                image_indexes = tuple(accepted[entry] for entry in entries) if enabled else ()
            except KeyError as error:
                raise ValueError('A collected post lost its original input') from error
            planned[batch.key] = PlannedPost(text, indexes, kind, enabled, image_indexes)

        def drain(now):
            nonlocal next_poll
            posted = []
            # Exactly Service.publish_one's ordering: at most one ready batch
            # of each type, then the FIFO delivery head, repeated until idle.
            while True:
                nankai.maintain(now)
                typhoons.maintain(now)
                batch = nankai.due(now)
                if batch is not None:
                    batch_post(nankai, batch, 'nankai', nankai_summary(batch), now)
                batch = typhoons.due(now)
                if batch is not None:
                    batch_post(typhoons, batch, 'typhoon',
                               typhoon_summary(batch.reports, update=batch.update), now)
                box.maintain(now)
                delivery = box.next_due(now)
                if delivery is None:
                    # publisher() waits one second after publish_one returns
                    # false. A suppressed duplicate batch may have consumed
                    # this poll while another group remains due. Receptions
                    # explicitly wake the publisher and may drain sooner.
                    next_poll = now + 1
                    return tuple(posted)
                posted.append(planned[delivery['key']])
                box.mark_sent(delivery['key'], now)

        def step(now, received=(), outcomes=(), posts=()):
            steps.append(TimelineStep(now, received, outcomes, posts, *counts()))

        def next_boundary():
            # Read only this replay's collector tables, because collectors
            # expose due(now), not their next wake time. Expiry matters after a
            # missing-page notice has cleared the collection deadline.
            row = box.connection.execute('''
                SELECT MIN(at) FROM (
                    SELECT deadline AS at FROM nankai_groups
                    WHERE identity=? AND state='active' AND notified=0
                    UNION ALL SELECT deadline FROM typhoon_groups
                    WHERE identity=? AND state='active'
                    UNION ALL SELECT first_at + ? FROM nankai_groups
                    WHERE identity=? AND state='active'
                ) WHERE at IS NOT NULL
            ''', (box.identity, box.identity, box.max_age_seconds, box.identity)).fetchone()
            return None if row[0] is None else max(int(row[0]), next_poll)

        def advance(target):
            while (deadline := next_boundary()) is not None and deadline <= target:
                before = counts()
                posts = drain(deadline)
                if posts or counts() != before:
                    step(deadline, posts=posts)
                next_time = next_boundary()
                if next_time is not None and next_time <= deadline:
                    raise ValueError('Offline collection timer did not advance')

        for index, ((sentence, milliseconds), now) in enumerate(zip(records, arrivals, strict=True)):
            advance(now)
            try:
                report = azarashi.decode(sentence, timestamp=datetime.fromtimestamp(
                    milliseconds / 1000, timezone.utc))
            except azarashi.AzarashiException as error:
                raise ValueError(f'Cannot decode posting input {index}') from error
            if isinstance(report, (dcx.NullMsg, dcx.Unknown)):
                result = 'ignored'
            else:
                if not isinstance(report, (dcr.Base, dcx.AlertBase)):
                    raise ValueError(f'Unsupported posting input {index}')
                nankai.maintain(now)
                typhoons.maintain(now)
                result = nankai.accept(report, now)
                if result == 'immediate':
                    result = typhoons.accept(report, now)
                if result == 'buffered':
                    entry = report.nmea, round(report.timestamp.timestamp() * 1000)
                    accepted[entry] = index
                elif result == 'immediate':
                    text, key = report_message(report), report_identity(report)
                    result = box.enqueue(key, _PREVIEW_EVENT, 'offline-' + key, now)
                    if result == 'queued':
                        entries = ((report.nmea, round(report.timestamp.timestamp() * 1000)),)
                        enabled = (not isinstance(report, dcr.NankaiTroughEarthquake)
                                   and _image_eligible(entries))
                        planned[key] = PlannedPost(text, (index,), 'single', enabled,
                                                   (index,) if enabled else ())
            if result not in ('buffered', 'duplicate', 'queued', 'ignored'):
                raise ValueError(f'Posting input {index} could not be planned: {result}')
            # Service.accept only wakes the publisher after new queued or
            # buffered input. A duplicate/ignored receipt must not shorten an
            # idle wait after a previously suppressed batch at this instant.
            posts = drain(now) if result in ('buffered', 'queued') or next_poll <= now else ()
            step(now, (index,), (result,), posts)
        advance(until)
        # advance has already run all eligible timer polls. An extra drain at
        # the horizon would bypass the one-second idle wait for a second due
        # group after a duplicate batch was suppressed at that same second.
        if steps[-1].elapsed_seconds != until or steps[-1].received_indexes:
            step(until)
    return tuple(steps)
