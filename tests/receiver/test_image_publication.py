from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit

from shirasu.images.protocol import parse_path
from shirasu.images.publication import ImagePublisher, image_entry, with_image


NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
KEY = bytes(range(32))
ENVIRONMENT = {
    'SHIRASU_IMAGE_BASE_URL': 'https://images.example.test/',
    'SHIRASU_IMAGE_KEY_ID': 'k1',
    'SHIRASU_IMAGE_SIGNING_KEY': KEY.hex(),
}


class ImagePublicationTests(unittest.TestCase):
    def test_image_support_is_optional_but_partial_configuration_is_an_error(self):
        self.assertIsNone(ImagePublisher.from_environment({}))
        self.assertIsNone(ImagePublisher.from_environment(dict.fromkeys(ENVIRONMENT, '')))
        for missing in ENVIRONMENT:
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    ImagePublisher.from_environment({k: v for k, v in ENVIRONMENT.items() if k != missing})

    def test_configured_origin_builds_a_signed_url_without_query_or_expiry(self):
        publisher = ImagePublisher.from_environment(ENVIRONMENT)
        entry = (NMEA, 1794092400123)
        url = publisher.url([entry])
        parsed = urlsplit(url)
        self.assertEqual((parsed.scheme, parsed.netloc), ('https', 'images.example.test'))
        self.assertEqual((parsed.query, parsed.fragment), ('', ''))
        self.assertTrue(parsed.path.startswith('/v1/k1/'))
        self.assertEqual(parse_path(parsed.path, {'k1': KEY}), (entry,))
        self.assertEqual(publisher.url([entry]), url)

    def test_invalid_origin_cannot_publish_broken_or_credential_bearing_links(self):
        invalid = (
            '', None, 'http://images.example.test', '//images.example.test', 'https://',
            'https://user:password@images.example.test', 'https://images.example.test/path',
            'https://images.example.test/?q=1', 'https://images.example.test/#fragment',
            'https://images.example.test?', 'https://images.example.test#',
            'https://images.example.test:bad', 'https://images.example.test:70000',
            'https://images.example.test:', 'https://images.example.test:0',
            'https://[broken', 'https://images.example.test\n', 'https://images.example.test\x7f',
            'https://images.example.test\\bad', 'https://images.example.test%2fevil',
            'https://images..example.test', 'https://-images.example.test', 'https://例.example.test',
        )
        for origin in invalid:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    ImagePublisher(origin, 'k1', KEY)
        for origin in ('https://images.example.test:8443', 'https://[2001:db8::1]'):
            self.assertEqual(ImagePublisher(origin, 'k1', KEY).base_url, origin)

    def test_key_validation_rejects_short_wrong_type_and_unsafe_identifiers(self):
        for kid, secret in (('k1', b'x' * 31), ('k1', b'x' * 33), ('k1', KEY.hex()),
                            ('../key', KEY), ('', KEY), (1, KEY)):
            with self.subTest(key_id=kid, length=len(secret)):
                with self.assertRaises(ValueError):
                    ImagePublisher('https://images.example.test', kid, secret)
        for value in ('not hex', 'aa' * 31, 'aa' * 33):
            with self.assertRaises(ValueError):
                ImagePublisher.from_environment(dict(ENVIRONMENT, SHIRASU_IMAGE_SIGNING_KEY=value))

    def test_configuration_repr_never_exposes_the_signing_key(self):
        publisher = ImagePublisher.from_environment(ENVIRONMENT)
        displayed = repr(publisher)
        self.assertIn('https://images.example.test', displayed)
        self.assertIn('k1', displayed)
        self.assertNotIn(KEY.hex(), displayed)
        self.assertNotIn(repr(KEY), displayed)
        self.assertNotIn('secret=', displayed)

    def test_receipt_context_uses_the_decoder_timestamp_with_timezone_and_milliseconds(self):
        jst = timezone(timedelta(hours=9))
        timestamp = datetime(2026, 11, 8, 9, 0, 0, 123000, tzinfo=jst)
        entry = image_entry(SimpleNamespace(nmea=NMEA, timestamp=timestamp))
        self.assertEqual(entry, (NMEA, 1794096000123))
        self.assertEqual(entry, image_entry(SimpleNamespace(nmea=NMEA, timestamp=timestamp.astimezone(timezone.utc))))
        for invalid in (datetime(2026, 11, 8), None, 100):
            with self.assertRaises(ValueError):
                image_entry(SimpleNamespace(nmea=NMEA, timestamp=invalid))

    def test_url_precedes_body_and_keeps_all_original_frames_at_the_end(self):
        message = '本文\n---\nNMEA: original one\nNMEA: original two'
        url = 'https://images.example.test/image.png'
        self.assertEqual(with_image(message, url),
                         url + '\n\n本文\n---\nNMEA: original one\nNMEA: original two')
        self.assertEqual(with_image('本文', url), url + '\n\n本文')


if __name__ == '__main__':
    unittest.main()
