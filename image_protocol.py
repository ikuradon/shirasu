"""Versioned, self-contained URLs for the public image renderer.

Only the shared signing key authorizes new inputs. A published URL has no
expiry: readers can reuse it, and a renderer can recreate its PNG after CDN
eviction. This module deliberately has no third-party dependencies or I/O.

v1/v2 payload: canonical UTF-8 JSON ``{"r":[[offset_ms,nmea],...],"t":base_ms}``,
compressed with zlib and encoded as unpadded base64url. The base is the earliest
receipt timestamp; records are sorted by receipt timestamp and NMEA text.
"""

import base64
import binascii
from collections.abc import Mapping
import hashlib
import hmac
import json
import re
import zlib


VERSION = 'v1'
SUPPORTED_VERSIONS = ('v1', 'v2')
MAX_REPORTS = 32
MAX_PATH_LENGTH = 8192
MAX_COMPRESSED_BYTES = 4096
MAX_EXPANDED_BYTES = 8192
MAX_SPAN_MS = 24 * 60 * 60 * 1000
# Leave the final calendar year available for decoded forecast dates.
MAX_TIMESTAMP_MS = 253370764799999  # 9998-12-31T23:59:59.999Z
_DOMAIN = b'shirasu-image\x00'
_KEY_ID = re.compile(r'[A-Za-z0-9_-]{1,16}', re.ASCII)
_BASE64URL = re.compile(r'[A-Za-z0-9_-]+', re.ASCII)
_NMEA = re.compile(r'\$QZQSM,[0-9]{2},[0-9A-Fa-f]{63}\*[0-9A-Fa-f]{2}', re.ASCII)


class ProtocolError(ValueError):
    """An invalid signing configuration or image URL input."""


def validate_version(value):
    """Validate a renderer generation without changing the default for old callers."""
    if not isinstance(value, str) or value not in SUPPORTED_VERSIONS:
        raise ProtocolError('Unsupported image URL version')
    return value


def path_version(path):
    """Read a supported generation; this does not authenticate the URL.

    Call parse_path before selecting a renderer from this value.
    """
    if not isinstance(path, str) or len(path) > MAX_PATH_LENGTH:
        raise ProtocolError('Invalid image URL path')
    parts = path.split('/')
    if len(parts) != 4 or parts[0]:
        raise ProtocolError('Unsupported image URL path or version')
    return validate_version(parts[1])


def _key_id(value):
    if not isinstance(value, str) or _KEY_ID.fullmatch(value) is None:
        raise ProtocolError('Invalid image signing key ID')
    return value


def _secret(value):
    if not isinstance(value, bytes) or len(value) < 32:
        raise ProtocolError('Image signing keys must contain at least 32 bytes')
    return value


def _integer(value, maximum, label):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProtocolError(f'Invalid {label}')
    return value


def _nmea(value):
    if not isinstance(value, str) or len(value) != 76 or _NMEA.fullmatch(value) is None:
        raise ProtocolError('Invalid QZQSM sentence')
    checksum = 0
    for char in value[1:-3]:
        checksum ^= ord(char)
    if checksum != int(value[-2:], 16):
        raise ProtocolError('Invalid QZQSM checksum')
    return value


def _records(reports):
    records = []
    try:
        iterator = iter(reports)
    except TypeError as error:
        raise ProtocolError('Image reports must be an iterable') from error
    for record in iterator:
        if len(records) >= MAX_REPORTS:
            raise ProtocolError('Too many image reports')
        if not isinstance(record, (tuple, list)) or len(record) != 2:
            raise ProtocolError('Invalid image report entry')
        sentence, timestamp = record
        records.append((_nmea(sentence), _integer(timestamp, MAX_TIMESTAMP_MS, 'receipt timestamp')))
    if not records:
        raise ProtocolError('At least one image report is required')
    records.sort(key=lambda item: (item[1], item[0]))
    if len(set(records)) != len(records):
        raise ProtocolError('Duplicate image report entry')
    if records[-1][1] - records[0][1] > MAX_SPAN_MS:
        raise ProtocolError('Image receipt timestamps span more than 24 hours')
    return tuple(records)


def _json(records):
    base = records[0][1]
    payload = {'t': base, 'r': [[timestamp - base, sentence] for sentence, timestamp in records]}
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')


def _encode64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _decode64(value, maximum):
    if (not isinstance(value, str) or not value
            or len(value) > (maximum * 8 + 5) // 6
            or _BASE64URL.fullmatch(value) is None):
        raise ProtocolError('Invalid image URL encoding')
    try:
        decoded = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error) as error:
        raise ProtocolError('Invalid image URL encoding') from error
    if len(decoded) > maximum or _encode64(decoded) != value:
        raise ProtocolError('Noncanonical image URL encoding')
    return decoded


