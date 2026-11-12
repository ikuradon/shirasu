"""Real weather badges, geometry, fallbacks and immutable URL behaviour."""

import hashlib
import importlib.util
import struct
import unittest
from unittest.mock import patch

from shirasu.images.protocol import make_path
from tests.images import test_image_renderer as helpers
from tests.images.test_image_renderer_v3 import sample, changed

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.colors import to_rgba
    from matplotlib.offsetbox import PaddedBox
    from matplotlib.text import Text
    from image_service import render as v1
    from image_service import render_v2 as v2
    from image_service import render_v3 as v3
    from image_service import render_v4 as v4
    from image_service import map_v3, map_v4


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for v4 PNG tests')
class WeatherColourRendererTests(unittest.TestCase):
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
            image = v4.render(records)
        return image, figures[-1]

    def texts(self, figure):
        return '\n'.join(text.get_text() for text in figure.findobj(Text))

    def badges(self, figure):
        result = {}
        for badge in figure.findobj(PaddedBox):
            texts = [text for text in badge.findobj(Text)
                     if (text.get_gid() or '').startswith('weather-hazard-')]
            self.assertEqual(len(texts), 1)
            result[texts[0].get_gid()] = (badge, texts[0])
        return result

    def assert_regions(self, figure, codes):
        self.assertTrue(figure.axes, 'All received regions must remain on the map')
        anchors = {artist.get_gid() for artist in figure.axes[0].collections
                   if (artist.get_gid() or '').startswith('region-anchor-')}
        self.assertEqual(anchors, {f'region-anchor-{code}' for code in codes})
        helpers.RendererTests.assert_clear_leaders(self, figure, figure.axes[0], len(codes))

    def test_mixed_region_has_separate_black_and_purple_badges_with_clear_leaders(self):
        image, figure = self.capture(sample('weather'))
        self.assertEqual(image.category, 'Weather')
        self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', image.png[16:24]), (1440, 1200))
        self.assert_regions(figure, {110000, 120000, 130010, 140000})
        badges = self.badges(figure)
        self.assertEqual(len(badges), 5)
        emergency, emergency_text = badges['weather-hazard-130010-0']
        landslide, landslide_text = badges['weather-hazard-130010-1']
        self.assertEqual(emergency_text.get_text(), '大雨特別警報')
        self.assertEqual(landslide_text.get_text(), '土砂災害警戒情報')
        self.assertEqual(emergency.patch.get_facecolor(), to_rgba('#0c000c'))
        self.assertEqual(emergency.patch.get_edgecolor(), to_rgba('#ffffff'))
        self.assertEqual(landslide.patch.get_facecolor(), to_rgba('#aa00aa'))
        for text in (emergency_text, landslide_text):
            self.assertEqual(to_rgba(text.get_color()), to_rgba('#ffffff'))
            self.assertEqual(text.get_path_effects(), [], 'Halos must not obscure badge lettering')
        texts = self.texts(figure)
        for expected in ('訓練', '東京地方', '神奈川県', '千葉県', '埼玉県',
                         '竜巻注意情報', '区域全体の警戒・解除'):
            self.assertIn(expected, texts)
        # PaddedBox includes the coloured background and border in its extent.
        renderer = figure.canvas.get_renderer()
        for annotation in figure.axes[0].artists:
            if not hasattr(annotation, 'offsetbox'):
                continue
            outer = annotation.offsetbox.get_window_extent(renderer)
            for badge in annotation.findobj(PaddedBox):
                inner = badge.get_window_extent(renderer)
                self.assertGreaterEqual(inner.x0, outer.x0 - 1)
                self.assertGreaterEqual(inner.y0, outer.y0 - 1)
                self.assertLessEqual(inner.x1, outer.x1 + 1)
                self.assertLessEqual(inner.y1, outer.y1 + 1)

    def test_other_information_keeps_names_without_an_invented_warning_class(self):
        _, figure = self.capture(changed('weather', (56, 5, 21)))
        matching = [(badge, text) for badge, text in self.badges(figure).values()
                    if text.get_text() in ('記録的短時間大雨情報', '竜巻注意情報')]
        self.assertEqual({text.get_text() for _, text in matching},
                         {'記録的短時間大雨情報', '竜巻注意情報'})
        for badge, text in matching:
            self.assertEqual(badge.patch.get_facecolor()[3], 0)
            self.assertEqual(to_rgba(text.get_color()), to_rgba('#ffffff'))
        self.assertNotIn('レベル', self.texts(figure))

    def test_forecast_boundaries_and_points_do_not_claim_uniform_severity(self):
        _, figure = self.capture(sample('weather'))
        areas = [artist for artist in figure.axes[0].patches
                 if (artist.get_gid() or '').startswith('region-area-')]
        self.assertEqual(len(areas), 4)
        for area in areas:
            self.assertEqual(area.get_edgecolor(), to_rgba('#8b98a5'))
            self.assertEqual(area.get_facecolor(), to_rgba('#a3b3c2', .035))
        anchors = [artist for artist in figure.axes[0].collections
                   if (artist.get_gid() or '').startswith('region-anchor-')]
        for anchor in anchors:
            self.assertEqual(tuple(anchor.get_edgecolors()[0]), to_rgba('#ffffff'))

    def test_release_removes_active_colours_but_preserves_which_information_ended(self):
        _, figure = self.capture(changed('weather', (53, 3, 2), (56, 5, 7)))
        self.assert_regions(figure, {110000, 120000, 130010, 140000})
        texts = self.texts(figure)
        self.assertIn('【解除】', texts)
        self.assertIn('全ての気象特別警報（解除）', texts)
        self.assertIn('土砂災害警戒情報（解除）', texts)
        for badge, text in self.badges(figure).values():
            self.assertIn('（解除）', text.get_text())
            self.assertEqual(badge.patch.get_facecolor()[3], 0)
            self.assertEqual(badge.patch.get_edgecolor(), to_rgba('#c8c8cb'))
            self.assertEqual(to_rgba(text.get_color()), to_rgba('#c8c8cb'))

    def test_cancellation_and_unknown_codes_preserve_complete_summary(self):
        for fields in (((41, 2, 2),), ((56, 5, 31),), ((61, 19, 500000),)):
            with self.subTest(fields=fields):
                records = changed('weather', *fields)
                image, figure = self.capture(records)
                self.assertEqual(figure.axes, [])
                self.assertEqual(image.png, v2.render(records).png)
                if fields[0][0] == 41:
                    self.assertIn('取消', self.texts(figure))

    def test_missing_geometry_or_unplaceable_badges_never_show_a_partial_map(self):
        records = sample('weather')
        original = map_v3._expand

        def missing_region(layer, code):
            if code == 140000:
                raise ValueError('One received region is missing')
            return original(layer, code)

        for target, effect in ((map_v3, missing_region),
                               (map_v4.style, ValueError('No readable badge placement'))):
            name = '_expand' if target is map_v3 else '_place_labels'
            with self.subTest(missing=name), patch.object(target, name, side_effect=effect):
                image, figure = self.capture(records)
            self.assertEqual(figure.axes, [])
            self.assertEqual(image.png, v2.render(records).png)

    def test_new_weather_output_is_deterministic_and_does_not_mutate_v3(self):
        records = sample('weather')
        old = v3.render(records)
        first = v4.render(records)
        self.assertEqual(first, v4.render(records))
        self.assertEqual(old, v3.render(records))
        self.assertNotEqual(first.png, old.png)

    def test_all_other_existing_image_categories_are_byte_identical_to_v3(self):
        cases = [sample(name) for name in ('eew', 'volcano', 'marine', 'seismic_intensity', 'tsunami')]
        cases.extend((helpers.TYPHOON, (helpers.hypocenter(),)))
        for records in cases:
            with self.subTest(category=type(v1._decode(records)[0]).__name__):
                self.assertEqual(v4.render(records), v3.render(records))

    def test_signed_v4_http_and_conditional_cache_do_not_accept_v3_relabeling(self):
        records = sample('weather')
        path = make_path(records, 'test', helpers.KEY, version='v4')
        response = helpers.request(path)
        self.assertEqual(response['status'], '200 OK')
        self.assertEqual(response['headers']['Content-Type'], 'image/png')
        self.assertIn('immutable', response['headers']['Cache-Control'])
        self.assertEqual(response['body'], v4.render(records).png)
        etag = '"' + hashlib.sha256(response['body']).hexdigest() + '"'
        self.assertEqual(response['headers']['ETag'], etag)
        conditional = helpers.request(path, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(conditional['status'], '304 Not Modified')
        self.assertEqual(conditional['body'], b'')
        for tampered in (path.replace('/v4/', '/v3/', 1),
                         make_path(records, 'test', helpers.KEY, version='v3').replace('/v3/', '/v4/', 1)):
            response = helpers.request(tampered)
            self.assertEqual(response['status'], '403 Forbidden')
            self.assertEqual(response['headers']['Cache-Control'], 'no-store')


if __name__ == '__main__':
    unittest.main()
