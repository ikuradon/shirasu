"""Decode a finite recording into posting previews without delivery side effects."""

from datetime import datetime

import azarashi


_UBX_HEADER = b'\xb5\x62\x02\x13'  # RXM-SFRBX
_UBX_SYNC = _UBX_HEADER[:2]


def _safe_text(value):
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in str(value)).split())


def _ubx_frames(stream):
    """Frame recorded RXM-SFRBX messages and report incomplete data at EOF.

    The live decoder retains incomplete frames for a later read. A recording has
    no later read after EOF, so frame it here and use azarashi for message decoding.
    Other valid UBX frames are skipped whole, including their payloads. Buffering
    is bounded by a read block plus the maximum U2-sized UBX payload.
    """
    buffer = bytearray()
    ended = False
    read = getattr(stream, 'read1', None) or stream.read

    def fill(size):
        nonlocal ended
        while len(buffer) < size and not ended:
            chunk = read(8192)
            if chunk is not None and not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError('UBX input must be opened in binary mode')
            if not chunk:
                ended = True
            else:
                buffer.extend(chunk)

    while True:
        fill(2)
        start = buffer.find(_UBX_SYNC)
        if start < 0:
            if ended:
                if buffer.endswith(_UBX_SYNC[:1]):
                    yield 'UBX header is incomplete at end of input'
                return
            # Retain a possible header that spans the next read boundary.
            del buffer[:-1]
            continue
        del buffer[:start]
        fill(6)
        if len(buffer) < 6:
            yield 'UBX header is incomplete at end of input'
            return
        is_sfrbx = buffer[:4] == _UBX_HEADER
        length = int.from_bytes(buffer[4:6], 'little')
        # SFRBX has an eight-byte fixed part followed by numWords (U1) words.
        if is_sfrbx and (not 8 <= length <= 8 + 4 * 255 or length % 4):
            yield f'Invalid UBX SFRBX payload length: {length}'
            del buffer[0]
            continue
        size = length + 8
        fill(size)
        if len(buffer) < size:
            yield f'UBX frame is incomplete at end of input: expected {size} bytes, got {len(buffer)}'
            # A corrupted length may have hidden a complete following frame.
            following = buffer.find(_UBX_SYNC, 1)
            if following < 0:
                return
            del buffer[:following]
            continue
        frame = bytes(buffer[:size])
        ck_a = ck_b = 0
        for byte in frame[2:-2]:
            ck_a = (ck_a + byte) & 255
            ck_b = (ck_b + ck_a) & 255
        valid_checksum = frame[-2:] == bytes((ck_a, ck_b))
        if valid_checksum:
            del buffer[:size]
        else:
            # A corrupted length/checksum must not consume the next good report.
            del buffer[0]
        if not is_sfrbx:
            continue
        if frame[6] != 5 or frame[8] != 1:  # QZSS, L1S
            continue
        if len(frame) > 18 and frame[16] >> 2 not in (43, 44):  # DCR, DCX
            continue
        yield frame  # azarashi reports invalid checksums and malformed payloads


def _reports(stream, input_format, received_at):
    if input_format == 'ubx':
        for frame in _ubx_frames(stream):
            if isinstance(frame, str):
                yield frame
                continue
            try:
                yield azarashi.decode(frame, msg_type='ublox', timestamp=received_at)
            except azarashi.AzarashiReadOn as error:
                yield error.message
        return
    while True:
        try:
            yield azarashi.decode_stream(stream, msg_type='nmea', unique=False,
                                        ignore_dcx=False, timestamp=received_at)
        except azarashi.AzarashiNoMoreData:
            return
        except azarashi.AzarashiTimeoutError as error:
            # Finite inputs cannot recover from a repeatedly timing-out reader.
            yield error.message
            return
        except azarashi.AzarashiReadOn as error:
            yield error.message


def preview_stream(stream, *, input_format, received_at, message_formatter,
                   report_filter, stdout, stderr, source_name):
    """Write each eligible report once per occurrence; return decoding counts.

    decoded counts reports, ignored counts reports excluded by report_filter,
    and errors counts malformed messages or unreadable/truncated input. Routine
    GPS traffic is skipped by framing. Formatting/output errors propagate rather
    than being misreported as decoding errors.
    """
    if input_format not in ('nmea', 'ubx'):
        raise ValueError('input_format must be nmea or ubx')
    if not isinstance(received_at, datetime) or received_at.utcoffset() is None:
        raise ValueError('received_at must be a timezone-aware datetime')
    counts = {'decoded': 0, 'shown': 0, 'ignored': 0, 'errors': 0}
    source = _safe_text(source_name)
    reports = iter(_reports(stream, input_format, received_at))
    while True:
        try:
            report = next(reports)
        except StopIteration:
            break
        except OSError as error:
            counts['errors'] += 1
            print(f'{source}: {_safe_text(error)}', file=stderr)
            break
        if isinstance(report, str):
            counts['errors'] += 1
            print(f'{source}: {_safe_text(report)}', file=stderr)
            continue
        counts['decoded'] += 1
        if not report_filter(report):
            counts['ignored'] += 1
            continue
        message = message_formatter(report)
        counts['shown'] += 1
        print(f'=== プレビュー {counts["shown"]} ({source}) ===', file=stdout)
        print(message, file=stdout)
        print(file=stdout)
    return counts
