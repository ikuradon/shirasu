"""Offline timeline output preserves real posting units and review evidence."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from image_alt import image_alt_text
from image_publication import with_image
from scripts import render_gallery, render_timeline
from scripts.timeline_catalog import load_scenarios
from scripts.timeline_html import render_html
from test_gallery_html import Document, PostContents


ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII=')


def posts(manifest):
    return [(case, step, post) for case in manifest['scenarios']
            for step in case['steps'] for post in step['posts']]


class TimelineCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='shirasu-timeline-tests-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / 'timeline'

    def generate(self, **options):
        with patch.object(render_timeline, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision',
                             return_value=(PNG, 'topic', None)) as render:
            result = render_timeline.generate(self.output, **options)
        return result, render

    def test_all_scenarios_save_timed_posts_original_inputs_and_only_actual_attachments(self):
        result, render = self.generate()
        self.assertEqual(result['error_count'], 0)
        self.assertEqual(result['kind'], 'reception-timeline')
        self.assertEqual(len(result['scenarios']), 4)
        self.assertEqual(len(posts(result)), 6)
        self.assertEqual(render.call_count, 5)
        expected_times = {
            'nankai-late': [180, 270], 'nankai-correction': [100],
            'nankai-cancellation': [60], 'typhoon-additional': [180, 420],
        }
        catalog = {case.slug: case for case in load_scenarios()}
        expected_render_inputs = []
        for case in result['scenarios']:
            original = catalog[case['slug']]
            self.assertEqual([step['elapsed_seconds'] for step in case['steps']
                              for _ in step['posts']], expected_times[case['slug']])
            self.assertEqual(tuple(map(tuple, case['records'])), original.records)
            saved = json.loads((self.output / case['records_path']).read_text())
            self.assertEqual(tuple(map(tuple, saved['records'])), original.records)
            self.assertEqual(saved['arrival_seconds'], list(original.arrival_seconds))
            self.assertEqual(saved['until_seconds'], original.until_seconds)
        for case, step, post in posts(result):
            records = catalog[case['slug']].records
            self.assertEqual(post['text'].partition('\n---\n')[2].splitlines(),
                             ['NMEA: ' + records[index][0] for index in post['record_indexes']])
            self.assertEqual(post['record_count'], len(post['record_indexes']))
            self.assertEqual(bool(post['images']), post['image_enabled'])
            if post['image_enabled']:
                entries = tuple(records[index] for index in post['image_record_indexes'])
                expected_render_inputs.append((entries, 'preview'))
                image = post['images']['preview']
                self.assertEqual(image['alt'], image_alt_text(entries))
                self.assertEqual(image['sha256'], hashlib.sha256(PNG).hexdigest())
                self.assertEqual((self.output / image['path']).read_bytes(), PNG)
            else:
                self.assertEqual((case['slug'], step['elapsed_seconds']), ('nankai-late', 180))
                self.assertIn('【本文未受信】', post['text'])
                self.assertEqual(post['image_record_indexes'], [])
        self.assertEqual([call.args for call in render.call_args_list], expected_render_inputs)
        self.assertEqual(len(list((self.output / 'images').iterdir())), 5)
        self.assertEqual(json.loads((self.output / 'manifest.json').read_text()), result)
        self.assertEqual((self.output / 'LICENSE.azarashi').read_bytes(),
                         (ROOT / 'tests/fixtures/qzqsm/LICENSE.azarashi').read_bytes())
        content = (self.output / 'index.html').read_text()
        self.assertEqual(PostContents(content).bodies,
                         [with_image(post['text'], PostContents.MEDIA)
                          if post['image_enabled'] else post['text'] for _, _, post in posts(result)])
        document = Document(content)
        self.assertEqual([attrs['alt'] for attrs in document.attrs('img')],
                         [post['images']['preview']['alt'] for _, _, post in posts(result)
                          if post['image_enabled']])
        # NMEA remains in ordinary post text; only whole posts can fold.
        self.assertEqual({attrs.get('class') for attrs in document.attrs('details')},
                         {'image-alt', 'source-details'})
        for case in result['scenarios']:
            self.assertIn(case['records_path'], content)

    def test_identical_attachment_inputs_share_a_png_across_scenarios(self):
        original = load_scenarios()[0]
        duplicate = replace(original, slug='same-input', title='同じ入力の別シナリオ')
        with patch.object(render_timeline, 'load_scenarios', return_value=(original, duplicate)):
            result, render = self.generate()
        self.assertEqual(len(posts(result)), 4)
        render.assert_called_once_with((original.records[0],), 'preview')
        images = [post['images']['preview'] for _, _, post in posts(result) if post['image_enabled']]
        self.assertEqual(images[0], images[1])
        self.assertEqual(len(list((self.output / 'images').iterdir())), 1)

    def test_failed_render_keeps_text_evidence_and_cli_returns_failure(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(render_timeline, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision',
                             side_effect=RuntimeError('example render failure')), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = render_timeline.main(['--output', str(self.output), '--scenario', 'nankai-late'])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout.getvalue())['errors'], 1)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(manifest['error_count'], 1)
        notice, completed = [post for _, _, post in posts(manifest)]
        self.assertEqual(notice['images'], {})
        self.assertEqual(completed['record_count'], 5)
        self.assertIn('example render failure', completed['images']['preview']['error'])
        content = (self.output / 'index.html').read_text()
        self.assertIn('画像の描画に1件失敗しました', content)
        self.assertEqual(PostContents(content).bodies,
                         [notice['text'], with_image(completed['text'], PostContents.MEDIA)])

    def test_existing_destinations_and_invalid_scenarios_fail_before_render_or_write(self):
        file = self.root / 'file'
        file.write_text('keep')
        directory = self.root / 'existing'
        directory.mkdir()
        symlink = self.root / 'link'
        symlink.symlink_to(directory, target_is_directory=True)
        dangling = self.root / 'dangling'
        dangling.symlink_to(self.root / 'missing')
        with patch.object(render_timeline, '_preflight') as preflight, \
                patch.object(render_gallery, '_render_with_decision') as render:
            for path in (file, directory, symlink, dangling):
                with self.subTest(path=path), self.assertRaises(FileExistsError):
                    render_timeline.generate(path)
            for selection in ([], ['missing'], ['nankai-late', 'missing']):
                with self.subTest(selection=selection), self.assertRaises(ValueError):
                    render_timeline.generate(self.output, selected_scenarios=selection)
            preflight.assert_not_called()
            render.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual(file.read_text(), 'keep')
        self.assertEqual(list(directory.iterdir()), [])
        self.assertTrue(symlink.is_symlink() and dangling.is_symlink())

    def test_html_escapes_scenario_text_post_content_and_alt(self):
        result, _ = self.generate(selected_scenarios=('nankai-late',))
        result = deepcopy(result)
        value = '<script>alert("sample")</script> & <img src=x onerror="bad">'
        case = result['scenarios'][0]
        case['title'] = case['description'] = value
        case['receipt_labels'][0] = value
        case['sources'][0]['label'] = case['sources'][0]['transformation'] = value
        _, _, post = posts(result)[-1]
        post['text'] = value + '\n---\nNMEA: preserved & <frame>'
        post['images']['preview']['alt'] = value
        content = render_html(result)
        self.assertNotIn('<script>alert(', content)
        self.assertNotIn('<img src=x', content)
        document = Document(content)
        self.assertEqual(document.attrs('img')[0]['alt'], value)
        self.assertEqual(PostContents(content).bodies[-1], with_image(post['text'], PostContents.MEDIA))
        self.assertFalse(any('onerror' in attrs for _, attrs in document.elements))

    def test_list_runs_without_runtime_config_renderer_or_network(self):
        command = '''
import builtins
import runpy
import socket
import sys
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if (name == 'config' or name.startswith(('nostr_sdk', 'matplotlib', 'numpy',
                                            'image_service.render'))):
        raise AssertionError('Listing imported runtime/render configuration: ' + name)
    return original_import(name, *args, **kwargs)
def forbidden_network(*args, **kwargs):
    raise AssertionError('Listing attempted a network connection')
builtins.__import__ = guarded_import
socket.create_connection = forbidden_network
socket.socket.connect = forbidden_network
sys.argv = [sys.argv[1], '--list']
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        result = subprocess.run(
            [sys.executable, '-B', '-c', command, str(ROOT / 'scripts/render_timeline.py')],
            cwd=self.root, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line.split('\t')[0] for line in result.stdout.splitlines()],
                         [case.slug for case in load_scenarios()])
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
