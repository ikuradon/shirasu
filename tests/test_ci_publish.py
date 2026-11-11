"""Only tested images from trusted manual runs may reach private release storage."""

import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import publish_ci_release as publish


REPOSITORY = 'Example/Shirasu'
SOURCE = 'a' * 40
RECEIVER, RENDERER = ('sha256:' + x * 64 for x in ('1', '2'))
ENV = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
       'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': REPOSITORY,
       'GITHUB_SHA': SOURCE, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}


class CIPublishTests(unittest.TestCase):
    def test_only_manual_main_runs_have_a_release_context(self):
        self.assertEqual(publish.context(ENV)['sha'], SOURCE)
        for changed in ({'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_EVENT_NAME': 'push'},
                        {'GITHUB_REF': 'refs/heads/topic'}, {'GITHUB_ACTIONS': 'false'},
                        {'GITHUB_REPOSITORY': 'owner/repo;command'}, {'GITHUB_RUN_ID': '../1'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                publish.context(dict(ENV, **changed))

    def test_public_or_unlinked_packages_are_rejected_before_any_docker_operation(self):
        for info in ({'visibility': 'public', 'repository': {'full_name': REPOSITORY}},
                     {'visibility': 'private', 'repository': None},
                     {'visibility': 'internal', 'repository': {'full_name': REPOSITORY}}):
            with self.subTest(info=info), patch.object(publish, 'api', return_value=info), \
                    patch.object(publish, 'run') as run:
                with self.assertRaises(ValueError):
                    publish.ensure_private(REPOSITORY, 'User', 'shirasu-images', '123-1')
                run.assert_not_called()

    def test_new_package_is_checked_using_only_an_empty_bootstrap_image(self):
        operations = []

        def run(command, **kwargs):
            operations.append(command)
            if command[:2] == ['docker', 'build']:
                directory = Path(command[-1])
                self.assertEqual([p.name for p in directory.iterdir()], ['Dockerfile'])
                self.assertEqual((directory / 'Dockerfile').read_text(),
                                 'FROM scratch\nLABEL org.opencontainers.image.source="https://github.com/'
                                 + REPOSITORY + '"\n')
            else:
                self.assertEqual(command, ['docker', 'push',
                                          'ghcr.io/example/shirasu-images:bootstrap-123-1'])
            return ''

        private = {'visibility': 'private', 'repository': {'full_name': REPOSITORY}}
        with patch.object(publish, 'api', side_effect=[None, private]), patch.object(publish, 'run', side_effect=run):
            publish.ensure_private(REPOSITORY, 'User', 'shirasu-images', '123-1')
        self.assertEqual(len(operations), 2)

    def test_existing_tag_or_draft_cannot_be_overwritten(self):
        for responses in ([{'ref': 'existing'}], [None, {'id': 8}],
                          [None, None, [{'tag_name': 'images-v5', 'draft': True}]]):
            with self.subTest(responses=responses), patch.object(publish, 'api', side_effect=responses):
                with self.assertRaises(ValueError):
                    publish.require_new_version(REPOSITORY, 'images-v5')

    def test_registry_digest_and_config_image_id_are_both_checked(self):
        digest = 'sha256:' + 'f' * 64
        with patch.object(publish, 'run', side_effect=['', 'tag: digest: ' + digest, '', RECEIVER]):
            result = publish.push_image(RECEIVER, 'ghcr.io/example/shirasu-receiver', 'run-123-1')
        self.assertEqual(result, {'reference': 'ghcr.io/example/shirasu-receiver@' + digest,
                                  'image_id': RECEIVER})
        with patch.object(publish, 'run', side_effect=['', 'tag: digest: ' + digest, '', RENDERER]):
            with self.assertRaisesRegex(ValueError, 'differs'):
                publish.push_image(RECEIVER, 'ghcr.io/example/shirasu-receiver', 'run-123-1')

    def test_permission_error_cannot_masquerade_as_an_absent_package(self):
        error = HTTPError('https://api.github.com/test', 403, 'private detail', {}, None)
        with patch.dict(publish.os.environ, {'GITHUB_TOKEN': 'fixture-token'}, clear=True), \
                patch.object(publish, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(ValueError, 'HTTP 403') as raised:
                publish.api('test', missing=True)
        self.assertNotIn('fixture-token', str(raised.exception))
        self.assertNotIn('private detail', str(raised.exception))

    def fixture(self, *, dirty=False):
        directory = tempfile.TemporaryDirectory(prefix='ci-publish-tests-')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        manifest = {'schema_version': 1, 'candidate_version': 'v5', 'rollback_version': 'v4',
                    'source': {'commit': SOURCE, 'working_tree': dirty},
                    'files': {'receiver/main.py': 'b' * 64, 'images/render.py': 'c' * 64}}
        raw = (json.dumps(manifest, sort_keys=True) + '\n').encode()
        digest = hashlib.sha256(raw).hexdigest()
        for directory in ('candidate', 'public', 'results'):
            (root / directory).mkdir()
        for directory in ('candidate', 'public'):
            (root / directory / 'candidate-manifest.json').write_bytes(raw)
        (root / 'public/public-manifest.json').write_text('{}')
        (root / 'results/compose.json').write_text(json.dumps({'image_id': RECEIVER}))
        self.enterContext(patch.dict(publish.os.environ, ENV, clear=True))
        self.enterContext(patch.object(publish, 'verify_bundle', return_value=dict(manifest, manifest_sha256=digest)))
        self.enterContext(patch.object(publish, 'verify_public', return_value={'candidate_manifest_sha256': digest}))
        self.enterContext(patch.object(publish, 'inspect_image', return_value=RECEIVER))
        return root

    def test_dirty_candidate_is_rejected_before_any_network_call(self):
        root = self.fixture(dirty=True)
        with patch.object(publish, 'api') as api, patch.object(publish, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'clean Actions'):
                publish.publish(root, 'receiver:test', 'renderer:test')
            api.assert_not_called()
            run.assert_not_called()

    def simulate(self, root, *, fail_upload=False):
        calls, commands, uploads = [], [], []

        def api(path, **kwargs):
            calls.append((path, kwargs))
            if '/git/ref/' in path or '/releases/tags/' in path:
                return None
            if '/releases?' in path:
                return []
            if '/packages/' in path:
                return {'visibility': 'private', 'repository': {'full_name': REPOSITORY}}
            if kwargs.get('method') == 'POST':
                self.assertTrue(kwargs['body']['draft'])
                return {'id': 42}
            if kwargs.get('method') == 'PATCH':
                self.assertEqual(len(uploads), 1)
                return {}
            return {'full_name': REPOSITORY, 'owner': {'type': 'User'}}

        def run(command, **kwargs):
            commands.append(command)
            if command[:3] == ['docker', 'image', 'inspect']:
                return RECEIVER if '-receiver@' in command[-1] else RENDERER
            if command[:2] == ['docker', 'run']:
                self.assertIn('--read-only', command)
                self.assertIn('none', command)
                self.assertIn(command[-4], (RECEIVER, RENDERER))
                return 'verified\n'
            if command[:2] == ['docker', 'push']:
                return 'digest: sha256:' + ('d' if '-receiver:' in command[-1] else 'e') * 64
            if command[:3] == ['gh', 'release', 'upload']:
                receipt = json.loads(Path(command[-3]).read_text())
                uploads.append(receipt)
                with tarfile.open(command[-1]) as archive:
                    self.assertEqual(set(archive.getnames()), {'candidate-manifest.json', 'public-manifest.json'})
                if fail_upload:
                    raise ValueError('fixture upload failure')
            return ''

        with patch.object(publish, 'api', side_effect=api), patch.object(publish, 'run', side_effect=run):
            if fail_upload:
                with self.assertRaisesRegex(ValueError, 'upload failure'):
                    publish.publish(root, 'receiver:test', 'renderer:test')
            else:
                result = publish.publish(root, 'receiver:test', 'renderer:test')
                self.assertEqual(result, uploads[0])
                self.assertNotEqual(result['images']['receiver']['reference'].split('@')[1], RECEIVER)
        return calls, commands

    def test_publish_uses_tested_images_and_only_finalizes_after_receipt_upload(self):
        calls, commands = self.simulate(self.fixture())
        self.assertEqual(calls[-1], ('repos/' + REPOSITORY + '/releases/42',
                                    {'method': 'PATCH', 'body': {'draft': False}}))
        self.assertFalse(any(command[:2] == ['docker', 'build'] for command in commands))
        self.assertFalse(any('--clobber' in command for command in commands))

    def test_failed_upload_keeps_reserved_draft_and_does_not_publish(self):
        calls, _ = self.simulate(self.fixture(), fail_upload=True)
        self.assertFalse(any(options.get('method') == 'PATCH' for _, options in calls))


if __name__ == '__main__':
    unittest.main()