def _signature(key_id, payload, secret, version):
    message = _DOMAIN + version.encode('ascii') + b'\x00' + key_id.encode('ascii') + b'\x00' + payload.encode('ascii')
    return hmac.new(secret, message, hashlib.sha256).digest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError('Duplicate JSON key')
        result[key] = value
    return result


def _reject_number(value):
    raise ProtocolError('Only integer JSON numbers are supported')


def _load_json(value):
    try:
        return json.loads(value, object_pairs_hook=_object,
                          parse_float=_reject_number, parse_constant=_reject_number)
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError) as error:
        raise ProtocolError('Invalid image JSON') from error


def load_keys(value):
    """Read a JSON object mapping public key IDs to 64-digit secret hex strings.

    Callers decide how to obtain this configuration. This function never reads
    environment variables, files, or the Nostr account's private key.
    """
    if not isinstance(value, str) or len(value) > 65536:
        raise ProtocolError('Invalid image signing key configuration')
    data = _load_json(value)
    if not isinstance(data, dict) or not data:
        raise ProtocolError('Image signing keys must be a nonempty object')
    keys = {}
    for key_id, secret in data.items():
        _key_id(key_id)
        if not isinstance(secret, str) or re.fullmatch('[0-9A-Fa-f]{64}', secret) is None:
            raise ProtocolError('Image signing key must be 64 hexadecimal characters')
        keys[key_id] = bytes.fromhex(secret)
    return keys


def make_path(reports, key_id, secret, *, version=VERSION):
    """Return a signed absolute URL path without performing any network I/O.

    ``reports`` contains ``(nmea, received_at_ms)`` pairs. Receipt timestamps
    must come from the saved inputs, not from the time a reader opens the URL.
    """
    version = validate_version(version)
    key_id, secret = _key_id(key_id), _secret(secret)
    raw = _json(_records(reports))
    if len(raw) > MAX_EXPANDED_BYTES:
        raise ProtocolError('Image JSON exceeds the size limit')
    compressed = zlib.compress(raw, level=9)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise ProtocolError('Compressed image input exceeds the size limit')
    payload = _encode64(compressed)
    signature = _encode64(_signature(key_id, payload, secret, version))
    path = f'/{version}/{key_id}/{payload}.{signature}.png'
    if len(path) > MAX_PATH_LENGTH:
        raise ProtocolError('Image URL exceeds the size limit')
    return path


def parse_path(path, keys):
    """Authenticate a supported path, then safely expand and validate its reports.

    The HMAC covers the compressed representation. Unauthenticated input is
    never decompressed or parsed as JSON. No expiry check is made: published
    URLs remain valid while their verification key and renderer are retained.
    """
    version = path_version(path)
    parts = path.split('/')
    key_id = _key_id(parts[2])
    filename = parts[3].split('.')
    if len(filename) != 3 or filename[2] != 'png':
        raise ProtocolError('Invalid image URL filename')
    payload, signature, _ = filename
    compressed = _decode64(payload, MAX_COMPRESSED_BYTES)
    signed = _decode64(signature, hashlib.sha256().digest_size)
    if not isinstance(keys, Mapping) or key_id not in keys:
        raise ProtocolError('Unknown image signing key')
    secret = _secret(keys[key_id])
    if len(signed) != 32 or not hmac.compare_digest(signed, _signature(key_id, payload, secret, version)):
        raise ProtocolError('Invalid image URL signature')
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, MAX_EXPANDED_BYTES + 1)
    except zlib.error as error:
        raise ProtocolError('Invalid compressed image input') from error
    if (len(raw) > MAX_EXPANDED_BYTES or not decompressor.eof
            or decompressor.unconsumed_tail or decompressor.unused_data):
        raise ProtocolError('Invalid or oversized compressed image input')
    data = _load_json(raw)
    if not isinstance(data, dict) or set(data) != {'t', 'r'}:
        raise ProtocolError('Invalid image payload fields')
    base = _integer(data['t'], MAX_TIMESTAMP_MS, 'base receipt timestamp')
    if not isinstance(data['r'], list) or not 1 <= len(data['r']) <= MAX_REPORTS:
        raise ProtocolError('Invalid image report count')
    records = []
    for item in data['r']:
        if not isinstance(item, list) or len(item) != 2:
            raise ProtocolError('Invalid image payload entry')
        offset, sentence = item
        offset = _integer(offset, MAX_SPAN_MS, 'receipt offset')
        records.append((sentence, base + offset))
    reports = _records(records)
    if _json(reports) != raw:
        raise ProtocolError('Noncanonical image JSON')
    return reports
