"""Validated Nankai topic metadata, without decoding the shared text assembler.

An image contains bulletin headers only. A representative page proves those
headers, never that the receiver has assembled or understood the whole text.
This module has no receiver, database, image or third-party dependencies.
"""

from calendar import isleap
from dataclasses import dataclass
from datetime import datetime, timezone

from image_protocol import MAX_TIMESTAMP_MS, _nmea


_KEYWORDS = {1: '調査中', 2: '調査中', 3: '調査中', 4: '巨大地震警戒',
             5: '巨大地震注意', 6: '調査終了', 15: 'その他の情報'}


@dataclass(frozen=True)
class NankaiHeader:
    issued_at: datetime
    classification: int
    information: int
    serial: int
    page: int
    total: int

    @property
    def title(self):
        return ('南海トラフ地震関連情報' if self.information == 2 or self.serial == 15
                else '南海トラフ地震臨時情報')

    @property
    def source_keyword(self):
        return _KEYWORDS[self.serial]

    @property
    def keyword(self):
        # Do not present a cancelled advisory as a current call for caution.
        return '取消' if self.information == 2 else self.source_keyword

    @property
    def markers(self):
        return (('訓練・試験',) if self.classification == 7 else ()) + {
            0: (), 1: ('訂正',), 2: ('取消',)}[self.information]


def _entry(entry):
    if not isinstance(entry, (list, tuple)) or len(entry) != 2:
        raise ValueError('Nankai image input requires a (NMEA, received_ms) pair')
    sentence, milliseconds = entry
    _nmea(sentence)
    if type(milliseconds) is not int or not 0 <= milliseconds <= MAX_TIMESTAMP_MS:
        raise ValueError('Invalid Nankai receipt timestamp')
    value = int(sentence.split(',')[2].split('*')[0] + '0', 16)
    return (sentence, milliseconds), value


def _bits(value, offset, width):
    return (value >> (256 - offset - width)) & ((1 << width) - 1)


def is_nankai_entry(entry):
    """Identify MT43/DC4 before decode; ``header`` must still validate it.

    Malformed input raises ValueError. False means another message type, not a
    successful validation of that other type's CRC or version.
    """
    _, value = _entry(entry)
    return _bits(value, 8, 6) == 43 and _bits(value, 17, 4) == 4


def _frame(entry):
    original, value = _entry(entry)
    bits = lambda offset, width: _bits(value, offset, width)
    # The same CRC/header/date rules used by nankai._stored_frame and azarashi,
    # kept independent of their receiver imports and process-wide assembler.
    crc = 0
    for bit in range(226):
        carry = ((crc >> 23) & 1) ^ bits(bit, 1)
        crc = (crc << 1) & 0xffffff
        if carry:
            crc ^= 0x864cfb
    if (bits(0, 8) not in (0x53, 0x9a, 0xc6) or bits(8, 6) != 43
            or bits(17, 4) != 4 or bits(214, 6) != 1 or crc != bits(226, 24)):
        raise ValueError('Invalid Nankai frame, version or CRC')
    classification, information = bits(14, 3), bits(41, 2)
    serial, page, total = bits(53, 4), bits(201, 6), bits(207, 6)
    if (classification not in (1, 2, 3, 7) or information not in (0, 1, 2)
            or serial not in _KEYWORDS or not 1 <= page <= total <= 63):
        raise ValueError('Unknown Nankai metadata or page position')
    month, day, hour, minute = bits(21, 4), bits(25, 5), bits(30, 5), bits(35, 6)
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59):
        raise ValueError('Invalid Nankai issue date fields')
    received = datetime.fromtimestamp(original[1] / 1000, timezone.utc)
    year = received.year + (1 if received.month - month > 6 else -1 if month - received.month > 6 else 0)
    if month == 2 and day == 29 and not isleap(year):
        candidates = [candidate for candidate in range(max(1, year - 8), min(9999, year + 8) + 1)
                      if isleap(candidate)]
        year = min(candidates, key=lambda candidate: abs(
            datetime(candidate, 2, 29, hour, minute, tzinfo=timezone.utc) - received))
    issued = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    text = bytes(bits(57 + index * 8, 8) for index in range(18))
    return NankaiHeader(issued, classification, information, serial, page, total), text, original


def header(entry):
    """Return immutable, strictly validated headers from one original page."""
    return _frame(entry)[0]


def nankai_topic_entries(batch):
    """Select one original page from a validated collector batch, or no image.

    Missing text and unreadable UTF-8 stay text-only. Complete batches require
    every page and a matching assembled body; cancellation needs no full text.
    The collector remains responsible for committing the batch's generation
    atomically, so this helper cannot authorize a stale batch for publication.
    """
    mode = getattr(batch, 'mode', None)
    if mode == 'missing':
        return ()
    if mode not in ('complete', 'cancelled'):
        raise ValueError('Unknown Nankai batch mode')
    entries = getattr(batch, 'entries', None)
    pages = getattr(batch, 'pages', None)
    if (not isinstance(entries, (list, tuple)) or not 1 <= len(entries) <= 63
            or not isinstance(pages, (list, tuple)) or len(entries) != len(pages)
            or any(type(page) is not int for page in pages)):
        raise ValueError('Invalid Nankai batch entries or pages')
    frames = tuple(_frame(entry) for entry in entries)
    first = frames[0][0]
    identity = lambda item: (item.issued_at, item.classification, item.information, item.serial, item.total)
    if any(identity(item) != identity(first) for item, _, _ in frames):
        raise ValueError('Mixed Nankai announcement headers')
    actual_pages = tuple(item.page for item, _, _ in frames)
    if tuple(pages) != actual_pages or len(set(pages)) != len(pages):
        raise ValueError('Inconsistent or repeated Nankai pages')
    for field in ('classification', 'information', 'serial', 'total'):
        if type(getattr(batch, field, None)) is not int or getattr(batch, field) != getattr(first, field):
            raise ValueError('Nankai batch metadata differs from original pages')
    issued = getattr(batch, 'issued_at', None)
    if (isinstance(issued, bool) or not isinstance(issued, (int, float))
            or issued != first.issued_at.timestamp()):
        raise ValueError('Nankai batch issue time differs from original pages')
    if (mode == 'cancelled') != (first.information == 2):
        raise ValueError('Nankai batch mode differs from information type')
    frames = tuple(sorted(frames, key=lambda frame: frame[0].page))
    if mode == 'complete':
        if tuple(frame[0].page for frame in frames) != tuple(range(1, first.total + 1)):
            raise ValueError('Complete Nankai image requires all pages')
        body = b''.join(text for _, text, _ in frames)
        if not isinstance(getattr(batch, 'body', None), bytes) or batch.body != body:
            raise ValueError('Nankai assembled body differs from original pages')
        try:
            body.replace(b'\0', b'').decode('utf-8')
        except UnicodeDecodeError:
            return ()
    return (frames[0][2],)
