"""Legacy reference export stays allowlisted and rejects changed rendering code."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_release_baseline as baseline


class ReleaseBaselineTests(unittest.TestCase):
    def test_export_reads_only_committed_allowlist_without_executing_old_builder(self):
        blobs = {
            'scripts/prepare_image_service.py': b"FILES = ('image_protocol.py', 'image_service/render.py', 'licenses/pyserial-3.5-LICENSE.txt')\nraise RuntimeError('never execute')\n",
            'image_protocol.py': b'SUPPORTED_VERSIONS = ("v1",)\n',
            'image_service/render.py': b'# renderer\n',
            'image_service/requirements.txt': b'# locked requirements\n',
            'licenses/pyserial-3.5-LICENSE.txt': b'upstream license fixture\n',
        }
        reads = []
        def git(*args):
            if args[0] == 'rev-parse':
                return b'a' * 40 + b'\n'
            if args[0] == 'ls-tree':
                return b'100644 blob abc\tfile\n'
            name = args[1].split(':', 1)[1]
            reads.append(name)
            return blobs[name]
        with tempfile.TemporaryDirectory() as directory, patch.object(baseline, '_git', side_effect=git):
            output = Path(directory) / 'legacy'
            baseline.prepare(output)
            self.assertEqual(set(reads), set(blobs))
            self.assertEqual((output / 'image_service/render.py').read_bytes(), blobs['image_service/render.py'])
            self.assertEqual((output / 'licenses/pyserial-3.5-LICENSE.txt').read_bytes(),
                             blobs['licenses/pyserial-3.5-LICENSE.txt'])
            self.assertFalse((output / 'config.py').exists())
            with self.assertRaises(FileExistsError):
                baseline.prepare(output)

    def test_source_comparison_allows_only_registry_extraction(self):
        old = b'SUPPORTED_VERSIONS = ("v1", "v2", "v3", "v4")\ndef parse():\n    return 1\n'
        new = b'from image_versions import SUPPORTED_VERSIONS\ndef parse():\n    return 1\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, candidate = root / 'legacy', root / 'candidate'
            legacy.mkdir()
            candidate.mkdir()
            (legacy / 'image_protocol.py').write_bytes(old)
            (candidate / 'image_protocol.py').write_bytes(new)
            (legacy / baseline.MANIFEST).write_text(json.dumps({
                'schema_version': 1, 'commit': 'a' * 40,
                'files': {'image_protocol.py': hashlib.sha256(old).hexdigest()}}))
            result = baseline.compare_sources(legacy, candidate)
            self.assertEqual(result['profile_changes_verified'], ['image_protocol.py'])
            (candidate / 'image_protocol.py').write_bytes(new.replace(b'return 1', b'return 2'))
            with self.assertRaisesRegex(ValueError, 'changed'):
                baseline.compare_sources(legacy, candidate)
            result = baseline.compare_sources(legacy, candidate, allow_routing_changes=True)
            self.assertEqual(result['routing_changes_require_runtime_checks'], ['image_protocol.py'])
            self.assertEqual(result['profile_changes_verified'], [])
            # The opt-in never relaxes renderer or asset byte checks.
            renderer = 'image_service/render.py'
            for folder, content in ((legacy, b'original'), (candidate, b'changed')):
                (folder / 'image_service').mkdir()
                (folder / renderer).write_bytes(content)
            manifest = json.loads((legacy / baseline.MANIFEST).read_text())
            manifest['files'][renderer] = hashlib.sha256(b'original').hexdigest()
            (legacy / baseline.MANIFEST).write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'Frozen renderer'):
                baseline.compare_sources(legacy, candidate, allow_routing_changes=True)
            (candidate / renderer).write_bytes(b'original')
            (candidate / 'image_protocol.py').write_bytes(new)
            (legacy / 'image_protocol.py').write_bytes(b'changed reference')
            with self.assertRaisesRegex(ValueError, 'modified'):
                baseline.compare_sources(legacy, candidate)

    def test_unsafe_allowlist_and_symlinks_are_rejected(self):
        for value in ('../config.py', 'config.py', '/image_protocol.py', 'image_service/../config.py',
                      'licenses/other.txt', 'licenses/../config.py'):
            self.assertFalse(baseline._allowed(value))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                if args[0] == 'rev-parse':
                    return b'a' * 40
                if args[0] == 'show':
                    return b"FILES = ('image_protocol.py',)"
                return b'120000 blob abc\timage_protocol.py'
            with patch.object(baseline, '_git', side_effect=git), self.assertRaises(ValueError):
                baseline.prepare(root / 'legacy')
            self.assertFalse((root / 'legacy').exists())


if __name__ == '__main__':
    unittest.main()
