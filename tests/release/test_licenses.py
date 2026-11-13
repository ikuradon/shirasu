"""Missing notices must fail the same built-image check used by CI."""

from email.message import Message
import base64
import hashlib
from importlib.metadata import PackagePath
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tools.release.check_licenses import PROJECT_NOTICES, check


class LicenseBundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in PROJECT_NOTICES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Retained license notice\n')
        self.lock = self.root / 'requirements.txt'
        self.lock.write_text('example==1.0 \\\n    --hash=sha256:abc\n')
        self.notices = [PackagePath('example-1.0.dist-info/licenses/LICENSE')]
        path = self.root / str(self.notices[0])
        path.parent.mkdir(parents=True)
        path.write_text('Example copyright and license\n')
        self.metadata = Message()
        self.metadata['License-File'] = 'LICENSE'
        self.version = '1.0'

    def distribution(self, name):
        return SimpleNamespace(version=self.version, files=self.notices,
                               metadata=self.metadata, locate_file=lambda path: self.root / str(path))

    def run_check(self):
        return check(self.root, [self.lock], self.distribution)

    def test_keeps_declared_notice_and_pinned_version(self):
        result = self.run_check()
        self.assertEqual(result['dependencies'][0]['version'], '1.0')
        self.assertEqual(len(result['dependencies'][0]['notices']), 1)

    def test_missing_application_license_fails(self):
        (self.root / 'LICENSE').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or empty'):
            self.run_check()

    def test_stripped_or_empty_wheel_license_fails(self):
        path = self.root / str(self.notices[0])
        path.write_text('')
        with self.assertRaisesRegex(ValueError, 'Missing or empty'):
            self.run_check()
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or empty'):
            self.run_check()

    def test_another_license_does_not_hide_a_missing_declaration(self):
        self.metadata['License-File'] = 'THIRD_PARTY_LICENSE'
        with self.assertRaisesRegex(ValueError, 'Declared license file missing'):
            self.run_check()

    def test_changed_license_text_is_not_accepted_as_the_original(self):
        path = self.root / str(self.notices[0])
        digest = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).decode().rstrip('=')
        self.notices[0].hash = SimpleNamespace(mode='sha256', value=digest)
        self.run_check()
        path.write_text('A different nonempty license\n')
        with self.assertRaisesRegex(ValueError, 'differs from installed RECORD'):
            self.run_check()

    def test_new_dependency_without_notices_fails(self):
        self.notices = []
        self.metadata = Message()
        with self.assertRaisesRegex(ValueError, 'No retained license text'):
            self.run_check()

    def test_pyserial_35_has_an_explicit_supplement(self):
        self.lock.write_text('pyserial==3.5\n')
        self.version = '3.5'
        self.notices = []
        self.metadata = Message()
        result = self.run_check()
        self.assertEqual(result['dependencies'][0]['supplement'], 'licenses/pyserial-3.5-LICENSE.txt')

    def test_other_pyserial_version_cannot_reuse_old_supplement(self):
        self.lock.write_text('pyserial==3.6\n')
        self.version = '3.6'
        self.notices = []
        self.metadata = Message()
        with self.assertRaisesRegex(ValueError, 'No retained license text'):
            self.run_check()

    def test_installed_version_drift_fails(self):
        self.version = '2.0'
        with self.assertRaisesRegex(ValueError, 'Installed version differs'):
            self.run_check()


if __name__ == '__main__':
    unittest.main()
