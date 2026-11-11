"""The connected w1 renderer uses the same scene and releases its Agg buffer."""

from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image
from image_service import render_preview as preview, webp_w1
import test_webp_encoding_probe as probe_tests


def pixels(data):
    with Image.open(BytesIO(data)) as image, image.convert('RGBA') as rgba:
        return image.format, rgba.size, rgba.tobytes()


class WebPRenderTests(unittest.TestCase):
    setUp = probe_tests.WebPRenderProbeTests.setUp
    assert_cleanup = probe_tests.WebPRenderProbeTests.assert_cleanup

    def test_direct_webp_matches_png_without_encoding_png_or_changing_decision(self):
        entries = (('synthetic', 0),)
        png = preview.render(entries)
        save = Image.Image.save
        encoded = []

        def record(image, target, format=None, **kwargs):
            encoded.append((format.upper(), image.mode, kwargs))
            return save(image, target, format=format, **kwargs)

        with patch.object(Image.Image, 'save', record):
            webp = preview.render_webp(entries)
        self.assertEqual([(fmt, mode) for fmt, mode, _ in encoded], [('WEBP', 'RGBA')])
        self.assertEqual(webp.mime, 'image/webp')
        self.assertEqual((webp.category, webp.decision), (png.category, png.decision))
        self.assertEqual(pixels(webp.data)[0], 'WEBP')
        self.assertEqual(pixels(webp.data)[1:], pixels(png.png)[1:])
        for key, value in webp_w1.PARAMETERS.items():
            self.assertEqual(encoded[0][2][key], value)
        self.assert_cleanup(preview._finish)
        self.assertEqual(preview.render(entries).png, png.png)

    def test_failed_encoder_propagates_without_png_fallback_and_cleans_buffers(self):
        save = Image.Image.save
        calls = []

        def fail(image, target, format=None, **kwargs):
            calls.append(format.upper())
            if format.upper() == 'WEBP':
                raise RuntimeError('WebP failure')
            return save(image, target, format=format, **kwargs)

        with patch.object(Image.Image, 'save', fail), self.assertRaisesRegex(RuntimeError, 'WebP failure'):
            preview.render_webp((('synthetic', 0),))
        self.assertEqual(calls, ['WEBP'])
        self.assert_cleanup(preview._finish)

    def test_changed_encoder_is_rejected_before_drawing_but_png_still_works(self):
        for target, attribute, value in ((webp_w1.PIL, '__version__', '99.0'),
                                         (webp_w1.features, 'version', lambda name: '99.0'),
                                         (webp_w1.features, 'check', lambda name: False)):
            with patch.object(target, attribute, value), self.assertRaisesRegex(RuntimeError, 'frozen w1'):
                preview.render_webp((('synthetic', 0),))
            self.assertFalse(self.figures)
        with patch.object(webp_w1.PIL, '__version__', '99.0'):
            self.assertTrue(preview.render((('synthetic', 0),)).png.startswith(b'\x89PNG'))


if __name__ == '__main__':
    unittest.main()
