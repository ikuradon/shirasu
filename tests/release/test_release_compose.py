"""Exercise the release override with Compose's actual merge/interpolation rules.

Compose tests run only ``docker compose config``; file checks execute the
container verifier against temporary fixtures. Neither contacts the Docker
daemon nor reads the checkout's private settings. All Compose inputs other
than the tracked base file are synthetic files in a temporary directory.
"""

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools.release.prepare_release_compose import VERIFY_IMAGE, render_override


ROOT = Path(__file__).resolve().parents[2]
IMAGE_ID = 'sha256:' + 'a' * 64
MANIFEST_SHA256 = 'b' * 64
PROJECT = 'shirasu-release-compose-fixture'
SERVICES = ('shirasu', 'metrics', 'backup')


class ReceiverNoticeVerificationTests(unittest.TestCase):
    """Execute the container verifier itself against isolated file trees."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='shirasu-receiver-notices-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = self.root / 'app'
        self.license = self.app / 'licenses/pyserial-3.5-LICENSE.txt'
        self.license.parent.mkdir(parents=True)
        self.license.write_bytes((ROOT / 'licenses/pyserial-3.5-LICENSE.txt').read_bytes())
        main = self.app / 'shirasu/service.py'
        main.parent.mkdir(parents=True, exist_ok=True)
        main.write_text('raise RuntimeError("must never import application")\n')
        self.expected = {path.relative_to(self.app).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in (main, self.license)}
        self.code = VERIFY_IMAGE.replace("pathlib.Path('/app')", 'pathlib.Path(' + repr(str(self.app)) + ')')

    def verify(self, success):
        result = subprocess.run([sys.executable, '-I', '-B', '-c', self.code],
                                input=json.dumps(self.expected), text=True,
                                capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        if success:
            self.assertEqual(json.loads(result.stdout), {'verified_files': 2})

    def test_declared_license_directory_is_accepted_and_contents_are_verified(self):
        self.verify(True)
        self.license.write_text('truncated license')
        self.verify(False)
        self.license.unlink()
        self.verify(False)

    def test_extra_empty_directories_and_files_are_rejected(self):
        for name in ('unexpected', 'licenses/unexpected'):
            with self.subTest(directory=name):
                path = self.app / name
                path.mkdir()
                self.verify(False)
                path.rmdir()
        extra = self.app / 'licenses/unknown.txt'
        extra.write_text('undeclared')
        self.verify(False)

    def test_expected_file_or_parent_replaced_with_a_symlink_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        target = outside / self.license.name
        target.write_bytes(self.license.read_bytes())
        self.license.unlink()
        self.license.symlink_to(target)
        self.verify(False)
        self.license.unlink()
        self.license.parent.rmdir()
        self.license.parent.symlink_to(outside, target_is_directory=True)
        self.verify(False)


def compose_environment():
    # Compose interpolation can take precedence from shell variables. Do not
    # inherit host release settings or discover a host Compose file via them.
    return {name: value for name, value in os.environ.items()
            if not name.startswith(('COMPOSE_', 'SHIRASU_'))}


class ReleaseComposeMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('docker') is None:
            raise unittest.SkipTest('Docker Compose CLI is required for merge tests')
        result = subprocess.run(['docker', 'compose', 'version', '--short'],
                                env=compose_environment(), capture_output=True,
                                text=True, timeout=15, check=False)
        if result.returncode:
            raise unittest.SkipTest('Docker Compose CLI is required for merge tests')

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='shirasu-release-compose-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.base = self.root / 'compose.yaml'
        self.host = self.root / 'compose.override.yaml'
        self.release = self.root / 'compose.release.yaml'
        self.environment = self.root / '.env'
        self.base.write_bytes((ROOT / 'compose.yaml').read_bytes())
        self.release.write_text(render_override(IMAGE_ID, MANIFEST_SHA256))
        self.environment.write_text('')
        (self.root / 'config.py').write_text('# Synthetic configuration; never imported.\n')
        self.host.write_text('''services:
  shirasu:
    build:
      context: ./unverified-source
      dockerfile: NeverBuildThis
    image: example.invalid/unverified:mutable
    pull_policy: always
    environment:
      HOST_SETTING: retained
    stop_grace_period: 23s
  metrics:
    networks:
      default:
      observer:
        aliases:
          - shirasu-metrics-fixture
  backup:
    environment:
      SHIRASU_BACKUP_KEEP_COUNT: "41"
volumes:
  shirasu-state:
    external: true
    name: fixture-existing-state
networks:
  observer:
    external: true
    name: fixture-existing-observer
''')

    def config(self, *files, automatic=False):
        command = ['docker', 'compose', '--project-directory', str(self.root)]
        if not automatic:
            command += ['--project-name', PROJECT, '--env-file', str(self.environment)]
            for path in files:
                command += ['-f', str(path)]
        command += ['config', '--format', 'json']
        result = subprocess.run(command, cwd=self.root, env=compose_environment(),
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_release_removes_all_builds_and_overrides_mutable_image_and_pull_policy(self):
        result = self.config(self.base, self.host, self.release)
        self.assertEqual(result['name'], PROJECT)
        self.assertEqual(set(result['services']), set(SERVICES))
        for name in SERVICES:
            with self.subTest(service=name):
                service = result['services'][name]
                self.assertNotIn('build', service)
                self.assertEqual(service['image'], IMAGE_ID)
                self.assertEqual(service['pull_policy'], 'never')

    def test_existing_state_configuration_network_and_process_settings_are_preserved(self):
        baseline = self.config(self.base, self.host)
        result = self.config(self.base, self.host, self.release)
        self.assertEqual(result['name'], baseline['name'])
        self.assertEqual(result['volumes'], baseline['volumes'])
        self.assertEqual(result['networks'], baseline['networks'])
        self.assertEqual(result['volumes']['shirasu-state'],
                         {'name': 'fixture-existing-state', 'external': True})
        for name in SERVICES:
            with self.subTest(service=name):
                before = baseline['services'][name]
                after = result['services'][name]
                # The override must not silently reset future base settings.
                for key, value in before.items():
                    if key == 'healthcheck':
                        self.assertEqual({k: v for k, v in after[key].items() if k != 'test'},
                                         {k: v for k, v in value.items() if k != 'test'})
                    elif key not in ('build', 'image', 'pull_policy', 'command'):
                        self.assertEqual(after[key], value, name + '.' + key)
        receiver = result['services']['shirasu']
        bind = next(volume for volume in receiver['volumes']
                    if volume['target'] == '/app/config.py')
        self.assertEqual(bind['source'], str(self.root / 'config.py'))
        self.assertTrue(bind['read_only'])
        state = next(volume for volume in receiver['volumes'] if volume['target'] == '/data')
        self.assertEqual((state['type'], state['source']), ('volume', 'shirasu-state'))
        self.assertEqual(receiver['network_mode'], 'host')
        self.assertEqual(receiver['healthcheck']['test'], ['CMD', 'python', '-m', 'shirasu', '--health'])
        self.assertEqual(result['services']['metrics']['command'], ['python', '-m', 'shirasu.metrics'])
        self.assertEqual(result['services']['backup']['command'], ['python', '-m', 'shirasu.backup_worker'])
        self.assertEqual(result['services']['backup']['network_mode'], 'none')
        self.assertEqual(result['services']['metrics']['networks']['observer']['aliases'],
                         ['shirasu-metrics-fixture'])

    def test_retained_flat_release_pins_its_own_entrypoints(self):
        self.release.write_text(render_override(IMAGE_ID, MANIFEST_SHA256, legacy=True))
        model = self.config(self.base, self.host, self.release)
        self.assertEqual(model['services']['shirasu']['command'], ['python', './main.py'])
        self.assertEqual(model['services']['shirasu']['healthcheck']['test'],
                         ['CMD', 'python', './main.py', '--health'])
        self.assertEqual(model['services']['metrics']['command'], ['python', 'metrics.py'])
        self.assertEqual(model['services']['backup']['command'], ['python', 'backup_worker.py'])
        self.assertEqual(model['services']['backup']['healthcheck']['test'],
                         ['CMD', 'python', 'backup_worker.py', '--health'])

    def test_format_switches_and_image_disable_only_change_inherited_environment(self):
        configurations = (
            {'SHIRASU_IMAGE_VERSION': 'v5', 'SHIRASU_IMAGE_ENCODING': 'w1',
             'SHIRASU_IMAGE_BASE_URL': 'https://images.example.test',
             'SHIRASU_IMAGE_KEY_ID': 'fixture', 'SHIRASU_IMAGE_SIGNING_KEY': 'c' * 64},
            {'SHIRASU_IMAGE_VERSION': 'v4', 'SHIRASU_IMAGE_ENCODING': 'png',
             'SHIRASU_IMAGE_BASE_URL': 'https://images.example.test',
             'SHIRASU_IMAGE_KEY_ID': 'fixture', 'SHIRASU_IMAGE_SIGNING_KEY': 'c' * 64},
            {'SHIRASU_IMAGE_VERSION': 'v5', 'SHIRASU_IMAGE_ENCODING': 'w1',
             'SHIRASU_IMAGE_BASE_URL': '', 'SHIRASU_IMAGE_KEY_ID': '',
             'SHIRASU_IMAGE_SIGNING_KEY': ''},
        )
        before = self.release.read_bytes()
        for settings in configurations:
            with self.subTest(version=settings['SHIRASU_IMAGE_VERSION'],
                              enabled=bool(settings['SHIRASU_IMAGE_BASE_URL'])):
                self.environment.write_text(''.join(f'{name}={value}\n'
                                                    for name, value in settings.items()))
                baseline = self.config(self.base, self.host)
                result = self.config(self.base, self.host, self.release)
                self.assertEqual(result['services']['shirasu']['environment'],
                                 baseline['services']['shirasu']['environment'])
                for name, value in settings.items():
                    self.assertEqual(result['services']['shirasu']['environment'][name], value)
                for name in SERVICES:
                    self.assertEqual(result['services'][name]['image'], IMAGE_ID)
                    self.assertNotIn('build', result['services'][name])
        self.assertEqual(self.release.read_bytes(), before)

    def test_dotenv_compose_file_keeps_host_override_and_release_for_normal_commands(self):
        self.environment.write_text(
            'COMPOSE_FILE=compose.yaml:compose.override.yaml:compose.release.yaml\n'
            'COMPOSE_PROJECT_NAME=' + PROJECT + '\n'
            'SHIRASU_IMAGE_VERSION=v5\n'
            'SHIRASU_IMAGE_ENCODING=w1\n')
        explicit = self.config(self.base, self.host, self.release)
        automatic = self.config(automatic=True)
        self.assertEqual(automatic, explicit)
        self.assertEqual(automatic['services']['shirasu']['environment']['HOST_SETTING'], 'retained')
        for name in SERVICES:
            self.assertNotIn('build', automatic['services'][name])
            self.assertEqual(automatic['services'][name]['image'], IMAGE_ID)

    def test_normal_base_configuration_and_checkout_versions_remain_unchanged(self):
        result = self.config(self.base)
        for name in SERVICES:
            self.assertIn('build', result['services'][name])
            self.assertNotIn('image', result['services'][name])
        environment = result['services']['shirasu']['environment']
        self.assertEqual(environment['SHIRASU_IMAGE_VERSION'], 'v1')
        self.assertEqual(environment['SHIRASU_IMAGE_ENCODING'], 'png')
        self.assertEqual(environment['SHIRASU_IMAGE_BASE_URL'], '')
        profile = runpy.run_path(str(ROOT / 'shirasu/images/versions.py'))
        self.assertEqual(profile['SUPPORTED_VERSIONS'], ('v1', 'v2', 'v3', 'v4'))
        self.assertEqual(profile['WEBP_VERSIONS'], ())


if __name__ == '__main__':
    unittest.main()
