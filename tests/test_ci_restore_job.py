"""Only a restored, current formal release may advance the latest image tags."""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_release_candidate as candidate
from scripts import verify_ci_restore as check


SOURCE = 'a' * 40
REPOSITORY = 'Example/Shirasu'
IDS = {'receiver': 'sha256:' + '1' * 64, 'renderer': 'sha256:' + '2' * 64}


class RestoreJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ci-restore-job-')
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
        self.manifest = {'schema_version': 1, 'candidate_version': 'v5', 'rollback_version': 'v4',
                         'source': {'commit': SOURCE, 'working_tree': False}, 'files': files}
        self.raw = (json.dumps(self.manifest, sort_keys=True) + '\n').encode()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        (self.fixture / candidate.MANIFEST_NAME).write_bytes(self.raw)
        self.manifest_path = self.root / candidate.MANIFEST_NAME
        self.manifest_path.write_bytes(self.raw)
        self.receipt = {
            'schema_version': 1, 'version': 'v5', 'source_commit': SOURCE,
            'candidate_manifest_sha256': self.digest,
            'run': {'repository': REPOSITORY, 'id': '123', 'attempt': '1'},
            'images': {
                'receiver': {'reference': 'ghcr.io/example/shirasu-receiver@sha256:' + '3' * 64,
                             'image_id': IDS['receiver']},
                'renderer': {'reference': 'ghcr.io/example/shirasu-images@sha256:' + '4' * 64,
                             'image_id': IDS['renderer']},
            },
        }
        self.receipt_path = self.root / 'release.json'
        self.receipt_path.write_text(json.dumps(self.receipt))
        self.output = self.root / 'verification'
        self.ctx = {'repository': REPOSITORY, 'sha': SOURCE, 'id': '123', 'attempt': '1',
                    'tag': 'images-v5'}
        self.release = {'id': 42, 'tag_name': 'images-v5', 'target_commitish': SOURCE,
                        'draft': False, 'prerelease': False}
        self.latest = {'id': 42, 'tag_name': 'images-v5'}
        self.operations = []
        self.context = self.enterContext(patch.object(check.publication, 'context', return_value=self.ctx))
        self.api = self.enterContext(patch.object(check.publication, 'api', side_effect=self.read_api))
        self.tag_commit = self.enterContext(patch.object(check.publication, 'resolve_tag_commit',
                                                         return_value=SOURCE))
        self.restore = self.enterContext(patch.object(check, 'restore', side_effect=self.extract))
        self.compose = self.enterContext(patch.object(check, 'prepare_compose', side_effect=self.make_compose))
        self.private = self.enterContext(patch.object(check.publication, 'ensure_private',
                                                      side_effect=lambda *args: self.operations.append('private')))
        self.alias = self.enterContext(patch.object(check.publication, 'tag_image', side_effect=self.tag_image))

    def read_api(self, path):
        self.operations.append('api:' + path)
        if path.endswith('/releases/tags/images-v5'):
            return self.release
        if path.endswith('/releases/latest'):
            return self.latest
        self.assertEqual(path, 'repos/' + REPOSITORY)
        return {'full_name': REPOSITORY, 'owner': {'type': 'User'}}

    def extract(self, receipt, manifest, output):
        self.operations.append('restore')
        self.assertEqual((receipt, manifest), (self.receipt_path, self.manifest_path))
        self.assertFalse(output.exists())
        shutil.copytree(self.fixture, output)
        return {'candidate_manifest_sha256': self.digest, 'source_commit': SOURCE,
                'images': self.receipt['images']}

    def make_compose(self, bundle, image_id, output):
        self.operations.append('compose')
        self.assertEqual(bundle, self.output / 'candidate')
        self.assertEqual(image_id, IDS['receiver'])
        output.write_text('# fixture compose override\n')
        return {'bundle_manifest_sha256': self.digest, 'image_id': image_id, 'compose_file': str(output)}

    def tag_image(self, image, tag):
        self.operations.append('alias')
        self.assertEqual(tag, 'latest')
        self.assertIn('compose', self.operations)
        self.assertEqual(self.operations.count('private'), 2)
        self.assertEqual(self.operations.count('api:repos/' + REPOSITORY + '/releases/latest'), 2)
        return image

    def verify(self):
        return check.verify(self.receipt_path, self.manifest_path, self.output)

    def failed_status(self, stage):
        value = json.loads((self.output / 'restore-status.json').read_text())
        self.assertEqual(value['state'], 'failed')
        self.assertEqual(value['stage'], stage)
        self.assertFalse((self.output / 'restore-result.json').exists())
        return value

    def test_restored_manifest_and_compose_pass_before_latest_and_public_report(self):
        result = self.verify()
        self.assertEqual(result['file_count'], len(self.manifest['files']))
        self.assertEqual(result['candidate_manifest_sha256'], self.digest)
        self.assertEqual(result['images'], self.receipt['images'])
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(result['aliases'], {'receiver': 'ghcr.io/example/shirasu-receiver:latest',
                                             'renderer': 'ghcr.io/example/shirasu-images:latest'})
        self.assertEqual(json.loads((self.output / 'restore-result.json').read_text()), result)
        self.assertEqual(json.loads((self.output / 'restore-status.json').read_text())['state'], 'success')
        self.assertNotIn('files', result)
        self.assertNotIn(str(self.output), json.dumps(result))
        self.assertNotIn('fixture ', json.dumps(result))
        self.assertEqual(candidate.verify_bundle(self.output / 'candidate')['files'], self.manifest['files'])
        self.assertEqual(self.alias.call_count, 2)

    def test_context_and_metadata_mismatches_fail_before_network_or_output(self):
        for changed in ({'sha': 'b' * 40}, {'repository': 'Someone/Shirasu'}, {'id': '124'},
                        {'attempt': '0'}, {'tag': 'images-v6'}):
            with self.subTest(changed=changed):
                self.context.return_value = dict(self.ctx, **changed)
                with self.assertRaises(ValueError):
                    self.verify()
                self.assertFalse(self.output.exists())
        self.api.assert_not_called()
        self.restore.assert_not_called()
        self.alias.assert_not_called()

    def test_restore_job_retry_accepts_same_run_with_later_attempt(self):
        self.context.return_value = dict(self.ctx, attempt='2')
        result = self.verify()
        self.assertEqual(result['publication_run']['attempt'], '1')
        self.assertEqual(result['run']['attempt'], '2')

    def test_existing_git_tag_supplies_source_when_target_commitish_is_a_branch(self):
        self.release['target_commitish'] = 'main'
        result = self.verify()
        self.assertEqual(result['source_commit'], SOURCE)
        self.assertEqual(self.tag_commit.call_args.args, (REPOSITORY, 'images-v5'))

    def test_wrong_or_unpublished_release_and_moved_git_tag_are_rejected(self):
        original = copy.deepcopy(self.release)
        for changed in ({'id': 0}, {'tag_name': 'images-v4'},
                        {'draft': True}, {'prerelease': True}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.release = dict(original, **changed)
                self.verify()
        self.release = original
        self.tag_commit.return_value = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'Git tag differs'):
            self.verify()
        self.restore.assert_not_called()
        self.alias.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_older_release_cannot_move_latest_backwards(self):
        self.latest = {'id': 43, 'tag_name': 'images-v6'}
        with self.assertRaisesRegex(ValueError, 'Only the latest'):
            self.verify()
        self.restore.assert_not_called()
        self.alias.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_failed_pull_leaves_only_status_and_never_advances_latest(self):
        self.restore.side_effect = ValueError('registry unavailable')
        with self.assertRaisesRegex(ValueError, 'registry unavailable'):
            self.verify()
        self.failed_status('restore')
        self.assertEqual({p.name for p in self.output.iterdir()}, {'restore-status.json'})
        self.compose.assert_not_called()
        self.alias.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.verify()

    def test_modified_restored_files_fail_before_compose_or_latest(self):
        def changed(*args):
            result = self.extract(*args)
            (self.output / 'candidate/receiver/main.py').write_text('changed')
            return result
        self.restore.side_effect = changed
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            self.verify()
        self.failed_status('restore')
        self.compose.assert_not_called()
        self.alias.assert_not_called()

    def test_compose_failure_preserves_restored_evidence_without_latest(self):
        self.compose.side_effect = ValueError('wrong startup')
        with self.assertRaisesRegex(ValueError, 'wrong startup'):
            self.verify()
        self.failed_status('compose')
        self.assertEqual(candidate.verify_bundle(self.output / 'candidate')['manifest_sha256'], self.digest)
        self.private.assert_not_called()
        self.alias.assert_not_called()

    def test_privacy_or_newer_release_change_during_restore_blocks_both_aliases(self):
        self.private.side_effect = ValueError('package became public')
        with self.assertRaisesRegex(ValueError, 'became public'):
            self.verify()
        self.failed_status('latest')
        self.alias.assert_not_called()
        second_output = self.root / 'another-verification'
        self.output = second_output
        self.private.side_effect = lambda *args: self.operations.append('private')
        def superseded(*args):
            result = self.make_compose(*args)
            self.latest = {'id': 43, 'tag_name': 'images-v6'}
            return result
        self.compose.side_effect = superseded
        with self.assertRaisesRegex(ValueError, 'Only the latest'):
            self.verify()
        self.failed_status('latest')
        self.alias.assert_not_called()

    def test_partial_alias_failure_is_recorded_without_a_success_report(self):
        self.alias.side_effect = [self.receipt['images']['receiver'], ValueError('registry write failed')]
        with self.assertRaisesRegex(ValueError, 'registry write failed'):
            self.verify()
        status = self.failed_status('latest')
        self.assertEqual(status['completed_aliases'], {'receiver': 'ghcr.io/example/shirasu-receiver:latest'})
        self.assertTrue((self.output / 'candidate').is_dir())

    def test_existing_or_linked_paths_never_overwrite_or_start_network_calls(self):
        self.output.mkdir()
        (self.output / 'keep').write_text('preserved')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.verify()
        self.assertEqual((self.output / 'keep').read_text(), 'preserved')
        link = self.root / 'linked'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Linked verification'):
            check.verify(self.receipt_path, self.manifest_path, link / 'new-output')
        metadata_link = self.root / 'linked-receipt.json'
        metadata_link.symlink_to(self.receipt_path)
        with self.assertRaisesRegex(ValueError, 'unlinked release metadata'):
            check.verify(metadata_link, self.manifest_path, self.root / 'new-output')
        self.api.assert_not_called()
        self.restore.assert_not_called()


if __name__ == '__main__':
    unittest.main()
