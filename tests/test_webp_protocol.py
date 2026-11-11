"""Fixed WebP profiles authenticate independently without changing PNG URLs."""

import base64
import hashlib
import hmac
import unittest
from unittest.mock import patch
import zlib

import image_protocol as protocol


NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
RECORDS = ((NMEA, 1794092400123),)
KEY = bytes(range(32))
KEYS = {'k1': KEY}


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def webp_path(payload):
    message = b'shirasu-image-encoding\0v5\0k1\0' + payload.encode() + b'\0w1\0webp'
    signature = b64(hmac.new(KEY, message, hashlib.sha256).digest())
    return f'/v5/k1/{payload}.{signature}.w1.webp'


class WebPProtocolTests(unittest.TestCase):
    def candidate(self):
        self.enterContext(patch.object(protocol, 'SUPPORTED_VERSIONS', ('v1', 'v2', 'v3', 'v4', 'v5', 'v6')))
        self.enterContext(patch.object(protocol, 'WEBP_VERSIONS', ('v5', 'v6')))

    def test_normal_distribution_cannot_issue_or_parse_webp_even_for_old_versions(self):
        for version in ('v1', 'v2', 'v3', 'v4', 'v5'):
            with self.subTest(version=version), self.assertRaises(protocol.ProtocolError):
                protocol.make_path(RECORDS, 'k1', KEY, version=version, encoding='w1')
        with patch.object(protocol.zlib, 'decompressobj', side_effect=AssertionError('No expansion')):
            with self.assertRaises(protocol.ProtocolError):
                protocol.parse_path('/v5/k1/eA.c2ln.w1.webp', KEYS)

    def test_webp_uses_a_distinct_domain_with_profile_extension_and_all_input_context(self):
        self.candidate()
        png = protocol.make_path(RECORDS, 'k1', KEY, version='v5')
        webp = protocol.make_path(RECORDS, 'k1', KEY, version='v5', encoding='w1')
        payload, png_signature, _ = png.rsplit('/', 1)[1].split('.')
        expected_png = b64(hmac.new(KEY, b'shirasu-image\0v5\0k1\0' + payload.encode(), hashlib.sha256).digest())
        self.assertEqual(png_signature, expected_png)
        self.assertEqual(webp, webp_path(payload))
        self.assertNotEqual(webp.split('.')[-3], png_signature)
        self.assertEqual(protocol.parse_path(webp, KEYS), RECORDS)
        self.assertEqual(protocol.parse_path(png, KEYS), RECORDS)
        self.assertEqual(protocol.path_encoding(webp), 'w1')
        self.assertEqual(protocol.path_encoding(png), 'png')
        self.assertEqual(protocol.path_version(webp), 'v5')

    def test_all_existing_generations_keep_png_signatures_and_default(self):
        self.candidate()
        for version in ('v1', 'v2', 'v3', 'v4'):
            with self.subTest(version=version):
                original = protocol.make_path(RECORDS, 'k1', KEY, version=version)
                explicit = protocol.make_path(RECORDS, 'k1', KEY, version=version, encoding='png')
                self.assertEqual(original, explicit)
                payload, signature, suffix = original.rsplit('/', 1)[1].split('.')
                message = b'shirasu-image\0' + version.encode() + b'\0k1\0' + payload.encode()
                self.assertEqual(signature, b64(hmac.new(KEY, message, hashlib.sha256).digest()))
                self.assertEqual(suffix, 'png')
                self.assertEqual(protocol.parse_path(original, KEYS), RECORDS)
        self.assertEqual(protocol.make_path(RECORDS, 'k1', KEY),
                         protocol.make_path(RECORDS, 'k1', KEY, version='v1'))

    def test_relabeling_any_authenticated_part_fails_before_decompression(self):
        self.candidate()
        webp = protocol.make_path(RECORDS, 'k1', KEY, version='v5', encoding='w1')
        png = protocol.make_path(RECORDS, 'k1', KEY, version='v5')
        payload = webp.rsplit('/', 1)[1].split('.')[0]
        changed_payload = b64(zlib.compress(b'{}'))
        invalid = (
            webp.replace('.w1.webp', '.png'), png[:-4] + '.w1.webp',
            webp.replace('/v5/', '/v6/'), webp.replace('/k1/', '/k2/'),
            webp.replace(payload, changed_payload), webp.replace('.w1.', '.w2.'),
            webp.replace('.w1.webp', '.w1.png'), webp.replace('.w1.webp', '.webp'),
            webp + '?quality=0', webp + '#fragment',
        )
        with patch.object(protocol.zlib, 'decompressobj', side_effect=AssertionError('No expansion')):
            for path in invalid:
                with self.subTest(path=path), self.assertRaises(protocol.ProtocolError):
                    protocol.parse_path(path, {'k1': KEY, 'k2': KEY})

    def test_profiles_are_explicit_and_version_bounded(self):
        self.candidate()
        for encoding in ('webp', 'w2', '', None, 1, [], 'w1.webp'):
            with self.subTest(encoding=encoding), self.assertRaises(protocol.ProtocolError):
                protocol.make_path(RECORDS, 'k1', KEY, version='v5', encoding=encoding)
        with self.assertRaises(protocol.ProtocolError):
            protocol.make_path(RECORDS, 'k1', KEY, version='v4', encoding='w1')
        self.assertEqual(protocol.validate_encoding('w1', 'v5'), 'w1')

    def test_authorized_webp_keeps_bounded_expansion_and_canonical_input_rules(self):
        self.candidate()
        for raw in (b' ' * (protocol.MAX_EXPANDED_BYTES * 100), b'{}', b'{"t":0,"t":1,"r":[]}'):
            with self.subTest(raw=raw[:30]), self.assertRaises(protocol.ProtocolError):
                protocol.parse_path(webp_path(b64(zlib.compress(raw, 9))), KEYS)
        with patch.object(protocol, 'MAX_PATH_LENGTH', 50), self.assertRaises(protocol.ProtocolError):
            protocol.make_path(RECORDS, 'k1', KEY, version='v5', encoding='w1')


if __name__ == '__main__':
    unittest.main()
