"""Exercise the release override with Compose's actual merge/interpolation rules.

These tests only run ``docker compose config``; they neither contact the Docker
daemon nor read the checkout's private settings. All Compose inputs other than
the tracked base file are synthetic files in a temporary directory.
"""

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest

from scripts.prepare_release_compose import render_override


ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID = 'sha256:' + 'a' * 64
MANIFEST_SHA256 = 'b' * 64
PROJECT = 'shirasu-release-compose-fixture'
SERVICES = ('shirasu', 'metrics', 'backup')


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
                    if key not in ('build', 'image', 'pull_policy'):
                        self.assertEqual(after[key], value, name + '.' + key)
        receiver = result['services']['shirasu']
        bind = next(volume for volume in receiver['volumes']
                    if volume['target'] == '/app/config.py')
        self.assertEqual(bind['source'], str(self.root / 'config.py'))
        self.assertTrue(bind['read_only'])
        state = next(volume for volume in receiver['volumes'] if volume['target'] == '/data')
        self.assertEqual((state['type'], state['source']), ('volume', 'shirasu-state'))
        self.assertEqual(receiver['network_mode'], 'host')
        self.assertEqual(receiver['healthcheck']['test'], ['CMD', 'python', 'main.py', '--health'])
        self.assertEqual(result['services']['metrics']['command'], ['python', 'metrics.py'])
        self.assertEqual(result['services']['backup']['command'], ['python', 'backup_worker.py'])
        self.assertEqual(result['services']['backup']['network_mode'], 'none')
        self.assertEqual(result['services']['metrics']['networks']['observer']['aliases'],
                         ['shirasu-metrics-fixture'])

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
        profile = runpy.run_path(str(ROOT / 'image_versions.py'))
        self.assertEqual(profile['SUPPORTED_VERSIONS'], ('v1', 'v2', 'v3', 'v4'))
        self.assertEqual(profile['WEBP_VERSIONS'], ())


if __name__ == '__main__':
    unittest.main()
