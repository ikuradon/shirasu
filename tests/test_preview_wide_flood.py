"""Readable flood labels retain the exact received reach and warning state."""

import importlib.util
import unittest
from unittest.mock import patch

from test_image_renderer_v3 import changed, sample

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from matplotlib.colors import to_rgba
    from matplotlib.text import Text
    from image_service import flood_map
    from image_service import preview_wide_flood as flood
    from image_service import preview_wide_layout as wide
    from image_service import render as base
    from scripts.gallery_catalog import load_cases
    from test_flood_renderer import forecast


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for wide flood tests')
class WideFloodTests(unittest.TestCase):
    def figure(self):
        figure = wide.figure()
        self.addCleanup(figure.clear)
        return figure

    def case(self, slug):
        return next(case for case in load_cases() if case.slug == slug).records

    def test_single_reach_is_named_without_covering_or_replacing_any_source_line(self):
        fig = self.figure()
        plan = flood.draw_flood(fig, base._decode(self.case('flood-issue')))
        notice, = plan.notices
        wide.validate(fig)
        texts = {label.get_gid(): label for label in fig.texts}
        self.assertEqual(texts[f'flood-map-name-{notice.code}'].get_text(), '鬼怒川')
        self.assertEqual(texts[f'flood-name-{notice.code}'].get_text(), '鬼怒川')
        self.assertEqual(texts[f'flood-jurisdiction-{notice.code}'].get_text(), '栃木県・茨城県')
        self.assertFalse(any(identity.startswith('wide-number-flood') for identity in texts))
        source = flood_map._parts(flood_map._rivers()[notice.code])
        lines = [line for line in fig.axes[0].lines
                 if (line.get_gid() or '').startswith('flood-reach-')]
        self.assertEqual(len(lines), len(source))
        name_box = texts[f'flood-map-name-{notice.code}'].get_bbox_patch().get_window_extent(
            fig.canvas.get_renderer())
        for line, points in zip(lines, source, strict=True):
            np.testing.assert_array_equal(line.get_xydata(), points)
            path = line.get_transform().transform_path(line.get_path())
            self.assertFalse(path.intersects_bbox(name_box, filled=False))
        locator, = [ax for ax in fig.axes if ax.get_gid() == 'preview-locator']
        self.assertFalse(name_box.overlaps(locator.get_window_extent()))
        badge = texts[f'flood-hazard-{notice.code}']
        self.assertEqual(badge.get_text(), notice.lines[0])
        self.assertGreaterEqual(badge.get_fontsize(), 25)
        self.assertEqual(badge.get_bbox_patch().get_facecolor(),
                         to_rgba(notice.styles[0].background))

    def test_single_release_remains_dashed_and_mentions_advisory_transition(self):
        fig = self.figure()
        flood.draw_flood(fig, base._decode(self.case('flood-release')))
        wide.validate(fig)
        lines = [line for line in fig.axes[0].lines
                 if (line.get_gid() or '').startswith('flood-reach-')]
        self.assertTrue(lines)
        self.assertTrue(all(line.is_dashed() for line in lines))
        text = ''.join(label.get_text() for label in fig.findobj(Text))
        self.assertIn('警報解除', text)
        self.assertIn('氾濫注意情報への切替', text)
        self.assertNotIn('安全', text)
        self.assertNotIn('取消', text)

    def test_multiple_reaches_keep_every_reference_interval_and_warning(self):
        fig = self.figure()
        plan = flood.draw_flood(fig, base._decode(sample('flood')))
        wide.validate(fig)
        self.assertEqual(len(plan.notices), 3)
        text = ''.join(label.get_text() for label in fig.findobj(Text))
        labels = [label for label in fig.texts
                  if (label.get_gid() or '').startswith('wide-number-flood-anchor-')]
        self.assertEqual({label.get_text() for label in labels}, {'1', '2', '3'})
        for notice in plan.notices:
            self.assertIn(notice.title, text)
            self.assertIn(notice.lines[0], text)

    def test_conflicting_or_missing_forecasts_never_create_a_partial_map(self):
        for records in (forecast((870705000101, 4), (870705000102, 1)),
                        changed('flood', (53, 4, 15)), changed('flood', (57, 40, 0))):
            with self.subTest(records=records):
                fig = self.figure()
                with self.assertRaises(ValueError):
                    flood.draw_flood(fig, base._decode(records))
                self.assertEqual(fig.axes, [])
                self.assertEqual(fig.texts, [])
        fig = self.figure()
        with patch.object(flood_map, '_rivers', return_value={}):
            with self.assertRaises(ValueError):
                flood.draw_flood(fig, base._decode(self.case('flood-issue')))
        self.assertEqual(fig.axes, [])

    def test_title_split_retains_forecast_interval_qualifiers(self):
        for title, expected in (
                ('鬼怒川(栃木県・茨城県)', ('鬼怒川', '栃木県・茨城県')),
                ('江の川上流（広島県）', ('江の川上流', '広島県')),
                ('河川（区間名）上流(県名)', ('河川（区間名）上流', '県名')),
                ('江の川下流', ('江の川下流', None))):
            with self.subTest(title=title):
                self.assertEqual(flood._name_and_jurisdiction(title), expected)


if __name__ == '__main__':
    unittest.main()
