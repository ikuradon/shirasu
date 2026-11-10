"""Exercise the offline dispatcher across all geographic QZQSM families."""

import importlib.util
import unittest
from unittest.mock import patch

import test_flood_renderer as flood_helpers
import test_image_renderer as helpers
from test_image_renderer_v3 import sample, changed

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from image_service import preview_regions, preview_points
    from image_service import render_preview as preview
    from image_service import render_v4 as published


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
                    self.assertGreater(locator.get_position().x0, .7)
                    self.assertLess(locator.get_position().y1, .5)

    def test_context_layout_failure_discards_new_maps_and_keeps_source_summary(self):
        for module, function, records in (
                (preview_regions, 'draw_map', sample('weather')),
                (preview_points, '_axes', helpers.TYPHOON)):
            with self.subTest(function=function), patch.object(
                    module, function, side_effect=ValueError('No readable context layout')):
                image, figure = self.capture(records)
            self.assertEqual(figure.axes, [])
            self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')

    def test_cancellations_keep_published_complete_cards(self):
        for name in ('weather', 'typhoon', 'volcano', 'flood'):
            records = changed(name, (41, 2, 2))
            with self.subTest(category=name):
                self.assertEqual(preview.render(records), published.render(records))


if __name__ == '__main__':
    unittest.main()
