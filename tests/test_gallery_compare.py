"""Saved previews compare identical inputs, with all copied assets verified first."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from scripts.gallery_compare import attach_comparison, load_baseline


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII=')
NMEA = '$QZQSM,56,C6ADE5A5990002580100008004619010EC80F511B200000000000012D31A380*0C'
CONDITIONS = {'images_enabled': True, 'history': 'empty',
              'input_timing': 'same_virtual_time_in_input_order', 'collection_wait_seconds': 180}


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


OTHER_PNG = PNG[:-12] + chunk(b'tEXt', b'comment\x00another image') + PNG[-12:]


def reason(code='no_area'):
    return {'code': code, 'stage': 'area', 'message': code, 'detail': ''}


class GalleryCompareTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='shirasu-compare-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.old, self.new = self.root / 'old', self.root / 'new'

    def gallery(self, directory, slugs=('sample',), *, posts=True, png=PNG):
        directory.mkdir()
        (directory / 'images').mkdir()
        (directory / 'records').mkdir()
        (directory / 'LICENSE.azarashi').write_text('MIT License\nFixture license\n')
        manifest = {'schema_version': 1, 'renderer': 'preview', 'error_count': 0,
                    'source_license_path': 'LICENSE.azarashi', 'commit': 'a' * 40,
                    'working_tree': False, 'cases': []}
        if posts:
            manifest['post_preview'] = deepcopy(CONDITIONS)
        for slug in slugs:
            data = [{'nmea': NMEA, 'received_ms': 1794228531340}]
            (directory / 'records' / (slug + '.json')).write_text(json.dumps(data))
            (directory / 'images' / (slug + '.png')).write_bytes(png)
            image = {'path': f'images/{slug}.png', 'sha256': hashlib.sha256(png).hexdigest(),
                     'width': 1, 'height': 1, 'kind': 'map', 'version': 'preview',
                     'decision': {'mode': 'map', 'reasons': []}}
            case = {'slug': slug, 'title': '台風の例', 'category': 'Typhoon', 'record_count': 1,
                    'records_path': f'records/{slug}.json', 'source_sha256': 'b' * 64,
                    'images': {'preview': image}}
            if posts:
                case['posts'] = [{'text': '本文\n---\nNMEA: ' + NMEA, 'record_indexes': [0],
                                  'record_count': 1, 'kind': 'typhoon', 'image_enabled': True,
                                  'images': {'preview': deepcopy(image)}}]
            manifest['cases'].append(case)
        self.save(directory, manifest)
        return manifest

    def save(self, directory, manifest):
        (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False))

    def compare(self, old=None, new=None):
        if old is not None:
            self.save(self.old, old)
        baseline = load_baseline(self.old)
        if new is None:
            new = json.loads((self.new / 'manifest.json').read_text())
        return attach_comparison(new, baseline, self.new)

    def test_identical_previews_and_posts_are_unchanged_and_snapshot_is_self_contained(self):
        old = self.gallery(self.old)
        new = self.gallery(self.new)
        before = {str(path.relative_to(self.old)): path.read_bytes()
                  for path in self.old.rglob('*') if path.is_file()}
        result = self.compare(new=new)
        self.assertIs(result, new)
        comparison = result['cases'][0]['comparison']
        for view in ('images', 'posts'):
            self.assertEqual(comparison[view], {'status': 'unchanged', 'changes': [], 'note': ''})
            self.assertEqual(result['baseline']['counts'][view]['unchanged'], 1)
        self.assertEqual(result['baseline']['commit'], old['commit'])
        self.assertFalse(result['baseline']['working_tree'])
        previous = comparison['previous']
        self.assertTrue(previous['records_path'].startswith('baseline/records/'))
        self.assertEqual((self.new / previous['images']['preview']['path']).read_bytes(), PNG)
        self.assertEqual(previous['images'], previous['posts'][0]['images'])
        snapshot = load_baseline(self.new / 'baseline').manifest
        self.assertNotIn('comparison', snapshot['cases'][0])
        self.assertNotIn('baseline', snapshot)
        self.assertEqual(snapshot['cases'][0]['title'], old['cases'][0]['title'])
        self.assertEqual(before, {str(path.relative_to(self.old)): path.read_bytes()
                                 for path in self.old.rglob('*') if path.is_file()})

    def test_title_and_source_metadata_changes_do_not_change_input_identity(self):
        old = self.gallery(self.old)
        old['commit'] = 'f4752c2'  # Saved galleries also use abbreviated Git IDs.
        self.save(self.old, old)
        new = self.gallery(self.new)
        new['cases'][0].update(title='別の見出し', source_sha256='c' * 64)
        result = self.compare(new=new)
        self.assertEqual(result['cases'][0]['comparison']['images']['status'], 'unchanged')
        self.assertEqual(result['cases'][0]['comparison']['posts']['status'], 'unchanged')
        self.assertEqual(result['baseline']['commit'], 'f4752c2')

    def test_png_mode_and_reasons_changes_are_independently_identified(self):
        self.gallery(self.old)
        new = self.gallery(self.new, png=OTHER_PNG)
        for image in (new['cases'][0]['images']['preview'], new['cases'][0]['posts'][0]['images']['preview']):
            image.update(kind='summary', decision={'mode': 'summary', 'reasons': [reason()]})
        result = self.compare(new=new)
        for view in ('images', 'posts'):
            decision = result['cases'][0]['comparison'][view]
            self.assertEqual(decision['status'], 'changed')
            self.assertEqual(decision['changes'], ['image', 'mode', 'reasons'])

    def test_post_specific_attachment_changes_without_changing_the_case_image(self):
        old = self.gallery(self.old)
        new = self.gallery(self.new)
        third_png = PNG[:-12] + chunk(b'tEXt', b'comment\x00third image') + PNG[-12:]
        for root, manifest, png in ((self.old, old, OTHER_PNG), (self.new, new, third_png)):
            case = manifest['cases'][0]
            rows = [{'nmea': NMEA, 'received_ms': 1000}, {'nmea': NMEA, 'received_ms': 2000}]
            (root / case['records_path']).write_text(json.dumps(rows))
            case['record_count'] = 2
            post = deepcopy(case['posts'][0])
            post.update(record_indexes=[1], kind='single')
            image = post['images']['preview']
            image.update(path='images/post-two.png', sha256=hashlib.sha256(png).hexdigest())
            (root / image['path']).write_bytes(png)
            case['posts'].append(post)
        result = self.compare(old, new)
        comparison = result['cases'][0]['comparison']
        self.assertEqual(comparison['images']['status'], 'unchanged')
        self.assertEqual(comparison['posts']['status'], 'changed')
        self.assertEqual(comparison['posts']['changes'], ['image'])
        previous_image = comparison['previous']['posts'][1]['images']['preview']
        self.assertEqual((self.new / previous_image['path']).read_bytes(), OTHER_PNG)
        snapshot = load_baseline(self.new / 'baseline').manifest
        self.assertEqual(snapshot['cases'][0]['posts'][1]['images']['preview']['sha256'],
                         hashlib.sha256(OTHER_PNG).hexdigest())
        self.assertEqual(len(list((self.new / 'baseline' / 'images').iterdir())), 2)

    def test_reason_order_is_ignored_but_absent_reason_history_is_not_empty(self):
        old = self.gallery(self.old)
        new = self.gallery(self.new)
        old['cases'][0]['images']['preview']['decision']['reasons'] = [reason('no_area'), reason('cancelled')]
        new['cases'][0]['images']['preview']['decision']['reasons'] = [reason('cancelled'), reason('no_area')]
        del old['cases'][0]['posts'][0]['images']['preview']['decision']
        result = self.compare(old, new)
        comparison = result['cases'][0]['comparison']
        self.assertEqual(comparison['images']['status'], 'unchanged')
        self.assertEqual(comparison['posts']['status'], 'changed')
        self.assertIn('reasons', comparison['posts']['changes'])

    def test_record_order_timestamp_category_and_reference_determine_same_input(self):
        for change in ('timestamp', 'order', 'category', 'reference'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.root) as directory:
                self.old, self.new = Path(directory) / 'old', Path(directory) / 'new'
                old = self.gallery(self.old)
                new = self.gallery(self.new)
                if change == 'category':
                    new['cases'][0]['category'] = 'Weather'
                elif change == 'reference':
                    path = self.old / 'records' / 'reference.json'
                    path.write_bytes((self.old / old['cases'][0]['records_path']).read_bytes())
                    old['cases'][0]['cancel_reference_path'] = 'records/reference.json'
                else:
                    original = [{'nmea': NMEA, 'received_ms': 1000}, {'nmea': NMEA, 'received_ms': 2000}]
                    changed = list(reversed(original)) if change == 'order' else [*original[:1], {'nmea': NMEA, 'received_ms': 2001}]
                    for root, manifest, rows in ((self.old, old, original), (self.new, new, changed)):
                        (root / manifest['cases'][0]['records_path']).write_text(json.dumps(rows))
                        manifest['cases'][0]['record_count'] = 2
                result = self.compare(old, new)
                for view in ('images', 'posts'):
                    self.assertEqual(result['cases'][0]['comparison'][view]['status'], 'input_changed')

    def test_post_text_structure_and_scenario_changes_are_distinguished(self):
        for change, code, status in (('text', 'text', 'changed'), ('structure', 'structure', 'changed'),
                                     ('conditions', 'conditions', 'input_changed')):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.root) as directory:
                self.old, self.new = Path(directory) / 'old', Path(directory) / 'new'
                self.gallery(self.old)
                new = self.gallery(self.new)
                if change == 'text':
                    new['cases'][0]['posts'][0]['text'] += '\n本文を補足'
                elif change == 'structure':
                    new['cases'][0]['posts'][0]['kind'] = 'single'
                else:
                    new['post_preview']['collection_wait_seconds'] = 30
                result = self.compare(new=new)
                comparison = result['cases'][0]['comparison']
                self.assertEqual(comparison['images']['status'], 'unchanged')
                self.assertEqual(comparison['posts']['status'], status)
                self.assertIn(code, comparison['posts']['changes'])

    def test_old_manifests_without_posts_and_legal_error_or_missing_preview_are_unavailable(self):
        old = self.gallery(self.old, slugs=('legacy', 'error', 'absent'), posts=False)
        new = self.gallery(self.new, slugs=('legacy', 'error', 'absent'))
        old['cases'][1]['images']['preview'] = {'version': 'preview', 'error': 'ValueError: failed'}
        old['cases'][2]['images'] = {}
        result = self.compare(old, new)
        self.assertEqual(result['cases'][0]['comparison']['images']['status'], 'unchanged')
        self.assertEqual([case['comparison']['posts']['status'] for case in result['cases']], ['unavailable'] * 3)
        self.assertEqual([case['comparison']['images']['status'] for case in result['cases'][1:]], ['unavailable'] * 2)

    def test_missing_post_history_stays_unavailable_even_when_the_input_changed(self):
        self.gallery(self.old, posts=False)
        new = self.gallery(self.new)
        new['cases'][0]['category'] = 'Weather'
        comparison = self.compare(new=new)['cases'][0]['comparison']
        self.assertEqual(comparison['images']['status'], 'input_changed')
        self.assertEqual(comparison['posts']['status'], 'unavailable')

    def test_added_and_unmatched_cases_are_not_mislabelled_as_render_changes_or_deletions(self):
        self.gallery(self.old, slugs=('shared', 'older-only'))
        new = self.gallery(self.new, slugs=('shared', 'newer-only'))
        result = self.compare(new=new)
        self.assertEqual(result['cases'][1]['comparison']['images']['status'], 'added')
        self.assertIsNone(result['cases'][1]['comparison']['previous'])
        self.assertEqual(result['baseline']['unmatched_cases'], [{'slug': 'older-only', 'title': '台風の例'}])
        snapshot = load_baseline(self.new / 'baseline').manifest
        self.assertEqual([case['slug'] for case in snapshot['cases']], ['shared', 'older-only'])

    def test_snapshot_uses_validated_bytes_even_if_baseline_changes_after_loading(self):
        old = self.gallery(self.old)
        new = self.gallery(self.new)
        baseline = load_baseline(self.old)
        (self.old / old['cases'][0]['images']['preview']['path']).write_bytes(b'changed after validation')
        (self.old / 'manifest.json').write_text('{}')
        baseline.manifest['cases'].clear()  # A caller's copy cannot mutate the snapshot.
        result = attach_comparison(new, baseline, self.new)
        previous = result['cases'][0]['comparison']['previous']
        self.assertEqual((self.new / previous['images']['preview']['path']).read_bytes(), PNG)
        self.assertEqual(result['cases'][0]['comparison']['images']['status'], 'unchanged')

    def test_old_nested_comparisons_and_published_assets_are_not_recursively_copied(self):
        old = self.gallery(self.old)
        new = self.gallery(self.new)
        published = deepcopy(old['cases'][0]['images']['preview'])
        published['version'] = 'v4'
        published['path'] = 'images/published.png'
        (self.old / published['path']).write_bytes(OTHER_PNG)
        published['sha256'] = hashlib.sha256(OTHER_PNG).hexdigest()
        old['cases'][0]['images']['published'] = published
        old['cases'][0]['comparison'] = {'previous': {'records_path': '../../secret.json'}}
        old['baseline'] = {'manifest_path': '../../older.json'}
        result = self.compare(old, new)
        previous = result['cases'][0]['comparison']['previous']
        self.assertNotIn('comparison', previous)
        self.assertEqual(set(previous['images']), {'preview'})
        self.assertEqual(len(list((self.new / 'baseline' / 'images').iterdir())), 1)

    def test_missing_tampered_truncated_png_and_wrong_dimensions_fail_before_output(self):
        for change in ('missing', 'digest', 'dimensions', 'header', 'crc', 'truncated'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.root) as directory:
                old_path = Path(directory) / 'old'
                old = self.gallery(old_path)
                image = old['cases'][0]['images']['preview']
                path = old_path / image['path']
                if change == 'missing':
                    path.unlink()
                elif change == 'dimensions':
                    image['width'] = 2
                elif change == 'digest':
                    path.write_bytes(OTHER_PNG)
                else:
                    data = b'not a PNG' if change == 'header' else PNG[:-2] if change == 'truncated' else PNG[:29] + b'xxxx' + PNG[33:]
                    path.write_bytes(data)
                    image['sha256'] = hashlib.sha256(data).hexdigest()
                self.save(old_path, old)
                with self.assertRaises(ValueError):
                    load_baseline(old_path)
                self.assertFalse((Path(directory) / 'output').exists())

    def test_unsafe_paths_symlinked_children_and_arbitrary_file_kinds_are_rejected(self):
        for path in ('../secret.json', '/tmp/secret.json', 'https://example.com/records.json',
                     'records/../sample.json', 'records//sample.json', 'records\\sample.json',
                     'records/%2e%2e/secret.json', 'manifest.json'):
            with self.subTest(path=path), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory) / 'old'
                old = self.gallery(root)
                old['cases'][0]['records_path'] = path
                self.save(root, old)
                with self.assertRaises(ValueError):
                    load_baseline(root)
        old = self.gallery(self.old)
        path = self.old / 'images' / 'sample.png'
        outside = self.root / 'outside.png'
        outside.write_bytes(PNG)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'シンボリックリンク'):
            load_baseline(self.old)
        path.unlink()
        path.write_bytes(PNG)
        old['source_license_path'] = 'secret.txt'
        (self.old / 'secret.txt').write_text('not a license')
        self.save(self.old, old)
        with self.assertRaises(ValueError):
            load_baseline(self.old)

    def test_malformed_schema_duplicate_slugs_records_and_posts_are_rejected(self):
        for change in ('schema', 'duplicate', 'records', 'record_count', 'post_indexes', 'text_image', 'reasons'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory) / 'old'
                old = self.gallery(root)
                case = old['cases'][0]
                if change == 'schema':
                    old['schema_version'] = True
                elif change == 'duplicate':
                    old['cases'].append(deepcopy(case))
                elif change == 'records':
                    (root / case['records_path']).write_text('[{"nmea":"secret", "received_ms":1}]')
                elif change == 'record_count':
                    case['record_count'] = 2
                elif change == 'post_indexes':
                    case['posts'][0]['record_indexes'] = [False]
                elif change == 'text_image':
                    case['posts'][0]['image_enabled'] = False
                else:
                    case['images']['preview']['decision']['reasons'] = None
                self.save(root, old)
                with self.assertRaises(ValueError):
                    load_baseline(root)

    def test_no_repository_network_or_rendering_dependency_and_existing_copy_is_preserved(self):
        self.gallery(self.old)
        new = self.gallery(self.new)
        with patch('socket.socket', side_effect=AssertionError('No network')), \
                patch('subprocess.run', side_effect=AssertionError('No Git or subprocess')):
            baseline = load_baseline(self.old)
            attach_comparison(new, baseline, self.new)
            marker = self.new / 'baseline' / 'keep'
            marker.write_text('existing')
            with self.assertRaisesRegex(ValueError, '上書き'):
                attach_comparison(new, baseline, self.new)
            self.assertEqual(marker.read_text(), 'existing')

    def test_identical_recorded_alt_is_preserved_in_the_copied_snapshot(self):
        old = self.gallery(self.old)
        new = self.gallery(self.new)
        description = ' 【訓練】台風第5号の進路。\n南の海上から北へ進む。\n'
        for manifest in (old, new):
            case = manifest['cases'][0]
            case['images']['preview']['alt'] = description
            case['posts'][0]['images']['preview']['alt'] = description + '予報範囲を表示。'
        result = self.compare(old, new)
        comparison = result['cases'][0]['comparison']
        for mode in ('images', 'posts'):
            self.assertEqual(comparison[mode]['status'], 'unchanged')
            self.assertEqual(comparison[mode]['changes'], [])
        previous = comparison['previous']
        self.assertEqual(previous['images']['preview']['alt'], description)
        self.assertEqual(previous['posts'][0]['images']['preview']['alt'], description + '予報範囲を表示。')
        copied = load_baseline(self.new / 'baseline').manifest['cases'][0]
        self.assertEqual(copied['images']['preview']['alt'], description)
        self.assertEqual(copied['posts'][0]['images']['preview']['alt'], description + '予報範囲を表示。')

    def test_alt_only_changes_are_independent_for_case_images_and_post_attachments(self):
        for changed in ('images', 'posts'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory(dir=self.root) as directory:
                self.old, self.new = Path(directory) / 'old', Path(directory) / 'new'
                old, new = self.gallery(self.old), self.gallery(self.new)
                for manifest in (old, new):
                    case = manifest['cases'][0]
                    for owner in (case, case['posts'][0]):
                        owner['images']['preview']['alt'] = '以前の画像の説明'
                case = new['cases'][0]
                owner = case if changed == 'images' else case['posts'][0]
                owner['images']['preview']['alt'] = '具体的に書き直した画像の説明'
                comparison = self.compare(old, new)['cases'][0]['comparison']
                self.assertEqual(comparison[changed]['status'], 'changed')
                self.assertEqual(comparison[changed]['changes'], ['alt'])
                other = 'posts' if changed == 'images' else 'images'
                self.assertEqual(comparison[other]['status'], 'unchanged')
                self.assertEqual(new['cases'][0]['images']['preview']['sha256'],
                                 comparison['previous']['images']['preview']['sha256'])

    def test_alt_recording_changes_preserve_png_comparability_in_both_directions(self):
        for recorded in ('old', 'new'):
            with self.subTest(recorded=recorded), tempfile.TemporaryDirectory(dir=self.root) as directory:
                self.old, self.new = Path(directory) / 'old', Path(directory) / 'new'
                old, new = self.gallery(self.old), self.gallery(self.new)
                case = (old if recorded == 'old' else new)['cases'][0]
                for owner in (case, case['posts'][0]):
                    owner['images']['preview']['alt'] = '記録された画像の説明'
                result = self.compare(old, new)
                comparison = result['cases'][0]['comparison']
                for mode in ('images', 'posts'):
                    self.assertEqual(comparison[mode]['status'], 'changed')
                    self.assertEqual(comparison[mode]['changes'], ['alt_recording'])
                    self.assertEqual(result['baseline']['counts'][mode]['unavailable'], 0)
                previous = comparison['previous']
                self.assertEqual('alt' in previous['images']['preview'], recorded == 'old')
                self.assertEqual('alt' in previous['posts'][0]['images']['preview'], recorded == 'old')
                self.assertEqual((self.new / previous['images']['preview']['path']).read_bytes(), PNG)

    def test_a_render_error_can_retain_known_alt_changes_but_stays_unavailable(self):
        old, new = self.gallery(self.old), self.gallery(self.new)
        for manifest, description in ((old, '以前の説明'), (new, '今回の説明')):
            case = manifest['cases'][0]
            for owner in (case, case['posts'][0]):
                owner['images']['preview']['alt'] = description
        for owner in (old['cases'][0], old['cases'][0]['posts'][0]):
            owner['images']['preview'] = {'version': 'preview', 'error': 'Rendering failed', 'alt': '以前の説明'}
        comparison = self.compare(old, new)['cases'][0]['comparison']
        for mode in ('images', 'posts'):
            self.assertEqual(comparison[mode]['status'], 'unavailable')
            self.assertEqual(comparison[mode]['changes'], ['alt'])

    def test_invalid_optional_alt_is_rejected_for_old_and_new_image_cells_before_copying(self):
        def target(manifest, place):
            case = manifest['cases'][0]
            if place == 'post':
                return case['posts'][0]['images']['preview']
            if place == 'published':
                case['images']['published'] = {**case['images']['preview'], 'version': 'v4'}
                return case['images']['published']
            if place == 'error':
                case['images']['preview'] = {'version': 'preview', 'error': 'Rendering failed'}
            return case['images']['preview']
        for invalid in ('', ' \n\t', None, 0, False, [], {}):
            for place in ('case', 'post', 'published', 'error'):
                with self.subTest(invalid=invalid, place=place), tempfile.TemporaryDirectory(dir=self.root) as directory:
                    old_path, new_path = Path(directory) / 'old', Path(directory) / 'new'
                    old, new = self.gallery(old_path), self.gallery(new_path)
                    target(old, place)['alt'] = invalid
                    self.save(old_path, old)
                    with self.assertRaisesRegex(ValueError, 'ALT'):
                        load_baseline(old_path)
                    target(old, place)['alt'] = '有効な画像の説明'
                    self.save(old_path, old)
                    baseline = load_baseline(old_path)
                    target(new, place)['alt'] = invalid
                    with self.assertRaisesRegex(ValueError, 'ALT'):
                        attach_comparison(new, baseline, new_path)
                    self.assertFalse((new_path / 'baseline').exists())


if __name__ == '__main__':
    unittest.main()
