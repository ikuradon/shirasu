"""Public artifacts contain verified images and reports, never private inputs."""

import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from tools.release import prepare_ci_artifact as artifact


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))


class CIArtifactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='ci-artifact-tests-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.checks = self.root / 'checks'
        self.output = self.root / 'public'
        self.checks.mkdir()
        self.candidate = {'schema_version': 1, 'candidate_version': 'v5', 'rollback_version': 'v4',
                          'source': {'commit': 'a' * 40, 'working_tree': True}, 'files': {}}
        self.write('candidate/candidate-manifest.json', self.candidate)
        self.candidate_sha = self.hash('candidate/candidate-manifest.json')
        self.verified_candidate = {**self.candidate, 'manifest_sha256': self.candidate_sha}
        self.enterContext(patch.object(artifact, 'verify_bundle', return_value=self.verified_candidate))
        self.picture = png()
        self.picture_sha = hashlib.sha256(self.picture).hexdigest()
        self.write('results/png-gallery/images/example.png', self.picture)
        self.write('results/png-gallery/records/example.json', [{
            'nmea': '$QZQSM,55,C6AF89A820000324000050400548C5E2C000000003DFF8001C00001185443FC*05',
            'received_ms': 1794124800000}])
        image = {'version': 'preview', 'kind': 'map', 'path': 'images/example.png',
                 'width': 1, 'height': 1, 'sha256': self.picture_sha}
        case = {'title': 'Synthetic fixture', 'category': 'Weather', 'source_kind': 'synthetic',
                'classification': 'training', 'state': 'issue', 'description': 'Public fixture only',
                'record_count': 1, 'records_path': 'records/example.json', 'images': {'preview': image}}
        self.gallery = {'schema_version': 1, 'error_count': 0, 'renderer': 'preview',
                        'cases': [{**case, 'slug': 'case-' + str(i)} for i in range(40)]}
        self.write('results/png-gallery/manifest.json', self.gallery)
        self.write('results/png-gallery/index.html', b'private original HTML must not be copied')
        common = {'schema_version': 1, 'candidate_version': 'v5',
                  'bundle_manifest_sha256': self.candidate_sha, 'case_count': 40, 'post_count': 46,
                  'posts': [{} for _ in range(46)], 'transition_posts': [{} for _ in range(20)]}
        checks = {name: True for name in ('runtime_from_bundle', 'signed_events_verified',
                                         'catalog_complete_text_nmea_url_alt', 'restart_retry_event_unchanged')}
        checks['network_connections'] = 0
        self.write('results/posts.json', {**common, 'checks': checks})
        self.write('results/webp-posts.json', {**common, 'mode': 'webp-receiver', 'encoding': 'w1',
                                            'checks': {**checks, 'real_nostr_mime': True}})
        self.write('results/legacy-images/example.png', self.picture)
        legacy_row = {'png': 'legacy-images/example.png', 'sha256': self.picture_sha}
        self.write('results/legacy.json', {'schema_version': 1, 'mode': 'legacy',
                   'receiver_export_sha256': self.hash('results/posts.json'), 'records': [legacy_row] * 5,
                   'checks': {'isolated_runtime': True, 'real_wsgi_pngs': True}})
        self.write('results/images.json', {**common, 'mode': 'candidate',
                   'receiver_export_sha256': self.hash('results/posts.json'),
                   'legacy': {'baseline_sha256': self.hash('results/legacy.json'), 'records': [{}] * 5},
                   'checks': {name: True for name in ('isolated_runtime', 'health', 'real_wsgi_pngs',
                              'event_ids', 'signed_inputs', 'imeta_and_body', 'head', 'conditional_get', 'artifact_unchanged')}})
        self.write('results/webp-images/example.webp', b'RIFF' + b'\x10\0\0\0' + b'WEBPVP8Lfixture')
        row = {'path': 'webp-images/example.webp', 'sha256': self.hash('results/webp-images/example.webp'),
               'pixel_exact': True}
        self.write('results/webp-http.json', {**common, 'mode': 'webp-http', 'encoding': 'w1',
                   'receiver_export_sha256': self.hash('results/webp-posts.json'),
                   'gallery_manifest_sha256': self.hash('results/png-gallery/manifest.json'),
                   'approved_unique_inputs': 49, 'verified_http_inputs': 45,
                   'corpus': [row] * 45, 'offline_only_inputs': [{}] * 4,
                   'checks': {name: True for name in ('real_loopback_http', 'fixed_candidate',
                              'approved_post_inputs_text_alt', 'imeta_and_body', 'health', 'rgba_pixels_exact',
                              'head', 'conditional_get', 'accept_does_not_negotiate')}})
        self.write('results/compose.json', {'schema_version': 1, 'mode': 'release-compose',
                   'bundle_manifest_sha256': self.candidate_sha, 'stages': [{}, {}, {}],
                   'checks': {'fixed_image_all_services': True, 'saved_event_json_id_signature_unchanged': True}})
        self.write('source-comparison.json', {'unchanged_files': ['source-' + str(i) for i in range(42)],
                   'profile_changes_verified': ['image_versions.py'],
                   'routing_changes_require_runtime_checks': ['image_protocol.py', 'image_service/app.py']})

    def write(self, name, value):
        path = self.checks / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else (json.dumps(value) + '\n').encode())

    def hash(self, name):
        return hashlib.sha256((self.checks / name).read_bytes()).hexdigest()

    def prepare(self):
        return artifact.prepare(self.checks, self.output)

    def test_only_declared_public_assets_are_copied_and_artifact_is_portable(self):
        for name in ('candidate/images/private.geojson.zlib', 'candidate/config.py', '.env',
                     'results/private.json', 'results/docker-images.tar',
                     'results/png-gallery/private.geojson.zlib', 'results/png-gallery/unrelated.png'):
            self.write(name, b'private input must remain local')
        raw = (self.checks / 'candidate/candidate-manifest.json').read_bytes()
        result = self.prepare()
        self.assertEqual(result['candidate_manifest_sha256'], self.candidate_sha)
        self.assertEqual(result['source_commit'], 'a' * 40)
        self.assertIs(result['source_dirty'], True)
        self.assertEqual((self.output / 'candidate-manifest.json').read_bytes(), raw)
        self.assertEqual(artifact.verify_public(self.output), result)
        self.assertTrue((self.output / 'png-gallery/images/example.png').is_file())
        self.assertTrue((self.output / 'results/webp-images/example.webp').is_file())
        for path in self.output.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'private input must remain local', path.read_bytes())
                self.assertNotIn(b'private original HTML must not be copied', path.read_bytes())
        moved = self.root / 'downloaded-artifact'
        self.output.rename(moved)
        self.assertEqual(artifact.verify_public(moved), result)

    def test_ci_gallery_comes_from_results_and_public_gallery_remains_at_root(self):
        self.write('png-gallery/manifest.json', b'unrelated local gallery must be ignored')
        result = self.prepare()
        self.assertTrue((self.output / 'png-gallery/index.html').is_file())
        self.assertFalse((self.output / 'results/png-gallery').exists())
        self.assertEqual((self.output / 'png-gallery/manifest.json').read_bytes(),
                         (self.checks / 'results/png-gallery/manifest.json').read_bytes())
        self.assertEqual(artifact.verify_public(self.output), result)

    def test_mixed_candidates_exports_galleries_counts_and_failed_checks_are_rejected(self):
        mutations = [
            ('results/posts.json', ('bundle_manifest_sha256',), 'b' * 64),
            ('results/images.json', ('receiver_export_sha256',), 'b' * 64),
            ('results/images.json', ('legacy', 'baseline_sha256'), 'b' * 64),
            ('results/webp-http.json', ('gallery_manifest_sha256',), 'b' * 64),
            ('results/webp-http.json', ('post_count',), 45),
            ('results/webp-http.json', ('checks', 'rgba_pixels_exact'), False),
            ('results/compose.json', ('checks', 'fixed_image_all_services'), False),
        ]
        for name, keys, value in mutations:
            with self.subTest(name=name, keys=keys):
                path = self.checks / name
                original = path.read_bytes()
                data = json.loads(original)
                target = data
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                self.write(name, data)
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.output.exists())
                path.write_bytes(original)

    def test_http_assets_cannot_escape_the_results_or_export_private_file_types(self):
        path = self.checks / 'results/webp-http.json'
        original = json.loads(path.read_text())
        for name in ('../candidate/images/private.geojson.zlib', 'private.geojson.zlib', '/etc/passwd'):
            with self.subTest(name=name):
                changed = copy.deepcopy(original)
                changed['corpus'][0]['path'] = name
                self.write('results/webp-http.json', changed)
                with self.assertRaises(ValueError):
                    self.prepare()
        self.write('results/webp-http.json', original)
        image = self.checks / 'results/webp-images/example.webp'
        image.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.prepare()

    def test_existing_output_candidate_output_and_linked_inputs_are_rejected(self):
        self.output.mkdir()
        (self.output / 'keep').write_text('existing')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'outside the fixed candidate'):
            artifact.prepare(self.checks, self.checks / 'candidate/public')
        self.assertEqual((self.output / 'keep').read_text(), 'existing')
        output = self.root / 'other-output'
        source = self.checks / 'results/posts.json'
        moved = self.root / 'outside.json'
        source.rename(moved)
        source.symlink_to(moved)
        with self.assertRaisesRegex(ValueError, 'linked'):
            artifact.prepare(self.checks, output)
        self.assertFalse(output.exists())

    def test_downloaded_artifact_rejects_tampering_missing_extra_and_linked_files(self):
        self.prepare()
        path = self.output / 'results/posts.json'
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        with self.assertRaises(ValueError):
            artifact.verify_public(self.output)
        path.unlink()
        with self.assertRaises(ValueError):
            artifact.verify_public(self.output)
        path.write_bytes(original)
        extra = self.output / 'private.geojson.zlib'
        extra.write_bytes(b'private')
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            artifact.verify_public(self.output)
        extra.unlink()
        extra.symlink_to(self.checks)
        with self.assertRaisesRegex(ValueError, 'linked'):
            artifact.verify_public(self.output)
        extra.unlink()
        index = self.output / 'png-gallery/index.html'
        index.write_text('replaced HTML')
        with self.assertRaisesRegex(ValueError, 'checksums'):
            artifact.verify_public(self.output)

    def test_saved_gallery_verification_survives_html_template_changes(self):
        expected = self.prepare()
        with patch.object(artifact, 'render_html', side_effect=AssertionError('current template must not run')):
            self.assertEqual(artifact.verify_public(self.output), expected)

    def test_output_through_linked_parent_is_rejected(self):
        linked = self.root / 'linked'
        linked.symlink_to(self.checks, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'linked output'):
            artifact.prepare(self.checks, linked / 'public')
        self.assertFalse((self.checks / 'public').exists())

    def test_public_manifest_provenance_and_checksum_changes_are_rejected(self):
        self.prepare()
        path = self.output / artifact.MANIFEST
        value = json.loads(path.read_text())
        value['source_dirty'] = False
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'provenance differ'):
            artifact.verify_public(self.output)


if __name__ == '__main__':
    unittest.main()
