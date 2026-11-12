"""Candidate artifacts are explicit snapshots, isolated from normal releases."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.release import prepare_image_service as images
from tools.release import prepare_release_candidate as candidate


ROOT = Path(__file__).resolve().parents[2]
SOURCE = {'commit': 'a' * 40, 'working_tree': True}
ENTRY = ('$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05',
         1794092400123)


class ReleaseCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='shirasu-candidate-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.output = self.root / 'candidate'
        names = {*images.FILES, *images.PREVIEW_FILES, *candidate.RECEIVER_FILES,
                 'image_service/requirements.txt', 'image_service/Dockerfile'}
        # Exercise the real protocol/profile/import boundary without repeatedly
        # copying large fonts or requiring any image dependencies. Asset bytes
        # are deliberate placeholders; actual rendering is checked separately.
        for name in names:
            destination = self.source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / name).read_bytes() if name.endswith('.py')
                                    else ('fixture: ' + name + '\n').encode())
        for name in ('config.py', '.env', 'state/signing-key.json', 'backups/database.sqlite3'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('must never be packaged\n')
        self.enterContext(patch.object(candidate, 'ROOT', self.source))
        self.enterContext(patch.object(images, 'ROOT', self.source))
        self.enterContext(patch.object(candidate, '_source_info', return_value=SOURCE))

    def make(self):
        return candidate.prepare(self.output)

    def manifest(self):
        return json.loads((self.output / candidate.MANIFEST_NAME).read_text())

    def write_manifest(self, manifest):
        (self.output / candidate.MANIFEST_NAME).write_text(json.dumps(manifest))

    def isolated(self, package, code):
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-c', 'import os, sys; sys.path.insert(0, os.getcwd());\n' + code],
            cwd=package, env={'PATH': os.environ.get('PATH', '')}, capture_output=True,
            text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_exact_allowlist_and_manifest_cover_independent_packages_without_live_files(self):
        self.assertEqual(self.make(), self.output)
        result = candidate.verify_bundle(self.output)
        self.assertEqual(result['source'], SOURCE)
        self.assertEqual((result['schema_version'], result['candidate_version'], result['rollback_version']),
                         (1, 'v5', 'v4'))
        actual = {str(path.relative_to(self.output)) for path in self.output.rglob('*') if path.is_file()}
        self.assertEqual(actual, set(result['files']) | {candidate.MANIFEST_NAME})
        self.assertEqual(result['manifest_sha256'], hashlib.sha256(
            (self.output / candidate.MANIFEST_NAME).read_bytes()).hexdigest())
        for name, digest in result['files'].items():
            self.assertEqual(digest, hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        for package in ('images', 'receiver'):
            self.assertEqual((self.output / package / 'shirasu/images/versions.py').read_text(), candidate.CANDIDATE_PROFILE)
            for name in ('config.py', '.env', 'state', 'backups', '.git'):
                self.assertFalse((self.output / package / name).exists())
        self.assertFalse((self.output / 'receiver/image_service').exists())
        self.assertFalse((self.output / 'images/shirasu/service.py').exists())
        self.assertEqual((self.output / 'receiver/requirements/receiver.txt').read_bytes(),
                         (self.source / 'requirements/receiver.txt').read_bytes())
        self.assertEqual((self.output / 'images/requirements.txt').read_bytes(),
                         (self.source / 'image_service/requirements.txt').read_bytes())

    def test_retained_flat_bundle_still_verifies_without_rewriting_it(self):
        output = self.root / 'retained'
        files = {}
        for name in sorted(candidate._names(legacy=True)):
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            content = (candidate.CANDIDATE_PROFILE.encode() if name.endswith('/image_versions.py')
                       else ('retained: ' + name).encode())
            target.write_bytes(content)
            files[name] = hashlib.sha256(content).hexdigest()
        manifest = {'schema_version': 1, 'candidate_version': 'v5', 'rollback_version': 'v4',
                    'source': SOURCE, 'files': files}
        (output / candidate.MANIFEST_NAME).write_text(json.dumps(manifest))
        checked = candidate.verify_bundle(output)
        self.assertEqual(checked['files'], files)
        # A future package addition must not invalidate an already published
        # snapshot or silently extend its allowlist.
        with patch.object(images, 'FILES', (*images.FILES, 'image_service/new.py')):
            self.assertEqual(candidate.verify_bundle(output), checked)
        extra = output / 'receiver/shirasu/__init__.py'
        extra.parent.mkdir()
        extra.write_text('')
        with self.assertRaises(ValueError):
            candidate.verify_bundle(output)

    def test_normal_package_stays_v1_to_v4_and_candidate_registers_v5_in_both_artifacts(self):
        self.make()
        production = images.prepare(self.root / 'production')
        code = '''import json
from shirasu.images import versions as image_versions, protocol as image_protocol
from image_service.app import RENDERERS
print(json.dumps({'versions': image_protocol.SUPPORTED_VERSIONS, 'routes': RENDERERS,
                  'candidate': image_versions.CANDIDATE_VERSION, 'default': image_protocol.VERSION,
                  'webp': image_versions.WEBP_VERSIONS,
                  'origin': image_versions.__file__}))
'''
        regular = self.isolated(production, code)
        future = self.isolated(self.output / 'images', code)
        self.assertEqual(regular['versions'], ['v1', 'v2', 'v3', 'v4'])
        self.assertIsNone(regular['candidate'])
        self.assertEqual(regular['webp'], [])
        self.assertEqual(future['webp'], ['v5'])
        self.assertEqual(future['versions'], ['v1', 'v2', 'v3', 'v4', 'v5'])
        self.assertEqual(future['routes']['v5'], 'image_service.render_preview')
        for version in regular['versions']:
            self.assertEqual(regular['routes'][version], future['routes'][version])
        self.assertEqual((regular['default'], future['default']), ('v1', 'v1'))
        self.assertEqual(Path(future['origin']), self.output / 'images/shirasu/images/versions.py')
        receiver = self.isolated(self.output / 'receiver', '''import json
from shirasu.images.publication import ImagePublisher
from shirasu.images.protocol import parse_path
from urllib.parse import urlsplit
entries = ''' + repr((ENTRY,)) + '''
key = bytes(range(32))
publishers = [ImagePublisher('https://images.example.test', 'test', key, version=v)
              for v in ('v1', 'v2', 'v3', 'v4', 'v5')]
assert all(parse_path(urlsplit(p.url(entries)).path, {'test': key}) == entries for p in publishers)
webp = ImagePublisher('https://images.example.test', 'test', key, version='v5', encoding='w1')
assert parse_path(urlsplit(webp.url(entries)).path, {'test': key}) == entries
assert webp.url(entries).endswith('.w1.webp')
print(json.dumps({'topics': [p.supports_nankai_topic for p in publishers],
                  'webp_mime': webp.mime,
                  'v5': urlsplit(publishers[-1].url(entries)).path.split('/')[1]}))
''')
        self.assertEqual(receiver, {'topics': [False, False, False, False, True], 'v5': 'v5',
                                    'webp_mime': 'image/webp'})
        self.assertEqual((self.source / 'shirasu/images/versions.py').read_bytes(), (ROOT / 'shirasu/images/versions.py').read_bytes())
        candidate.verify_bundle(self.output)  # Isolated imports created no bytecode or mutable state.

    def test_both_images_include_the_complete_upstream_pyserial_license(self):
        # Exact v3.5 LICENSE.txt, upstream Git blob:
        # https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt
        name = 'licenses/pyserial-3.5-LICENSE.txt'
        raw = (ROOT / name).read_bytes()
        self.assertEqual(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(),
                         '8920d4ee89b8e7ad5ea54eb91d48e33473c3a9a1')
        (self.source / name).write_bytes(raw)
        self.make()
        manifest = candidate.verify_bundle(self.output)
        for package in ('receiver', 'images'):
            path = package + '/' + name
            self.assertEqual((self.output / path).read_bytes(), raw)
            self.assertEqual(manifest['files'][path],
                             'f91cb9813de6a5b142b8f7f2dede630b5134160aedaeaf55f4d6a7e2593ca3f3')

    def test_later_working_tree_edits_cannot_change_the_saved_artifact(self):
        self.make()
        before = candidate.verify_bundle(self.output)
        for name in ('image_service/render_preview.py', 'shirasu/images/publication.py', 'shirasu/images/versions.py'):
            with (self.source / name).open('a') as target:
                target.write('\n# Later working change\n')
        self.assertEqual(candidate.verify_bundle(self.output), before)

    def test_existing_destination_and_missing_sources_fail_before_partial_output(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            self.make()
        self.assertEqual(list(self.output.iterdir()), [])
        self.output.rmdir()
        (self.source / 'image_service/assets/preview/flood.geojson.zlib').unlink()
        with self.assertRaisesRegex(ValueError, 'prepare_vendor_assets.py'):
            self.make()
        self.assertFalse(self.output.exists())

    def test_missing_published_asset_explains_offline_preparation(self):
        (self.source / 'image_service/assets/v1/land.geojson').unlink()
        with self.assertRaisesRegex(ValueError, 'prepare_vendor_assets.py'):
            self.make()
        self.assertFalse(self.output.exists())

    def test_source_symlinks_and_symlink_destination_are_rejected(self):
        source = self.source / 'shirasu/images/nankai.py'
        source.unlink()
        source.symlink_to(ROOT / 'shirasu/images/nankai.py')
        with self.assertRaisesRegex(ValueError, 'Linked source'):
            self.make()
        self.assertFalse(self.output.exists())
        self.output.symlink_to(self.root / 'nonexistent')
        with self.assertRaises(FileExistsError):
            self.make()

    def test_changed_missing_extra_and_linked_files_are_all_rejected(self):
        self.make()
        target = self.output / 'receiver/shirasu/service.py'
        original = target.read_bytes()
        target.write_bytes(original + b'\n# modified\n')
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            candidate.verify_bundle(self.output)
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'missing declared'):
            candidate.verify_bundle(self.output)
        target.symlink_to(self.source / 'shirasu/service.py')
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            candidate.verify_bundle(self.output)
        target.unlink()
        target.write_bytes(original)
        unexpected = self.output / 'receiver/.env'
        unexpected.write_text('unexpected')
        with self.assertRaisesRegex(ValueError, 'unexpected file'):
            candidate.verify_bundle(self.output)
        unexpected.unlink()
        unexpected.mkdir()
        with self.assertRaisesRegex(ValueError, 'unexpected directory'):
            candidate.verify_bundle(self.output)

    def test_manifest_cannot_add_paths_drop_required_files_or_forge_profile(self):
        self.make()
        original = self.manifest()
        for unsafe in ('../outside', '/absolute', 'images//bad', 'images/../bad', 'images/.'):
            with self.subTest(path=unsafe):
                altered = {**original, 'files': {**original['files'], unsafe: '0' * 64}}
                self.write_manifest(altered)
                with self.assertRaisesRegex(ValueError, 'unsafe file path'):
                    candidate.verify_bundle(self.output)
        files = dict(original['files'])
        del files['receiver/shirasu/service.py']
        self.write_manifest({**original, 'files': files})
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            candidate.verify_bundle(self.output)
        target = self.output / 'receiver/shirasu/images/versions.py'
        target.write_text('NANKAI_TOPIC_VERSIONS = ()\n')
        original['files']['receiver/shirasu/images/versions.py'] = hashlib.sha256(target.read_bytes()).hexdigest()
        self.write_manifest(original)
        with self.assertRaisesRegex(ValueError, 'generation profile'):
            candidate.verify_bundle(self.output)

    def test_manifest_types_duplicate_keys_and_excess_size_fail_closed(self):
        self.make()
        original = self.manifest()
        for changed in ({'schema_version': True}, {'candidate_version': 'v6'},
                        {'rollback_version': 'v5'}, {'files': []},
                        {'source': {'commit': 'a' * 40, 'working_tree': 1}},
                        {'source': {'commit': None, 'working_tree': False}},
                        {'files': dict(original['files'], **{'receiver/shirasu/service.py': 'wrong'})}):
            with self.subTest(changed=changed):
                self.write_manifest({**original, **changed})
                with self.assertRaises(ValueError):
                    candidate.verify_bundle(self.output)
        path = self.output / candidate.MANIFEST_NAME
        path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON'):
            candidate.verify_bundle(self.output)
        path.write_bytes(b' ' * (candidate.MAX_MANIFEST_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            candidate.verify_bundle(self.output)

    def test_cli_can_prepare_then_verify_without_changing_files(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(candidate.main([str(self.output)]), 0)
        result = json.loads(output.getvalue())
        before = candidate.verify_bundle(self.output)
        self.assertEqual(result['manifest_sha256'], before['manifest_sha256'])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(candidate.main(['--verify', str(self.output)]), 0)
        self.assertEqual(candidate.verify_bundle(self.output), before)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(candidate.main([str(self.output)]), 1)


if __name__ == '__main__':
    unittest.main()
