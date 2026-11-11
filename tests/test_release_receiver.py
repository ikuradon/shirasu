"""Candidate receiver checks use real isolated code and preserve signed retries."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.prepare_release_candidate import prepare, verify_bundle


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/check_release_receiver.py'
HAS_CANDIDATE_SOURCE = (ROOT / 'image_service/assets/preview/flood.geojson.zlib').is_file()
HAS_SDK = importlib.util.find_spec('nostr_sdk') is not None


@unittest.skipUnless(HAS_CANDIDATE_SOURCE and HAS_SDK,
                     'Use the receiver test container with the source fixtures mounted read-only')
class ReleaseReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix='shirasu-release-receiver-tests-')
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.bundle = cls.root / 'candidate'
        prepare(cls.bundle)
        cls.manifest = verify_bundle(cls.bundle)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shirasu-release-export-tests-')
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / 'posts.json'

    def run_cli(self, *, output=None, source_first=False):
        paths = [self.bundle / 'receiver', ROOT, ROOT / 'tests']
        if source_first:
            paths = [ROOT, self.bundle / 'receiver', ROOT / 'tests']
        return subprocess.run(
            [sys.executable, '-B', str(SCRIPT), '--bundle', str(self.bundle),
             '--output', str(self.output if output is None else output)],
            cwd=self.output.parent, env={'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
                                         'PYTHONPATH': os.pathsep.join(str(path) for path in paths)},
            text=True, capture_output=True, timeout=60, check=False)

    def test_export_uses_real_candidate_and_preserves_every_retry_after_version_changes(self):
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(self.output.read_text())
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['bundle_manifest_sha256'], self.manifest['manifest_sha256'])
        self.assertEqual((result['candidate_version'], result['rollback_version']), ('v5', 'v4'))
        self.assertEqual((result['case_count'], result['post_count']), (40, 46))
        self.assertEqual(len(result['posts']), 46)
        for post in result['posts']:
            self.assertEqual(set(post['event']), {'id', 'pubkey', 'created_at', 'kind', 'tags', 'content', 'sig'})
            self.assertTrue(post['url'].startswith('https://images.example.test/v5/rehearsal/'))
            self.assertIn('url ' + post['url'], post['event']['tags'][0])
            self.assertIn('alt ' + post['alt'], post['event']['tags'][0])
            self.assertIn(post['url'], post['event']['content'])
            self.assertTrue(post['image_record_indexes'])
            self.assertTrue(set(post['image_record_indexes']) <= set(post['record_indexes']))
            if post['kind'] == 'nankai':
                self.assertEqual(len(post['image_record_indexes']), 1)
        changes = {post['label']: post for post in result['transition_posts']}
        for original, retry in (('new-1-v4', 'retry-2'), ('new-2-v5', 'retry-3'),
                                ('new-3-v4', 'retry-4'), ('new-4-images-off', 'retry-images-off')):
            self.assertEqual(changes[original]['event'], changes[retry]['event'])
            self.assertEqual(changes[original]['url'], changes[retry]['url'])
            self.assertEqual(changes[original]['alt'], changes[retry]['alt'])
        self.assertIn('/v4/', changes['new-1-v4']['url'])
        self.assertIn('/v5/', changes['new-2-v5']['url'])
        self.assertIn('/v4/', changes['new-3-v4']['url'])
        self.assertIsNone(changes['new-4-images-off']['url'])
        self.assertIsNone(changes['v5-missing']['url'])
        self.assertIn('/v5/', changes['v5-late-complete']['url'])
        self.assertIn('/v5/', changes['v5-cancel']['url'])
        for label, post in changes.items():
            if label.startswith(('legacy-', 'v4-', 'images-off-')):
                self.assertIsNone(post['url'])
        self.assertEqual(result['checks']['version_sequence'], ['v4', 'v5', 'v4', None])
        self.assertEqual(result['checks']['network_connections'], 0)
        self.assertEqual(verify_bundle(self.bundle), self.manifest)

    def test_checkout_modules_cannot_substitute_for_the_candidate(self):
        process = self.run_cli(source_first=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('module is outside the candidate: main', process.stderr)
        self.assertFalse(self.output.exists())

    def test_existing_outputs_and_writes_inside_the_candidate_are_rejected(self):
        self.output.write_text('keep existing result')
        for output in (self.output, self.bundle / 'receiver' / 'result.json'):
            with self.subTest(output=output):
                process = self.run_cli(output=output)
                self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.output.read_text(), 'keep existing result')
        self.assertEqual(verify_bundle(self.bundle), self.manifest)

    def test_tampered_candidate_fails_before_any_events_are_exported(self):
        target = self.bundle / 'receiver' / 'image_versions.py'
        original = target.read_bytes()
        try:
            target.write_bytes(original + b'\n# unverified change\n')
            process = self.run_cli()
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse(self.output.exists())
        finally:
            target.write_bytes(original)
        self.assertEqual(verify_bundle(self.bundle), self.manifest)


if __name__ == '__main__':
    unittest.main()
