"""Keep published v1 URLs valid while opting new publications into later renderers."""

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from image_protocol import MAX_EXPANDED_BYTES, ProtocolError, make_path, parse_path, path_version
from image_publication import ImagePublisher

if importlib.util.find_spec('image_service'):
    from image_service.app import app
else:
    app = None

NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
AT = 1794092400123
RECORDS = ((NMEA, AT),)
KEY = bytes(range(32))  # Isolated test key, never loaded from the deployment.
KEYS = {'k1': KEY}
LEGACY_PATH = ('/v1/k1/eNotjDEKgDAMAP8SnCRD0iaadivVbg7ipvgJcRP_bhVvObjhLjggbhshNPM6LxOq'
               'Yu5SsZDMUcU7eaVUpWJZR5fpxw-lGBF_gdlUxJfcksK-I5wQuQ9C4T2w8_cDOTcYBQ.'
               'Qjb7-aZGigftCfS02dY4NL2QRK-dT_yMJJtcU2UJB5E.png')
V2_PATH = ('/v2/k1/eNotjDEKgDAMAP8SnCRD0iaadivVbg7ipvgJcRP_bhVvObjhLjggbhshNPM6LxOq'
           'Yu5SsZDMUcU7eaVUpWJZR5fpxw-lGBF_gdlUxJfcksK-I5wQuQ9C4T2w8_cDOTcYBQ.'
           'uT2cLp1gyMoQNmPLeQ9SvWZSBaAkbQSKJFHTeCBEQsw.png')
V3_PATH = ('/v3/k1/eNotjDEKgDAMAP8SnCRD0iaadivVbg7ipvgJcRP_bhVvObjhLjggbhshNPM6LxOq'
           'Yu5SsZDMUcU7eaVUpWJZR5fpxw-lGBF_gdlUxJfcksK-I5wQuQ9C4T2w8_cDOTcYBQ.'
           'tfkLOLECr-etPI8TaOJM7e2hmfImkvNeTAtlOUaLT5M.png')
GENERATIONS = (('v1', 'image_service.render'), ('v2', 'image_service.render_v2'),
               ('v3', 'image_service.render_v3'), ('v4', 'image_service.render_v4'))
ENVIRONMENT = {
    'SHIRASU_IMAGE_BASE_URL': 'https://images.example.test',
    'SHIRASU_IMAGE_KEY_ID': 'k1',
    'SHIRASU_IMAGE_SIGNING_KEY': KEY.hex(),
}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


