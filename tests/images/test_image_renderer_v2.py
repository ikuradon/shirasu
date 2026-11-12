"""End-to-end v2 PNG contracts using actual decoders and pinned regional assets."""

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from shirasu.images.protocol import make_path
from tests.images.test_image_renderer import KEY, TYPHOON, change_fields, hypocenter, request

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.text import Text
    from image_service import render as v1
    from image_service import render_v2 as v2
    from image_service import region_map

EXAMPLES = Path(__file__).resolve().parents[2] / 'image_service' / 'examples'


def sample(name):
    rows = json.loads((EXAMPLES / (name + '.json')).read_text())
    return tuple((row['nmea'], row['received_ms']) for row in rows)


def changed(name, *fields):
    nmea, received_ms = sample(name)[0]
    # Every modified sample is explicitly training, including the real marine input.
    return ((change_fields(nmea, [(14, 3, 7), *fields]), received_ms),)


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for regional PNG tests')
class RegionalRendererTests(unittest.TestCase):
    def capture(self, records):
        figures = []
        original = v1._figure

        def make_figure():
            figure = original()
            figures.append(figure)
            figure.clear = lambda *args, **kwargs: None
            self.addCleanup(lambda: v1.Figure.clear(figure))
            return figure

        with patch.object(v1, '_figure', side_effect=make_figure):
            rendered = v2.render(records)
        # A rejected plan may have allocated an earlier figure. Inspect the
        # final image's figure so a partial map cannot masquerade as success.
        return rendered, figures[-1]

    def texts(self, figure):
        return '\n'.join(artist.get_text() for artist in figure.findobj(Text))

    def assert_png(self, data):
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', data[16:24]), (1440, 1200))

    def assert_regions(self, figure, codes, *, inset=False, coastline=False):
        self.assertEqual(len(figure.axes), 2 if inset else 1,
                         'The sample must retain its main map and any required overview')
        anchors = {artist.get_gid() for artist in figure.axes[0].collections
                   if (artist.get_gid() or '').startswith('region-anchor-')}
        self.assertEqual(anchors, {f'region-anchor-{code}' for code in codes})
        prefix = 'region-coast-' if coastline else 'region-area-'
        selected = {artist.get_gid() for artist in
                    [*figure.axes[0].patches, *figure.axes[0].lines]
                    if (artist.get_gid() or '').startswith(prefix)}
        self.assertEqual(selected, {f'{prefix}{code}' for code in codes})

    def test_real_marine_map_keeps_seven_area_labels_and_both_hazards(self):
        image, figure = self.capture(sample('marine'))
        self.assert_png(image.png)
        self.assertEqual(image.category, 'Marine')
        self.assert_regions(figure, {1020, 1040, 1050, 1150, 3020, 3110, 3140})
        texts = self.texts(figure)
        for label in ('海上警報', '北海道西方海上', '着氷', '風', 'サハリン西方海上', '能登沖'):
            self.assertIn(label, texts)

    def test_seismic_training_map_retains_received_values_and_occurrence_time(self):
        image, figure = self.capture(sample('seismic_intensity'))
        self.assert_png(image.png)
        self.assertEqual(image.category, 'SeismicIntensity')
        self.assert_regions(figure, {11, 12, 13, 14}, inset=True)
        main, overview = figure.axes
        self.assertEqual(overview.get_gid(), 'region-overview')
        areas = {artist.get_gid() for artist in overview.patches
                 if (artist.get_gid() or '').startswith('region-area-')}
        self.assertEqual(areas, {f'region-area-{code}' for code in (11, 12, 13, 14)})
        extent = [artist for artist in overview.patches if artist.get_gid() == 'region-main-extent']
        self.assertEqual(len(extent), 1, 'The overview must locate the main map')
        left, right = main.get_xlim()
        bottom, top = main.get_ylim()
        for actual, expected in zip(extent[0].get_bbox().bounds,
                                    (left, bottom, right - left, top - bottom), strict=True):
            self.assertAlmostEqual(actual, expected)
        texts = self.texts(figure)
        for label in ('訓練', '都道府県別の受信震度', '東京都', '神奈川県', '5弱', '5強',
                      '2026/11/09 23:45 JST', '県全体の揺れを示さず',
                      '離島を含む全域', '破線は主図範囲'):
            self.assertIn(label, texts)

    def test_tsunami_training_map_retains_warning_heights_and_arrival_meanings(self):
        image, figure = self.capture(sample('tsunami'))
        self.assert_png(image.png)
        self.assertEqual(image.category, 'Tsunami')
        self.assert_regions(figure, {250, 300, 310}, coastline=True)
        texts = self.texts(figure)
        for label in ('訓練', '津波警報', '茨城県', '千葉県九十九里・外房', '福島県',
                      '予想高 3m', '11/10 00:15 JST', '津波到達中と推測', 'ただちに避難してください。'):
            self.assertIn(label, texts)

    def test_cancellation_uses_text_instead_of_drawing_an_active_warning_map(self):
        for name in ('marine', 'seismic_intensity', 'tsunami'):
            with self.subTest(category=name):
                records = changed(name, (41, 2, 2))
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertIn('取消', self.texts(figure))
                self.assertEqual(image.png, v1.render(records).png)

    def test_marine_release_remains_a_map_and_does_not_become_cancellation(self):
        _, figure = self.capture(changed('marine', (53, 5, 0)))
        self.assert_regions(figure, {1020, 1040, 1050, 1150, 3020, 3110, 3140})
        self.assertIn('警報解除', self.texts(figure))
        self.assertNotIn('取消', self.texts(figure))

    def test_unknown_single_item_rejects_the_whole_map(self):
        cases = (('marine', (58, 14, 16383)), ('marine', (53, 5, 31)),
                 ('seismic_intensity', (72, 6, 48)), ('tsunami', (100, 10, 1000)))
        for name, field in cases:
            with self.subTest(category=name, field=field):
                records = changed(name, field)
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertEqual(image.png, v1.render(records).png)

    def test_missing_one_regions_geometry_does_not_emit_a_partial_map(self):
        records = sample('tsunami')
        original = region_map._expand

        def incomplete_layer(layer, code):
            if code == 300:
                raise ValueError('Fixture: one received region has no geometry')
            return original(layer, code)

        with patch.object(region_map, '_expand', side_effect=incomplete_layer):
            image, figure = self.capture(records)
        self.assertEqual(figure.axes, [])
        self.assertEqual(image.png, v1.render(records).png)

    def test_regional_pngs_are_deterministic_for_the_same_input(self):
        for name in ('marine', 'seismic_intensity', 'tsunami'):
            with self.subTest(category=name):
                records = sample(name)
                self.assertEqual(v2.render(records).png, v2.render(records).png)

    def test_existing_typhoon_and_hypocenter_pngs_are_byte_identical_to_v1(self):
        for records in (TYPHOON, (hypocenter(),)):
            with self.subTest(category=v1._decode(records)[0].__class__.__name__):
                legacy = v1.render(records)
                current = v2.render(records)
                self.assertEqual(current, legacy)

    def test_signed_v2_http_route_returns_actual_png_with_content_hash(self):
        records = sample('marine')
        path = make_path(records, 'test', KEY, version='v2')
        response = request(path)
        self.assertEqual(response['status'], '200 OK')
        self.assertEqual(response['headers']['Content-Type'], 'image/png')
        self.assertEqual(response['headers']['X-Content-Type-Options'], 'nosniff')
        self.assertIn('immutable', response['headers']['Cache-Control'])
        self.assert_png(response['body'])
        self.assertEqual(response['body'], v2.render(records).png)
        self.assertEqual(response['headers']['ETag'], '"' + hashlib.sha256(response['body']).hexdigest() + '"')


if __name__ == '__main__':
    unittest.main()
