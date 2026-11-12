"""Offline WebP routes preserve pixels, renderer decisions and Agg cleanup."""

import gc
import importlib.util
from io import BytesIO
import unittest
from unittest.mock import patch
import weakref

from PIL import Image, features
from tools.preview import webp_encoding_probe as probe

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from image_service import render_preview as preview
    from image_service import preview_wide_layout as layout


class SyntheticReport:
    pass


def rgba(data):
    with Image.open(BytesIO(data)) as decoded:
        with decoded.convert('RGBA') as pixels:
            return decoded.format, pixels.size, pixels.tobytes()


@unittest.skipUnless(features.check('webp'), 'Pillow requires WebP support')
class WebPTranscodeTests(unittest.TestCase):
    def test_transcode_preserves_transparency_and_hidden_rgb(self):
        for transparent in (False, True):
            with self.subTest(transparent=transparent), Image.new('RGBA', (12, 8), (23, 62, 91, 255)) as source:
                if transparent:
                    source.putpixel((0, 0), (147, 69, 211, 0))
                    source.putpixel((1, 0), (33, 200, 15, 80))
                output = BytesIO()
                source.save(output, format='PNG')
                original = output.getvalue()
                encoded, stages = probe.transcode_png(original)
                self.assertEqual(rgba(encoded)[0], 'WEBP')
                self.assertEqual(rgba(encoded)[1:], rgba(original)[1:])
                self.assertEqual(set(stages), {'transcode_decode_ms', 'transcode_prepare_ms',
                                               'transcode_encode_ms'})
                self.assertTrue(all(value >= 0 for value in stages.values()))

    def test_invalid_source_and_unknown_variant_do_not_render(self):
        for value in (None, '', b'not a PNG'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe.transcode_png(value)
        for value in ('webp', 'jpeg', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe.render((), variant=value)


@unittest.skipUnless(HAS_RENDERER and features.check('webp'), 'Install image renderer dependencies')
class WebPRenderProbeTests(unittest.TestCase):
    def setUp(self):
        enabled = gc.isenabled()
        gc.disable()
        self.addCleanup(gc.enable if enabled else gc.disable)
        self.enterContext(patch.object(gc, 'collect', side_effect=AssertionError('No forced GC')))
        self.figures, self.renderers = [], []
        make_figure = layout.figure
        get_renderer = FigureCanvasAgg.get_renderer

        def figure():
            fig = make_figure()
            fig.set_size_inches(3, 2)
            self.figures.append(fig)
            self.addCleanup(fig.clear)
            return fig

        def renderer(canvas):
            value = get_renderer(canvas)
            self.renderers.append(weakref.ref(value))
            return value

        def draw(fig, reports):
            ax = fig.add_axes((.1, .15, .8, .65))
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.plot((.1, .9), (.2, .8), color='#e98833')
            ax.text(.15, .6, 'warning', color='#5577aa')
            fig.text(.1, .9, 'test image')

        self.enterContext(patch.object(layout, 'figure', figure))
        self.enterContext(patch.object(FigureCanvasAgg, 'get_renderer', renderer))
        self.enterContext(patch.object(preview, 'verify_assets'))
        self.enterContext(patch.object(preview, 'is_nankai_entry', return_value=False))
        self.enterContext(patch.object(preview.base, '_decode', return_value=(SyntheticReport(),)))
        self.enterContext(patch.object(preview.base, '_metadata', return_value=('', '', '', False)))
        self.enterContext(patch.object(preview.base, '_reset_assembly'))
        self.enterContext(patch.object(preview, 'DRAWERS', {SyntheticReport: draw}))
        self.enterContext(patch.object(layout, 'context_names'))
        self.enterContext(patch.object(layout, 'validate', side_effect=lambda fig: fig.canvas.draw()))

    def assert_cleanup(self, finish):
        self.assertIs(preview._finish, finish)
        self.assertTrue(all(reference() is None for reference in self.renderers))
        for fig in self.figures:
            self.assertNotIn('renderer', vars(fig.canvas))
            self.assertNotIn('savefig', vars(fig))
        self.assertFalse(gc.isenabled())

    def test_three_routes_keep_pixels_png_bytes_and_decisions(self):
        original = preview.render((('synthetic', 0),))
        finish = preview._finish
        for variant in probe.VARIANTS:
            with self.subTest(variant=variant):
                result = probe.render((('synthetic', 0),), variant=variant)
                self.assertEqual(result.category, original.category)
                self.assertEqual(result.decision, original.decision)
                self.assertEqual(rgba(result.data)[1:], rgba(original.png)[1:])
                self.assertEqual(result.mime, 'image/png' if variant == 'png' else 'image/webp')
                if variant == 'png':
                    self.assertEqual(result.data, original.png)
                self.assertTrue(all(value >= 0 for value in result.stages.values()))
                self.assertGreater(result.stages['savefig_ms'], 0)
                self.assertEqual(result.stages['transcode_encode_ms'] > 0, variant == 'png-webp')
                self.assert_cleanup(finish)

    def test_direct_route_encodes_rgba_once_without_a_png_intermediate(self):
        saved = []
        image_save = Image.Image.save

        def save(image, target, format=None, **kwargs):
            saved.append((format.upper(), image.mode, kwargs))
            return image_save(image, target, format=format, **kwargs)

        with patch.object(Image.Image, 'save', save):
            result = probe.render((('synthetic', 0),), variant='direct-webp')
        self.assertEqual([(format, mode) for format, mode, _ in saved], [('WEBP', 'RGBA')])
        for key, expected in probe.WEBP_PARAMETERS.items():
            self.assertEqual(saved[0][2][key], expected)
        self.assertEqual(rgba(result.data)[0], 'WEBP')

    def test_encoder_failure_restores_hooks_and_releases_owned_buffers(self):
        finish, image_save = preview._finish, Image.Image.save

        def save(image, target, format=None, **kwargs):
            if format.upper() == 'WEBP':
                raise RuntimeError('synthetic WebP failure')
            return image_save(image, target, format=format, **kwargs)

        for variant in ('direct-webp', 'png-webp'):
            with self.subTest(variant=variant), patch.object(Image.Image, 'save', save):
                with self.assertRaisesRegex(RuntimeError, 'synthetic WebP failure'):
                    probe.render((('synthetic', 0),), variant=variant)
            self.assert_cleanup(finish)
        self.assertTrue(preview.render((('synthetic', 0),)).png.startswith(b'\x89PNG'))
        self.assert_cleanup(finish)


if __name__ == '__main__':
    unittest.main()
