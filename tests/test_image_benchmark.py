"""Benchmark inputs and serial worker evidence stay faithful and isolated."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import benchmark_images
from scripts.gallery_catalog import load_cases
from scripts.gallery_posts import plan_posts


ROOT = Path(__file__).resolve().parents[1]


class ImageBenchmarkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='shirasu-benchmark-tests-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / 'run'

    def test_unique_workloads_keep_every_gallery_and_actual_post_input(self):
        items = benchmark_images.workloads()
        self.assertEqual(len(items), 49)
        self.assertEqual(len({item['id'] for item in items}), 49)
        self.assertEqual(len({(item['records'], item['cancel_reference']) for item in items}), 49)
        cases = {case.slug: case for case in load_cases()}
        usage = {}
        for item in items:
            self.assertEqual(item['record_count'], len(item['records']))
            for use in item['usages']:
                key = use['case_slug'], use['role'], use.get('post_number')
                self.assertNotIn(key, usage)
                usage[key] = item
        self.assertEqual(sum(key[1] == 'case' for key in usage), 40)
        self.assertEqual(sum(key[1] == 'post' for key in usage), 46)
        for slug, case in cases.items():
            with self.subTest(case=slug):
                posts = plan_posts(case.records, nankai_topics=True)
                raw = usage[slug, 'case', None]
                indexes = (posts[0].image_record_indexes
                           if case.category == 'NankaiTroughEarthquake'
                           else tuple(range(len(case.records))))
                self.assertEqual(raw['records'], tuple(case.records[index] for index in indexes))
                self.assertEqual(raw['cancel_reference'], case.cancel_reference)
                if case.category == 'NankaiTroughEarthquake':
                    self.assertEqual(len(raw['records']), 1)
                    self.assertEqual(raw['records'][0], case.records[0])
                for number, post in enumerate(posts, 1):
                    item = usage[slug, 'post', number]
                    self.assertEqual(item['records'], tuple(case.records[index]
                                                           for index in post.image_record_indexes))
                    self.assertEqual(item['cancel_reference'], ())
                if case.cancel_reference:
                    self.assertIsNot(raw, usage[slug, 'post', 1])

    def test_selection_and_invalid_configuration_fail_before_writing(self):
        self.assertEqual([item['id'] for item in benchmark_images.workloads(('marine', 'marine'))],
                         ['marine'])
        for options in ({'selected_cases': ('unknown-case',)}, {'selected_cases': ()}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                benchmark_images.generate(self.output, **options)
            self.assertFalse(self.output.exists())
        for name, values in (
                ('warm_repeats', (0, 11, True, 1.5)),
                ('encoding_repeats', (0, 11, False, 1.5)),
                ('timeout', (0, 3601, True, 1.5))):
            for value in values:
                with self.subTest(name=name, value=value), \
                        patch.object(benchmark_images, 'workloads') as workloads, \
                        self.assertRaises(ValueError):
                    benchmark_images.generate(self.output, **{name: value})
                workloads.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_existing_output_is_never_replaced_including_dangling_links(self):
        original = self.root / 'original'
        original.write_text('preserve this')
        directory = self.root / 'existing-directory'
        directory.mkdir()
        (directory / 'sentinel').write_text('preserve directory')
        link = self.root / 'link'
        link.symlink_to(directory)
        dangling = self.root / 'dangling'
        dangling.symlink_to(self.root / 'not-created')
        for output in (original, directory, link, dangling):
            with self.subTest(output=output.name), \
                    patch.object(benchmark_images, 'workloads') as workloads, \
                    self.assertRaises(FileExistsError):
                benchmark_images.generate(output)
            workloads.assert_not_called()
        self.assertEqual(original.read_text(), 'preserve this')
        self.assertEqual((directory / 'sentinel').read_text(), 'preserve directory')
        self.assertFalse((self.root / 'not-created').exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(dangling.is_symlink())

    def test_failed_workers_and_timeouts_checkpoint_then_continue_and_fail_cli(self):
        calls, caches = [], []
        fake_html = SimpleNamespace(render_html=lambda manifest: '<html>benchmark evidence</html>')

        def run(command, **kwargs):
            number = len(calls)
            calls.append(command)
            caches.append(kwargs['env']['MPLCONFIGDIR'])
            self.assertTrue(Path(caches[-1]).is_dir())
            self.assertEqual(kwargs['timeout'], 7)
            self.assertTrue(kwargs['check'])
            self.assertEqual(command[command.index('--warm-repeats') + 1], '2')
            self.assertEqual(command[command.index('--encoding-repeats') + 1], '1')
            if number:
                checkpoint = json.loads((self.output / 'manifest.json').read_text())
                self.assertEqual(len(checkpoint['results']), number)
                self.assertEqual(checkpoint['error_count'], number)
            if number == 0:
                raise subprocess.CalledProcessError(2, command, stderr='synthetic worker failure')
            if number == 1:
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            target = Path(command[command.index('--output') + 1])
            target.mkdir()
            (target / 'original.png').write_bytes(b'placeholder artifact')
            result = {
                'environment': {'python': 'test-only', 'machine': 'test-machine'},
                'stable_png': True,
                'formats': [{'id': 'png', 'path': 'original.png', 'bytes': 20}],
            }
            (target / 'worker.json').write_text(json.dumps(result))
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {'scripts.benchmark_html': fake_html}), \
                patch('scripts.render_gallery._source_info', return_value={}), \
                patch.object(benchmark_images.subprocess, 'run', side_effect=run), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = benchmark_images.main([
                '--output', str(self.output), '--warm-repeats', '2', '--encoding-repeats', '1',
                '--timeout', '7', '--case', 'nankai-advisory', '--case', 'nankai-ended',
                '--case', 'nankai-cancel',
            ])
        self.assertEqual(status, 1)
        result = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(set(caches)), 3)
        self.assertTrue(all(not Path(cache).exists() for cache in caches))
        self.assertEqual(result['error_count'], 2)
        first, second, third = result['results']
        self.assertIn('CalledProcessError', first['error'])
        self.assertIn('synthetic worker failure', first['error'])
        self.assertIn('TimeoutExpired', second['error'])
        self.assertNotIn('error', third)
        self.assertEqual(third['formats'][0]['path'], 'images/nankai-cancel/original.png')
        self.assertEqual(result['environment'], {'python': 'test-only', 'machine': 'test-machine'})
        self.assertEqual((self.output / 'index.html').read_text(), '<html>benchmark evidence</html>')
        self.assertEqual(json.loads(stdout.getvalue().splitlines()[-1])['errors'], 2)
        self.assertEqual(stderr.getvalue(), '')
        for row in result['results']:
            source = json.loads((self.output / row['records_path']).read_text())
            self.assertEqual(source['id'], row['id'])
            self.assertEqual(len(source['records']), 1)
        self.assertNotIn('MPLCONFIGDIR', json.dumps(result))
        self.assertEqual((self.output / 'LICENSE.azarashi').read_bytes(),
                         (ROOT / 'tests/fixtures/qzqsm/LICENSE.azarashi').read_bytes())

    def test_codec_failure_preserves_successful_worker_evidence_and_fails_cli(self):
        fake_html = SimpleNamespace(render_html=lambda manifest: '<html>codec evidence</html>')

        def run(command, **kwargs):
            target = Path(command[command.index('--output') + 1])
            target.mkdir()
            (target / 'png-original.png').write_bytes(b'placeholder artifact')
            result = {
                'environment': {'python': 'test-only'}, 'stable_png': True,
                'formats': [
                    {'id': 'png-original', 'path': 'png-original.png', 'bytes': 20},
                    {'id': 'webp-lossless', 'path': None, 'error': 'OSError: synthetic codec failure'},
                ],
            }
            (target / 'worker.json').write_text(json.dumps(result))
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

        stdout = io.StringIO()
        with patch.dict(sys.modules, {'scripts.benchmark_html': fake_html}), \
                patch('scripts.render_gallery._source_info', return_value={}), \
                patch.object(benchmark_images.subprocess, 'run', side_effect=run), \
                redirect_stdout(stdout):
            status = benchmark_images.main(['--output', str(self.output), '--case', 'marine'])
        self.assertEqual(status, 1)
        result = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(result['error_count'], 0)
        self.assertEqual(result['codec_error_count'], 1)
        row, = result['results']
        self.assertNotIn('error', row)
        self.assertTrue(row['stable_png'])
        original, failed = row['formats']
        self.assertEqual(original['path'], 'images/marine/png-original.png')
        self.assertEqual((self.output / original['path']).read_bytes(), b'placeholder artifact')
        self.assertEqual(failed, {'id': 'webp-lossless', 'path': None,
                                  'error': 'OSError: synthetic codec failure'})
        summary = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual((summary['errors'], summary['codec_errors']), (0, 1))
        self.assertEqual((self.output / 'index.html').read_text(), '<html>codec evidence</html>')

    def test_list_is_a_lightweight_offline_entrypoint(self):
        command = '''
import builtins
import runpy
import socket
import sys
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'config' or name.startswith(('nostr_sdk', 'matplotlib', 'numpy', 'PIL',
                                           'image_service.render')):
        raise AssertionError('Listing imported runtime or renderer: ' + name)
    return original_import(name, *args, **kwargs)
def no_network(*args, **kwargs):
    raise AssertionError('Listing attempted a network connection')
builtins.__import__ = guarded_import
socket.create_connection = no_network
socket.socket.connect = no_network
sys.argv = [sys.argv[1], '--list']
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        result = subprocess.run(
            [sys.executable, '-B', '-c', command, str(ROOT / 'scripts/benchmark_images.py')],
            cwd=self.root, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 49)
        self.assertIn('typhoon-cancel-reference', result.stdout)
        self.assertIn('nankai-advisory', result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
