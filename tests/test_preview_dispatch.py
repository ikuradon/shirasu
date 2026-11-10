"""Exercise the offline dispatcher across all geographic QZQSM families."""

import importlib.util
import unittest
from unittest.mock import patch

import test_flood_renderer as flood_helpers
import test_image_renderer as helpers
from test_image_renderer_v3 import sample, changed

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from image_service import preview_wide_layout as wide
    from image_service import render_preview as preview
    from image_service import render as base


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for preview dispatch tests')
class PreviewDispatchTests(unittest.TestCase):
    capture = flood_helpers.FloodRendererTests.capture

    def test_every_geographic_family_reaches_its_context_map(self):
        cases = [(name, sample(name)) for name in (
            'flood', 'ash_fall', 'weather', 'eew', 'marine', 'seismic_intensity',
            'tsunami', 'volcano', 'northwest_pacific_tsunami')]
        cases.extend((('typhoon', helpers.TYPHOON), ('hypocenter', (helpers.hypocenter(),))))
        for name, records in cases:
            with self.subTest(category=name):
                image, figure = self.capture(records)
                self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')
                self.assertTrue(figure.axes, 'A supported sample must retain its geographic map')
                self.assertTrue(any(artist.get_gid() == 'preview-prefecture-boundaries'
                                    for artist in figure.axes[0].collections))
                locators = [axis for axis in figure.axes if axis.get_gid() == 'preview-locator']
                self.assertLessEqual(len(locators), 1)
                for locator in locators:
                    self.assertGreater(locator.get_position().x0, .5)
                    self.assertLess(locator.get_position().x1, wide.PANEL_X)
                    self.assertLess(locator.get_position().y1, .5)
                    # Every family shares a plain locator: land/coastline,
                    # its frame, and the main viewport rectangle only.
                    self.assertEqual({artist.get_gid() for artist in locator.patches},
                                     {'v3-land', None, 'preview-main-extent'})
                    self.assertFalse(locator.lines)
                    self.assertFalse(locator.collections)
                    self.assertFalse(locator.artists)
                    self.assertFalse(locator.texts)

    def test_context_layout_failure_discards_new_maps_and_keeps_source_summary(self):
        def fail(fig, reports):
            fig.add_subplot()
            raise ValueError('No readable context layout')

        for kind, records in ((base.dcr.Weather, sample('weather')),
                              (base.dcr.Typhoon, helpers.TYPHOON)):
            with self.subTest(category=kind.__name__), patch.dict(preview.DRAWERS, {kind: fail}):
                image, figure = self.capture(records)
            self.assertEqual(figure.axes, [])
            self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')

    def test_cancellations_are_explicit_and_do_not_reuse_the_cancelled_map(self):
        for name in ('weather', 'typhoon', 'volcano', 'flood'):
            records = changed(name, (41, 2, 2))
            with self.subTest(category=name):
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                text = ''.join(label.get_text() for label in figure.texts)
                self.assertIn('【取消】', text)
                self.assertIn('取り消されました', text)


if __name__ == '__main__':
    unittest.main()
