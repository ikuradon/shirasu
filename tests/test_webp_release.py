"""Release evidence binds signed posts, exact gallery inputs and real HTTP bytes."""

from contextlib import ExitStack
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

import image_protocol
from scripts import check_webp_release as check


NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
ENTRIES = ((NMEA, 1794092400123),)
TEXT = '検証用本文。\n---\nNMEA: ' + NMEA


def signed_id(event):
    serial = [0, event['pubkey'], event['created_at'], event['kind'], event['tags'], event['content']]
    event['id'] = hashlib.sha256(json.dumps(serial, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return event


def post():
    url = check.ORIGIN + image_protocol.make_path(ENTRIES, check.KEY_ID, check.KEY, version='v5', encoding='w1')
    alt = '検証用ALT'
    return {'case': 'weather', 'number': 1, 'kind': 'single', 'text': TEXT,
            'records': [list(row) for row in ENTRIES], 'record_indexes': [0], 'image_record_indexes': [0],
            'url': url, 'alt': alt, 'event': signed_id({
                'pubkey': 'a' * 64, 'sig': 'b' * 128, 'created_at': 1, 'kind': 1,
                'content': TEXT.replace('\n---\n', '\n\n' + url + '\n---\n'),
                'tags': [['imeta', 'url ' + url, 'm image/webp', 'alt ' + alt]]})}


def sample():
    with Image.new('RGBA', (3, 2), (23, 44, 177, 255)) as image:
        image.putpixel((1, 0), (200, 17, 80, 50))
        png, webp = BytesIO(), BytesIO()
        image.save(png, format='PNG')
        image.save(webp, format='WEBP', lossless=True, quality=100, method=4, exact=True)
        return png.getvalue(), webp.getvalue()


def response(data):
    return {'status': '200 OK', 'body': data, 'headers': {
        'content-type': 'image/webp', 'content-length': str(len(data)),
        'etag': '"' + hashlib.sha256(data).hexdigest() + '"',
        'cache-control': 'public, max-age=31536000, immutable',
        'vercel-cdn-cache-control': 'public, max-age=31536000',
        'x-content-type-options': 'nosniff'}}


class WebPReleaseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(image_protocol, 'SUPPORTED_VERSIONS', ('v1', 'v2', 'v3', 'v4', 'v5')))
        self.stack.enter_context(patch.object(image_protocol, 'WEBP_VERSIONS', ('v5',)))

    def test_attachment_requires_matching_format_body_alt_and_signed_original_inputs(self):
        row = post()
        path = check.attachment(row, image_protocol, entries=ENTRIES, text=TEXT, version='v5', encoding='w1')
        self.assertTrue(path.endswith('.w1.webp'))
        for kind in ('mime', 'alt', 'body', 'origin', 'encoding'):
            changed = deepcopy(row)
            if kind == 'mime':
                changed['event']['tags'][0][2] = 'm image/png'
            elif kind == 'alt':
                changed['alt'] = 'different'
            elif kind == 'body':
                changed['event']['content'] += 'changed'
            elif kind == 'origin':
                changed['url'] = changed['url'].replace('images.example.test', 'example.org')
            else:
                changed['url'] = changed['url'].replace('.w1.webp', '.png')
            signed_id(changed['event'])
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                check.attachment(changed, image_protocol, entries=ENTRIES, text=TEXT, encoding='w1')
        with self.assertRaises(ValueError):
            check.attachment(row, image_protocol, entries=((NMEA, ENTRIES[0][1] + 1),), text=TEXT)

    def test_webp_response_requires_real_format_exact_rgba_and_all_cache_headers(self):
        png, webp = sample()
        result = check.webp_response(response(webp), png)
        self.assertTrue(result['pixel_exact'])
        self.assertEqual((result['width'], result['height']), (3, 2))
        for key in response(webp)['headers']:
            wrong = response(webp)
            wrong['headers'][key] = 'incorrect'
            with self.subTest(key=key), self.assertRaises(ValueError):
                check.webp_response(wrong, png)
        with self.assertRaisesRegex(ValueError, 'not WebP'):
            check.webp_response(response(png), png)
        with Image.new('RGBA', (3, 2), 'red') as image:
            other = BytesIO()
            image.save(other, format='PNG')
        with self.assertRaisesRegex(ValueError, 'RGBA'):
            check.webp_response(response(webp), other.getvalue())

    def test_case_reference_remains_separate_from_actual_post_inputs(self):
        png, _ = sample()
        metadata = {'path': 'image.png', 'sha256': hashlib.sha256(png).hexdigest(), 'alt': 'ALT'}
        baseline = SimpleNamespace(_inputs=(('cancel', ('Weather', ENTRIES, ENTRIES)),),
            _assets=(('image.png', 'image', png),), manifest={'cases': [{
                'slug': 'cancel', 'images': {'preview': metadata}, 'preview_record_indexes': [0],
                'posts': [{'image_enabled': True, 'image_record_indexes': [0], 'record_indexes': [0],
                           'images': {'preview': metadata}}]}]})
        rows = check.gallery_inputs(baseline)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['cancel_reference'], ENTRIES)
        self.assertEqual(rows[1]['cancel_reference'], ())
        self.assertEqual(rows[0]['usages'][0]['role'], 'case')
        self.assertEqual(rows[1]['usages'][0]['role'], 'post')

    def test_post_comparison_rejects_changed_source_text_and_approved_alt(self):
        row = post()
        expected = {'kind': 'single', 'text': TEXT, 'record_indexes': [0], 'image_record_indexes': [0],
                    'images': {'preview': {'alt': row['alt']}}}
        baseline = SimpleNamespace(_inputs=(('weather', ('Weather', ENTRIES, ())),),
                                  manifest={'cases': [{'slug': 'weather', 'posts': [expected]}]})
        self.assertEqual(len(check.compare_posts({'posts': [row]}, baseline, image_protocol)), 1)
        for field, value in (('text', TEXT + 'changed'), ('records', [[NMEA, ENTRIES[0][1] + 1]]),
                             ('alt', 'different')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                check.compare_posts({'posts': [{**row, field: value}]}, baseline, image_protocol)

    def test_order_only_examples_are_separate_but_actual_posts_cannot_be_skipped(self):
        original = (('later', 2), ('earlier', 1))
        canonical = tuple(reversed(original))
        row = {'records': original, 'cancel_reference': (),
               'usages': [{'case': 'example', 'role': 'case', 'number': None}]}
        reason = check.offline_reason(row, canonical)
        self.assertEqual(reason['canonical_record_indexes'], [1, 0])
        self.assertIn('record order', reason['reason'])
        self.assertIsNone(check.offline_reason(row, original))
        # A post sharing that case must still undergo exact HTTP pixel checks.
        row['usages'].append({'case': 'example', 'role': 'post', 'number': 1})
        self.assertIsNone(check.offline_reason(row, canonical))
        row['cancel_reference'] = ENTRIES
        with self.assertRaisesRegex(ValueError, 'actual post'):
            check.offline_reason(row, canonical)

    def test_loopback_uses_real_http_method_headers_query_and_conditional_response(self):
        received = []

        def app(environ, start_response):
            received.append((environ['REQUEST_METHOD'], environ['PATH_INFO'], environ['QUERY_STRING'],
                             environ.get('HTTP_ACCEPT'), environ.get('HTTP_IF_NONE_MATCH')))
            status = '304 Not Modified' if environ.get('HTTP_IF_NONE_MATCH') else '200 OK'
            body = b'hello' if status == '200 OK' else b''
            start_response(status, [('Content-Type', 'image/webp'), ('Content-Length', str(len(body)))])
            return [] if environ['REQUEST_METHOD'] == 'HEAD' else [body]

        with check.local_http(app) as request:
            self.assertEqual(request('/v5/test/test.w1.webp', accept='image/png')['body'], b'hello')
            self.assertEqual(request('/v5/test/test.w1.webp', method='HEAD', query='x=1')['body'], b'')
            self.assertEqual(request('/v5/test/test.w1.webp', etag='"test"')['status'], '304 Not Modified')
            with self.assertRaises(ValueError):
                request('https://example.org/')
        self.assertEqual(received[0], ('GET', '/v5/test/test.w1.webp', '', 'image/png', None))
        self.assertEqual(received[1][:3], ('HEAD', '/v5/test/test.w1.webp', 'x=1'))
        self.assertEqual(received[2][-1], '"test"')

    def test_existing_output_is_refused_before_loading_any_runtime_or_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            output.write_text('preserve')
            with patch('scripts.prepare_release_candidate.verify_bundle') as verify:
                with self.assertRaises(ValueError):
                    check.check_receiver(Path(directory) / 'bundle', output)
                verify.assert_not_called()
            self.assertEqual(output.read_text(), 'preserve')


if __name__ == '__main__':
    unittest.main()
