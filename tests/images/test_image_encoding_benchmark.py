"""Image-format measurements preserve source pixels and disclose lossy changes."""

from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, features

from tools.preview.image_encoding_benchmark import evaluate


def sample(*, transparent=False):
    image = Image.new('RGBA', (96, 64), (19, 32, 44, 255))
    draw = ImageDraw.Draw(image)
    for x in range(0, 96, 2):
        draw.line((x, 0, x, 63), fill=(x * 2, 255 - x * 2, 135, 255))
    draw.text((3, 8), 'Warning 123', fill=(255, 230, 31, 255))
    draw.line((4, 60, 89, 4), fill=(229, 23, 54, 255), width=1)
    if transparent:
        image.putpixel((0, 0), (147, 69, 211, 0))
        image.putpixel((1, 0), (33, 200, 15, 80))
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


class ImageEncodingBenchmarkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='shirasu-encoding-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / 'variants'

    def test_reference_lossless_roundtrips_and_timing_metadata(self):
        png = sample()
        rows = evaluate(png, self.output, repeats=2)
        self.assertEqual(len(rows), 7 if features.check('avif') else 6)
        indexed = {row['id']: row for row in rows}
        original = indexed['png-original']
        self.assertEqual((self.output / original['path']).read_bytes(), png)
        self.assertIsNone(original['encode_ms'])
        self.assertEqual(original['prepare_ms'], 0)
        for row in rows:
            self.assertNotIn('error', row)
            encoded = (self.output / row['path']).read_bytes()
            self.assertEqual(row['sha256'], hashlib.sha256(encoded).hexdigest())
            self.assertEqual(row['bytes'], len(encoded))
            self.assertEqual(row['ratio_to_png'], len(encoded) / len(png))
            self.assertEqual((row['width'], row['height']), (96, 64))
            self.assertGreaterEqual(row['source_decode_ms'], 0)
            self.assertGreaterEqual(row['prepare_ms'], 0)
            self.assertEqual(len(row['decode_ms']['samples']), 2)
            for field in ('encode_ms', 'decode_ms'):
                if row[field] is not None:
                    self.assertEqual(len(row[field]['samples']), 2)
                    self.assertTrue(all(value >= 0 for value in row[field]['samples']))
                    self.assertEqual(row[field]['median'], sum(row[field]['samples']) / 2)
            self.assertFalse(row['alpha_flattened'])
        for name in ('png-original', 'png-optimized', 'webp-lossless-m0', 'webp-lossless'):
            self.assertTrue(indexed[name]['pixel_exact'])
            self.assertEqual(indexed[name]['rgb_mse'], 0)
            self.assertTrue(indexed[name]['psnr_rgb_exact'])
            self.assertIsNone(indexed[name]['psnr_rgb_db'])
        self.assertEqual(indexed['png-optimized']['parameters']['mode'], 'RGB')
        self.assertEqual(indexed['webp-lossless']['parameters']['method'], 4)
        self.assertEqual(indexed['webp-lossless-m0']['parameters']['method'], 0)
        for name in ('webp-q90', 'jpeg-q90'):
            self.assertFalse(indexed[name]['pixel_exact'])
            self.assertGreater(indexed[name]['psnr_rgb_db'], 0)
        # There are no Infinity/NaN values in exported evidence.
        json.dumps(rows, allow_nan=False)

    def test_transparency_and_hidden_rgb_are_preserved_only_by_lossless_formats(self):
        rows = {row['id']: row for row in evaluate(sample(transparent=True), self.output, repeats=1)}
        for name in ('png-original', 'png-optimized', 'webp-lossless-m0', 'webp-lossless'):
            self.assertNotIn('error', rows[name])
            self.assertTrue(rows[name]['pixel_exact'])
        self.assertEqual(rows['png-optimized']['parameters']['mode'], 'RGBA')
        jpeg = rows['jpeg-q90']
        self.assertTrue(jpeg['alpha_flattened'])
        self.assertTrue(jpeg['warnings'])
        self.assertFalse(jpeg['pixel_exact'])
        self.assertEqual(jpeg['comparison_background'], 'white')
        with Image.open(self.output / jpeg['path']) as image:
            self.assertEqual(image.mode, 'RGB')

    def test_invalid_inputs_and_collisions_do_not_replace_saved_files(self):
        for png in (b'', b'not-png', b'\x89PNG\r\n\x1a\ninvalid', sample().decode('latin1')):
            with self.subTest(png=type(png)), self.assertRaises(ValueError):
                evaluate(png, self.output)
        for repeats in (True, 0, -1, 1.5, 21):
            with self.subTest(repeats=repeats), self.assertRaises(ValueError):
                evaluate(sample(), self.output, repeats=repeats)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        target = self.output / 'jpeg-q90.jpg'
        target.write_bytes(b'keep saved evidence')
        with self.assertRaises(FileExistsError):
            evaluate(sample(), self.output, repeats=1)
        self.assertEqual(list(self.output.iterdir()), [target])
        self.assertEqual(target.read_bytes(), b'keep saved evidence')
        target.unlink()
        target.symlink_to(self.root / 'missing')
        with self.assertRaises(FileExistsError):
            evaluate(sample(), self.output, repeats=1)
        self.assertTrue(target.is_symlink())

    def test_encoder_failure_is_reported_without_losing_other_formats(self):
        png = sample()
        original = Image.Image.save

        def fail_jpeg(image, fp, format=None, **parameters):
            if format == 'JPEG':
                raise OSError('example unavailable encoder')
            return original(image, fp, format=format, **parameters)

        with patch.object(Image.Image, 'save', new=fail_jpeg):
            rows = {row['id']: row for row in evaluate(png, self.output, repeats=1)}
        failed = rows['jpeg-q90']
        self.assertIn('example unavailable encoder', failed['error'])
        self.assertIsNone(failed['path'])
        self.assertFalse((self.output / 'jpeg-q90.jpg').exists())
        self.assertTrue(rows['webp-lossless']['pixel_exact'])
        self.assertEqual((self.output / rows['png-original']['path']).read_bytes(), png)


if __name__ == '__main__':
    unittest.main()
