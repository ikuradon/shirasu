"""Release verification rejects stale artifacts, changed inputs and false PNG evidence."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from shirasu.images.protocol import make_path
from tools.release import check_release_images as check


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII=')
NMEA = '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05'
ENTRIES = ((NMEA, 1794092400123),)
TEXT = '公開受信ログの検証用本文。\n---\nNMEA: ' + NMEA


def signed_id(event):
    content = [0, event['pubkey'], event['created_at'], event['kind'], event['tags'], event['content']]
    event['id'] = hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return event


def post():
    url = check.ORIGIN + make_path(ENTRIES, check.KEY_ID, check.KEY, version='v4')
    alt = '検証用の代替テキスト。'
    content = TEXT.replace('\n---\n', '\n\n' + url + '\n---\n')
    event = signed_id({'pubkey': 'a' * 64, 'sig': 'b' * 128, 'created_at': 1, 'kind': 1,
                       'tags': [['imeta', 'url ' + url, 'm image/png', 'alt ' + alt]], 'content': content})
    return {'case': 'weather', 'number': 1, 'kind': 'single', 'text': TEXT,
            'records': [list(row) for row in ENTRIES], 'record_indexes': [0], 'image_record_indexes': [0],
            'event': event, 'url': url, 'alt': alt}


def export():
    return {'schema_version': 1, 'candidate_version': 'v5', 'rollback_version': 'v4',
            'bundle_manifest_sha256': 'a' * 64, 'case_count': 1, 'post_count': 1,
            'posts': [post()], 'transition_posts': []}


def png_response(data=PNG):
    return {'status': '200 OK', 'body': data, 'headers': {
        'content-type': 'image/png', 'content-length': str(len(data)),
        'x-content-type-options': 'nosniff', 'etag': '"' + hashlib.sha256(data).hexdigest() + '"',
        'cache-control': 'public, max-age=31536000, immutable',
        'vercel-cdn-cache-control': 'public, max-age=31536000'}}


class ReleaseImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-http-release-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def save(self, value):
        path = self.root / 'input.json'
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def test_same_event_url_body_alt_and_original_input_are_required_together(self):
        from shirasu.images import protocol as image_protocol
        row = post()
        path = check._attachment(row, image_protocol, entries=ENTRIES, text=TEXT, version='v4')
        self.assertEqual(check.ORIGIN + path, row['url'])
        mutations = []
        wrong = deepcopy(row)
        wrong['alt'] += ' changed'
        mutations.append((wrong, ENTRIES, TEXT, 'v4'))
        wrong = deepcopy(row)
        wrong['event']['content'] += ' changed'
        mutations.append((wrong, ENTRIES, TEXT, 'v4'))
        wrong = deepcopy(row)
        wrong['event']['tags'][0][1] = 'url ' + row['url'] + '?x=1'
        signed_id(wrong['event'])
        mutations.append((wrong, ENTRIES, TEXT, 'v4'))
        wrong = deepcopy(row)
        wrong['url'] = wrong['url'].replace('images.example.test', 'example.org')
        mutations.append((wrong, ENTRIES, TEXT, 'v4'))
        mutations.append((row, ((NMEA, ENTRIES[0][1] + 1),), TEXT, 'v4'))
        mutations.append((row, ENTRIES, TEXT + ' changed', 'v4'))
        mutations.append((row, ENTRIES, TEXT, 'v3'))
        for value, entries, text, version in mutations:
            with self.subTest(version=version, alt=value['alt']), self.assertRaises(ValueError):
                check._attachment(value, image_protocol, entries=entries, text=text, version=version)

    def test_text_only_events_cannot_sneak_in_an_image_or_old_alt(self):
        from shirasu.images import protocol as image_protocol
        row = post()
        row.update(url=None, alt=None)
        row['event'].update(content=TEXT, tags=[])
        signed_id(row['event'])
        self.assertIsNone(check._attachment(row, image_protocol, entries=(), text=TEXT))
        for changes, entries in (({'alt': 'stale ALT'}, ()), ({}, ENTRIES)):
            with self.subTest(changes=changes, entries=entries), self.assertRaises(ValueError):
                check._attachment({**row, **changes}, image_protocol, entries=entries, text=TEXT)
        wrong = deepcopy(row)
        wrong['event']['tags'] = post()['event']['tags']
        signed_id(wrong['event'])
        with self.assertRaises(ValueError):
            check._attachment(wrong, image_protocol, entries=(), text=TEXT)

    def test_export_rejects_different_bundle_provenance_or_dropped_nmea(self):
        value = export()
        path = self.save(value)
        result, digest = check._export(path, manifest={'manifest_sha256': 'a' * 64})
        self.assertEqual(result, value)
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError, 'another candidate'):
            check._export(path, manifest={'manifest_sha256': 'b' * 64})
        for field, replacement in (('record_indexes', [False]), ('record_indexes', [1]),
                                   ('record_indexes', [0, 0]), ('image_record_indexes', [1]),
                                   ('number', 2), ('text', '本文だけ')):
            wrong = deepcopy(value)
            wrong['posts'][0][field] = replacement
            with self.subTest(field=field, value=replacement), self.assertRaises(ValueError):
                check._export(self.save(wrong))
        for field in ('case_count', 'post_count'):
            wrong = deepcopy(value)
            wrong[field] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                check._export(self.save(wrong))

    def test_json_duplicate_keys_and_linked_inputs_are_rejected(self):
        path = self.root / 'input.json'
        path.write_text('{"schema_version":1,"schema_version":2}')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            check._read_json(path)
        path.write_text('{"number":NaN}')
        with self.assertRaisesRegex(ValueError, 'Nonfinite'):
            check._read_json(path)
        path.write_text('{}')
        link = self.root / 'link.json'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            check._read_json(link)

    def test_runtime_origin_rejects_a_checkout_module_even_if_the_file_name_matches(self):
        artifact, checkout = self.root / 'artifact', self.root / 'checkout'
        artifact.mkdir()
        checkout.mkdir()
        module = ModuleType('image_service.app')
        module.__file__ = str(checkout / 'app.py')
        with self.assertRaisesRegex(ValueError, 'outside the isolated artifact'):
            check._origin(module, artifact)
        module.__file__ = str(artifact / 'app.py')
        check._origin(module, artifact)
        link = artifact / 'leak.py'
        link.symlink_to(checkout / 'app.py')
        module.__file__ = str(link)
        with self.assertRaises(ValueError):
            check._origin(module, artifact)

    def test_png_content_and_all_cache_headers_are_validated(self):
        result = check._png_response(png_response(), (1, 1))
        self.assertEqual(result['bytes'], len(PNG))
        for data in (PNG[:-1], PNG + b'extra', b'not PNG', PNG[:40] + b'bad' + PNG[43:]):
            with self.subTest(data_length=len(data)), self.assertRaises(ValueError):
                check._png_response(png_response(data), (1, 1))
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            check._png_response(png_response(), (1920, 1080))
        for name in png_response()['headers']:
            response = png_response()
            response['headers'][name] = 'wrong'
            with self.subTest(header=name), self.assertRaises(ValueError):
                check._png_response(response, (1, 1))

    def test_output_refuses_existing_file_and_existing_png_directory_before_rendering(self):
        output = self.root / 'result.json'
        self.assertEqual(check._fresh_output(output), (output, self.root / 'result-images'))
        output.write_text('preserve')
        with self.assertRaises(ValueError):
            check._fresh_output(output)
        self.assertEqual(output.read_text(), 'preserve')
        output.unlink()
        (self.root / 'result-images').mkdir()
        with self.assertRaises(ValueError):
            check._fresh_output(output)
        link = self.root / 'linked.json'
        link.symlink_to(self.root / 'absent')
        with self.assertRaises(ValueError):
            check._fresh_output(link)

    def test_legacy_comparison_cannot_pass_when_png_or_old_signed_url_changes(self):
        from shirasu.images import protocol as image_protocol
        records = []
        image = check._png_response(png_response(), (1, 1))
        for version, case in check.LEGACY_CASES:
            records.append({'version': version, 'case': case, 'entries': [list(row) for row in ENTRIES],
                            'url': check.ORIGIN + make_path(ENTRIES, check.KEY_ID, check.KEY, version=version),
                            **image})
        baseline = {'schema_version': 1, 'mode': 'legacy', 'receiver_export_sha256': 'e' * 64,
                    'records': records}
        # The response is independently checked; replace only the fixed pixel
        # dimensions here so no actual map renderer is needed by this unit test.
        with patch.object(check, '_request', return_value=png_response()), \
                patch.object(check, '_png_response', return_value=image), \
                patch('builtins.print'):
            result = check._compare_legacy(self.save(baseline), 'e' * 64, image_protocol, object())
            self.assertEqual(len(result['records']), 5)
            wrong = deepcopy(baseline)
            wrong['records'][0]['sha256'] = 'f' * 64
            with self.assertRaisesRegex(ValueError, 'Old PNG'):
                check._compare_legacy(self.save(wrong), 'e' * 64, image_protocol, object())
            wrong = deepcopy(baseline)
            wrong['records'][0]['url'] += '?changed=1'
            with self.assertRaisesRegex(ValueError, 'Old signed URL'):
                check._compare_legacy(self.save(wrong), 'e' * 64, image_protocol, object())
            with self.assertRaisesRegex(ValueError, 'mismatched receiver export'):
                check._compare_legacy(self.save(baseline), 'f' * 64, image_protocol, object())


if __name__ == '__main__':
    unittest.main()
