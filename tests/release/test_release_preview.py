"""Approved post images, rather than merely valid PNG responses, gate release."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from tools.release.compare_release_preview import compare
from shirasu.reports.publication import ATTRIBUTION


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
                       '+A8AAQUBAScY42YAAAAASUVORK5CYII=')
NMEA = '$QZQSM,56,C6ADE5A5990002580100008004619010EC80F511B200000000000012D31A380*0C'
PAYLOAD = b'caption\0different valid PNG'
CHUNK = struct.pack('>I', len(PAYLOAD)) + b'tEXt' + PAYLOAD + struct.pack('>I', zlib.crc32(b'tEXt' + PAYLOAD))
OTHER_PNG = PNG[:-12] + CHUNK + PNG[-12:]


def image(data=PNG, **fields):
    digest = hashlib.sha256(data).hexdigest()
    return {'sha256': digest, 'width': 1, 'height': 1, **fields}


class ReleasePreviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='shirasu-release-preview-tests-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.gallery = self.root / 'gallery'
        (self.gallery / 'images').mkdir(parents=True)
        (self.gallery / 'records').mkdir()
        self.events, self.images = self.root / 'posts.json', self.root / 'images.json'
        self.output = self.root / 'comparison.json'
        self.png_dir = self.root / 'images-images'
        self.png_dir.mkdir()
        self.png = self.png_dir / 'example-01.png'
        self.png.write_bytes(PNG)
        (self.gallery / 'images/case.png').write_bytes(OTHER_PNG)
        (self.gallery / 'images/post.png').write_bytes(PNG)
        records = [[NMEA, 1794228531340], [NMEA, 1794228531341]]
        (self.gallery / 'records/example.json').write_text(json.dumps([
            {'nmea': nmea, 'received_ms': ms} for nmea, ms in records]))
        text = '完成した訓練本文。\n---\n' + '\n'.join('NMEA: ' + nmea for nmea, _ in records)
        alt = '南海トラフの訓練用見出し。'
        post_image = image(path='images/post.png', version='preview', kind='topic', alt=alt)
        post = {'kind': 'nankai', 'text': text, 'record_indexes': [0, 1], 'record_count': 2,
                'image_record_indexes': [0], 'image_enabled': True, 'images': {'preview': post_image}}
        case_image = image(OTHER_PNG, path='images/case.png', version='preview', kind='summary', alt='別の受信束画像')
        manifest = {'schema_version': 1, 'commit': 'b' * 40, 'working_tree': True, 'cases': [{
            'slug': 'example', 'title': '訓練例', 'category': 'NankaiTroughEarthquake',
            'record_count': 2, 'records_path': 'records/example.json',
            'images': {'preview': case_image}, 'posts': [post]}]}
        (self.gallery / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False))
        url = 'https://images.example.test/v5/rehearsal/payload.signature.png'
        content = url + '\n\n' + text
        event = {'id': 'a' * 64, 'content': content,
                 'tags': [['imeta', 'url ' + url, 'm image/png', 'alt ' + alt]]}
        self.exported = {'schema_version': 1, 'candidate_version': 'v5', 'rollback_version': 'v4',
                         'bundle_manifest_sha256': 'd' * 64, 'case_count': 1, 'post_count': 1, 'posts': [{
                             'case': 'example', 'number': 1, 'kind': 'nankai', 'text': text,
                             'records': records, 'record_indexes': [0, 1], 'image_record_indexes': [0],
                             'event': event, 'url': url, 'alt': alt}]}
        metadata = image(path='images-images/example-01.png', bytes=len(PNG),
                         etag='"' + hashlib.sha256(PNG).hexdigest() + '"')
        self.report = {'schema_version': 1, 'mode': 'candidate', 'candidate_version': 'v5',
                       'bundle_manifest_sha256': 'd' * 64, 'case_count': 1, 'post_count': 1,
                       'attachment_count': 1, 'transition_posts': [], 'posts': [{
                           'case': 'example', 'number': 1, 'url': url, 'event_id': event['id'],
                           'alt': alt, 'image': metadata}]}
        self.save()

    def save(self, exported=None, report=None):
        self.events.write_text(json.dumps(self.exported if exported is None else exported, ensure_ascii=False))
        value = deepcopy(self.report if report is None else report)
        value['receiver_export_sha256'] = hashlib.sha256(self.events.read_bytes()).hexdigest()
        self.images.write_text(json.dumps(value, ensure_ascii=False))

    def run_check(self):
        return compare(self.events, self.images, self.gallery, self.output)

    def test_matches_exact_post_preview_not_case_bundle_and_ignores_commit_labels(self):
        result = self.run_check()
        self.assertEqual((result['matched_cases'], result['matched_posts'], result['matched_pngs']), (1, 1, 1))
        self.assertEqual(result['verified_saved_pngs'], 1)
        self.assertEqual(result['receiver_export_sha256'], hashlib.sha256(self.events.read_bytes()).hexdigest())
        self.assertEqual(result['gallery_manifest_sha256'], hashlib.sha256((self.gallery / 'manifest.json').read_bytes()).hexdigest())
        self.assertEqual(json.loads(self.output.read_text()), result)

    def test_different_input_text_kind_indexes_or_alt_cannot_pass_with_the_same_png(self):
        mutations = [('kind', 'single'), ('text', '別の本文'), ('record_indexes', [1, 0]),
                     ('image_record_indexes', [1]), ('image_record_indexes', [False]),
                     ('records', [[NMEA, 1794228531342], [NMEA, 1794228531341]]), ('alt', '別のALT')]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                exported, report = deepcopy(self.exported), deepcopy(self.report)
                exported['posts'][0][field] = value
                if field == 'alt':
                    exported['posts'][0]['event']['tags'][0][3] = 'alt ' + value
                    report['posts'][0]['alt'] = value
                self.save(exported, report)
                with self.assertRaises(ValueError):
                    self.run_check()
                self.assertFalse(self.output.exists())

    def test_generated_hashtag_footer_requires_exact_signed_tags_alongside_imeta(self):
        row = self.exported['posts'][0]
        text = row['text'].replace('\n---\n', '\n\n#QZSS #南海トラフ #訓練\n\n' + ATTRIBUTION + '\n---\n')
        manifest_path = self.gallery / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['cases'][0]['posts'][0]['text'] = text
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
        row['text'] = text
        row['event']['content'] = row['url'] + '\n\n' + text
        metadata = row['event']['tags']
        t_tags = [['t', 'qzss'], ['t', '南海トラフ'], ['t', '訓練']]
        row['event']['tags'] = t_tags + metadata
        self.save()
        self.assertEqual(self.run_check()['matched_posts'], 1)
        self.output.unlink()
        for tags in (metadata, t_tags[:2] + metadata,
                     t_tags + [['t', '地震']] + metadata, metadata + t_tags):
            row['event']['tags'] = tags
            self.save()
            with self.subTest(tags=tags), self.assertRaisesRegex(ValueError, 'signed post differs'):
                self.run_check()
            self.assertFalse(self.output.exists())

    def test_archived_post_layout_matches_without_rewriting_the_export(self):
        exported = deepcopy(self.exported)
        row = exported['posts'][0]
        row['event']['content'] = row['text'].replace('\n---\n', '\n\n' + row['url'] + '\n---\n')
        self.save(exported)
        before = self.events.read_bytes()
        self.assertEqual(self.run_check()['matched_posts'], 1)
        self.assertEqual(self.events.read_bytes(), before)

    def test_other_url_positions_cannot_pass_with_unchanged_post_text_and_png(self):
        row = self.exported['posts'][0]
        for content in (row['text'] + '\n\n' + row['url'], row['url'] + '\n' + row['text']):
            exported = deepcopy(self.exported)
            exported['posts'][0]['event']['content'] = content
            self.save(exported)
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, 'signed post differs'):
                self.run_check()
            self.assertFalse(self.output.exists())

    def test_valid_but_different_http_png_is_rejected_even_when_the_report_hash_matches(self):
        self.png.write_bytes(OTHER_PNG)
        report = deepcopy(self.report)
        report['posts'][0]['image'] = image(OTHER_PNG, path='images-images/example-01.png', bytes=len(OTHER_PNG),
                                            etag='"' + hashlib.sha256(OTHER_PNG).hexdigest() + '"')
        self.save(report=report)
        with self.assertRaisesRegex(ValueError, 'approved post preview'):
            self.run_check()

    def test_changed_missing_and_extra_saved_pngs_are_rejected(self):
        self.png.write_bytes(OTHER_PNG)
        with self.assertRaisesRegex(ValueError, 'differs from its report'):
            self.run_check()
        self.png.unlink()
        with self.assertRaises(ValueError):
            self.run_check()
        self.png.write_bytes(PNG)
        (self.png_dir / 'undeclared.png').write_bytes(PNG)
        with self.assertRaisesRegex(ValueError, 'missing or undeclared'):
            self.run_check()

    def test_rollback_generation_and_unrelated_bundle_or_event_exports_are_rejected(self):
        exported, report = deepcopy(self.exported), deepcopy(self.report)
        old = exported['posts'][0]['url']
        new = old.replace('/v5/', '/v4/')
        exported['posts'][0]['url'] = report['posts'][0]['url'] = new
        exported['posts'][0]['event']['content'] = exported['posts'][0]['event']['content'].replace(old, new)
        exported['posts'][0]['event']['tags'][0][1] = 'url ' + new
        self.save(exported, report)
        with self.assertRaises(ValueError):
            self.run_check()
        self.save()
        for field in ('bundle_manifest_sha256', 'receiver_export_sha256'):
            report = json.loads(self.images.read_text())
            report[field] = 'e' * 64
            self.images.write_text(json.dumps(report))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_check()
            self.save()

    def test_added_missing_and_duplicate_posts_cannot_be_reported_as_a_match(self):
        for rows in ([], self.report['posts'] * 2,
                     [dict(self.report['posts'][0], case='other')],
                     [dict(self.report['posts'][0], number=True)]):
            report = dict(self.report, posts=rows, post_count=len(rows))
            self.save(report=report)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.run_check()

    def test_unsafe_or_linked_image_paths_and_corrupt_approved_preview_are_rejected(self):
        for name in ('../escape.png', 'images-images/../escape.png', '/tmp/escape.png'):
            report = deepcopy(self.report)
            report['posts'][0]['image']['path'] = name
            self.save(report=report)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.run_check()
        self.save()
        self.png.unlink()
        self.png.symlink_to(self.gallery / 'images/post.png')
        with self.assertRaises(ValueError):
            self.run_check()
        self.png.unlink()
        self.png.write_bytes(PNG)
        (self.gallery / 'images/post.png').write_bytes(OTHER_PNG)
        with self.assertRaises(ValueError):
            self.run_check()

    def test_existing_result_and_output_inside_gallery_are_never_written(self):
        self.output.write_text('preserve previous result')
        with self.assertRaises(ValueError):
            self.run_check()
        self.assertEqual(self.output.read_text(), 'preserve previous result')
        self.output = self.gallery / 'new-result.json'
        with self.assertRaises(ValueError):
            self.run_check()
        self.assertFalse(self.output.exists())
        self.output = self.png_dir / 'new-result.json'
        with self.assertRaises(ValueError):
            self.run_check()
        self.assertFalse(self.output.exists())

    def test_declared_transition_pngs_are_verified_without_counting_them_as_approved_posts(self):
        report = deepcopy(self.report)
        existing = {key: value for key, value in report['posts'][0]['image'].items() if key != 'path'}
        report['transition_posts'] = [
            {'label': 'already-rendered', 'url': report['posts'][0]['url'], **existing},
            {'label': 'text-only', 'image': None},
            {'label': 'old-url', 'url': report['posts'][0]['url'].replace('/v5/', '/v4/'), **existing},
        ]
        (self.png_dir / 'transition-03-v4.png').write_bytes(PNG)
        self.save(report=report)
        result = self.run_check()
        self.assertEqual((result['matched_pngs'], result['verified_saved_pngs']), (1, 2))
        self.output.unlink()
        (self.png_dir / 'transition-03-v4.png').write_bytes(OTHER_PNG)
        with self.assertRaisesRegex(ValueError, 'differs from its report'):
            self.run_check()


if __name__ == '__main__':
    unittest.main()
