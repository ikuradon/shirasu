"""Unpublished renderers are offline opt-ins, never new production URL versions."""

from contextlib import redirect_stderr, redirect_stdout
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

from image_protocol import ProtocolError, SUPPORTED_VERSIONS, make_path
from image_service.__main__ import main
from image_service.app import RENDERERS
from scripts.prepare_image_service import FILES, PREVIEW_FILES, prepare
import test_image_renderer as helpers

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'image_service/examples/northwest_pacific_tsunami.json'
HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))


class PreviewBoundaryTests(unittest.TestCase):
    def test_missing_flood_asset_explains_preparation_without_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'preview'
            with patch('scripts.prepare_image_service.ROOT', Path(directory) / 'checkout'), \
                    patch('scripts.prepare_image_service.FILES', ()), \
                    patch('scripts.prepare_image_service.PREVIEW_FILES',
                          ('image_service/assets/preview/flood.geojson.zlib',)):
                with self.assertRaisesRegex(ValueError, 'prepare_vendor_assets.py'):
                    prepare(destination, include_preview=True)
            self.assertFalse(destination.exists())

    def test_unpublished_renderer_cannot_be_signed_or_reached_over_http(self):
        self.assertEqual(SUPPORTED_VERSIONS, ('v1', 'v2', 'v3', 'v4'))
        self.assertEqual(set(RENDERERS), set(SUPPORTED_VERSIONS))
        for version in ('preview', 'v5'):
            with self.subTest(version=version), self.assertRaises(ProtocolError):
                make_path(helpers.TYPHOON, 'test', helpers.KEY, version=version)
            path = make_path(helpers.TYPHOON, 'test', helpers.KEY).replace('/v1/', '/' + version + '/', 1)
            response = helpers.request(path)
            self.assertEqual(response['status'], '404 Not Found')
            self.assertEqual(response['headers']['Cache-Control'], 'no-store')

    def test_preview_cannot_serve_or_ambiguously_select_a_published_version(self):
        combinations = [('--serve', '--preview')]
        combinations.extend((('--serve', '--cancel-reference', 'not-read.json'),
                             ('--records', 'not-read.json', '--output', 'not-created.png',
                              '--cancel-reference', 'not-read.json')))
        combinations.extend(('--records', 'not-read.json', '--output', 'not-created.png',
                             '--preview', '--version', version) for version in SUPPORTED_VERSIONS)
        for arguments in combinations:
            with self.subTest(arguments=arguments), patch.object(sys, 'argv', ['images', *arguments]), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                main()
            self.assertEqual(stopped.exception.code, 2)

    def test_default_package_omits_unpublished_modules_and_assets(self):
        self.assertTrue(PREVIEW_FILES)
        self.assertTrue(set(PREVIEW_FILES).isdisjoint(FILES))
        with tempfile.TemporaryDirectory() as directory:
            production = prepare(Path(directory) / 'production')
            preview = prepare(Path(directory) / 'preview', include_preview=True)
            for name in PREVIEW_FILES:
                with self.subTest(file=name):
                    self.assertFalse((production / name).exists())
                    self.assertEqual((preview / name).read_bytes(), (ROOT / name).read_bytes())
            self.assertEqual((production / 'image_service/app.py').read_bytes(),
                             (preview / 'image_service/app.py').read_bytes())
            for directory in (production, preview):
                for secret in ('config.py', '.env', 'state/image-signing-keys.json'):
                    self.assertFalse((directory / secret).exists())
            rejected = subprocess.run(
                [sys.executable, '-B', '-m', 'image_service', '--preview',
                 '--records', 'not-read.json', '--output', 'not-created.png'],
                cwd=production, capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn('Preview sources are not included', rejected.stderr)
            self.assertFalse((production / 'not-created.png').exists())


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for offline preview tests')
class OfflinePreviewCLITests(unittest.TestCase):
    def test_confirmed_cancel_reference_is_forwarded_only_to_offline_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cancel.json'
            reference = Path(directory) / 'reference.json'
            destination = Path(directory) / 'preview.png'
            source.write_text(json.dumps([{'nmea': 'cancel', 'received_ms': 2}]))
            reference.write_text(json.dumps([{'nmea': 'original', 'received_ms': 1}]))
            arguments = ['images', '--preview', '--records', str(source), '--output', str(destination),
                         '--cancel-reference', str(reference)]
            with patch.object(sys, 'argv', arguments), redirect_stdout(io.StringIO()), \
                    patch('image_service.render_preview.render', return_value=SimpleNamespace(
                        png=b'preview-renderer', category='Weather')) as render:
                main()
            render.assert_called_once_with([('cancel', 2)], cancel_reference=[('original', 1)])
            self.assertEqual(destination.read_bytes(), b'preview-renderer')

    def test_default_cli_still_selects_published_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'image.png'
            source = ROOT / 'image_service/examples/typhoon.json'
            arguments = ['images', '--records', str(source), '--output', str(destination)]
            with patch.object(sys, 'argv', arguments), redirect_stdout(io.StringIO()), \
                    patch('image_service.render.render', return_value=SimpleNamespace(
                        png=b'published-renderer', category='Typhoon')) as render:
                main()
            self.assertEqual(destination.read_bytes(), b'published-renderer')
            render.assert_called_once_with([(r['nmea'], r['received_ms'])
                                           for r in json.loads(source.read_text())])

    def test_explicit_preview_writes_a_png_and_refuses_to_overwrite_it(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'preview.png'
            command = [sys.executable, '-B', '-m', 'image_service', '--preview',
                       '--records', str(EXAMPLE), '--output', str(destination)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                    timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('NorthwestPacificTsunami', result.stdout)
            data = destination.read_bytes()
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            again = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                   timeout=60, check=False)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(destination.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
