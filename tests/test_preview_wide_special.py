"""Semantic and placement contracts for the unpublished special wide maps."""

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_image_renderer_v3 import changed, sample

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from matplotlib.text import Text
    from matplotlib.colors import to_rgba
    import numpy as np
    from image_service import preview_wide_layout as wide
    from image_service import preview_wide_special as special
    from image_service import nwp_map
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
        hours = special._ash_hours(notice)
        self.assertEqual(hours, {1: {'ash': 3}, 2: {}, 3: {'ash': 3, 'cinders': 4},
                                 4: {}, 5: {}, 6: {'ash': 3}})
        with self.assertRaises(ValueError):
            special._ash_hours(SimpleNamespace(lines=('not a forecast',), styles=(notice.styles[0],)))
        fig = self.figure()
        special.draw_ash(fig, reports)
        wide.validate(fig)
        cells = {artist.get_gid(): artist for artist in fig.artists
                 if (artist.get_gid() or '').startswith('ash-cell-')}
        self.assertEqual(len(cells), 12, 'Cinders must use a lane independent of ash')
        self.assertEqual(cells['ash-cell-4620100-3-ash'].get_facecolor(), to_rgba('#ff0000'))
        self.assertEqual(cells['ash-cell-4620100-3-cinders'].get_edgecolor(), to_rgba('#ff0000'))
        text = {label.get_gid(): label.get_text() for label in fig.texts}
        for hour in (2, 4, 5):
            self.assertEqual(text[f'ash-cell-4620100-{hour}-ash-missing'], '—')
        self.assertIn('—：この時間の記載なし', ''.join(text.values()))

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
        cells = {artist.get_gid(): artist for artist in fig.artists
                 if (artist.get_gid() or '').startswith('ash-cell-')}
        self.assertEqual(len(cells), 18)
        for notice in plan.notices:
            for line, style in zip(notice.lines, notice.styles, strict=True):
                hour = int(line.removeprefix('基点＋').split('時間')[0])
                self.assertEqual(cells[f'ash-cell-{notice.code}-{hour}-ash'].get_facecolor(),
                                 to_rgba(style.background))
        self.assertNotIn('基点＋2・3・4', text)
        wide.validate(fig)
        locator, = [ax for ax in fig.axes if ax.get_gid() == 'preview-locator']
        self.assertLess(locator.get_position().x1, wide.PANEL_X)

    def test_inland_aso_map_retains_coastal_context_and_correct_land_sea_colours(self):
        case = next(case for case in load_cases() if case.slug == 'ash-detail')
        fig = self.figure()
        special.draw_ash(fig, base._decode(case.records))
        wide.validate(fig)
        main = fig.axes[0]
        pixels = np.asarray(fig.canvas.buffer_rgba())
        # Kumamoto's adjacent open water must be visible in the primary map,
        # not only behind the header, information panel, or locator.
        for lon, lat in ((130.45, 32.7),):
            pixel = main.transData.transform((lon, base._my(lat)))
            x, y = fig.transFigure.inverted().transform(pixel)
            self.assertTrue(.015 < x < wide.PANEL_X and .055 < y < .78)
            self.assertFalse(wide.LOCATOR_OBSTACLE[0] <= x <= wide.LOCATOR_OBSTACLE[2]
                             and wide.LOCATOR_OBSTACLE[1] <= y <= wide.LOCATOR_OBSTACLE[3])
            actual = pixels[wide.HEIGHT - 1 - round(pixel[1]), round(pixel[0]), :3]
            expected = np.rint(np.asarray(to_rgba(wide.SEA)[:3]) * 255).astype(int)
            np.testing.assert_array_equal(actual, expected)
        # Adaptive county framing need not include another prefecture's bay.
        # Require a recognisable sea area in the unobstructed map body itself.
        body = pixels[round(.24 * wide.HEIGHT):round(.89 * wide.HEIGHT),
                      round(.04 * wide.WIDTH):round(.55 * wide.WIDTH), :3]
        self.assertGreater(np.all(body == expected, axis=2).mean(), .02)
        volcano, = [artist for artist in main.collections
                    if (artist.get_gid() or '').startswith('volcano-point-')]
        x, y = volcano.get_offsets()[0]
        land, = [artist for artist in main.patches if artist.get_gid() == 'v3-land']
        self.assertTrue(land.get_path().contains_point((x, y)))
        self.assertEqual(land.get_facecolor(), to_rgba(wide.LAND))

    def test_shared_flood_segments_reject_conflicts_before_adding_artists(self):
        fig = self.figure()
        records = flood_forecast((870705000101, 4), (870705000102, 1))
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            special.draw_flood(fig, base._decode(records))
        self.assertEqual(fig.axes, [])
        self.assertEqual(fig.texts, [])

    def test_missing_nwp_point_is_rejected_before_any_partial_presentation(self):
        fig = self.figure()
        positions = dict(nwp_map._positions())
        del positions[29]
        with patch.object(nwp_map, '_positions', return_value=positions):
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
