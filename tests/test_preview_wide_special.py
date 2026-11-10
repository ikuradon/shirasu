"""Semantic and placement contracts for the unpublished special wide maps."""

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_image_renderer_v3 import changed, sample

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.text import Text
    from image_service import preview_wide_layout as wide
    from image_service import preview_wide_special as special
    from image_service import render as base
    from image_service.ash_reports import ash_plan
    from scripts.gallery_catalog import load_cases
    from test_ash_renderer import forecast as ash_forecast
    from test_flood_renderer import forecast as flood_forecast


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for wide map tests')
class WideSpecialTests(unittest.TestCase):
    def figure(self):
        fig = wide.figure()
        self.addCleanup(fig.clear)
        return fig

    def test_repeated_ash_forecasts_keep_discrete_hours_and_simultaneous_phenomena(self):
        reports = base._decode(ash_forecast((4620100, 1, 3), (4620100, 3, 3),
                                           (4620100, 6, 3), (4620100, 3, 4)))
        notice, = ash_plan(reports).notices
        lines, styles = special._ash_sidebar_lines(notice)
        self.assertEqual(lines, ('基点＋1・3・6時間：多量の降灰', '基点＋3時間：小さな噴石の落下'))
        self.assertEqual(len(styles), 2)
        self.assertNotEqual(styles[0], styles[1])
        self.assertNotIn('2', ''.join(lines))
        with self.assertRaises(ValueError):
            special._ash_sidebar_lines(SimpleNamespace(lines=('not a forecast',), styles=(styles[0],)))

    def test_received_eighteen_item_ash_bundle_keeps_all_hours_and_municipalities(self):
        case = next(case for case in load_cases() if case.slug == 'ash-detail')
        reports = base._decode(case.records)
        fig = self.figure()
        plan = special.draw_ash(fig, reports)
        self.assertEqual(len(plan.notices), 3)
        self.assertEqual(sum(len(notice.lines) for notice in plan.notices), 18)
        text = ''.join(label.get_text() for label in fig.findobj(Text))
        for notice in plan.notices:
            self.assertIn(notice.title, text)
        self.assertEqual(text.count('基点＋1時間：やや多量の降灰'), 2)
        self.assertEqual(text.count('基点＋2・3・4・5・6時間：少量の降灰'), 2)
        self.assertEqual(text.count('基点＋1・2・3・4・5・6時間：少量の降灰'), 1)
        wide.validate(fig)
        locator, = [ax for ax in fig.axes if ax.get_gid() == 'preview-locator']
        self.assertLess(locator.get_position().x1, wide.PANEL_X)

    def test_shared_flood_segments_reject_conflicts_before_adding_artists(self):
        fig = self.figure()
        records = flood_forecast((870705000101, 4), (870705000102, 1))
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            special.draw_flood(fig, base._decode(records))
        self.assertEqual(fig.axes, [])
        self.assertEqual(fig.texts, [])

    def test_missing_nwp_point_is_rejected_before_any_partial_presentation(self):
        fig = self.figure()
        positions = dict(special.nwp_map._positions())
        del positions[29]
        with patch.object(special.nwp_map, '_positions', return_value=positions):
            with self.assertRaises(ValueError):
                special.draw_nwp(fig, base._decode(sample('northwest_pacific_tsunami')))
        self.assertEqual(fig.axes, [])
        self.assertEqual(fig.texts, [])

    def test_numbered_labels_move_apart_without_changing_source_positions(self):
        fig = self.figure()
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_axis_off()
        view = wide.MapView(ax, None, (0, 1, 0, 1), (0, .755, 0, 1), wide.OBSTACLES)
        points = ((.3, .4), (.3, .4), (.301, .403))
        notices = [SimpleNamespace(code=index, color=wide.WHITE) for index in range(3)]
        boxes = special._numbered_markers(fig, view, points, notices, prefix='test-anchor')
        fig.canvas.draw()
        self.assertEqual(len(boxes), 3)
        for index, box in enumerate(boxes):
            self.assertFalse(any(base._overlap(box, other) for other in boxes[index + 1:]))
        for artist, expected in zip(ax.collections, points, strict=True):
            self.assertEqual(tuple(artist.get_offsets()[0]), expected)
        labels = [label for label in fig.texts if label.get_gid().startswith('wide-number-')]
        self.assertEqual({label.get_text() for label in labels}, {'1', '2', '3'})

    def test_nwp_potential_categories_remain_distinct_and_small_is_not_no_possibility(self):
        expected = {1: '破壊的津波の可能性：海洋全域', 2: '破壊的津波の可能性：地域規模',
                    3: '破壊的津波の可能性：震源付近（局地的）',
                    4: '局地的な破壊的津波の可能性はごく小さい'}
        for code, description in expected.items():
            with self.subTest(potential=code):
                fig = self.figure()
                reports = base._decode(changed('northwest_pacific_tsunami', (53, 3, code)))
                special.draw_nwp(fig, reports)
                text = ''.join(label.get_text() for label in fig.texts)
                self.assertIn(description, text)
                self.assertNotIn('可能性なし', text)
                self.assertIn('到達予想 11/10 00:20 UTC', text)


if __name__ == '__main__':
    unittest.main()
