"""Only a matching, restored release may become a Vercel deployment input."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_vercel_release as release


REPOSITORY = 'Example/Shirasu'
TAG = 'images-v5-r1'
SOURCE = 'a' * 40


class MetadataFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='vercel-prepare-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.metadata = self.root / 'metadata'
        self.metadata.mkdir()
        self.manifest = {'schema_version': 1, 'candidate_version': 'v5',
                         'source': {'commit': SOURCE, 'working_tree': False},
                         'files': {'receiver/main.py': '1' * 64, 'images/image_service/app.py': '2' * 64}}
        self.raw = json.dumps(self.manifest, sort_keys=True).encode()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.receipt = {
            'schema_version': 2, 'version': 'v5', 'release_tag': TAG, 'platform': 'linux/arm64',
            'source_commit': SOURCE, 'candidate_manifest_sha256': self.digest,
            'run': {'repository': REPOSITORY, 'id': '123', 'attempt': '1'},
            'images': {
                'receiver': {'reference': 'ghcr.io/example/shirasu-receiver@sha256:' + '3' * 64,
                             'image_id': 'sha256:' + '4' * 64},
                'renderer': {'reference': 'ghcr.io/example/shirasu-images@sha256:' + '5' * 64,
                             'image_id': 'sha256:' + '6' * 64},
            },
        }
        self.result = {name: copy.deepcopy(self.receipt[name]) for name in
                       ('version', 'source_commit', 'release_tag', 'platform',
                        'candidate_manifest_sha256', 'images')}
        self.result.update(schema_version=1, mode='ci-restore', publication_run=self.receipt['run'],
                           file_count=2, checks={name: True for name in release.RESTORE_CHECKS})
        (self.metadata / 'candidate-manifest.json').write_bytes(self.raw)
        self.write('release.json', self.receipt)
        self.write('restore-result.json', self.result)

    def write(self, name, data):
        (self.metadata / name).write_text(json.dumps(data), encoding='utf-8')


class MetadataTests(MetadataFixture):
    def test_saved_revision_is_accepted_without_requiring_new_runtime_features(self):
        self.assertEqual(release.validate_metadata(self.metadata, TAG, REPOSITORY), self.receipt)

    def test_different_requested_repository_or_tag_is_rejected(self):
        for tag, repository in ((TAG, 'Other/Shirasu'), ('images-v5-r2', REPOSITORY),
                                ('images-v6-r1', REPOSITORY), ('refs/tags/' + TAG, REPOSITORY)):
            with self.subTest(tag=tag, repository=repository), self.assertRaises(ValueError):
                release.validate_metadata(self.metadata, tag, repository)

    def test_manifest_bytes_remain_bound_to_receipt(self):
        (self.metadata / 'candidate-manifest.json').write_bytes(self.raw + b' ')
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            release.validate_metadata(self.metadata, TAG, REPOSITORY)

    def test_restore_identity_must_match_every_recorded_release_field(self):
        changed = {'version': 'v4', 'source_commit': 'b' * 40, 'release_tag': 'images-v5-r2',
                   'platform': 'linux/amd64', 'candidate_manifest_sha256': '0' * 64,
                   'images': {}, 'publication_run': dict(self.receipt['run'], id='124'),
                   'file_count': 1}
        for name, value in changed.items():
            with self.subTest(field=name):
                self.write('restore-result.json', dict(self.result, **{name: value}))
                with self.assertRaises(ValueError):
                    release.validate_metadata(self.metadata, TAG, REPOSITORY)

    def test_all_restore_checks_must_have_passed_as_boolean_true(self):
        for name in release.RESTORE_CHECKS:
            for value in (False, None, 1):
                with self.subTest(check=name, value=value):
                    result = copy.deepcopy(self.result)
                    result['checks'][name] = value
                    self.write('restore-result.json', result)
                    with self.assertRaisesRegex(ValueError, 'restoration checks'):
                        release.validate_metadata(self.metadata, TAG, REPOSITORY)
            result = copy.deepcopy(self.result)
            del result['checks'][name]
            self.write('restore-result.json', result)
            with self.subTest(missing=name), self.assertRaises(ValueError):
                release.validate_metadata(self.metadata, TAG, REPOSITORY)

    def test_missing_or_malformed_restore_report_fails_closed(self):
        invalid = [None, [], {}, dict(self.result, mode='local-restore'),
                   dict(self.result, schema_version=True), dict(self.result, schema_version=1.0),
                   dict(self.result, file_count=2.0), dict(self.result, checks=None),
                   dict(self.result, checks=[]), dict(self.result, checks=True)]
        for value in invalid:
            with self.subTest(value=value):
                self.write('restore-result.json', value)
                with self.assertRaises(ValueError):
                    release.validate_metadata(self.metadata, TAG, REPOSITORY)
        (self.metadata / 'restore-result.json').unlink()
        with self.assertRaises(ValueError):
            release.validate_metadata(self.metadata, TAG, REPOSITORY)

    def test_linked_metadata_files_and_parents_are_rejected(self):
        path = self.metadata / 'release.json'
        outside = self.root / 'receipt.json'
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            release.validate_metadata(self.metadata, TAG, REPOSITORY)
        path.unlink()
        outside.rename(path)
        linked = self.root / 'linked'
        linked.symlink_to(self.metadata, target_is_directory=True)
        with self.assertRaises(ValueError):
            release.validate_metadata(linked, TAG, REPOSITORY)


class PublicArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='vercel-public-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / 'public.tar.gz'
        self.output = self.root / 'public'

    def archive_entries(self, entries):
        with tarfile.open(self.archive, 'w:gz') as stream:
            for name, kind, data in entries:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.size = len(data) if kind == tarfile.REGTYPE else 0
                entry.linkname = '../outside' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                stream.addfile(entry, io.BytesIO(data) if kind == tarfile.REGTYPE else None)

    def test_regular_files_are_verified_after_extraction(self):
        self.archive_entries([('public-manifest.json', tarfile.REGTYPE, b'{}'),
                              ('results/known.png', tarfile.REGTYPE, b'image')])
        with patch.object(release, 'verify_public', return_value={'checked': True}) as verify:
            self.assertEqual(release.extract_public(self.archive, self.output), {'checked': True})
        self.assertEqual((self.output / 'results/known.png').read_bytes(), b'image')
        verify.assert_called_once_with(self.output)

    def test_traversal_absolute_and_ambiguous_paths_fail_before_any_extraction(self):
        for name in ('../outside', '/absolute', 'results/../../outside', 'results//bad',
                     './bad', 'results/./bad', 'results\\bad', 'results/bad\nname'):
            with self.subTest(name=name):
                self.archive_entries([('valid.json', tarfile.REGTYPE, b'{}'),
                                      (name, tarfile.REGTYPE, b'bad')])
                with patch.object(release, 'verify_public') as verify, self.assertRaises(ValueError):
                    release.extract_public(self.archive, self.output)
                self.assertFalse(self.output.exists())
                self.assertFalse((self.root / 'outside').exists())
                verify.assert_not_called()

    def test_links_special_files_and_duplicates_are_rejected_before_extraction(self):
        entries = [[('results/item', kind, b'')] for kind in
                   (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE)]
        entries.append([('same.json', tarfile.REGTYPE, b'one'), ('same.json', tarfile.REGTYPE, b'two')])
        for items in entries:
            with self.subTest(entries=items):
                self.archive_entries(items)
                with patch.object(release, 'verify_public') as verify, self.assertRaises(ValueError):
                    release.extract_public(self.archive, self.output)
                self.assertFalse(self.output.exists())
                verify.assert_not_called()

    def test_linked_archive_or_output_parents_cannot_redirect_files(self):
        self.archive_entries([('file.json', tarfile.REGTYPE, b'{}')])
        linked_archive = self.root / 'linked.tar.gz'
        linked_archive.symlink_to(self.archive)
        with self.assertRaises(ValueError):
            release.extract_public(linked_archive, self.output)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        linked_parent = self.root / 'linked'
        linked_parent.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            release.extract_public(self.archive, linked_parent / 'public')
        self.assertEqual(list(elsewhere.iterdir()), [])


class PreparationTests(MetadataFixture):
    def setUp(self):
        super().setUp()
        self.output = self.root / 'prepared'
        self.environment = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': REPOSITORY,
                            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
                            'GITHUB_SHA': 'b' * 40}
        self.evidence = {'candidate_manifest_sha256': self.digest, 'source_commit': SOURCE,
                         'source_dirty': False}
        self.current = self.enterContext(patch.object(release, 'current_release'))
        self.git = self.enterContext(patch.object(release.subprocess, 'run',
                         return_value=subprocess.CompletedProcess([], 0)))
        self.restore = self.enterContext(patch.object(release, 'restore', side_effect=self.fake_restore))
        self.extract = self.enterContext(patch.object(release, 'extract_public', side_effect=self.fake_extract))
        self.verify = self.enterContext(patch.object(release, 'verify_bundle',
                                       return_value={'manifest_sha256': self.digest}))

    def fake_restore(self, receipt, manifest, output):
        self.assertEqual(receipt, self.metadata / 'release.json')
        self.assertEqual(manifest, self.metadata / 'candidate-manifest.json')
        self.anonymous_config = Path(os.environ['DOCKER_CONFIG'])
        self.assertTrue(self.anonymous_config.is_dir())
        self.assertEqual(list(self.anonymous_config.iterdir()), [])
        output.mkdir()
        (output / 'restored').write_text('fixed image bytes')

    def fake_extract(self, archive, output):
        self.assertEqual(archive, self.metadata / 'public-verification.tar.gz')
        output.mkdir()
        (output / 'verified').write_text('public evidence')
        return self.evidence

    def prepare(self, **changes):
        return release.prepare(self.metadata, self.output, TAG,
                               environment=dict(self.environment, **changes))

    def test_manual_main_can_prepare_older_saved_source_using_current_workflow(self):
        with patch.dict(os.environ, {'DOCKER_CONFIG': '/preserve-existing-config'}):
            result = self.prepare()
            self.assertEqual(os.environ['DOCKER_CONFIG'], '/preserve-existing-config')
        self.assertEqual(result['source_commit'], SOURCE)
        self.assertEqual(result['release_tag'], TAG)
        self.assertEqual(result['candidate_manifest_sha256'], self.digest)
        self.current.assert_called_once_with(REPOSITORY, TAG, SOURCE)
        self.assertEqual(self.git.call_args.args[0],
                         ['git', 'merge-base', '--is-ancestor', SOURCE, 'refs/remotes/origin/main'])
        self.assertTrue((self.output / 'candidate/restored').is_file())
        self.assertTrue((self.output / 'public/verified').is_file())
        self.assertFalse(self.anonymous_config.exists())
        self.assertFalse(list(self.root.glob('.vercel-release-*')))

    def test_tag_push_must_point_to_the_saved_release_source(self):
        options = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/tags/' + TAG}
        with self.assertRaisesRegex(ValueError, 'Tag push differs'):
            self.prepare(**options)
        self.restore.assert_not_called()
        self.prepare(**options, GITHUB_SHA=SOURCE)
        self.restore.assert_called_once()

    def test_pull_requests_and_other_untrusted_events_are_rejected_before_restoring(self):
        invalid = [dict(GITHUB_EVENT_NAME='pull_request', GITHUB_REF='refs/pull/1/merge'),
                   dict(GITHUB_EVENT_NAME='pull_request_target'), dict(GITHUB_EVENT_NAME='push'),
                   dict(GITHUB_REF='refs/heads/topic'), dict(GITHUB_ACTIONS='false'),
                   dict(GITHUB_REPOSITORY='../outside'),
                   dict(GITHUB_EVENT_NAME='push', GITHUB_REF='refs/tags/images-v5-r2')]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare(**changes)
        self.current.assert_not_called()
        self.restore.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_release_lookup_and_main_ancestry_fail_before_docker(self):
        self.current.side_effect = ValueError('Release is no longer current')
        with self.assertRaises(ValueError):
            self.prepare()
        self.current.side_effect = None
        self.git.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaisesRegex(ValueError, 'main history'):
            self.prepare()
        self.restore.assert_not_called()

    def test_mismatched_or_dirty_public_evidence_discards_staging(self):
        for name, value in (('candidate_manifest_sha256', '0' * 64),
                            ('source_commit', 'c' * 40), ('source_dirty', True)):
            with self.subTest(name=name):
                original = self.evidence[name]
                self.evidence[name] = value
                with self.assertRaisesRegex(ValueError, 'Public HTTP evidence'):
                    self.prepare()
                self.evidence[name] = original
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob('.vercel-release-*')))

    def test_changed_candidate_or_restore_failure_discards_staging_and_anonymous_config(self):
        self.verify.return_value = {'manifest_sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'Restored candidate changed'):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.vercel-release-*')))
        self.assertFalse(self.anonymous_config.exists())
        self.restore.side_effect = ValueError('Pull failed')
        with patch.dict(os.environ, {'DOCKER_CONFIG': '/preserve-existing-config'}):
            with self.assertRaisesRegex(ValueError, 'Pull failed'):
                self.prepare()
            self.assertEqual(os.environ['DOCKER_CONFIG'], '/preserve-existing-config')
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.vercel-release-*')))


if __name__ == '__main__':
    unittest.main()
