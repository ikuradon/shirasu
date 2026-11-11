"""A codec trial must compare the same inputs and preserve saved evidence."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import benchmark_webp as benchmark


class WebPBenchmarkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / 'new'
        self.baseline, self.gallery = self.root / 'baseline', self.root / 'gallery'
        (self.baseline / 'records').mkdir(parents=True)
        (self.baseline / 'images/example').mkdir(parents=True)
        self.gallery.mkdir()
        self.item = {'id': 'example', 'records': (('synthetic input', 100),), 'cancel_reference': (),
                     'usages': [{'case_slug': 'example', 'role': 'case'}]}
        (self.baseline / 'records/example.json').write_text(json.dumps(self.item))
        # Input agreement is checked before workers decode this opaque fixture.
        png = b'baseline image fixture'
        (self.baseline / 'images/example/png-original.png').write_bytes(png)
        (self.baseline / 'manifest.json').write_text('{}')
        self.manifest = {'cases': [{'slug': 'example', 'images': {'preview': {
            'sha256': hashlib.sha256(png).hexdigest(), 'decision': {'mode': 'topic', 'reasons': []}}}}]}
        self.save_gallery()

    def save_gallery(self):
        (self.gallery / 'manifest.json').write_text(json.dumps(self.manifest))

    def generate(self):
        return benchmark.generate(self.output, baseline=self.baseline, gallery=self.gallery,
                                  timing_cases=('example',))

    def test_changed_input_or_gallery_image_is_rejected_before_output_or_worker(self):
        for mismatch in ('records', 'reference', 'png'):
            with self.subTest(mismatch=mismatch), patch.object(benchmark, 'workloads',
                    return_value=[dict(self.item)]), patch.object(benchmark.subprocess, 'run') as run:
                saved = dict(self.item)
                if mismatch == 'records':
                    saved['records'] = [('different input', 100)]
                elif mismatch == 'reference':
                    saved['cancel_reference'] = [('unexpected reference', 100)]
                else:
                    self.manifest['cases'][0]['images']['preview']['sha256'] = '0' * 64
                    self.save_gallery()
                (self.baseline / 'records/example.json').write_text(json.dumps(saved))
                with self.assertRaises(ValueError):
                    self.generate()
                run.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_existing_or_linked_output_and_invalid_limits_never_touch_evidence(self):
        self.output.mkdir()
        (self.output / 'sentinel').write_text('keep')
        with self.assertRaises(FileExistsError):
            self.generate()
        link = self.root / 'link'
        link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            benchmark.generate(link, baseline=self.baseline, gallery=self.gallery)
        for repeats in (0, 11, True):
            with self.assertRaises(ValueError):
                benchmark.generate(self.root / 'unused', baseline=self.baseline,
                                   gallery=self.gallery, repeats=repeats)
        self.assertEqual((self.output / 'sentinel').read_text(), 'keep')
        self.assertFalse((self.root / 'unused').exists())

    def test_worker_failure_leaves_input_plan_and_explicit_failure_manifest(self):
        with patch.object(benchmark, 'workloads', return_value=[dict(self.item)]), \
                patch.object(benchmark.subprocess, 'run', side_effect=RuntimeError('encoder failed')):
            with self.assertRaisesRegex(RuntimeError, 'encoder failed'):
                self.generate()
        manifest = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(manifest['error'], 'RuntimeError: encoder failed')
        self.assertNotIn('verified_inputs', manifest)
        self.assertEqual(json.loads((self.output / 'plan.json').read_text())['inputs'][0]['records'],
                         [['synthetic input', 100]])


if __name__ == '__main__':
    unittest.main()
