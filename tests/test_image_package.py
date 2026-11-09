"""The deployment artifact must never contain receiver config or state."""

from pathlib import Path
import tempfile
import unittest
from scripts.prepare_image_service import FILES, prepare


class ImagePackageTests(unittest.TestCase):
    def test_artifact_is_an_allowlist_and_refuses_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'deploy'
            prepare(destination)
            actual = {str(path.relative_to(destination)) for path in destination.rglob('*') if path.is_file()}
            self.assertEqual(actual, set(FILES) | {'requirements.txt', 'api/image.py', '.python-version', 'vercel.json'})
            self.assertNotIn('config.py', actual)
            self.assertNotIn('main.py', actual)
            self.assertTrue((destination / 'image_service/assets/v1/OFL.txt').is_file())
            with self.assertRaises(FileExistsError):
                prepare(destination)


if __name__ == '__main__':
    unittest.main()
