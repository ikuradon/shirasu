"""The deployment artifact must never contain receiver config or state."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.prepare_image_service import FILES, prepare


class ImagePackageTests(unittest.TestCase):
    def test_missing_assets_explain_preparation_but_missing_sources_do_not(self):
        for name, error in (
                ('image_service/assets/v1/land.geojson', 'prepare_vendor_assets.py'),
                ('image_service/assets/v1/manifest.json', 'Missing or linked source'),
                ('image_service/render.py', 'Missing or linked source')):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / 'deploy'
                with patch('scripts.prepare_image_service.ROOT', Path(directory) / 'checkout'), \
                        patch('scripts.prepare_image_service.FILES', (name,)), \
                        self.assertRaisesRegex(ValueError, error):
                    prepare(destination)
                self.assertFalse(destination.exists())

    def test_linked_asset_is_rejected_without_suggesting_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = 'image_service/assets/v1/land.geojson'
            source = root / name
            source.parent.mkdir(parents=True)
            source.symlink_to(root / 'missing')
            destination = root / 'deploy'
            with patch('scripts.prepare_image_service.ROOT', root), \
                    patch('scripts.prepare_image_service.FILES', (name,)), \
                    self.assertRaisesRegex(ValueError, 'Missing or linked source'):
                prepare(destination)
            self.assertFalse(destination.exists())

    def test_artifact_is_an_allowlist_and_refuses_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'deploy'
            prepare(destination)
            actual = {str(path.relative_to(destination)) for path in destination.rglob('*') if path.is_file()}
            self.assertEqual(actual, set(FILES) | {'requirements.txt', 'api/image.py', '.python-version', 'vercel.json'})
            self.assertNotIn('config.py', actual)
            self.assertNotIn('main.py', actual)
            self.assertTrue((destination / 'image_service/assets/v1/OFL.txt').is_file())
            self.assertEqual((destination / 'licenses/pyserial-3.5-LICENSE.txt').read_bytes(),
                             (Path(__file__).resolve().parents[1] / 'licenses/pyserial-3.5-LICENSE.txt').read_bytes())
            with self.assertRaises(FileExistsError):
                prepare(destination)


if __name__ == '__main__':
    unittest.main()
