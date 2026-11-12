"""Offline vendor materialization preserves fixed bytes without overwriting work."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.assets import prepare_vendor_assets as vendor


class VendorAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = json.loads((vendor.ROOT / vendor.MANIFEST).read_text())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lock = copy.deepcopy(self.template)
        for row in self.lock['files']:
            data = ('fixture for ' + row['destination'] + '\n').encode()
            path = self.root / row['source']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            row.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        self.save_lock()

    def save_lock(self):
        (self.root / vendor.MANIFEST).write_text(json.dumps(self.lock))

    def test_offline_copy_and_safe_rerun_preserve_all_bytes_and_existing_mtime(self):
        result = vendor.prepare(self.root)
        self.assertEqual(result['created'], len(vendor.DESTINATIONS))
        self.assertEqual(result['unchanged'], 0)
        before = {}
        for row in self.lock['files']:
            source, target = (self.root / row[key] for key in ('source', 'destination'))
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
            before[row['destination']] = target.stat().st_mtime_ns
        self.assertEqual(vendor.prepare(self.root)['created'], 0)
        self.assertEqual(vendor.prepare(self.root, check=True)['created'], 0)
        self.assertEqual(before, {name: (self.root / name).stat().st_mtime_ns for name in before})

    def test_check_never_materializes_missing_assets(self):
        with self.assertRaisesRegex(ValueError, 'runtime assets are missing'):
            vendor.prepare(self.root, check=True)
        self.assertFalse((self.root / 'image_service').exists())

    def test_corrupt_source_fails_before_any_destination_is_written(self):
        source = self.root / self.lock['files'][-1]['source']
        source.write_bytes(b'changed upstream data')
        with self.assertRaisesRegex(ValueError, 'vendor checksum or size differs'):
            vendor.prepare(self.root)
        self.assertFalse((self.root / 'image_service').exists())

    def test_missing_source_does_not_download_or_write_partial_output(self):
        (self.root / self.lock['files'][-1]['source']).unlink()
        with self.assertRaisesRegex(ValueError, 'expected a regular file'):
            vendor.prepare(self.root)
        self.assertFalse((self.root / 'image_service').exists())

    def test_existing_different_runtime_asset_is_preserved_and_other_outputs_are_not_created(self):
        row = self.lock['files'][-1]
        path = self.root / row['destination']
        path.parent.mkdir(parents=True)
        path.write_bytes(b'local work that must be preserved')
        with self.assertRaisesRegex(ValueError, 'existing runtime asset differs'):
            vendor.prepare(self.root)
        self.assertEqual(path.read_bytes(), b'local work that must be preserved')
        self.assertFalse((self.root / self.lock['files'][0]['destination']).exists())

    def test_linked_source_and_destination_are_rejected(self):
        row = self.lock['files'][0]
        source = self.root / row['source']
        backup = source.with_name('retained-source')
        source.rename(backup)
        source.symlink_to(backup)
        with self.assertRaisesRegex(ValueError, 'linked asset paths'):
            vendor.prepare(self.root)
        source.unlink()
        backup.rename(source)
        target = self.root / row['destination']
        target.parent.mkdir(parents=True)
        target.symlink_to(source)
        with self.assertRaisesRegex(ValueError, 'linked asset paths'):
            vendor.prepare(self.root)

    def test_linked_parent_and_manifest_are_rejected(self):
        (self.root / 'image_service').symlink_to(self.root / 'outside', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'linked asset paths'):
            vendor.prepare(self.root)
        (self.root / 'image_service').unlink()
        manifest = self.root / vendor.MANIFEST
        saved = manifest.with_name('retained-manifest.json')
        manifest.rename(saved)
        manifest.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, 'linked asset paths'):
            vendor.prepare(self.root)

    def test_lock_rejects_traversal_unknown_providers_and_undeclared_destinations(self):
        for field, value in (('source', '../external'), ('source', '/tmp/external'),
                             ('source', 'vendor/jma/../private'), ('source', 'vendor/private/file'),
                             ('destination', 'config.py'), ('destination', 'image_service/assets/v1/manifest.json')):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(self.lock)
                changed['files'][0][field] = value
                (self.root / vendor.MANIFEST).write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    vendor.prepare(self.root)
        self.assertFalse((self.root / 'image_service').exists())

    def test_lock_rejects_missing_duplicate_and_invalid_entries(self):
        mutations = []
        missing = copy.deepcopy(self.lock)
        missing['files'].pop()
        mutations.append(missing)
        duplicate = copy.deepcopy(self.lock)
        duplicate['files'][-1] = duplicate['files'][0]
        mutations.append(duplicate)
        for key, value in (('sha256', 'not-a-hash'), ('size_bytes', True),
                           ('size_bytes', -1), ('size_bytes', vendor.MAX_FILE_BYTES + 1)):
            changed = copy.deepcopy(self.lock)
            changed['files'][0][key] = value
            mutations.append(changed)
        for changed in mutations:
            with self.subTest(value=changed['files'][-1]):
                (self.root / vendor.MANIFEST).write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    vendor.prepare(self.root)

    def test_duplicate_json_keys_are_rejected(self):
        text = json.dumps(self.lock).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
        (self.root / vendor.MANIFEST).write_text(text)
        with self.assertRaisesRegex(ValueError, 'duplicate manifest key'):
            vendor.prepare(self.root)

    def test_source_change_during_copy_never_installs_partial_bytes(self):
        original = vendor.install
        row = self.lock['files'][0]
        def changed(root, item):
            (root / item['source']).write_bytes(b'raced source')
            return original(root, item)
        with patch.object(vendor, 'install', side_effect=changed), \
                self.assertRaisesRegex(ValueError, 'vendor source changed'):
            vendor.prepare(self.root)
        self.assertFalse((self.root / row['destination']).exists())
        self.assertEqual(list(self.root.rglob('.vendor-*')), [])


if __name__ == '__main__':
    unittest.main()
