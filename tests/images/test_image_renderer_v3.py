"""Full v3 PNG and HTTP contracts, with real decoder input and frozen assets."""

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from shirasu.images.protocol import make_path
from tests.images import test_image_renderer as helpers

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.text import Text
    from image_service import render as v1
    from image_service import render_v2 as v2
    from image_service import render_v3 as v3
    from image_service import map_v3

EXAMPLES = Path(__file__).resolve().parents[2] / 'image_service' / 'examples'


def sample(name):
    rows = json.loads((EXAMPLES / (name + '.json')).read_text())
    return tuple((row['nmea'], row['received_ms']) for row in rows)


def changed(name, *fields):
    nmea, received_ms = sample(name)[0]
    return ((helpers.change_fields(nmea, [(14, 3, 7), *fields]), received_ms),)


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for v3 PNG tests')
class ForecastRendererTests(unittest.TestCase):
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
            rendered = v3.render(records)
        return rendered, figures[-1]

    def texts(self, figure):
        return '\n'.join(artist.get_text() for artist in figure.findobj(Text))

    def assert_png(self, data):
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', data[16:24]), (1440, 1200))

    def assert_regions(self, figure, codes):
        self.assertTrue(figure.axes, 'The final image must be a complete map')
        anchors = {artist.get_gid() for artist in figure.axes[0].collections
                   if (artist.get_gid() or '').startswith('region-anchor-')}
        self.assertEqual(anchors, {f'region-anchor-{code}' for code in codes})
        # Measure real TextArea bounds and rendered leader segments. A caption
        # that fits by character count alone must not hide another caption.
        helpers.RendererTests.assert_clear_leaders(self, figure, figure.axes[0], len(codes))

    def test_weather_training_keeps_every_region_and_distinct_hazard(self):
        image, figure = self.capture(sample('weather'))
        self.assert_png(image.png)
        self.assertEqual(image.category, 'Weather')
        self.assert_regions(figure, {110000, 120000, 130010, 140000})
        texts = self.texts(figure)
        for expected in ('訓練', '気象情報', '東京地方', '神奈川県', '千葉県', '埼玉県',
                         '大雨特別警報', '土砂災害警戒情報', '竜巻注意情報', '府県予報区',
                         '区域全体の警戒・解除'):
            self.assertIn(expected, texts)

    def test_weather_release_remains_a_map_and_differs_from_cancellation(self):
        _, figure = self.capture(changed('weather', (53, 3, 2)))
        self.assert_regions(figure, {110000, 120000, 130010, 140000})
        texts = self.texts(figure)
        self.assertIn('【解除】', texts)
        self.assertIn('大雨特別警報（解除）', texts)
        self.assertNotIn('取消', texts)

    def test_eew_shows_one_bulletin_prediction_and_only_the_received_warning_areas(self):
        image, figure = self.capture(sample('eew'))
        self.assert_png(image.png)
        self.assertEqual(image.category, 'EarthquakeEarlyWarning')
        self.assert_regions(figure, {11, 12, 14, 15, 16, 19})
        texts = self.texts(figure)
        for expected in ('訓練', '緊急地震速報', '予測震度 5弱〜6弱', '長周期地震動',
                         '階級 1〜3', '茨城県沖', 'M6.8', '50km', '2026/11/10 09:45 JST',
                         '強い揺れに警戒してください。', '震度分布ではなく'):
            self.assertIn(expected, texts)
        self.assertEqual(texts.count('予測震度 5弱〜6弱'), 1)
        self.assertFalse(any((artist.get_gid() or '').startswith(('epicentre', 'hypocenter'))
                             for axes in figure.axes for artist in axes.collections))
        labels = [artist for artist in figure.axes[0].artists if hasattr(artist, 'offsetbox')]
        for label in labels:
            regional = '\n'.join(artist.get_text() for artist in label.findobj(Text))
            self.assertNotIn('震度', regional, 'A bulletin-wide range is not a local prediction')

    def test_volcano_training_has_an_official_point_and_complete_received_information(self):
        image, figure = self.capture(sample('volcano'))
        self.assert_png(image.png)
        self.assertEqual(image.category, 'Volcano')
        self.assertEqual(len(figure.axes), 1)
        points = [artist for artist in figure.axes[0].collections if artist.get_gid() == 'volcano-point-503']
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].get_offsets()[0, 0], 131.104)
        helpers.RendererTests.assert_clear_leaders(self, figure, figure.axes[0], 1)
        texts = self.texts(figure)
        for expected in ('訓練', '火山情報', '阿蘇山', 'レベル3(入山規制)', '23時45分 JST',
                         '熊本県阿蘇市', '熊本県高森町', '熊本県南阿蘇村', '代表位置',
                         '避難対象区域を示すものではありません'):
            self.assertIn(expected, texts)

    def test_cancellation_never_leaves_an_active_map(self):
        for name in ('weather', 'eew', 'volcano'):
            with self.subTest(name=name):
                records = changed(name, (41, 2, 2))
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertIn('取消', self.texts(figure))
                self.assertEqual(image.png, v2.render(records).png)

    def test_unknown_single_value_rejects_the_entire_map(self):
        cases = (
            ('weather', (61, 19, 500000)), ('weather', (56, 5, 31)),
            ('eew', (122, 4, 0)), ('eew', (209, 1, 1)),
            ('volcano', (76, 12, 4000)), ('volcano', (69, 7, 127)),
        )
        for name, field in cases:
            with self.subTest(name=name, field=field):
                records = changed(name, field)
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertEqual(image.png, v2.render(records).png)

    def test_missing_one_forecast_geometry_falls_back_without_a_partial_map(self):
        records = sample('weather')
        original = map_v3._expand

        def incomplete(layer, code):
            if layer == 'weather' and code == 140000:
                raise ValueError('Fixture: one received area has no geometry')
            return original(layer, code)

        with patch.object(map_v3, '_expand', side_effect=incomplete):
            image, figure = self.capture(records)
        self.assertEqual(figure.axes, [])
        self.assertEqual(image.png, v2.render(records).png)

    def test_same_v3_inputs_produce_identical_pngs(self):
        for name in ('weather', 'eew', 'volcano'):
            with self.subTest(name=name):
                records = sample(name)
                self.assertEqual(v3.render(records), v3.render(records))

    def test_existing_regional_and_point_images_are_byte_identical(self):
        cases = [sample(name) for name in ('marine', 'seismic_intensity', 'tsunami')]
        cases.extend((helpers.TYPHOON, (helpers.hypocenter(),)))
        for records in cases:
            with self.subTest(category=type(v1._decode(records)[0]).__name__):
                self.assertEqual(v3.render(records), v2.render(records))

    def test_v3_signed_http_uses_the_real_renderer_and_content_etag(self):
        records = sample('weather')
        path = make_path(records, 'test', helpers.KEY, version='v3')
        response = helpers.request(path)
        self.assertEqual(response['status'], '200 OK')
        self.assertEqual(response['headers']['Content-Type'], 'image/png')
        self.assertIn('immutable', response['headers']['Cache-Control'])
        self.assert_png(response['body'])
        self.assertEqual(response['body'], v3.render(records).png)
        etag = '"' + hashlib.sha256(response['body']).hexdigest() + '"'
        self.assertEqual(response['headers']['ETag'], etag)
        conditional = helpers.request(path, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(conditional['status'], '304 Not Modified')
        self.assertEqual(conditional['body'], b'')
        tampered = helpers.request(path.replace('/v3/', '/v2/', 1))
        self.assertEqual(tampered['status'], '403 Forbidden')
        self.assertIn('no-store', tampered['headers']['Cache-Control'])


if __name__ == '__main__':
    unittest.main()
