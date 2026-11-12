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
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from tools.preview import render_gallery
from tools.preview.gallery_catalog import load_cases
from shirasu.images.alt import DEFAULT_IMAGE_ALT, image_alt_text


ROOT = Path(__file__).resolve().parents[2]
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII=')
HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('matplotlib', 'numpy'))


def alternate_png():
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00\xff'))
            + chunk(b'IEND', b''))


class GalleryCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-gallery-tests-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / 'gallery'
        self.case = next(case for case in load_cases() if case.slug == 'marine')

    def test_nankai_preview_uses_one_original_header_but_keeps_all_text_pages(self):
        for slug in ('nankai-advisory', 'nankai-ended', 'nankai-cancel'):
            with self.subTest(slug=slug):
                case = next(case for case in load_cases() if case.slug == slug)
                output = self.root / slug
                with patch.object(render_gallery, '_preflight'), \
                        patch.object(render_gallery, '_render_with_decision',
                                     return_value=(PNG, 'topic', {'mode': 'topic', 'reasons': []})) as render, \
                        redirect_stdout(io.StringIO()):
                    result = render_gallery.generate(output, selected_cases=(slug,), preview_only=True)
                row, = result['cases']
                post, = row['posts']
                self.assertEqual(post['kind'], 'nankai')
                self.assertEqual(post['record_count'], len(case.records))
                self.assertEqual(post['image_record_indexes'], [0])
                self.assertEqual(row['preview_record_indexes'], [0])
                self.assertEqual(post['images']['preview'], row['images']['preview'])
                for nmea, _ in case.records:
                    self.assertIn(nmea, post['text'])
                render.assert_called_once_with((case.records[0],), 'preview')
                self.assertIn('訓練・試験', post['images']['preview']['alt'])
                self.assertTrue(result['post_preview']['nankai_topic_images'])
                self.assertEqual(result['error_count'], 0)

    def test_nankai_published_post_remains_text_only_while_preview_has_a_topic(self):
        case = next(case for case in load_cases() if case.slug == 'nankai-advisory')
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision',
                             side_effect=lambda records, version: (
                                 PNG, 'topic' if version == 'preview' else 'summary', None)) as render, \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, selected_cases=(case.slug,))
        row, = result['cases']
        post, = row['posts']
        self.assertEqual(post['text_only_slots'], ['published'])
        self.assertEqual(set(post['images']), {'preview'})
        self.assertEqual(render.call_count, 2)
        self.assertEqual(render.call_args_list[0].args, (case.records, 'v4'))
        self.assertEqual(render.call_args_list[1].args, ((case.records[0],), 'preview'))
        html = (self.output / 'index.html').read_text()
        self.assertIn('公開版（本文のみ）', html)
        self.assertNotIn('添付画像の記録がありません', html)

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
            [sys.executable, '-B', '-c', command, str(ROOT / 'tools/preview/render_gallery.py')],
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
            with self.assertRaisesRegex(FileNotFoundError, 'prepare_vendor_assets.py'):
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
        post, = row['posts']
        self.assertEqual(post['record_indexes'], [0])
        self.assertEqual(post['kind'], 'single')
        self.assertEqual(post['images'], row['images'])
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
            self.assertEqual(image['alt'], image_alt_text(self.case.records))
        self.assertEqual(json.loads((self.output / 'manifest.json').read_text(encoding='utf-8')),
                         result)
        html = (self.output / 'index.html').read_text(encoding='utf-8')
        for path in (row['records_path'], *(item['path'] for item in row['images'].values())):
            self.assertIn(path, html)

    def test_alt_describes_each_actual_attachment_input_without_original_cancellation_values(self):
        selected = ('weather-many', 'weather-cancel-reference', 'typhoon-track')
        cases = {case.slug: case for case in load_cases()}
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision', return_value=(PNG, 'map', None)), \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, preview_only=True, selected_cases=selected)
        for row in result['cases']:
            records = cases[row['slug']].records
            self.assertEqual(row['images']['preview']['alt'], image_alt_text(records))
            for post in row['posts']:
                entries = tuple(records[index] for index in post['record_indexes'])
                if post['image_enabled']:
                    self.assertEqual(post['images']['preview']['alt'], image_alt_text(entries))
            if row['slug'] == 'weather-cancel-reference':
                self.assertIn('取消', row['images']['preview']['alt'])
                self.assertEqual(row['images']['preview']['alt'], row['posts'][0]['images']['preview']['alt'])

    def test_alt_failure_keeps_successful_png_and_records_the_same_fallback_as_live_posts(self):
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision', return_value=(PNG, 'map', None)), \
                patch.object(render_gallery, 'image_alt_text', side_effect=RuntimeError('unknown metadata')), \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, preview_only=True, selected_cases=('marine',))
        self.assertEqual(result['error_count'], 0)
        row, = result['cases']
        image = row['images']['preview']
        self.assertEqual(image['alt'], DEFAULT_IMAGE_ALT)
        self.assertEqual(row['posts'][0]['images']['preview']['alt'], DEFAULT_IMAGE_ALT)
        self.assertEqual((self.output / image['path']).read_bytes(), PNG)

    def test_confirmed_reference_is_saved_and_only_passed_to_unpublished_renderer(self):
        case = next(c for c in load_cases() if c.slug == 'weather-cancel-reference')
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision', return_value=(PNG, 'map', None)) as render, \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, selected_cases=(case.slug,))
        old, preview, posted = render.call_args_list
        self.assertEqual(old.kwargs, {})
        self.assertEqual(preview.kwargs, {'cancel_reference': case.cancel_reference})
        self.assertEqual(posted.args, (case.records, 'preview'))
        self.assertEqual(posted.kwargs, {})
        row, = result['cases']
        self.assertNotEqual(row['images']['preview']['path'], row['posts'][0]['images']['preview']['path'])
        self.assertEqual(row['images']['published'], row['posts'][0]['images']['published'])
        self.assertNotIn('cancel_reference', row)
        records = json.loads((self.output / row['cancel_reference_path']).read_text())
        self.assertEqual(tuple((r['nmea'], r['received_ms']) for r in records), case.cancel_reference)
        html = (self.output / 'index.html').read_text()
        self.assertIn(row['cancel_reference_path'], html)
        self.assertIn('実受信通報の自動照合ではありません', html)

    def test_only_single_evacuation_destinations_include_the_actual_post_with_nmea(self):
        import azarashi
        from shirasu.reports.format import report_message

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

    def test_weather_bundle_has_separate_posts_and_images_for_each_original_frame(self):
        import azarashi
        from shirasu.reports.format import report_message

        case = next(c for c in load_cases() if c.slug == 'weather-many')
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision', return_value=(PNG, 'map', None)) as render, \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, selected_cases=(case.slug,), preview_only=True)
        row, = result['cases']
        self.assertEqual(len(row['posts']), 3)
        self.assertEqual(render.call_count, 4)
        for index, post in enumerate(row['posts']):
            nmea, ms = case.records[index]
            report = azarashi.decode(nmea, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
            self.assertEqual(post['text'], report_message(report))
            self.assertEqual(post['record_indexes'], [index])
            self.assertEqual(post['record_count'], 1)
            self.assertTrue(post['image_enabled'])
            self.assertEqual(render.call_args_list[index + 1].args, ((case.records[index],), 'preview'))
            image = post['images']['preview']
            self.assertNotEqual(image['path'], row['images']['preview']['path'])
            self.assertEqual((self.output / image['path']).read_bytes(), PNG)
        self.assertEqual(result['error_count'], 0)

    def test_typhoon_post_uses_collector_text_and_corresponding_all_point_image(self):
        import azarashi
        from shirasu.reports.typhoon import typhoon_summary

        case = next(c for c in load_cases() if c.slug == 'typhoon-track')
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision', return_value=(PNG, 'map', None)) as render, \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, selected_cases=(case.slug,), preview_only=True)
        row, = result['cases']
        post, = row['posts']
        records = tuple(case.records[i] for i in post['record_indexes'])
        reports = tuple(azarashi.decode(nmea, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
                        for nmea, ms in records)
        self.assertEqual(post['kind'], 'typhoon')
        self.assertEqual(post['text'], typhoon_summary(reports))
        self.assertEqual(set(post['record_indexes']), set(range(len(case.records))))
        self.assertTrue(any(call.args == (records, 'preview') for call in render.call_args_list))
        self.assertEqual(len(case.records), 7)
        self.assertIn('受信した7時点', post['text'])

    def test_failed_post_attachment_remains_visible_without_replacing_other_post_images(self):
        case = next(c for c in load_cases() if c.slug == 'weather-many')
        def render(records, version):
            if records == (case.records[1],):
                raise ValueError('single frame attachment failed')
            return PNG, 'map', None
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision', side_effect=render), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = render_gallery.main(['--output', str(self.output), '--preview-only', '--case', case.slug])
        self.assertEqual(status, 1)
        result = json.loads((self.output / 'manifest.json').read_text())
        row, = result['cases']
        self.assertEqual(result['error_count'], 1)
        self.assertIn('path', row['images']['preview'])
        self.assertIn('path', row['posts'][0]['images']['preview'])
        self.assertIn('single frame attachment failed', row['posts'][1]['images']['preview']['error'])
        self.assertIn('path', row['posts'][2]['images']['preview'])

    def test_post_planning_failure_prevents_any_partial_output(self):
        with patch.object(render_gallery, '_preflight'), \
                patch('tools.preview.gallery_posts.plan_posts', side_effect=ValueError('cannot plan posts')), \
                patch.object(render_gallery, '_render_with_decision') as render:
            with self.assertRaisesRegex(ValueError, 'cannot plan posts'):
                render_gallery.generate(self.output, selected_cases=('marine',), preview_only=True)
        render.assert_not_called()
        self.assertFalse(self.output.exists())

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

    def make_baseline(self):
        baseline = self.root / 'previous'
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision',
                             return_value=(PNG, 'map', {'mode': 'map', 'reasons': []})), \
                redirect_stdout(io.StringIO()):
            render_gallery.generate(baseline, selected_cases=('marine',), preview_only=True)
        return baseline

    def test_baseline_cli_compares_saved_preview_without_rendering_a_published_version(self):
        baseline = self.make_baseline()
        before = {p.relative_to(baseline): p.read_bytes() for p in baseline.rglob('*') if p.is_file()}
        decision = {'mode': 'summary', 'reasons': [
            {'code': 'layout_overflow', 'stage': 'layout', 'message': '配置を切り替え', 'detail': ''}]}
        with patch.object(render_gallery, '_preflight') as preflight, \
                patch.object(render_gallery, '_render_with_decision',
                             return_value=(alternate_png(), 'summary', decision)) as render, \
                redirect_stdout(io.StringIO()):
            status = render_gallery.main(['--baseline', str(baseline), '--output', str(self.output),
                                          '--case', 'marine'])
        self.assertEqual(status, 0)
        preflight.assert_called_once_with('v4', preview_only=True)
        render.assert_called_once_with(self.case.records, 'preview')
        result = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(result['renderer'], 'preview')
        self.assertNotIn('published_version', result)
        row, = result['cases']
        for view in ('images', 'posts'):
            comparison = row['comparison'][view]
            self.assertEqual(comparison['status'], 'changed')
            self.assertTrue({'image', 'mode', 'reasons'} <= set(comparison['changes']))
            self.assertEqual(result['baseline']['counts'][view]['changed'], 1)
        previous = row['comparison']['previous']
        self.assertTrue(previous['images']['preview']['path'].startswith('baseline/'))
        self.assertEqual((self.output / previous['images']['preview']['path']).read_bytes(), PNG)
        self.assertEqual((self.output / row['images']['preview']['path']).read_bytes(), alternate_png())
        copied = json.loads((self.output / result['baseline']['manifest_path']).read_text())
        self.assertEqual(len(copied['cases']), 1)
        self.assertEqual(before, {p.relative_to(baseline): p.read_bytes()
                                 for p in baseline.rglob('*') if p.is_file()})

    def test_old_gallery_without_posts_still_compares_images_without_inventing_posts(self):
        baseline = self.make_baseline()
        path = baseline / 'manifest.json'
        old = json.loads(path.read_text())
        old.pop('post_preview')
        old['cases'][0].pop('posts')
        path.write_text(json.dumps(old))
        with patch.object(render_gallery, '_preflight'), \
                patch.object(render_gallery, '_render_with_decision',
                             return_value=(PNG, 'map', {'mode': 'map', 'reasons': []})), \
                redirect_stdout(io.StringIO()):
            result = render_gallery.generate(self.output, baseline=baseline, selected_cases=('marine',))
        comparison = result['cases'][0]['comparison']
        self.assertEqual(comparison['images']['status'], 'unchanged')
        self.assertEqual(comparison['posts']['status'], 'unavailable')
        self.assertNotIn('posts', comparison['previous'])

    def test_corrupt_baseline_and_nested_output_fail_before_rendering_or_writing(self):
        baseline = self.make_baseline()
        image = next((baseline / 'images').iterdir())
        image.write_bytes(b'broken')
        with patch.object(render_gallery, '_preflight') as preflight, \
                patch.object(render_gallery, '_render_with_decision') as render:
            with self.assertRaises(ValueError):
                render_gallery.generate(self.output, baseline=baseline, selected_cases=('marine',))
            self.assertFalse(self.output.exists())
            with self.assertRaisesRegex(ValueError, '比較元ギャラリーの中'):
                render_gallery.generate(baseline / 'next', baseline=baseline)
            self.assertFalse((baseline / 'next').exists())
        preflight.assert_not_called()
        render.assert_not_called()

    def test_baseline_cannot_be_mixed_with_published_version_or_listing(self):
        for options in (['--baseline', '/unused', '--published-version', 'v4'],
                        ['--baseline', '/unused', '--list']):
            with self.subTest(options=options), redirect_stderr(io.StringIO()), \
                    patch.object(render_gallery, 'generate') as generate:
                with self.assertRaises(SystemExit) as error:
                    render_gallery.main(options)
                self.assertEqual(error.exception.code, 2)
                generate.assert_not_called()

    def test_optional_git_provenance_does_not_make_archived_source_depend_on_git(self):
        with patch.object(render_gallery.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertEqual(render_gallery._source_info(), {})
        with patch.object(render_gallery.subprocess, 'run', side_effect=[
                SimpleNamespace(stdout='abc1234\n'), SimpleNamespace(stdout=' M tools/preview/render_gallery.py\n')]):
            self.assertEqual(render_gallery._source_info(), {'commit': 'abc1234', 'working_tree': True})


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
