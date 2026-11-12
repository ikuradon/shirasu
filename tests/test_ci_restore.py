"""A published candidate is restored by identity, without executing image code."""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_release_candidate as candidate
from scripts import restore_release_candidate as release


SOURCE = '1' * 40
IDS = {'receiver': 'sha256:' + '2' * 64, 'renderer': 'sha256:' + '3' * 64}


class RestoreReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ci-restore-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = self.root / 'fixture'
        files = {}
        for name in sorted(candidate._names()):
            path = self.fixture / name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = (candidate.CANDIDATE_PROFILE.encode() if name.endswith('/image_versions.py')
                       else ('fixture ' + name).encode())
            path.write_bytes(content)
            files[name] = hashlib.sha256(content).hexdigest()
        self.manifest = {'schema_version': 1, 'candidate_version': candidate.CANDIDATE_VERSION,
                         'rollback_version': candidate.ROLLBACK_VERSION,
                         'source': {'commit': SOURCE, 'working_tree': False}, 'files': files}
        self.raw = (json.dumps(self.manifest, sort_keys=True, indent=2) + '\n').encode()
        self.manifest_path = self.root / candidate.MANIFEST_NAME
        self.manifest_path.write_bytes(self.raw)
        self.receipt = {
            'schema_version': 1, 'version': candidate.CANDIDATE_VERSION, 'source_commit': SOURCE,
            'candidate_manifest_sha256': hashlib.sha256(self.raw).hexdigest(),
            'run': {'repository': 'Example/Shirasu', 'id': '123', 'attempt': '1'},
            'images': {
                'receiver': {'reference': 'ghcr.io/example/shirasu-receiver@sha256:' + '4' * 64,
                             'image_id': IDS['receiver']},
                'renderer': {'reference': 'ghcr.io/example/shirasu-images@sha256:' + '5' * 64,
                             'image_id': IDS['renderer']},
            },
        }
        self.release_path = self.root / 'release.json'
        self.release_path.write_text(json.dumps(self.receipt))
        self.output = self.root / 'restored'
        self.commands = []
        self.containers = {'a' * 64: 'receiver', 'b' * 64: 'images'}

    def docker(self, args):
        self.commands.append(args)
        if args[0] == 'pull':
            self.assertIn(args[1], [image['reference'] for image in self.receipt['images'].values()])
            return ''
        if args[:2] == ['image', 'inspect']:
            return next(image['image_id'] for image in self.receipt['images'].values()
                        if image['reference'] == args[-1])
        if args[0] == 'create':
            self.assertEqual(args[args.index('--network') + 1], 'none')
            self.assertIn('--read-only', args)
            return 'a' * 64 if args[-1] == IDS['receiver'] else 'b' * 64
        if args[0] == 'cp':
            container, path = args[1].split(':')
            self.assertEqual(path, '/app/.')
            shutil.copytree(self.fixture / self.containers[container], args[2], dirs_exist_ok=True)
            return ''
        if args[0] == 'rm':
            self.assertIn(args[-1], self.containers)
            return ''
        self.fail('Unexpected Docker operation: ' + repr(args))

    def test_pull_uses_fixed_registry_digests_and_extracts_without_running_containers(self):
        with patch.object(release, 'docker', side_effect=self.docker):
            result = release.restore(self.release_path, self.manifest_path, self.output)
        self.assertEqual(result['candidate_manifest_sha256'], self.receipt['candidate_manifest_sha256'])
        self.assertEqual(result['source_commit'], SOURCE)
        self.assertEqual((self.output / candidate.MANIFEST_NAME).read_bytes(), self.raw)
        self.assertEqual(candidate.verify_bundle(self.output)['files'], self.manifest['files'])
        self.assertEqual([args[-1] for args in self.commands if args[0] == 'rm'], list(self.containers))
        self.assertFalse(list(self.root.glob('.restore-release-*')))
        self.assertFalse(any(args[0] in ('run', 'start', 'exec', 'rmi') for args in self.commands))

    def test_tags_other_registries_packages_and_wrong_identity_fail_before_docker(self):
        changes = [
            ('reference', 'ghcr.io/example/shirasu-receiver:latest'),
            ('reference', 'example.test/example/shirasu-receiver@sha256:' + '4' * 64),
            ('reference', 'ghcr.io/another/shirasu-receiver@sha256:' + '4' * 64),
            ('reference', 'ghcr.io/example/shirasu-images@sha256:' + '4' * 64),
            ('reference', 'ghcr.io/example/shirasu-receiver@sha256:' + '4' * 64 + ':tag'),
            ('image_id', 'sha256:short'),
        ]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                receipt = copy.deepcopy(self.receipt)
                receipt['images']['receiver'][field] = value
                with self.assertRaises(ValueError):
                    release.validate_receipt(receipt, self.raw)
        for key, value in [('schema_version', True), ('version', 'v6'),
                           ('source_commit', 'f' * 40), ('candidate_manifest_sha256', '0' * 64)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.validate_receipt(dict(self.receipt, **{key: value}), self.raw)
        for field, value in [('repository', '../other'), ('id', 123), ('attempt', '0')]:
            changed = copy.deepcopy(self.receipt)
            changed['run'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                release.validate_receipt(changed, self.raw)

    def test_dirty_source_or_mismatched_manifest_cannot_be_restored(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['source']['working_tree'] = True
        raw = json.dumps(manifest).encode()
        changed = dict(self.receipt, candidate_manifest_sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaisesRegex(ValueError, 'clean source commit'):
            release.validate_receipt(changed, raw)
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            release.validate_receipt(self.receipt, self.raw + b' ')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            release.read_json(b'{"schema_version":1,"schema_version":1}')

    def test_revision_receipt_keeps_renderer_version_and_requires_receiver_platform(self):
        receipt = dict(self.receipt, schema_version=2, release_tag='images-v5-r1', platform='linux/arm64')
        self.assertEqual(release.validate_receipt(receipt, self.raw), self.manifest)
        for platform in (None, '', 'linux/amd64'):
            with self.subTest(platform=platform), self.assertRaisesRegex(ValueError, 'platform'):
                release.validate_receipt(dict(receipt, platform=platform), self.raw)

    def test_wrong_pulled_image_id_fails_without_creating_a_container(self):
        with patch.object(release, 'docker', side_effect=['', 'sha256:' + '9' * 64]) as docker:
            with self.assertRaisesRegex(ValueError, 'image ID differs'):
                release.restore(self.release_path, self.manifest_path, self.output)
        self.assertEqual(docker.call_count, 2)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.restore-release-*')))

    def test_copy_failure_removes_only_the_created_container_and_partial_directory(self):
        def fail_copy(args):
            if args[0] == 'cp':
                raise ValueError('copy failed')
            return self.docker(args)
        with patch.object(release, 'docker', side_effect=fail_copy):
            with self.assertRaisesRegex(ValueError, 'copy failed'):
                release.restore(self.release_path, self.manifest_path, self.output)
        self.assertEqual(self.commands[-1], ['rm', '--volumes', 'a' * 64])
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.restore-release-*')))

    def test_changed_extracted_file_is_rejected_by_the_real_candidate_verifier(self):
        (self.fixture / 'receiver/main.py').write_text('wrong contents')
        with patch.object(release, 'docker', side_effect=self.docker):
            with self.assertRaisesRegex(ValueError, 'hash differs'):
                release.restore(self.release_path, self.manifest_path, self.output)
        self.assertEqual(len([args for args in self.commands if args[0] == 'rm']), 2)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.restore-release-*')))

    def test_existing_or_linked_destinations_and_metadata_are_rejected(self):
        self.output.mkdir()
        marker = self.output / 'keep'
        marker.write_text('preserve')
        with patch.object(release, 'docker') as docker:
            with self.assertRaisesRegex(ValueError, 'already exists'):
                release.restore(self.release_path, self.manifest_path, self.output)
            self.assertEqual(marker.read_text(), 'preserve')
            link = self.root / 'linked'
            link.symlink_to(self.root / 'missing')
            with self.assertRaises(ValueError):
                release.restore(self.release_path, self.manifest_path, link)
            linked_metadata = self.root / 'linked-manifest'
            linked_metadata.symlink_to(self.manifest_path)
            with self.assertRaises(ValueError):
                release.restore(self.release_path, linked_metadata, self.root / 'unused')
            docker.assert_not_called()

    def test_destination_created_during_restore_is_not_replaced(self):
        original = release.verify_bundle

        def race(directory):
            verified = original(directory)
            self.output.mkdir()
            (self.output / 'keep').write_text('another operation')
            return verified

        with patch.object(release, 'docker', side_effect=self.docker), \
                patch.object(release, 'verify_bundle', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'now exists'):
                release.restore(self.release_path, self.manifest_path, self.output)
        self.assertEqual((self.output / 'keep').read_text(), 'another operation')
        self.assertFalse(list(self.root.glob('.restore-release-*')))

    def test_docker_failures_do_not_dump_credentials_or_arbitrary_output(self):
        process = subprocess.CompletedProcess([], 1, 'private output', 'private output')
        with patch.object(release.subprocess, 'run', return_value=process):
            with self.assertRaises(ValueError) as error:
                release.docker(['pull', 'example'])
        self.assertNotIn('private output', str(error.exception))


if __name__ == '__main__':
    unittest.main()
