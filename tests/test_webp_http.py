"""Signed WebP HTTP responses preserve the independent PNG cache contract."""

import hashlib
import importlib
import json
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import image_protocol as protocol


app_module = importlib.import_module('image_service.app')
NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
RECORDS = ((NMEA, 1794092400123),)
KEY = bytes(range(32))
PNG = b'\x89PNG\r\n\x1a\nsynthetic legacy output'
# A container-shaped mock payload; real codec pixels are tested separately.
WEBP = b'RIFF' + (14).to_bytes(4, 'little') + b'WEBPVP8L' + (2).to_bytes(4, 'little') + b'\x2f\x00'


class WebPHTTPTests(unittest.TestCase):
    def setUp(self):
        self.renderer = ModuleType('image_service.webp_test_renderer')
        self.renderer.render = Mock(return_value=SimpleNamespace(png=PNG))
        self.renderer.render_webp = Mock(return_value=SimpleNamespace(data=WEBP, mime='image/webp'))
        self.renderer.verify_assets = Mock(return_value=True)
        self.renderer.verify_webp_encoder = Mock(return_value=True)
        self.enterContext(patch.dict(sys.modules, {self.renderer.__name__: self.renderer}))
        versions = ('v1', 'v2', 'v3', 'v4', 'v5')
        self.enterContext(patch.object(protocol, 'SUPPORTED_VERSIONS', versions))
        self.enterContext(patch.object(protocol, 'WEBP_VERSIONS', ('v5',)))
        self.enterContext(patch.object(app_module, 'SUPPORTED_VERSIONS', versions))
        self.enterContext(patch.object(app_module, 'WEBP_VERSIONS', ('v5',)))
        self.enterContext(patch.object(app_module, 'RENDERERS', {
            'v1': self.renderer.__name__, 'v5': self.renderer.__name__}))
        self.enterContext(patch.dict('os.environ', {'SHIRASU_IMAGE_KEYS': json.dumps({'k1': KEY.hex()})}))
        self.png_path = protocol.make_path(RECORDS, 'k1', KEY, version='v5')
        self.webp_path = protocol.make_path(RECORDS, 'k1', KEY, version='v5', encoding='w1')

    def request(self, path, **environ):
        response = {}

        def start_response(status, headers):
            response.update(status=status, headers=dict(headers))

        response['body'] = b''.join(app_module.app({
            'REQUEST_METHOD': 'GET', 'PATH_INFO': path, 'QUERY_STRING': '', **environ}, start_response))
        return response

    def test_get_head_and_conditional_responses_use_actual_webp_bytes(self):
        get = self.request(self.webp_path)
        self.assertEqual(get['status'], '200 OK')
        self.assertEqual(get['body'], WEBP)
        self.assertEqual(get['headers']['Content-Type'], 'image/webp')
        self.assertEqual(get['headers']['Content-Length'], str(len(WEBP)))
        self.assertEqual(get['headers']['ETag'], '"' + hashlib.sha256(WEBP).hexdigest() + '"')
        self.assertEqual(get['headers']['X-Content-Type-Options'], 'nosniff')
        self.assertIn('immutable', get['headers']['Cache-Control'])
        self.assertEqual(get['headers']['Vercel-CDN-Cache-Control'], 'public, max-age=31536000')
        self.renderer.render_webp.assert_called_once_with(RECORDS)
        self.renderer.render.assert_not_called()
        head = self.request(self.webp_path, REQUEST_METHOD='HEAD')
        self.assertEqual(head['status'], '200 OK')
        self.assertEqual(head['headers'], get['headers'])
        self.assertEqual(head['body'], b'')
        for method in ('GET', 'HEAD'):
            unchanged = self.request(self.webp_path, REQUEST_METHOD=method,
                                     HTTP_IF_NONE_MATCH=get['headers']['ETag'])
            self.assertEqual(unchanged['status'], '304 Not Modified')
            self.assertEqual(unchanged['body'], b'')
            self.assertEqual(unchanged['headers']['Content-Type'], 'image/webp')
            self.assertEqual(unchanged['headers']['ETag'], get['headers']['ETag'])

    def test_accept_never_changes_a_url_representation_and_png_remains_identical(self):
        for accept in ('image/png', 'image/webp', '*/*'):
            with self.subTest(accept=accept):
                png = self.request(self.png_path, HTTP_ACCEPT=accept)
                webp = self.request(self.webp_path, HTTP_ACCEPT=accept)
                self.assertEqual(png['body'], PNG)
                self.assertEqual(png['headers']['Content-Type'], 'image/png')
                self.assertEqual(png['headers']['ETag'], '"' + hashlib.sha256(PNG).hexdigest() + '"')
                self.assertEqual(webp['body'], WEBP)
                self.assertEqual(webp['headers']['Content-Type'], 'image/webp')
                self.assertNotEqual(png['headers']['ETag'], webp['headers']['ETag'])
                self.assertNotIn('Vary', webp['headers'])
        old = protocol.make_path(RECORDS, 'k1', KEY)
        self.assertEqual(self.request(old)['body'], PNG)

    def test_tampered_routes_fail_before_decompression_or_rendering(self):
        invalid = (self.webp_path.replace('.w1.webp', '.png'),
                   self.png_path[:-4] + '.w1.webp',
                   self.webp_path.replace('.w1.', '.w2.'),
                   self.webp_path.replace('/v5/', '/v1/'))
        with patch.object(protocol.zlib, 'decompressobj', side_effect=AssertionError('No expansion')):
            for path in invalid:
                with self.subTest(path=path):
                    response = self.request(path)
                    self.assertEqual(response['status'], '403 Forbidden')
                    self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        self.renderer.render.assert_not_called()
        self.renderer.render_webp.assert_not_called()

    def test_encoder_failures_or_wrong_formats_never_fall_back_to_png(self):
        failures = (
            SimpleNamespace(data=PNG, mime='image/webp'),
            SimpleNamespace(data=WEBP, mime='image/png'),
            SimpleNamespace(data=WEBP[:-1], mime='image/webp'),
            SimpleNamespace(data=WEBP.replace(b'VP8L', b'VP8 '), mime='image/webp'),
            SimpleNamespace(png=PNG),
        )
        for result in failures:
            with self.subTest(result=result):
                self.renderer.render_webp.return_value = result
                response = self.request(self.webp_path)
                self.assertEqual(response['status'], '503 Service Unavailable')
                self.assertEqual(response['headers']['Cache-Control'], 'no-store')
                self.assertNotIn('ETag', response['headers'])
                self.assertFalse(response['body'].startswith(b'\x89PNG'))
        for error, status in ((RuntimeError('private encoder failure'), '503 Service Unavailable'),
                              (ValueError('private report failure'), '400 Bad Request')):
            self.renderer.render_webp.side_effect = error
            response = self.request(self.webp_path)
            self.assertEqual(response['status'], status)
            self.assertNotIn(b'private', response['body'])
        self.renderer.render.assert_not_called()

    def test_health_checks_only_registered_webp_encoder_and_fails_closed(self):
        self.assertEqual(self.request('/healthz')['status'], '200 OK')
        self.renderer.verify_webp_encoder.assert_called_once_with()
        self.renderer.verify_webp_encoder.side_effect = RuntimeError('wrong codec runtime')
        self.assertEqual(self.request('/healthz')['status'], '503 Service Unavailable')
        with patch.object(app_module, 'WEBP_VERSIONS', ()):
            self.assertEqual(self.request('/healthz')['status'], '200 OK')

    def test_queries_methods_and_unregistered_generation_keep_error_contract(self):
        for environ, status in (({'QUERY_STRING': 'quality=0'}, '400 Bad Request'),
                                ({'REQUEST_METHOD': 'POST'}, '405 Method Not Allowed')):
            response = self.request(self.webp_path, **environ)
            self.assertEqual(response['status'], status)
            self.assertEqual(response['headers']['Cache-Control'], 'no-store')
        with patch.object(app_module, 'SUPPORTED_VERSIONS', ('v1', 'v2', 'v3', 'v4')):
            self.assertEqual(self.request(self.webp_path)['status'], '404 Not Found')
        self.renderer.render.assert_not_called()
        self.renderer.render_webp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
