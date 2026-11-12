"""Only tested images from trusted release runs may reach public storage."""

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
    def test_only_manual_main_or_release_tag_pushes_have_a_release_context(self):
        self.assertEqual(publish.context(ENV)['sha'], SOURCE)
        self.assertIsNone(publish.context(ENV)['tag'])
        tagged = dict(ENV, GITHUB_EVENT_NAME='push', GITHUB_REF='refs/tags/images-v5')
        self.assertEqual(publish.context(tagged)['tag'], 'images-v5')
        self.assertEqual(publish.context(dict(tagged, GITHUB_REF='refs/tags/images-v5-r1'))['tag'],
                         'images-v5-r1')
        for changed in ({'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_EVENT_NAME': 'push'},
                        {'GITHUB_REF': 'refs/heads/topic'}, {'GITHUB_ACTIONS': 'false'},
                        {'GITHUB_REPOSITORY': 'owner/repo;command'}, {'GITHUB_RUN_ID': '../1'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                publish.context(dict(ENV, **changed))
        for ref in ('refs/heads/main', 'refs/tags/v5', 'refs/tags/images-v0',
                    'refs/tags/images-v05', 'refs/tags/images-v5/extra', 'refs/tags/images-v5;command'):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                publish.context(dict(tagged, GITHUB_REF=ref))

    def test_private_or_unlinked_packages_are_rejected_before_any_docker_operation(self):
        for info in ({'visibility': 'private', 'repository': {'full_name': REPOSITORY}},
                     {'visibility': 'public', 'repository': None},
                     {'visibility': 'internal', 'repository': {'full_name': REPOSITORY}}):
            with self.subTest(info=info), patch.object(publish, 'api', return_value=info), \
                    patch.object(publish, 'run') as run:
                with self.assertRaises(ValueError):
                    publish.ensure_public(REPOSITORY, 'User', 'shirasu-images', '123-1')
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

        public = {'visibility': 'public', 'repository': {'full_name': REPOSITORY}}
        with patch.object(publish, 'api', side_effect=[None, public]), patch.object(publish, 'run', side_effect=run):
            publish.ensure_public(REPOSITORY, 'User', 'shirasu-images', '123-1')
        self.assertEqual(len(operations), 2)

    def test_bootstrap_waits_briefly_only_for_package_visibility_404s(self):
        public = {'visibility': 'public', 'repository': {'full_name': REPOSITORY}}
        with patch.object(publish, 'api', side_effect=[None, None, public]) as api, \
                patch.object(publish, 'run'), patch.object(publish.time, 'sleep') as sleep:
            publish.ensure_public(REPOSITORY, 'User', 'shirasu-images', '123-1')
        self.assertEqual(api.call_count, 3)
        self.assertTrue(all(call.kwargs == {'missing': True} for call in api.call_args_list))
        sleep.assert_called_once_with(1)
        with patch.object(publish, 'api', return_value=None) as api, \
                patch.object(publish, 'run'), patch.object(publish.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'must be public'):
                publish.ensure_public(REPOSITORY, 'User', 'shirasu-images', '123-1')
        self.assertEqual(api.call_count, 6)
        self.assertEqual(sleep.call_count, 4)
        with patch.object(publish, 'api', side_effect=[None, ValueError('HTTP 403')]), \
                patch.object(publish, 'run'), patch.object(publish.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'HTTP 403'):
                publish.ensure_public(REPOSITORY, 'User', 'shirasu-images', '123-1')
        sleep.assert_not_called()

    def test_existing_tag_or_draft_cannot_be_overwritten(self):
        for responses in ([{'ref': 'existing'}], [None, {'id': 8}],
                          [None, None, [{'tag_name': 'images-v5', 'draft': True}]]):
            with self.subTest(responses=responses), patch.object(publish, 'api', side_effect=responses):
                with self.assertRaises(ValueError):
                    publish.require_new_version(REPOSITORY, 'images-v5')

    def test_tag_push_accepts_only_the_same_peeled_commit_and_no_release_or_draft(self):
        light = {'object': {'type': 'commit', 'sha': SOURCE}}
        annotated = {'object': {'type': 'tag', 'sha': 'b' * 40}}
        for tag_objects in ([light], [annotated, light]):
            with self.subTest(tag_objects=tag_objects), \
                    patch.object(publish, 'api', side_effect=[*tag_objects, None, []]):
                publish.require_new_version(REPOSITORY, 'images-v5', SOURCE)
        for responses in ([None], [light, {'id': 8}],
                          [light, None, [{'tag_name': 'images-v5', 'draft': True}]]):
            with self.subTest(responses=responses), patch.object(publish, 'api', side_effect=responses):
                with self.assertRaises(ValueError):
                    publish.require_new_version(REPOSITORY, 'images-v5', SOURCE)
        with patch.object(publish, 'api', return_value=light):
            with self.assertRaisesRegex(ValueError, 'different source commit'):
                publish.require_new_version(REPOSITORY, 'images-v5', 'b' * 40)

    def test_annotated_tag_resolution_is_bounded_and_rejects_noncommit_targets(self):
        annotated = {'object': {'type': 'tag', 'sha': 'b' * 40}}
        commit = {'object': {'type': 'commit', 'sha': SOURCE}}
        with patch.object(publish, 'api', side_effect=[annotated] * 5 + [commit]) as api:
            self.assertEqual(publish.resolve_tag_commit(REPOSITORY, 'images-v5'), SOURCE)
        self.assertEqual(api.call_count, 6)
        with patch.object(publish, 'api', return_value=annotated) as api:
            with self.assertRaisesRegex(ValueError, 'depth'):
                publish.resolve_tag_commit(REPOSITORY, 'images-v5')
        self.assertEqual(api.call_count, 6)
        for target in ({'type': 'tree', 'sha': SOURCE}, {'type': 'commit', 'sha': '../unsafe'}, None):
            with self.subTest(target=target), patch.object(publish, 'api', return_value={'object': target}):
                with self.assertRaises(ValueError):
                    publish.resolve_tag_commit(REPOSITORY, 'images-v5')

    def test_fixed_container_tags_are_checked_across_all_package_version_pages(self):
        version = {'metadata': {'container': {'tags': ['run-123-1']}}}
        occupied = {'metadata': {'container': {'tags': ['images-v5']}}}
        with patch.object(publish, 'api', side_effect=[[version] * 100, [occupied]]) as api:
            with self.assertRaisesRegex(ValueError, 'Fixed container tag already exists'):
                publish.require_unused_image_tag(REPOSITORY, 'Organization', 'shirasu-images', 'images-v5')
        self.assertEqual([call.args[0] for call in api.call_args_list],
                         ['orgs/Example/packages/container/shirasu-images/versions?per_page=100&page=1',
                          'orgs/Example/packages/container/shirasu-images/versions?per_page=100&page=2'])
        with patch.object(publish, 'api', side_effect=[[version] * 100, []]):
            publish.require_unused_image_tag(REPOSITORY, 'User', 'shirasu-images', 'images-v5')
        for response in ({}, [{'metadata': {}}], [{'metadata': {'container': {'tags': None}}}]):
            with self.subTest(response=response), patch.object(publish, 'api', return_value=response):
                with self.assertRaisesRegex(ValueError, 'Cannot check'):
                    publish.require_unused_image_tag(REPOSITORY, 'User', 'shirasu-images', 'images-v5')

    def test_registry_digest_and_config_image_id_are_both_checked(self):
        digest = 'sha256:' + 'f' * 64
        with patch.object(publish, 'run', side_effect=['', 'tag: digest: ' + digest, '', RECEIVER]):
            result = publish.push_image(RECEIVER, 'ghcr.io/example/shirasu-receiver', 'run-123-1')
        self.assertEqual(result, {'reference': 'ghcr.io/example/shirasu-receiver@' + digest,
                                  'image_id': RECEIVER})
        with patch.object(publish, 'run', side_effect=['', 'tag: digest: ' + digest, '', RENDERER]):
            with self.assertRaisesRegex(ValueError, 'differs'):
                publish.push_image(RECEIVER, 'ghcr.io/example/shirasu-receiver', 'run-123-1')

    def test_container_alias_must_preserve_both_recorded_identities(self):
        image = {'reference': 'ghcr.io/example/shirasu-receiver@sha256:' + 'f' * 64,
                 'image_id': RECEIVER}
        with patch.object(publish, 'push_image', return_value=image) as push:
            self.assertEqual(publish.tag_image(image, 'images-v5'), image)
        push.assert_called_once_with(RECEIVER, 'ghcr.io/example/shirasu-receiver', 'images-v5')
        for changed in (dict(image, reference=image['reference'].replace('f' * 64, 'e' * 64)),
                        dict(image, image_id=RENDERER)):
            with self.subTest(changed=changed), patch.object(publish, 'push_image', return_value=changed):
                with self.assertRaisesRegex(ValueError, 'differs from the recorded image'):
                    publish.tag_image(image, 'latest')
        for reference, tag in ((image['reference'].split('@')[0] + ':latest', 'latest'),
                               (image['reference'], '../latest')):
            with self.subTest(reference=reference, tag=tag), patch.object(publish, 'push_image') as push:
                with self.assertRaises(ValueError):
                    publish.tag_image(dict(image, reference=reference), tag)
                push.assert_not_called()

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

    def test_release_tag_must_match_the_candidate_version_before_docker_or_api(self):
        root = self.fixture()
        with patch.dict(publish.os.environ, {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/tags/images-v6'}), \
                patch.object(publish, 'api') as api, patch.object(publish, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'candidate version'):
                publish.publish(root, 'receiver:test', 'renderer:test')
            api.assert_not_called()
            run.assert_not_called()

    def test_wrong_cpu_platform_cannot_publish_an_unusable_receiver(self):
        root = self.fixture()
        with patch.object(publish, 'run', side_effect=[RENDERER, 'linux/amd64']), \
                patch.object(publish, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'receiver platform'):
                publish.publish(root, 'receiver:test', 'renderer:test')
            api.assert_not_called()

    def simulate(self, root, *, fail_upload=False, tag_push=False, main_status='ahead',
                 existing_image_tag=False, expected_error=None):
        calls, commands, uploads = [], [], []

        def api(path, **kwargs):
            calls.append((path, kwargs))
            if '/git/ref/' in path:
                return {'object': {'type': 'commit', 'sha': SOURCE}} if tag_push else None
            if '/releases/tags/' in path:
                return None
            if '/compare/' in path:
                return {'status': main_status}
            if '/releases?' in path:
                return []
            if '/versions?' in path:
                return ([{'metadata': {'container': {'tags': ['images-v5-r1']}}}]
                        if existing_image_tag else [])
            if '/packages/' in path:
                return {'visibility': 'public', 'repository': {'full_name': REPOSITORY}}
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
                if command[-2] == '{{.Os}}/{{.Architecture}}':
                    return 'linux/arm64'
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

        environment = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/tags/images-v5-r1'} if tag_push else {}
        with patch.dict(publish.os.environ, environment), patch.object(publish, 'api', side_effect=api), \
                patch.object(publish, 'run', side_effect=run):
            if fail_upload or expected_error:
                with self.assertRaisesRegex(ValueError, expected_error or 'upload failure'):
                    publish.publish(root, 'receiver:test', 'renderer:test')
            else:
                result = publish.publish(root, 'receiver:test', 'renderer:test')
                self.assertEqual(result, uploads[0])
                self.assertEqual(result['schema_version'], 2)
                self.assertEqual(result['version'], 'v5')
                self.assertEqual(result['release_tag'], 'images-v5-r1')
                self.assertEqual(result['platform'], 'linux/arm64')
                self.assertNotEqual(result['images']['receiver']['reference'].split('@')[1], RECEIVER)
        return calls, commands

    def test_publish_uses_tested_images_and_only_finalizes_after_receipt_upload(self):
        calls, commands = self.simulate(self.fixture())
        self.assertEqual(calls[-1], ('repos/' + REPOSITORY + '/releases/42',
                                    {'method': 'PATCH', 'body': {'draft': False}}))
        self.assertFalse(any(command[:2] == ['docker', 'build'] for command in commands))
        self.assertFalse(any('--clobber' in command for command in commands))
        pushed = [command[-1] for command in commands if command[:2] == ['docker', 'push']]
        self.assertCountEqual(pushed, ['ghcr.io/example/shirasu-' + role + ':' + tag
                                      for role in ('receiver', 'images')
                                      for tag in ('run-123-1', 'images-v5-r1')])
        self.assertFalse(any(':latest' in command[-1] for command in commands))

    def test_tag_release_source_must_be_reachable_from_main_before_publication(self):
        for status in ('ahead', 'identical'):
            with self.subTest(status=status):
                calls, _ = self.simulate(self.fixture(), tag_push=True, main_status=status)
                self.assertIn(('repos/' + REPOSITORY + '/compare/' + SOURCE + '...main', {}), calls)
        for status in ('behind', 'diverged', 'unknown', None):
            with self.subTest(status=status):
                calls, commands = self.simulate(self.fixture(), tag_push=True, main_status=status,
                                                expected_error='must belong to main')
                self.assertFalse(any(options.get('method') == 'POST' for _, options in calls))
                self.assertFalse(any(command[:2] == ['docker', 'push'] for command in commands))

    def test_preexisting_fixed_container_tag_stops_before_payload_upload(self):
        calls, commands = self.simulate(self.fixture(), existing_image_tag=True,
                                       expected_error='Fixed container tag already exists')
        self.assertFalse(any(options.get('method') == 'POST' for _, options in calls))
        self.assertFalse(any(command[:2] == ['docker', 'push'] for command in commands))

    def test_failed_upload_keeps_reserved_draft_and_does_not_publish(self):
        calls, _ = self.simulate(self.fixture(), fail_upload=True)
        self.assertFalse(any(options.get('method') == 'PATCH' for _, options in calls))

    def test_published_revision_is_passed_to_the_restore_job(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            receipt = {'release_tag': 'images-v5-r1'}
            with patch.dict(publish.os.environ, {'GITHUB_OUTPUT': str(output)}), \
                    patch.object(publish, 'publish', return_value=receipt), patch('builtins.print'):
                self.assertEqual(publish.main(['--check-root', directory, '--receiver-image', 'receiver',
                                               '--renderer-image', 'images']), 0)
            self.assertEqual(output.read_text(), 'release_tag=images-v5-r1\n')


if __name__ == '__main__':
    unittest.main()
