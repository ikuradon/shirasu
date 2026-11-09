import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch
import zlib

from image_protocol import (
    MAX_COMPRESSED_BYTES, MAX_EXPANDED_BYTES, MAX_PATH_LENGTH, MAX_REPORTS,
    MAX_SPAN_MS, MAX_TIMESTAMP_MS, ProtocolError, load_keys, make_path, parse_path,
)


DCR = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
MARINE = '$QZQSM,58,53ADF5A111800283FC5083541042820D5047EA17994309A8622000113240198*7B'
AT = 1794092400123
# Isolated test-only keys; never loaded from a live signing configuration.
KEY = bytes(range(32))
KEYS = {'k1': KEY}


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def sign_compressed(compressed, *, key=KEY, key_id='k1'):
    """Mint test inputs, including malformed payloads signed by a valid issuer."""
    payload = b64(compressed)
    signed = hmac.new(key, b'shirasu-image\x00v1\x00' + key_id.encode() + b'\x00' + payload.encode(),
                      hashlib.sha256).digest()
    return f'/v1/{key_id}/{payload}.{b64(signed)}.png'


def sign_raw(raw):
    return sign_compressed(zlib.compress(raw, 9))


def sign_json(payload):
    return sign_raw(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())


class ImageProtocolTests(unittest.TestCase):
    def test_round_trip_preserves_original_nmea_and_millisecond_receipt_times(self):
        reports = [(MARINE, AT + 4231), (DCR, AT)]
        path = make_path(reports, 'k1', KEY)
        self.assertTrue(path.startswith('/v1/k1/'))
        self.assertTrue(path.endswith('.png'))
        self.assertNotIn('?', path)
        self.assertNotIn('=', path)
        self.assertEqual(parse_path(path, KEYS), ((DCR, AT), (MARINE, AT + 4231)))

    def test_constructor_is_deterministic_for_reordered_iterables(self):
        expected = make_path([(DCR, AT), (MARINE, AT + 50)], 'k1', KEY)
        self.assertEqual(make_path(iter([(MARINE, AT + 50), (DCR, AT)]), 'k1', KEY), expected)
        self.assertEqual(make_path([(DCR, AT), (MARINE, AT + 50)], 'k1', KEY), expected)

    def test_receipt_timestamp_is_signed_and_is_not_replaced_with_current_time(self):
        earlier = make_path([(DCR, AT)], 'k1', KEY)
        later = make_path([(DCR, AT + 1)], 'k1', KEY)
        self.assertNotEqual(earlier, later)
        self.assertEqual(parse_path(earlier, KEYS), ((DCR, AT),))

    def test_valid_signature_uses_version_key_id_and_exact_compressed_payload(self):
        raw = json.dumps({'r': [[0, DCR]], 't': AT}, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(make_path([(DCR, AT)], 'k1', KEY), sign_compressed(zlib.compress(raw, 9)))
        path = make_path([(DCR, AT)], 'k1', KEY)
        with self.assertRaises(ProtocolError):
            parse_path(path.replace('/k1/', '/k2/'), {'k1': KEY, 'k2': KEY})
        with self.assertRaises(ProtocolError):
            parse_path(path.replace('/v1/', '/v2/'), KEYS)

    def test_rotation_can_keep_old_links_valid_with_old_verification_key(self):
        old_path = make_path([(DCR, AT)], 'old', KEY)
        new_key = b'x' * 32
        new_path = make_path([(DCR, AT)], 'new', new_key)
        keys = {'old': KEY, 'new': new_key}
        self.assertEqual(parse_path(old_path, keys), parse_path(new_path, keys))
        with self.assertRaises(ProtocolError):
            parse_path(old_path, {'new': new_key})

    def test_modified_data_and_modified_signatures_are_rejected_before_decompression(self):
        path = make_path([(DCR, AT)], 'k1', KEY)
        prefix, filename = path.rsplit('/', 1)
        payload, signature, extension = filename.split('.')
        replacements = [
            f'{prefix}/{"A" if payload[0] != "A" else "B"}{payload[1:]}.{signature}.{extension}',
            f'{prefix}/{payload}.{"A" if signature[0] != "A" else "B"}{signature[1:]}.{extension}',
        ]
        with patch('image_protocol.zlib.decompressobj', side_effect=AssertionError('must authenticate first')):
            for tampered in replacements:
                with self.subTest(path=tampered):
                    with self.assertRaises(ProtocolError):
                        parse_path(tampered, KEYS)

    def test_wrong_or_unknown_key_is_rejected_before_decompression(self):
        path = make_path([(DCR, AT)], 'k1', KEY)
        with patch('image_protocol.zlib.decompressobj', side_effect=AssertionError('must authenticate first')):
            for keys in ({}, {'k1': b'z' * 32}):
                with self.subTest(keys=tuple(keys)):
                    with self.assertRaises(ProtocolError):
                        parse_path(path, keys)

    def test_paths_cannot_add_query_fragment_extra_segments_or_file_extensions(self):
        path = make_path([(DCR, AT)], 'k1', KEY)
        for invalid in (path + '?extra=1', path + '#fragment', path + '/extra',
                        path + '.jpg', path.replace('.png', '.PNG'),
                        path.lstrip('/'), 'https://example.test' + path,
                        path.replace('/v1/', '/v1//'), '/v1/../data.sig.png'):
            with self.subTest(path=invalid):
                with self.assertRaises(ProtocolError):
                    parse_path(invalid, KEYS)

    def test_rejects_oversized_path_and_compressed_input(self):
        for path in ('/' * (MAX_PATH_LENGTH + 1),
                     sign_compressed(b'a' * (MAX_COMPRESSED_BYTES + 1))):
            with self.assertRaises(ProtocolError):
                parse_path(path, KEYS)

    def test_signed_decompression_bomb_stops_at_expanded_limit(self):
        compressed = zlib.compress(b' ' * (MAX_EXPANDED_BYTES * 100), 9)
        self.assertLess(len(compressed), MAX_COMPRESSED_BYTES)
        with self.assertRaises(ProtocolError):
            parse_path(sign_compressed(compressed), KEYS)

    def test_rejects_truncated_invalid_concatenated_and_suffixed_streams(self):
        raw = json.dumps({'r': [[0, DCR]], 't': AT}, sort_keys=True, separators=(',', ':')).encode()
        compressed = zlib.compress(raw)
        for data in (b'not zlib', compressed[:-1], compressed + zlib.compress(b'{}'),
                     compressed + b'junk'):
            with self.subTest(data=data[:8]):
                with self.assertRaises(ProtocolError):
                    parse_path(sign_compressed(data), KEYS)

    def test_accepts_other_valid_zlib_compression_levels_without_recompression(self):
        raw = json.dumps({'r': [[0, DCR]], 't': AT}, sort_keys=True, separators=(',', ':')).encode()
        for level in (0, 1, 9):
            with self.subTest(level=level):
                self.assertEqual(parse_path(sign_compressed(zlib.compress(raw, level)), KEYS), ((DCR, AT),))

    def test_rejects_padded_and_noncanonical_base64url(self):
        path = make_path([(DCR, AT)], 'k1', KEY)
        prefix, filename = path.rsplit('/', 1)
        payload, signature, _ = filename.split('.')
        # A 32-byte signature has two unused low bits in its last base64 digit.
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
        alternate = alphabet[alphabet.index(signature[-1]) + 1]
        for filename in (f'{payload}=.{signature}.png', f'{payload}.{signature}=.png',
                         f'{payload}.{signature[:-1]}{alternate}.png',
                         f'{payload}.{signature[:-1]}.png',
                         f'{payload}.{signature.replace(signature[0], "+", 1)}.png'):
            with self.subTest(filename=filename):
                with self.assertRaises(ProtocolError):
                    parse_path(prefix + '/' + filename, KEYS)

    def test_rejects_unknown_fields_duplicate_keys_and_noncanonical_json(self):
        canonical = json.dumps({'r': [[0, DCR]], 't': AT}, sort_keys=True, separators=(',', ':'))
        bad = [
            canonical.replace('"t":', '"x":'),
            canonical[:-1] + ',"t":0}',
            canonical[:-1] + ',"other":0}',
            ' ' + canonical,
            json.dumps({'t': AT, 'r': [[0, DCR]]}, separators=(',', ':')),
            canonical.replace('"r"', '"\\u0072"'),
        ]
        for raw in bad:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ProtocolError):
                    parse_path(sign_raw(raw.encode()), KEYS)

    def test_rejects_nonnumeric_or_out_of_range_time_fields(self):
        for value in (True, False, -1, 1.5, '1', None, MAX_TIMESTAMP_MS + 1):
            with self.subTest(base=value):
                with self.assertRaises(ProtocolError):
                    parse_path(sign_json({'r': [[0, DCR]], 't': value}), KEYS)
                with self.assertRaises(ProtocolError):
                    make_path([(DCR, value)], 'k1', KEY)
        for value in (True, False, -1, 1.5, '1', None, MAX_SPAN_MS + 1):
            with self.subTest(offset=value):
                with self.assertRaises(ProtocolError):
                    parse_path(sign_json({'r': [[value, DCR]], 't': AT}), KEYS)
        with self.assertRaises(ProtocolError):
            parse_path(sign_json({'r': [[1, DCR]], 't': MAX_TIMESTAMP_MS}), KEYS)

    def test_rejects_nan_infinity_bad_utf8_and_excessive_nesting(self):
        for raw in (b'{"r":[],"t":NaN}', b'{"r":[],"t":Infinity}',
                    b'\xff', b'[' * 1100 + b'0' + b']' * 1100):
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(ProtocolError):
                    parse_path(sign_raw(raw), KEYS)

    def test_count_limit_allows_32_entries_and_rejects_33(self):
        records = [(DCR, AT + i) for i in range(MAX_REPORTS)]
        self.assertEqual(parse_path(make_path(records, 'k1', KEY), KEYS), tuple(records))
        with self.assertRaises(ProtocolError):
            make_path(records + [(DCR, AT + MAX_REPORTS)], 'k1', KEY)
        with self.assertRaises(ProtocolError):
            parse_path(sign_json({'r': [[i, DCR] for i in range(MAX_REPORTS + 1)], 't': AT}), KEYS)

    def test_duplicate_and_empty_input_are_rejected(self):
        for records in ([], [(DCR, AT), (DCR, AT)]):
            with self.subTest(records=records):
                with self.assertRaises(ProtocolError):
                    make_path(records, 'k1', KEY)
        for payload in ({'r': [], 't': AT}, {'r': [[0, DCR], [0, DCR]], 't': AT}):
            with self.assertRaises(ProtocolError):
                parse_path(sign_json(payload), KEYS)

    def test_wrong_record_shapes_are_rejected(self):
        for records in (None, DCR, [(DCR,)], [(DCR, AT, 'extra')], [{'nmea': DCR, 'at': AT}]):
            with self.subTest(records=records):
                with self.assertRaises(ProtocolError):
                    make_path(records, 'k1', KEY)
        for payload in ([], None, {'r': {}, 't': AT}, {'r': [[0, DCR, 1]], 't': AT},
                        {'r': [DCR], 't': AT}):
            with self.assertRaises(ProtocolError):
                parse_path(sign_json(payload), KEYS)

    def test_base_must_be_earliest_time_and_records_must_be_sorted(self):
        bad = [
            {'r': [[1, DCR]], 't': AT - 1},
            {'r': [[5, MARINE], [0, DCR]], 't': AT},
            {'r': [[0, MARINE], [0, DCR]], 't': AT},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError):
                    parse_path(sign_json(payload), KEYS)

    def test_24_hour_span_is_supported_but_larger_spans_are_rejected(self):
        records = [(DCR, AT), (MARINE, AT + MAX_SPAN_MS)]
        self.assertEqual(parse_path(make_path(records, 'k1', KEY), KEYS), tuple(records))
        with self.assertRaises(ProtocolError):
            make_path([(DCR, AT), (MARINE, AT + MAX_SPAN_MS + 1)], 'k1', KEY)

    def test_receipt_timestamp_bounds_are_supported(self):
        for timestamp in (0, MAX_TIMESTAMP_MS):
            with self.subTest(timestamp=timestamp):
                records = [(DCR, timestamp)]
                self.assertEqual(parse_path(make_path(records, 'k1', KEY), KEYS), tuple(records))

    def test_nmea_format_checksum_and_ascii_are_checked(self):
        sentences = [DCR + '\n', DCR.replace('$QZQSM', '$GPGGA'), DCR[:-2] + '00',
                     DCR.replace(',55,', ',5,'), DCR.replace(',55,', ',５５,'),
                     DCR.replace('C6AF', 'G6AF'), None, DCR.encode()]
        for sentence in sentences:
            with self.subTest(sentence=sentence):
                with self.assertRaises(ProtocolError):
                    make_path([(sentence, AT)], 'k1', KEY)
                if not isinstance(sentence, bytes):
                    with self.assertRaises(ProtocolError):
                        parse_path(sign_json({'r': [[0, sentence]], 't': AT}), KEYS)

    def test_signing_configuration_rejects_weak_keys_and_unsafe_key_ids(self):
        for key in (b'', b'x' * 31, 'x' * 32, bytearray(KEY), None):
            with self.subTest(key_type=type(key).__name__):
                with self.assertRaises(ProtocolError):
                    make_path([(DCR, AT)], 'k1', key)
        for key_id in ('', 'x' * 17, '..', 'k/1', 'k.1', 'k 1', '鍵', None):
            with self.subTest(key_id=key_id):
                with self.assertRaises(ProtocolError):
                    make_path([(DCR, AT)], key_id, KEY)

    def test_key_configuration_is_explicit_and_supports_multiple_verification_keys(self):
        keys = load_keys(json.dumps({'k1': KEY.hex(), 'old-2': (b'o' * 32).hex().upper()}))
        self.assertEqual(keys, {'k1': KEY, 'old-2': b'o' * 32})
        self.assertEqual(parse_path(make_path([(DCR, AT)], 'old-2', b'o' * 32), keys), ((DCR, AT),))

    def test_invalid_key_configuration_never_returns_partial_keyring(self):
        cases = ['{}', 'null', '[]', '{', '{"k1":"' + KEY.hex() + '","k1":"' + KEY.hex() + '"}',
                 json.dumps({'k1': 'a' * 63}), json.dumps({'k1': 'g' * 64}),
                 json.dumps({'unsafe/key': KEY.hex()}), json.dumps({'k1': 123}),
                 json.dumps({'k1': KEY.hex(), 'broken': 'abc'}), ' ' * 65537, None]
        for value in cases:
            with self.subTest(value=str(value)[:60]):
                with self.assertRaises(ProtocolError):
                    load_keys(value)


if __name__ == '__main__':
    unittest.main()
