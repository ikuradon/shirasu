"""Seismic markers must show received values, not raw codes or station readings."""
import importlib.util
import unittest
from unittest.mock import patch

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.text import Text
    from matplotlib.patches import Patch
    from image_service import preview_wide_layout as wide
    from image_service import preview_wide_regions as maps
    from image_service import regional_reports
    from image_service import render as base
    from scripts.gallery_catalog import load_cases
    from test_regional_reports import report
    import test_flood_renderer as flood_helpers


@unittest.skipUnless(HAS_RENDERER, 'Install the isolated image renderer dependencies')
class SeismicMarkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def draw(self, reports):
        fig = wide.figure()
        self.addCleanup(fig.clear)
        plan = maps.draw_seismic(fig, reports)
        wide.validate(fig)
        return fig, plan

    def received(self, *pairs):
        fields = [(69, 144, 0)]
        for index, (prefecture, intensity) in enumerate(pairs):
            fields.extend(((69 + 9 * index, 3, intensity), (72 + 9 * index, 6, prefecture)))
        return report('seismic_intensity', fields)

    def labels(self, fig):
        return {text.get_gid(): text.get_text().replace('\n', '') for text in fig.findobj(Text)
                if (text.get_gid() or '').startswith('seismic-intensity-')}

    def text(self, fig):
        return ''.join(label.get_text() for label in fig.findobj(Text)).replace('\n', '').replace(' ', '')

    def test_recorded_nine_prefectures_show_actual_intensities_and_grouped_names(self):
        fig, plan = self.draw(base._decode(self.cases['seismic-intensity'].records))
        self.assertEqual(self.labels(fig), {
            'seismic-intensity-35-2': '4', 'seismic-intensity-38-2': '4',
            'seismic-intensity-39-3': '5−', 'seismic-intensity-40-2': '4',
            'seismic-intensity-41-2': '4', 'seismic-intensity-43-3': '5−',
            'seismic-intensity-44-4': '5＋', 'seismic-intensity-45-4': '5＋',
            'seismic-intensity-46-2': '4',
        })
        text = self.text(fig)
        for name in ('山口県', '愛媛県', '高知県', '福岡県', '佐賀県', '熊本県', '大分県', '宮崎県', '鹿児島県'):
            self.assertIn(name, text)
        for phrase in ('県の代表位置', '受信した最大震度', '震度5強', '震度5弱', '震度4'):
            self.assertIn(phrase, text)
        self.assertFalse(any((artist.get_gid() or '').startswith(('region-reference-', 'region-anchor-'))
                             for artist in fig.findobj()))
        self.assertIn(plan.notice_lines[0].replace(' ', ''), text)

    def test_all_seven_codes_preserve_less_than_four_and_weak_strong_distinctions(self):
        pairs = ((1, 1), (2, 2), (15, 3), (13, 4), (23, 5), (34, 6), (46, 7))
        fig, _ = self.draw([self.received(*pairs)])
        self.assertEqual(self.labels(fig), dict(zip(
            ('seismic-intensity-1-1', 'seismic-intensity-2-2', 'seismic-intensity-15-3',
             'seismic-intensity-13-4', 'seismic-intensity-23-5', 'seismic-intensity-34-6',
             'seismic-intensity-46-7'), ('4未満', '4', '5−', '5＋', '6−', '6＋', '7'), strict=True)))
        self.assertIn('震度4未満', self.text(fig))
        self.assertNotIn('震度3', self.text(fig))

    def test_multiple_values_in_one_prefecture_remain_visible_without_duplicate_badges(self):
        received = self.received((14, 4), (14, 3), (14, 4))
        fig, _ = self.draw([received, received])
        self.assertEqual(self.labels(fig), {'seismic-intensity-14-4': '5＋',
                                           'seismic-intensity-14-3': '5−'})
        text = self.text(fig)
        self.assertEqual(text.count('神奈川県'), 2)
        self.assertIn('震度5強', text)
        self.assertIn('震度5弱', text)

    def test_badges_are_readable_and_avoid_each_other_and_the_plain_locator(self):
        fig, _ = self.draw(base._decode(self.cases['seismic-intensity'].records))
        renderer = fig.canvas.get_renderer()
        badges = [artist for artist in fig.findobj(Patch)
                  if (artist.get_gid() or '').startswith('seismic-badge-')]
        self.assertEqual(len(badges), 9)
        boxes = [artist.get_window_extent(renderer) for artist in badges]
        for index, box in enumerate(boxes):
            self.assertGreaterEqual(box.width, 48)
            self.assertGreaterEqual(box.height, 48)
            self.assertAlmostEqual(box.width, box.height)
            self.assertFalse(any(box.overlaps(other) for other in boxes[index + 1:]))
            self.assertLess(box.x1, wide.PANEL_X * wide.WIDTH)
            self.assertLess(box.y1, .78 * wide.HEIGHT)
        for label in fig.findobj(Text):
            if (label.get_gid() or '').startswith('seismic-intensity-'):
                identity = label.get_gid().replace('seismic-intensity-', 'seismic-badge-')
                badge = next(item for item in badges if item.get_gid() == identity)
                outer, inner = badge.get_window_extent(renderer), label.get_window_extent(renderer)
                self.assertTrue(outer.contains(inner.x0, inner.y0))
                self.assertTrue(outer.contains(inner.x1, inner.y1))
        locator, = [ax for ax in fig.axes if ax.get_gid() == 'preview-locator']
        self.assertFalse(locator.lines)
        self.assertFalse(locator.collections)
        self.assertFalse(any(box.overlaps(locator.get_window_extent(renderer)) for box in boxes))
        self.assertEqual({p.get_gid() for p in locator.patches},
                         {'v3-land', 'preview-main-extent', None})

    def test_tokyo_has_one_prefectural_value_and_no_invented_island_observations(self):
        palette = dict(regional_reports.INTENSITY_COLORS)
        fig, _ = self.draw(base._decode(self.cases['seismic-islands'].records))
        self.assertEqual(self.labels(fig), {'seismic-intensity-13-3': '5−'})
        self.assertEqual(regional_reports.INTENSITY_COLORS, palette)
        self.assertEqual((base.WIDTH, base.HEIGHT), (1440, 1200))

    def test_unplaceable_markers_fall_back_as_a_whole_bundle(self):
        with patch.object(maps, '_seismic_positions', side_effect=ValueError('No legible placement')):
            image, fig = flood_helpers.FloodRendererTests.capture(self, self.cases['seismic-intensity'].records)
        self.assertEqual(image.category, 'SeismicIntensity')
        self.assertEqual(fig.axes, [])
        self.assertEqual(self.labels(fig), {})
        self.assertIn('要旨', self.text(fig))


if __name__ == '__main__':
    unittest.main()