class ImageVersionProtocolTests(unittest.TestCase):
    def test_v4_preserves_saved_urls_and_authenticates_its_own_generation(self):
        for version, expected in (('v1', LEGACY_PATH), ('v2', V2_PATH), ('v3', V3_PATH)):
            with self.subTest(version=version):
                self.assertEqual(make_path(RECORDS, 'k1', KEY, version=version), expected)
                self.assertEqual(parse_path(expected, KEYS), RECORDS)
        current = make_path(RECORDS, 'k1', KEY, version='v4')
        self.assertEqual(parse_path(current, KEYS), RECORDS)
        self.assertEqual(path_version(current), 'v4')
        payload, signature, _ = current.rsplit('/', 1)[1].split('.')
        self.assertEqual(payload, V3_PATH.rsplit('/', 1)[1].split('.')[0])
        expected = hmac.new(KEY, b'shirasu-image\0v4\0k1\0' + payload.encode(), hashlib.sha256).digest()
        self.assertEqual(signature, b64(expected))
        publisher = ImagePublisher.from_environment(dict(ENVIRONMENT, SHIRASU_IMAGE_VERSION='v4'))
        self.assertEqual(publisher.version, 'v4')
        self.assertEqual(publisher.url(RECORDS), ENVIRONMENT['SHIRASU_IMAGE_BASE_URL'] + current)
        with patch('image_protocol.zlib.decompressobj', side_effect=AssertionError('must authenticate first')):
            for old in (LEGACY_PATH, V2_PATH, V3_PATH):
                with self.subTest(path=old[:7]), self.assertRaises(ProtocolError):
                    parse_path(old.replace('/' + path_version(old) + '/', '/v4/'), KEYS)
            for version in ('v1', 'v2', 'v3'):
                with self.subTest(version=version), self.assertRaises(ProtocolError):
                    parse_path(current.replace('/v4/', '/' + version + '/'), KEYS)

    def test_v3_preserves_v2_and_authenticates_its_own_generation(self):
        self.assertEqual(make_path(RECORDS, 'k1', KEY, version='v2'), V2_PATH)
        self.assertEqual(parse_path(V2_PATH, KEYS), RECORDS)
        current = make_path(RECORDS, 'k1', KEY, version='v3')
        self.assertEqual(parse_path(current, KEYS), RECORDS)
        publisher = ImagePublisher.from_environment(dict(ENVIRONMENT, SHIRASU_IMAGE_VERSION='v3'))
        self.assertEqual(publisher.url(RECORDS), ENVIRONMENT['SHIRASU_IMAGE_BASE_URL'] + current)
        with patch('image_protocol.zlib.decompressobj', side_effect=AssertionError('must authenticate first')):
            for old in (LEGACY_PATH, V2_PATH):
                with self.subTest(path=old[:7]), self.assertRaises(ProtocolError):
                    parse_path(old.replace('/' + path_version(old) + '/', '/v3/'), KEYS)
            for version in ('v1', 'v2'):
                with self.subTest(version=version), self.assertRaises(ProtocolError):
                    parse_path(current.replace('/v3/', '/' + version + '/'), KEYS)

    def test_saved_v1_url_and_new_default_url_stay_byte_identical(self):
        self.assertEqual(parse_path(LEGACY_PATH, KEYS), RECORDS)
        self.assertEqual(make_path(RECORDS, 'k1', KEY), LEGACY_PATH)
        self.assertEqual(make_path(RECORDS, 'k1', KEY, version='v1'), LEGACY_PATH)
        self.assertEqual(path_version(LEGACY_PATH), 'v1')

    def test_v2_uses_same_payload_with_its_own_authenticated_version(self):
        current = make_path(RECORDS, 'k1', KEY, version='v2')
        self.assertEqual(parse_path(current, KEYS), RECORDS)
        self.assertEqual(path_version(current), 'v2')
        payload, signature, _ = current.rsplit('/', 1)[1].split('.')
        self.assertEqual(payload, LEGACY_PATH.rsplit('/', 1)[1].split('.')[0])
        expected = hmac.new(KEY, b'shirasu-image\0v2\0k1\0' + payload.encode(), hashlib.sha256).digest()
        self.assertEqual(signature, b64(expected))
        self.assertNotEqual(signature, LEGACY_PATH.rsplit('/', 1)[1].split('.')[1])

    def test_relabeling_either_version_fails_before_decompression(self):
        paths = [LEGACY_PATH.replace('/v1/', '/v2/'),
                 make_path(RECORDS, 'k1', KEY, version='v2').replace('/v2/', '/v1/')]
        with patch('image_protocol.zlib.decompressobj', side_effect=AssertionError('must authenticate first')):
            for path in paths:
                with self.subTest(path=path[:7]), self.assertRaises(ProtocolError):
                    parse_path(path, KEYS)

    def test_unknown_versions_cannot_be_signed_or_read(self):
        for version in ('v0', 'v99', 'V2', '', 'v2/extra', None, 2):
            with self.subTest(version=version), self.assertRaises(ProtocolError):
                make_path(RECORDS, 'k1', KEY, version=version)
        with patch('image_protocol.zlib.decompressobj', side_effect=AssertionError('invalid route')):
            with self.assertRaises(ProtocolError):
                parse_path(LEGACY_PATH.replace('/v1/', '/v99/'), KEYS)

    def test_v2_keeps_bounded_decompression_for_authorized_inputs(self):
        payload = b64(zlib.compress(b' ' * (MAX_EXPANDED_BYTES * 100), 9))
        signature = b64(hmac.new(KEY, b'shirasu-image\0v2\0k1\0' + payload.encode(), hashlib.sha256).digest())
        with self.assertRaises(ProtocolError):
            parse_path(f'/v2/k1/{payload}.{signature}.png', KEYS)

    def test_receiver_version_is_explicit_and_does_not_rewrite_old_urls(self):
        old = ImagePublisher.from_environment(ENVIRONMENT)
        current = ImagePublisher.from_environment(dict(ENVIRONMENT, SHIRASU_IMAGE_VERSION='v2'))
        self.assertEqual(old.version, 'v1')
        self.assertEqual(old.url(RECORDS), ENVIRONMENT['SHIRASU_IMAGE_BASE_URL'] + LEGACY_PATH)
        self.assertEqual(current.version, 'v2')
        self.assertTrue(current.url(RECORDS).startswith('https://images.example.test/v2/'))
        self.assertEqual(parse_path(LEGACY_PATH, KEYS), RECORDS)
        self.assertNotIn(KEY.hex(), repr(current))

    def test_optional_version_does_not_enable_images_or_allow_partial_configuration(self):
        self.assertIsNone(ImagePublisher.from_environment({'SHIRASU_IMAGE_VERSION': 'v2'}))
        self.assertIsNone(ImagePublisher.from_environment(dict.fromkeys(ENVIRONMENT, '') | {'SHIRASU_IMAGE_VERSION': 'v2'}))
        for name in ENVIRONMENT:
            incomplete = dict(ENVIRONMENT, SHIRASU_IMAGE_VERSION='v2')
            del incomplete[name]
            with self.subTest(missing=name), self.assertRaises(ValueError):
                ImagePublisher.from_environment(incomplete)
        for version in ('', 'v99', 'V2', None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                ImagePublisher.from_environment(dict(ENVIRONMENT, SHIRASU_IMAGE_VERSION=version))


@unittest.skipUnless(app, 'The lightweight receiver image does not include the HTTP renderer')
class ImageVersionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.renderers = {}
        for version, name in GENERATIONS:
            module = ModuleType(name)
            module.render = Mock(return_value=SimpleNamespace(png=b'\x89PNG\r\n\x1a\n' + version.encode()))
            module.verify_assets = Mock(return_value=True)
            self.renderers[name] = module
        self.enterContext(patch.dict(sys.modules, self.renderers))
        self.enterContext(patch.dict('os.environ', {'SHIRASU_IMAGE_KEYS': json.dumps({'k1': KEY.hex()})}))

    def request(self, path, **environ):
        response = {}
        def start_response(status, headers):
            response.update(status=status, headers=dict(headers))
        response['body'] = b''.join(app({'REQUEST_METHOD': 'GET', 'PATH_INFO': path,
                                        'QUERY_STRING': '', **environ}, start_response))
        return response

    def test_authenticated_path_selects_only_its_frozen_renderer(self):
        for version, module_name in GENERATIONS:
            with self.subTest(version=version):
                for module in self.renderers.values():
                    module.render.reset_mock()
                response = self.request(make_path(RECORDS, 'k1', KEY, version=version))
                self.assertEqual(response['status'], '200 OK')
                self.assertEqual(response['headers']['Content-Type'], 'image/png')
                self.assertIn('immutable', response['headers']['Cache-Control'])
                self.assertTrue(response['body'].endswith(version.encode()))
                self.renderers[module_name].render.assert_called_once_with(RECORDS)
                for name, module in self.renderers.items():
                    if name != module_name:
                        module.render.assert_not_called()

    def test_relabeling_path_does_not_reach_any_renderer(self):
        for source, _ in GENERATIONS:
            path = make_path(RECORDS, 'k1', KEY, version=source)
            for target, _ in GENERATIONS:
                if target == source:
                    continue
                with self.subTest(source=source, target=target):
                    response = self.request(path.replace('/' + source + '/', '/' + target + '/', 1))
                    self.assertEqual(response['status'], '403 Forbidden')
                    self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        for module in self.renderers.values():
            module.render.assert_not_called()

    def test_unsupported_generation_is_not_found_and_not_cached(self):
        response = self.request(LEGACY_PATH.replace('/v1/', '/v99/'))
        self.assertEqual(response['status'], '404 Not Found')
        self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        for module in self.renderers.values():
            module.render.assert_not_called()

    def test_health_checks_assets_of_all_generations(self):
        response = self.request('/healthz')
        self.assertEqual(response['status'], '200 OK')
        for module in self.renderers.values():
            module.verify_assets.assert_called_once_with()
        for version, module in self.renderers.items():
            with self.subTest(version=version):
                module.verify_assets.side_effect = ValueError('bad asset')
                response = self.request('/healthz')
                self.assertEqual(response['status'], '503 Service Unavailable')
                self.assertEqual(response['headers']['Cache-Control'], 'no-store')
                self.assertNotIn(b'bad asset', response['body'])
                module.verify_assets.side_effect = None

    def test_generations_keep_head_conditional_and_error_cache_contract(self):
        for version, name in GENERATIONS:
            with self.subTest(version=version):
                path = make_path(RECORDS, 'k1', KEY, version=version)
                get = self.request(path)
                head = self.request(path, REQUEST_METHOD='HEAD')
                self.assertEqual(head['headers'], get['headers'])
                self.assertEqual(head['body'], b'')
                unchanged = self.request(path, HTTP_IF_NONE_MATCH=get['headers']['ETag'])
                self.assertEqual(unchanged['status'], '304 Not Modified')
                self.assertEqual(unchanged['body'], b'')
                self.renderers[name].render.side_effect = ValueError('bad report')
                failure = self.request(path)
                self.assertEqual(failure['status'], '400 Bad Request')
                self.assertEqual(failure['headers']['Cache-Control'], 'no-store')


if __name__ == '__main__':
    unittest.main()
