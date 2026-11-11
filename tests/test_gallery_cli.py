"""Offline gallery generation preserves inputs and makes incomplete runs visible."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import render_gallery
from scripts.gallery_catalog import load_cases


ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII=')
HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('matplotlib', 'numpy'))


class GalleryCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-gallery-tests-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / 'gallery'
        self.case = next(case for case in load_cases() if case.slug == 'marine')

    def test_list_runs_without_render_dependencies_config_or_network(self):
        # Exercise the actual script entrypoint in a fresh interpreter. Catalog
        # discovery should remain usable in the lightweight receiver environment.
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
            [sys.executable, '-B', '-c', command, str(ROOT / 'scripts/render_gallery.py')],
            cwd=self.root, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('marine', result.stdout)
        self.assertIn('typhoon-track', result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_files_directories_and_symlinks_are_never_overwritten(self):
        file = self.root / 'file'
        file.write_bytes(b'preserve file')
        directory = self.root / 'existing'
        directory.mkdir()
        (directory / 'marker').write_bytes(b'preserve directory')
        symlink = self.root / 'linked'
        symlink.symlink_to(directory, target_is_directory=True)
        dangling = self.root / 'dangling'
        dangling.symlink_to(self.root / 'absent')
        for destination in (file, directory, symlink, dangling):
            with self.subTest(destination=destination.name), \
                    patch.object(render_gallery, '_preflight') as preflight, \
                    patch.object(render_gallery, '_render_with_decision') as render:
                with self.assertRaises((FileExistsError, ValueError)):
                    render_gallery.generate(destination, selected_cases=('marine',))
                render.assert_not_called()
                preflight.assert_not_called()
        self.assertEqual(file.read_bytes(), b'preserve file')
        self.assertEqual((directory / 'marker').read_bytes(), b'preserve directory')
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ['marker'])
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(dangling.is_symlink())

    def test_invalid_case_or_version_fails_before_output_and_asset_loading(self):
        for options in ({'selected_cases': ('no-such-case',)},
                        {'published_version': 'preview'},
                        {'published_version': 'v5'}):
            with self.subTest(options=options), \
                    patch.object(render_gallery, '_preflight') as preflight, \
                    patch.object(render_gallery, '_render_with_decision') as render:
                with self.assertRaises(ValueError):
                    render_gallery.generate(self.output, **options)
                preflight.assert_not_called()
                render.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_missing_assets_leave_no_partial_gallery(self):
        with patch.object(render_gallery, 'ROOT', self.root), \
                patch.object(render_gallery, '_render_with_decision') as render:
            with self.assertRaisesRegex(FileNotFoundError, 'prepare_flood_assets.py'):
                render_gallery.generate(self.output, selected_cases=('marine',))
        render.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_generation_links_original_records_and_hashes_actual_png_bytes(self):
        with patch.object(render_gallery, '_preflight') as preflight, \
                patch.object(render_gallery, '_render_with_decision', return_value=(PNG, 'map', None)) as render, \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, published_version='v3',
                                             selected_cases=('marine',))
        preflight.assert_called_once_with('v3', preview_only=False)
        self.assertEqual(result['published_version'], 'v3')
        self.assertEqual(result['error_count'], 0)
        self.assertEqual((self.output / result['source_license_path']).read_bytes(),
                         (render_gallery.ROOT / 'tests/fixtures/qzqsm/LICENSE.azarashi').read_bytes())
        self.assertEqual(len(result['cases']), 1)
        row, = result['cases']
        self.assertEqual(row['slug'], self.case.slug)
        self.assertEqual(row['source_sha256'], self.case.source_sha256)
        self.assertNotIn('post_text', row)
        records = json.loads((self.output / row['records_path']).read_text(encoding='utf-8'))
        self.assertEqual([(r['nmea'], r['received_ms']) for r in records], list(self.case.records))
        self.assertEqual([(tuple(call.args[0]), call.args[1]) for call in render.call_args_list],
                         [(self.case.records, 'v3'), (self.case.records, 'preview')])
        for slot, version in (('published', 'v3'), ('preview', 'preview')):
            image = row['images'][slot]
            self.assertEqual((self.output / image['path']).read_bytes(), PNG)
            self.assertEqual(image['sha256'], hashlib.sha256(PNG).hexdigest())
            self.assertEqual((image['width'], image['height']), (1, 1))
            self.assertEqual(image['version'], version)
            self.assertEqual(image['kind'], 'map')
        self.assertEqual(json.loads((self.output / 'manifest.json').read_text(encoding='utf-8')),
                         result)
        html = (self.output / 'index.html').read_text(encoding='utf-8')
        for path in (row['records_path'], *(item['path'] for item in row['images'].values())):
            self.assertIn(path, html)

    def test_only_single_evacuation_destinations_include_the_actual_post_with_nmea(self):
        import azarashi
        from message_format import report_message

        selected = ('dcx-secondary-evacuation', 'dcx-evacuation-overlap',
                    'dcx-mtinfo-leave', 'dcx-mtinfo-head', 'dcx-mtinfo-area',
                    'dcx-secondary-ellipse', 'dcx-mtinfo', 'typhoon-track', 'marine')
        cases = {case.slug: case for case in load_cases()}
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision', return_value=(PNG, 'map', None)), \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, selected_cases=selected, preview_only=True)
        eligible = {'dcx-secondary-evacuation', 'dcx-evacuation-overlap',
                    'dcx-mtinfo-leave', 'dcx-mtinfo-head', 'dcx-mtinfo-area'}
        self.assertEqual({row['slug'] for row in result['cases'] if 'post_text' in row}, eligible)
        for row in result['cases']:
            case = cases[row['slug']]
            self.assertEqual(row['report_text'], render_gallery._decoded_text(case.records))
            if case.slug in eligible:
                (nmea, milliseconds), = case.records
                report = azarashi.decode(nmea, timestamp=datetime.fromtimestamp(
                    milliseconds / 1000, timezone.utc))
                self.assertEqual(row['post_text'], report_message(report))
                self.assertTrue(row['post_text'].endswith('\n---\nNMEA: ' + nmea))
        saved = json.loads((self.output / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(saved, result)

    def test_grouped_evacuation_records_are_not_presented_as_a_single_post(self):
        cases = {case.slug: case for case in load_cases()}
        grouped = (cases['dcx-secondary-evacuation'].records
                   + cases['dcx-evacuation-overlap'].records)
        with patch('azarashi.decode') as decode:
            self.assertIsNone(render_gallery._post_text(grouped))
        decode.assert_not_called()

    def test_failed_render_keeps_other_output_and_returns_failure_status(self):
        for failed_version, failed_slot, good_slot in (
                ('v4', 'published', 'preview'), ('preview', 'preview', 'published')):
            with self.subTest(failed_version=failed_version):
                output = self.root / failed_version

                def render(records, version):
                    if version == failed_version:
                        raise ValueError('fixture cannot be rendered')
                    return PNG, 'summary', None

                with patch.object(render_gallery, '_preflight'), \
                        patch.object(render_gallery, '_render_with_decision', side_effect=render), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    status = render_gallery.main(['--output', str(output), '--case', 'marine'])
                self.assertEqual(status, 1)
                result = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(result['error_count'], 1)
                row, = result['cases']
                self.assertEqual((output / row['images'][good_slot]['path']).read_bytes(), PNG)
                self.assertIn('fixture cannot be rendered', row['images'][failed_slot]['error'])
                self.assertNotIn('path', row['images'][failed_slot])
                html = (output / 'index.html').read_text(encoding='utf-8')
                self.assertIn('fixture cannot be rendered', html)
                self.assertTrue((output / row['records_path']).is_file())

    def test_topic_png_is_recorded_separately_without_changing_published_comparison_or_text(self):
        case = next(case for case in load_cases() if case.slug == 'dcx-jalert')
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision',
                             side_effect=lambda records, version: (PNG, 'topic' if version == 'preview' else 'summary', None)), \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, selected_cases=(case.slug,))
        self.assertEqual(result['error_count'], 0)
        row, = result['cases']
        self.assertEqual(row['images']['preview']['kind'], 'topic')
        self.assertEqual(row['images']['published']['kind'], 'summary')
        self.assertEqual(row['images']['published']['version'], 'v4')
        self.assertEqual(row['report_text'], render_gallery._decoded_text(case.records))
        self.assertEqual(row['source_sha256'], case.source_sha256)
        for image in row['images'].values():
            self.assertEqual((self.output / image['path']).read_bytes(), PNG)
            self.assertEqual(image['sha256'], hashlib.sha256(PNG).hexdigest())
        self.assertIn('トピック', (self.output / 'index.html').read_text())

    def test_unknown_png_kind_remains_a_visible_failure(self):
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision',
                             side_effect=lambda records, version: (PNG, 'unknown' if version == 'preview' else 'summary', None)), \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, selected_cases=('dcx-jalert',))
        self.assertEqual(result['error_count'], 1)
        row, = result['cases']
        self.assertIn('不正な描画結果', row['images']['preview']['error'])
        self.assertNotIn('path', row['images']['preview'])
        self.assertEqual(row['images']['published']['kind'], 'summary')

    def test_successful_fallback_records_reasons_without_counting_as_error(self):
        decision = {'mode': 'topic', 'reasons': [{
            'code': 'unsupported_version', 'stage': 'selection',
            'message': '未対応の通報バージョン', 'detail': '国内DCX Vn=0'}]}
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision',
                             return_value=(PNG, 'topic', decision)), \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, selected_cases=('dcx-jalert',))
        self.assertEqual(result['error_count'], 0)
        row, = result['cases']
        self.assertEqual(row['images']['preview']['decision'], decision)
        self.assertNotIn('decision', row['images']['published'])
        saved = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(saved['cases'][0]['images']['preview']['decision'], decision)
        self.assertIn('未対応の通報バージョン', (self.output / 'index.html').read_text())

    def test_preview_only_cli_renders_one_image_and_records_no_published_version(self):
        decision = {'mode': 'map', 'reasons': []}
        with patch.object(render_gallery, '_preflight') as preflight, \
                patch.object(render_gallery, '_render_with_decision',
                             return_value=(PNG, 'map', decision)) as render, \
                redirect_stdout(io.StringIO()):
            status = render_gallery.main(['--output', str(self.output), '--preview-only',
                                          '--case', 'marine'])
        self.assertEqual(status, 0)
        preflight.assert_called_once_with('v4', preview_only=True)
        render.assert_called_once_with(self.case.records, 'preview')
        result = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(result['renderer'], 'preview')
        self.assertNotIn('published_version', result)
        row, = result['cases']
        self.assertEqual(set(row['images']), {'preview'})
        self.assertEqual(row['images']['preview']['path'], 'images/marine.png')
        self.assertEqual(row['images']['preview']['decision'], decision)
        self.assertEqual(len(list((self.output / 'images').iterdir())), 1)
        self.assertNotIn('公開版', (self.output / 'index.html').read_text())


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for actual gallery rendering')
class GalleryRenderingTests(unittest.TestCase):
    def test_observation_identifies_explicit_topic_before_axes_and_restores_savefig(self):
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from image_service import render as base

        original_savefig = base.Figure.savefig
        for marker, axes, expected in ((None, False, 'summary'), (None, True, 'map'),
                                       ('dcx-topic', False, 'topic'), ('dcx-topic', True, 'topic'),
                                       ('other-topic', False, 'summary')):
            with self.subTest(marker=marker, axes=axes):
                def render(records):
                    figure = base.Figure(figsize=(.5, .5), dpi=96)
                    FigureCanvasAgg(figure)
                    if axes:
                        figure.add_axes((0, 0, 1, 1)).set_axis_off()
                    if marker:
                        figure.text(.5, .5, 'topic', gid=marker)
                    output = io.BytesIO()
                    figure.savefig(output, format='png')
                    figure.clear()
                    return SimpleNamespace(png=output.getvalue())

                with patch.dict(render_gallery.MODULES, {'preview': 'gallery_test_renderer'}), \
                        patch.dict(sys.modules, {'gallery_test_renderer': SimpleNamespace(render=render)}):
                    png, kind = render_gallery._render_image((), 'preview')
                self.assertEqual(kind, expected)
                self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
                self.assertIs(base.Figure.savefig, original_savefig)

    def test_actual_pngs_preserve_the_published_renderer_and_restore_observation(self):
        from image_service import render as base
        from image_service import render_v4

        case = next(case for case in load_cases() if case.slug == 'marine')
        original_savefig = base.Figure.savefig
        with tempfile.TemporaryDirectory(prefix='shirasu-gallery-render-') as directory, \
                redirect_stdout(io.StringIO()):
            output = Path(directory) / 'gallery'
            manifest = render_gallery.generate(output, selected_cases=('marine',))
            self.assertIs(base.Figure.savefig, original_savefig)
            self.assertEqual(manifest['error_count'], 0)
            row, = manifest['cases']
            for slot, image in row['images'].items():
                expected = (1920, 1080) if slot == 'preview' else (1440, 1200)
                self.assertEqual((image['width'], image['height']), expected)
                self.assertEqual(image['kind'], 'map')
                self.assertTrue((output / image['path']).is_file())
                if slot == 'preview':
                    self.assertEqual(image['decision'], {'mode': 'map', 'reasons': []})
                else:
                    self.assertNotIn('decision', image)
            published_png = (output / row['images']['published']['path']).read_bytes()
            self.assertEqual(published_png, render_v4.render(case.records).png)
            self.assertIs(base.Figure.savefig, original_savefig)


if __name__ == '__main__':
    unittest.main()
