"""A release override must name the checked image, not a movable tag."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_release_compose as release


IMAGE_ID = 'sha256:' + '1' * 64
MANIFEST_SHA256 = '2' * 64


def inspection(**changes):
    values = {'id': IMAGE_ID, 'workdir': '/app', 'command': ['python', './main.py'],
              'entrypoint': None, 'user': '', 'volumes': None}
    values.update(changes)
    return '\n'.join(json.dumps(value) for value in values.values())


class PrepareReleaseComposeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='release-compose-unit-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / 'candidate'
        self.bundle.mkdir()
        self.output = self.root / 'runtime/compose.release.yaml'
        self.manifest = {'manifest_sha256': MANIFEST_SHA256,
                         'files': {'receiver/main.py': '3' * 64, 'images/render.py': '4' * 64}}

    def test_verified_image_id_is_pinned_without_starting_services_or_reading_settings(self):
        with patch.object(release, 'verify_bundle', return_value=self.manifest) as verify, \
                patch.object(release, '_docker', side_effect=[inspection(), '{"verified_files":1}']) as docker:
            result = release.prepare(self.bundle, 'shirasu:verified', self.output)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result['image_id'], IMAGE_ID)
        self.assertEqual(result['bundle_manifest_sha256'], MANIFEST_SHA256)
        text = self.output.read_text()
        self.assertEqual(text.count('image: "' + IMAGE_ID + '"'), 3)
        self.assertEqual(text.count('build: !reset null'), 3)
        self.assertNotIn('shirasu:verified', text)
        self.assertNotIn('environment:', text)
        self.assertNotIn('volumes:', text)
        args, kwargs = docker.call_args_list[1]
        command = args[0]
        self.assertIn(IMAGE_ID, command)
        self.assertNotIn('shirasu:verified', command)
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertEqual(command[command.index('--pull') + 1], 'never')
        self.assertIn('--read-only', command)
        self.assertNotIn('--volume', command)
        self.assertEqual(json.loads(kwargs['input']), {'main.py': '3' * 64})

    def test_wrong_image_startup_configuration_is_rejected(self):
        for changed in ({'workdir': '/'}, {'command': ['other']}, {'entrypoint': ['sh']},
                        {'user': '1234'}, {'volumes': {'/app': {}}}, {'id': 'sha256:short'}):
            with self.subTest(changed=changed), patch.object(release, '_docker', return_value=inspection(**changed)):
                with self.assertRaises(ValueError):
                    release.inspect_image('shirasu:verified')

    def test_docker_omitted_optional_configuration_fields_use_defaults(self):
        with patch.object(release, '_docker', return_value=inspection(user=None)) as docker:
            self.assertEqual(release.inspect_image('shirasu:verified'), IMAGE_ID)
        template = docker.call_args.args[0][3]
        self.assertIn('index .Config "Entrypoint"', template)
        self.assertIn('index .Config "User"', template)

    def test_invalid_references_cannot_be_docker_options_or_mutable_override_values(self):
        with patch.object(release, '_docker') as docker:
            for reference in ('--help', '', 'foo\nbar', 'a;cmd', None):
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    release.inspect_image(reference)
            docker.assert_not_called()
        for image, digest in [('shirasu:latest', MANIFEST_SHA256), (IMAGE_ID, 'bad')]:
            with self.assertRaises(ValueError):
                release.render_override(image, digest)

    def test_candidate_output_existing_output_and_symlink_are_rejected_before_docker(self):
        self.output.parent.mkdir()
        self.output.write_text('keep existing')
        link = self.root / 'linked-output'
        link.symlink_to(self.root / 'absent')
        with patch.object(release, '_docker') as docker:
            for target in (self.output, self.bundle / 'compose.yaml', link):
                with self.subTest(target=target), self.assertRaises((ValueError, FileExistsError)):
                    release.prepare(self.bundle, 'shirasu:verified', target)
            docker.assert_not_called()
        self.assertEqual(self.output.read_text(), 'keep existing')

    def test_image_mismatch_or_changed_bundle_does_not_write_override(self):
        changed = copy.deepcopy(self.manifest)
        changed['files']['receiver/main.py'] = '5' * 64
        for second, reply in [(self.manifest, '{"verified_files":0}'), (changed, '{"verified_files":1}')]:
            with self.subTest(second=second, reply=reply), \
                    patch.object(release, 'verify_bundle', side_effect=[self.manifest, second]), \
                    patch.object(release, '_docker', side_effect=[inspection(), reply]):
                with self.assertRaises(ValueError):
                    release.prepare(self.bundle, 'shirasu:verified', self.output)
                self.assertFalse(self.output.exists())

    def test_image_file_check_rejects_changed_missing_extra_and_linked_sources(self):
        app = self.root / 'app'
        app.mkdir()
        main = app / 'main.py'
        content = b'print("fixture")\n'
        expected = {'main.py': hashlib.sha256(content).hexdigest()}
        code = release.VERIFY_IMAGE.replace("pathlib.Path('/app')", 'pathlib.Path(' + repr(str(app)) + ')')

        def check(success):
            result = subprocess.run([sys.executable, '-B', '-c', code], input=json.dumps(expected),
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode == 0, success, result.stderr)

        main.write_bytes(content)
        check(True)
        main.write_bytes(b'changed')
        check(False)
        main.unlink()
        check(False)
        main.write_bytes(content)
        extra = app / 'config.py'
        extra.write_text('must not read configuration')
        check(False)
        extra.unlink()
        main.unlink()
        main.symlink_to(self.root / 'outside')
        check(False)

    def test_docker_errors_do_not_dump_captured_output(self):
        with patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 1, stdout='private data', stderr='private data')):
            with self.assertRaises(ValueError) as raised:
                release._docker(['image', 'inspect', 'example'])
        self.assertNotIn('private data', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
