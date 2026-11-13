"""Recorded DCX and explicit training variants follow geometry → area → topic."""

from copy import deepcopy
from dataclasses import replace
import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.images import test_flood_renderer as helpers
from tests.images.test_image_renderer import change_fields

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from matplotlib.colors import to_rgba
    from matplotlib.path import Path
    from image_service import ash_map, region_map
    from image_service import render as base
    from image_service import preview_wide_dcx as dcx_images
    from image_service import preview_dcx_areas as dcx_areas
    from image_service import preview_wide_layout as wide
    from image_service.preview_dcx_reports import Area, Point, SecondaryEllipse, dcx_plan
    from tools.preview.gallery_catalog import load_cases
    from shirasu.reports.format import ATTRIBUTION, report_message


@unittest.skipUnless(HAS_RENDERER, 'DCX rendering uses the isolated image renderer')
class DcxRenderingTests(unittest.TestCase):
    capture = helpers.FloodRendererTests.capture

    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def text(self, fig):
        return '\n'.join(text.get_text() for text in fig.texts)

    def assert_topic(self, fig):
        self.assertFalse(fig.axes)
        self.assertTrue(any(text.get_gid() == 'dcx-topic' for text in fig.texts))
        self.assertFalse(any(text.get_gid() == 'wide-summary' for text in fig.texts))
        self.assertNotIn('A12 -', self.text(fig))

    def assert_plain_locator(self, fig):
        for ax in fig.axes[1:]:
            self.assertEqual(ax.get_gid(), 'preview-locator')
            self.assertFalse(ax.texts or ax.lines or ax.collections)
            self.assertEqual({patch.get_gid() for patch in ax.patches},
                             {'v3-land', None, 'preview-main-extent'})

    def assert_visible(self, fig, points):
        positions = fig.transFigure.inverted().transform(fig.axes[0].transData.transform(points))
        self.assertTrue(np.all((positions[:, 0] >= .015) & (positions[:, 0] < wide.PANEL_X)))
        self.assertTrue(np.all((positions[:, 1] >= .055) & (positions[:, 1] < .78)))
        if len(fig.axes) > 1:
            left, bottom, right, top = wide.LOCATOR_OBSTACLE
            self.assertFalse(np.any((left <= positions[:, 0]) & (positions[:, 0] <= right)
                                    & (bottom <= positions[:, 1]) & (positions[:, 1] <= top)))

    def test_original_domestic_versions_use_short_topics_without_repeating_the_report(self):
        for slug in ('dcx-lalert', 'dcx-jalert', 'dcx-mtinfo'):
            records = self.cases[slug].records
            with self.subTest(slug=slug):
                original = base._decode(records)[0]
                text = str(original)
                _, fig = self.capture(records)
                self.assert_topic(fig)
                self.assertIn('【訓練・試験】', self.text(fig))
                self.assertIn('詳細・指示内容は投稿本文を参照', self.text(fig))
                self.assertEqual(str(base._decode(records)[0]), text)

    def test_recorded_foreign_ellipse_is_a_scope_without_an_invented_disaster_pin(self):
        _, fig = self.capture(self.cases['dcx-outside-japan'].records)
        ellipses = [p for p in fig.axes[0].patches
                    if (p.get_gid() or '').startswith('dcx-target-ellipse-')]
        self.assertEqual(len(ellipses), 1)
        self.assert_visible(fig, ellipses[0].get_xy())
        self.assertFalse(any((a.get_gid() or '').startswith('dcx-hazard-point-')
                             for a in fig.axes[0].collections))
        self.assertIn('被害範囲ではありません', self.text(fig))
        self.assert_plain_locator(fig)
        self.assertLess(fig.axes[1].get_xlim()[1], 130,
                        'The overseas locator must use its own region rather than fixed Japan')

    def test_b4_earthquake_keeps_the_entire_map_unavailable_and_the_original_text(self):
        records = self.cases['dcx-earthquake-vector'].records
        report, = base._decode(records)
        before = str(report), deepcopy(report.camf.get_params()), report.nmea
        expected_post = f'{report}\n\n{ATTRIBUTION}\n---\nNMEA: {report.nmea}'
        # Valid main geometry cannot create a plausible partial map while the
        # earthquake's D3 reference direction and surface transform are unknown.
        with patch.object(dcx_images, 'ellipse_ring') as ellipse, \
                patch.object(dcx_images, 'secondary_ellipse_ring') as secondary:
            image, fig = self.capture(records)
        ellipse.assert_not_called()
        secondary.assert_not_called()
        self.assert_topic(fig)
        self.assertEqual(image.decision.mode, 'topic')
        failures = [item for item in image.decision.reasons if item.stage == 'geometry']
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0].code, failures[0].detail),
                         ('unsupported_geometry', 'B4地震: 震央方位D3の基準方向・地点変換が未確認'))
        self.assertIn('地震情報', self.text(fig))
        self.assertIn('【訓練・試験】', self.text(fig))
        for label in ('震央位置', '震源の深さ', 'D3', 'D4'):
            self.assertNotIn(label, self.text(fig))
        self.assertEqual(report_message(report), expected_post)
        self.assertEqual((str(report), report.camf.get_params(), report.nmea), before)
        decoded_again, = base._decode(records)
        self.assertEqual(str(decoded_again), before[0])
        self.assertEqual(report_message(decoded_again), expected_post)

    def test_secondary_and_overlapping_ellipses_keep_both_complete_scopes(self):
        for slug in ('dcx-secondary-ellipse', 'dcx-secondary-overlap'):
            with self.subTest(slug=slug):
                records = self.cases[slug].records
                plan = dcx_plan(base._decode(records))
                main, secondary = plan.geometries
                self.assertIsInstance(secondary, SecondaryEllipse)
                image, fig = self.capture(records)
                self.assertEqual(image.decision.mode, 'geometry')
                shapes = {p.get_gid(): p for p in fig.axes[0].patches
                          if (p.get_gid() or '').startswith('dcx-')}
                self.assertEqual(set(shapes), {'dcx-target-ellipse-0', 'dcx-secondary-ellipse-0'})
                primary_patch, secondary_patch = shapes['dcx-target-ellipse-0'], shapes['dcx-secondary-ellipse-0']
                np.testing.assert_allclose(primary_patch.get_xy(),
                                           dcx_images.ellipse_ring(main, reference=main.lon))
                np.testing.assert_allclose(secondary_patch.get_xy(),
                                           dcx_images.secondary_ellipse_ring(secondary, reference=main.lon))
                for shape in shapes.values():
                    self.assert_visible(fig, shape.get_xy())
                self.assertGreater(primary_patch.get_zorder(), secondary_patch.get_zorder())
                self.assertEqual(secondary_patch.get_facecolor()[3], 0,
                                 'Overlaps must not blend into an invented third warning colour')
                self.assertEqual(primary_patch.get_linestyle(), '-')
                self.assertEqual(secondary_patch.get_linestyle(), '--')
                self.assertNotEqual(primary_patch.get_edgecolor(), secondary_patch.get_edgecolor())
                self.assertFalse(any((artist.get_gid() or '').startswith('dcx-hazard-point-')
                                     for artist in fig.axes[0].collections))
                labels = self.text(fig).replace('\n', '')
                for phrase in ('主範囲（実線）', '第2範囲（破線）', '個別指示は投稿本文へ',
                               '重なる区域は主範囲の指示を優先'):
                    self.assertIn(phrase, labels)
                self.assert_plain_locator(fig)

    def test_unprojectable_secondary_discards_the_main_ellipse_as_well(self):
        records = self.cases['dcx-secondary-ellipse'].records
        with patch.object(dcx_images, 'secondary_ellipse_ring', side_effect=ValueError('bad secondary projection')):
            image, fig = self.capture(records)
        self.assert_topic(fig)
        self.assertEqual(image.decision.mode, 'topic')
        self.assertTrue(any(item.stage == 'geometry' for item in image.decision.reasons))

    def test_evacuation_destination_is_yellow_without_overriding_the_main_scope(self):
        for slug in ('dcx-secondary-evacuation', 'dcx-evacuation-overlap'):
            with self.subTest(slug=slug):
                records = self.cases[slug].records
                report, = base._decode(records)
                plan = dcx_plan((report,))
                main, destination = plan.geometries
                self.assertEqual(destination.role, 'evacuation_destination')
                image, fig = self.capture(records)
                self.assertEqual(image.decision.mode, 'geometry')
                self.assertFalse(image.decision.reasons)
                shapes = {p.get_gid(): p for p in fig.axes[0].patches
                          if (p.get_gid() or '').startswith('dcx-')}
                self.assertEqual(set(shapes), {'dcx-target-ellipse-0', 'dcx-secondary-ellipse-0'})
                primary, secondary = shapes['dcx-target-ellipse-0'], shapes['dcx-secondary-ellipse-0']
                np.testing.assert_allclose(primary.get_xy(), dcx_images.ellipse_ring(main, reference=main.lon))
                np.testing.assert_allclose(secondary.get_xy(),
                                           dcx_images.secondary_ellipse_ring(destination, reference=main.lon))
                for shape in shapes.values():
                    self.assert_visible(fig, shape.get_xy())
                self.assertEqual(secondary.get_edgecolor(), to_rgba('#ffd166'))
                self.assertEqual(secondary.get_facecolor()[3], 0,
                                 'A designated destination is not a uniformly safe filled area')
                self.assertEqual(secondary.get_linestyle(), '--')
                self.assertEqual(primary.get_linestyle(), '-')
                self.assertGreater(primary.get_zorder(), secondary.get_zorder())
                labels = self.text(fig).replace('\n', '')
                for phrase in ('通報の主範囲・避難先の指定', '避難先の指定（破線）',
                               '通報で指定された範囲', '重なる区域は主範囲の指示を優先'):
                    self.assertIn(phrase, labels)
                self.assertNotIn('安全区域', labels)
                self.assertNotIn('安全な場所', labels)
                self.assertFalse(any((artist.get_gid() or '').startswith('dcx-hazard-point-')
                                     for artist in fig.axes[0].collections))
                self.assert_plain_locator(fig)
                self.assertIn('IC-A-03', report_message(report))

    def test_unprojectable_evacuation_keeps_default_post_guidance_but_no_partial_map(self):
        records = self.cases['dcx-secondary-evacuation'].records
        report, = base._decode(records)
        text = report_message(report)
        self.assertIn('IC-A-03', text)
        with patch.object(dcx_images, 'secondary_ellipse_ring',
                          side_effect=ValueError('DCX ellipse has no unique near WGS84 surface')):
            image, fig = self.capture(records)
        self.assert_topic(fig)
        self.assertEqual(image.decision.mode, 'topic')
        self.assertTrue(any(reason.code == 'projection_unavailable' for reason in image.decision.reasons))
        self.assertEqual(report_message(report), text)

    def test_evacuation_release_retains_its_status_and_received_destination(self):
        sentence, at = self.cases['dcx-secondary-evacuation'].records[0]
        records = ((change_fields(sentence, ((24, 2, 3),)), at),)
        image, fig = self.capture(records)
        self.assertEqual(image.decision.mode, 'geometry')
        self.assertIn('【解除】', self.text(fig))
        self.assertNotIn('取消', self.text(fig))
        self.assertTrue(any(p.get_gid() == 'dcx-secondary-ellipse-0' for p in fig.axes[0].patches))
        report, = base._decode(records)
        self.assertIn('【解除】', report_message(report))

    def test_municipal_and_prefectural_maps_keep_every_received_component(self):
        for slug, expected_codes in (('dcx-municipality', {120200}),
                                     ('dcx-ward', {110100}),
                                     ('dcx-wards', {110100, 110200}),
                                     ('dcx-prefectures', {1, 2, 3})):
            with self.subTest(slug=slug):
                _, fig = self.capture(self.cases[slug].records)
                actual = [p for p in fig.axes[0].patches
                          if (p.get_gid() or '').startswith('region-area-dcx-')]
                self.assertEqual({int(p.get_gid().rsplit('-', 1)[1]) for p in actual}, expected_codes)
                expected_count = 0
                for code in expected_codes:
                    features = ((dcx_areas.municipality(code),) if code > 47 else
                                region_map._layer('prefectures')[code])
                    expected_count += sum(len(part) for f in features
                                          for part in region_map._geometry(f, 'prefectures', 139.))
                self.assertEqual(len(actual), len(expected_codes))
                self.assertEqual(sum(np.count_nonzero(p.get_path().codes == Path.MOVETO)
                                     for p in actual), expected_count)
                for polygon in actual:
                    self.assert_visible(fig, polygon.get_path().vertices)
                self.assert_plain_locator(fig)

    def test_refined_ellipse_variant_retains_exact_boundary_and_has_no_false_city_fallback(self):
        records = self.cases['dcx-ellipse'].records
        report, = base._decode(records)
        plan = dcx_plan((report,))
        self.assertEqual(plan.areas, (), 'EX1 is not a substitute while a main ellipse is supplied')
        _, fig = self.capture(records)
        polygon, = [p for p in fig.axes[0].patches
                    if (p.get_gid() or '').startswith('dcx-target-ellipse-')]
        np.testing.assert_allclose(polygon.get_xy(), dcx_images.ellipse_ring(plan.geometries[0]))
        with patch.object(dcx_images, 'ellipse_ring', side_effect=ValueError('unsupported projection')):
            _, fallback = self.capture(records)
        self.assert_topic(fallback)

    def test_mtinfo_keeps_complete_notification_and_additional_ranges_with_direction_labels(self):
        for slug, role, colour, instruction, mode in (
                ('dcx-mtinfo-leave', 'evacuate_from', '#f5a66b', '範囲から離れる指示', 'geometry'),
                ('dcx-mtinfo-head', 'evacuate_to', '#ffd166', '範囲へ向かう指示', 'geometry'),
                ('dcx-mtinfo-area', 'evacuate_to', '#ffd166', '範囲へ向かう指示', 'area')):
            with self.subTest(slug=slug):
                records = self.cases[slug].records
                report, = base._decode(records)
                before = str(report), deepcopy(report.camf.get_params()), report.nmea
                plan = dcx_plan((report,))
                self.assertEqual(plan.additional_ellipse.role, role)
                image, fig = self.capture(records)
                self.assertEqual(image.decision.mode, mode)
                extra, = [p for p in fig.axes[0].patches if p.get_gid() == 'dcx-additional-ellipse']
                reference = plan.geometries[0].lon if mode == 'geometry' else 139.
                np.testing.assert_allclose(extra.get_xy(),
                                           dcx_images.ellipse_ring(plan.additional_ellipse, reference=reference))
                self.assertEqual(extra.get_edgecolor(), to_rgba(colour))
                self.assertEqual(extra.get_facecolor()[3], 0)
                self.assertEqual(extra.get_linestyle(), '--')
                self.assert_visible(fig, extra.get_xy())
                if mode == 'geometry':
                    primary, = [p for p in fig.axes[0].patches if p.get_gid() == 'dcx-target-ellipse-0']
                    np.testing.assert_allclose(primary.get_xy(),
                                               dcx_images.ellipse_ring(plan.geometries[0], reference=reference))
                    self.assert_visible(fig, primary.get_xy())
                    self.assertEqual(primary.get_linestyle(), '-')
                    self.assertEqual(plan.areas, (), 'A received EX1 must not enlarge a primary ellipse')
                else:
                    primary, = [p for p in fig.axes[0].patches if p.get_gid() == 'region-area-dcx-120200']
                    feature = dcx_areas.municipality(120200)
                    paths = [geometry for part in region_map._geometry(feature, 'prefectures', 139.)
                             for geometry in part]
                    self.assertEqual(np.count_nonzero(primary.get_path().codes == Path.MOVETO), len(paths))
                    self.assert_visible(fig, primary.get_path().vertices)
                    self.assertEqual(plan.geometries, (), 'A missing main ellipse is not a pole-centred target')
                labels = self.text(fig).replace('\n', '')
                for label in ('通知対象・追加の避難指示範囲', '通知対象（実線）', '追加範囲（破線）', instruction):
                    self.assertIn(label, labels)
                for invented in ('主範囲の指示を優先', '安全区域', '安全な場所', 'IC-A-03'):
                    self.assertNotIn(invented, labels)
                self.assertFalse(any((artist.get_gid() or '').startswith('dcx-hazard-point-')
                                     for artist in fig.axes[0].collections))
                self.assert_plain_locator(fig)
                self.assertEqual((str(report), report.camf.get_params(), report.nmea), before)
                self.assertTrue(report_message(report).endswith(f'{report}\n\n{ATTRIBUTION}\n---\nNMEA: {report.nmea}'))

    def test_mtinfo_invalid_and_polar_additional_ranges_never_leave_a_primary_only_map(self):
        for slug in ('dcx-mtinfo-leave', 'dcx-mtinfo-area'):
            sentence, at = self.cases[slug].records[0]
            for fields, reason in (
                    (((197, 5, 0), (202, 5, 31)), 'invalid_fields'),
                    (((162, 52, 1 << 51),), 'projection_unavailable')):
                with self.subTest(slug=slug, reason=reason):
                    records = ((change_fields(sentence, fields), at),)
                    report, = base._decode(records)
                    if reason == 'projection_unavailable':
                        # The pinned decoder ignores EX2 when detecting an
                        # absent extension. EX2 alone still specifies a real
                        # south-pole ellipse and a "head to" instruction.
                        self.assertTrue(report.ignore_ex2_to_ex7)
                        extra = dcx_plan((report,)).additional_ellipse
                        self.assertIsNotNone(extra)
                        self.assertEqual((extra.lat, extra.lon, extra.role), (-90., 45., 'evacuate_to'))
                        self.assertIn('向かう', report_message(report))
                    image, fig = self.capture(records)
                    self.assert_topic(fig)
                    self.assertEqual(image.decision.mode, 'topic')
                    self.assertTrue(any(item.code == reason and item.stage == 'geometry'
                                        for item in image.decision.reasons))
                    self.assertTrue(report_message(report).endswith(f'{report}\n\n{ATTRIBUTION}\n---\nNMEA: {report.nmea}'))

    def test_mtinfo_missing_primary_area_does_not_leave_an_additional_only_map(self):
        records = self.cases['dcx-mtinfo-area'].records
        self.assertIsNotNone(dcx_plan(base._decode(records)).additional_ellipse)
        with patch.object(dcx_areas, 'municipality', return_value=None):
            image, fig = self.capture(records)
        self.assert_topic(fig)
        self.assertEqual(image.decision.mode, 'topic')
        self.assertTrue(any(item.code == 'missing_boundary' for item in image.decision.reasons))

    def test_mtinfo_release_keeps_direction_and_both_boundary_styles_distinct(self):
        sentence, at = self.cases['dcx-mtinfo-leave'].records[0]
        records = ((change_fields(sentence, ((24, 2, 3),)), at),)
        image, fig = self.capture(records)
        self.assertEqual(image.decision.mode, 'geometry')
        self.assertIn('【解除】', self.text(fig))
        self.assertIn('範囲から離れる指示', self.text(fig).replace('\n', ''))
        self.assertNotIn('主範囲の指示を優先', self.text(fig))
        shapes = {p.get_gid(): p for p in fig.axes[0].patches if (p.get_gid() or '').startswith('dcx-')}
        self.assertEqual(shapes['dcx-target-ellipse-0'].get_linestyle(), '-')
        self.assertEqual(shapes['dcx-additional-ellipse'].get_linestyle(), '--')
        self.assert_plain_locator(fig)

    def test_missing_ward_boundary_is_not_replaced_with_whole_sapporo(self):
        records = self.cases['dcx-ward'].records
        report, = base._decode(records)
        self.assertEqual(dcx_plan((report,)).areas[0].code, 110100)
        self.assertIn(110000, ash_map._municipalities(), 'The parent city is available but is not a substitute')
        original = dcx_areas.municipality
        with patch.object(dcx_areas, 'municipality',
                          side_effect=lambda code: None if code == 110100 else original(code)):
            _, fig = self.capture(records)
        self.assert_topic(fig)

    def test_one_missing_ward_discards_the_entire_received_jalert_area_list(self):
        records = self.cases['dcx-wards'].records
        self.assertEqual(tuple(area.code for area in dcx_plan(base._decode(records)).areas),
                         (110100, 110200))
        original = dcx_areas.municipality
        with patch.object(dcx_areas, 'municipality',
                          side_effect=lambda code: None if code == 110200 else original(code)):
            _, fig = self.capture(records)
        self.assert_topic(fig)

    def test_retired_ward_is_not_replaced_by_a_current_ward_with_the_same_name(self):
        sentence, at = self.cases['dcx-wards'].records[0]
        for retired, current in ((27101, 27127), (22137, 22140)):
            with self.subTest(retired=retired):
                self.assertIsNotNone(dcx_areas.municipality(current * 100))
                self.assertIsNone(dcx_areas.municipality(retired * 100))
                # A valid Sapporo ward preceding the obsolete code must not
                # survive as a plausible partial area map.
                bits = (1101 << 48) | (retired << 32)
                records = ((change_fields(sentence, ((147, 64, bits),)), at),)
                plan = dcx_plan(base._decode(records))
                self.assertEqual(tuple(area.code for area in plan.areas), (110100, retired * 100))
                _, fig = self.capture(records)
                self.assert_topic(fig)

    def test_partial_area_failure_discards_every_previously_drawn_region(self):
        records = self.cases['dcx-prefectures'].records
        original = region_map._layer

        def without_iwate(layer):
            values = original(layer)
            return {code: features for code, features in values.items() if code != 3} if layer == 'prefectures' else values

        with patch.object(region_map, '_layer', side_effect=without_iwate):
            _, fig = self.capture(records)
        self.assert_topic(fig)

    def test_degenerate_first_boundary_precedes_a_later_missing_area(self):
        feature = {'geometry': {'type': 'Polygon', 'coordinates': [[
            [139., 35.], [139.1, 35.], [139.2, 35.], [139., 35.],
        ]]}}
        plan = SimpleNamespace(areas=(Area('prefectures', 1, 'first'),
                                      Area('prefectures', 2, 'missing')),
                               additional_ellipse=None)
        fig = wide.figure()
        self.addCleanup(fig.clear)
        with patch.object(region_map, '_layer', return_value={1: [feature]}) as layer, \
                patch.object(dcx_images, '_additional_ring') as additional, \
                patch.object(wide, 'map_axes') as map_axes:
            with self.assertRaisesRegex(ValueError, '^Degenerate regional polygon$'):
                dcx_images._area(fig, None, plan)
        layer.assert_called_once_with('prefectures')
        additional.assert_not_called()
        map_axes.assert_not_called()
        self.assertFalse(fig.axes)

    def test_degenerate_boundary_precedes_invalid_additional_geometry(self):
        feature = {'geometry': {'type': 'Polygon', 'coordinates': [[
            [139., 35.], [139.1, 35.], [139.2, 35.], [139., 35.],
        ]]}}
        plan = SimpleNamespace(areas=(Area('prefectures', 1, 'first'),),
                               additional_ellipse=object())
        fig = wide.figure()
        self.addCleanup(fig.clear)
        with patch.object(region_map, '_layer', return_value={1: [feature]}), \
                patch.object(dcx_images, '_additional_ring', wraps=dcx_images._additional_ring) as additional, \
                patch.object(wide, 'map_axes') as map_axes:
            with self.assertRaisesRegex(ValueError, '^Degenerate regional polygon$'):
                dcx_images._area(fig, None, plan)
        additional.assert_not_called()
        map_axes.assert_not_called()
        self.assertFalse(fig.axes)

    def test_cached_feature_paths_keep_the_exact_combined_polygon_and_hole_order(self):
        outer = [[139., 35.], [140., 35.], [140., 36.], [139., 36.], [139., 35.]]
        hole = [[139.2, 35.2], [139.8, 35.2], [139.8, 35.8], [139.2, 35.8], [139.2, 35.2]]
        adjacent = [[140., 35.], [141., 35.], [141., 36.], [140., 36.], [140., 35.]]
        features = [{'geometry': {'type': 'Polygon', 'coordinates': rings}}
                    for rings in ([outer, hole], [adjacent])]
        # Both source features share one received area; the final artist must
        # preserve their polygon and hole order.
        expected = Path.make_compound_path(*(
            region_map._polygon_path(polygon) for feature in features
            for polygon in region_map._geometry(feature, 'prefectures', 139.)))
        plan = SimpleNamespace(areas=(Area('prefectures', 1, 'both features'),),
                               additional_ellipse=None)
        report, = base._decode(self.cases['dcx-prefectures'].records)
        fig = wide.figure()
        self.addCleanup(fig.clear)
        ax = fig.add_axes((0, 0, 1, 1))
        with patch.object(region_map, '_layer', return_value={1: features}), \
                patch.object(wide, 'map_axes', return_value=SimpleNamespace(ax=ax)), \
                patch.object(wide, 'header'), patch.object(dcx_images, '_sidebar'), \
                patch.object(dcx_images, '_draw_additional'):
            dcx_images._area(fig, report, plan)
        artist, = ax.patches
        self.assertEqual(artist.get_gid(), 'region-area-dcx-1')
        np.testing.assert_array_equal(artist.get_path().vertices, expected.vertices)
        np.testing.assert_array_equal(artist.get_path().codes, expected.codes)

    def test_drawing_failure_tries_an_independent_area_candidate_before_topic(self):
        records = self.cases['dcx-ellipse'].records
        plan = dcx_plan(base._decode(records))
        areas = dcx_plan(base._decode(self.cases['dcx-municipality'].records)).areas
        combined = replace(plan, areas=areas)
        with patch.object(dcx_images, 'dcx_plan', return_value=combined), \
                patch.object(dcx_images, 'ellipse_ring', side_effect=ValueError('unprojectable')):
            _, fig = self.capture(records)
        self.assertTrue(fig.axes)
        self.assertTrue(any(p.get_gid() == 'region-area-dcx-120200' for p in fig.axes[0].patches))

    def test_later_unprojectable_geometry_discards_an_earlier_valid_ellipse(self):
        records = self.cases['dcx-ellipse'].records
        plan = dcx_plan(base._decode(records))
        mixed = replace(plan, geometries=(*plan.geometries, Point(140., 89.)))
        with patch.object(dcx_images, 'dcx_plan', return_value=mixed):
            _, fig = self.capture(records)
        self.assert_topic(fig)

    def test_national_prefecture_mask_retains_every_remote_component(self):
        sentence, at = self.cases['dcx-jalert'].records[0]
        records = ((change_fields(sentence, ((214, 6, 1),)), at),)
        _, fig = self.capture(records)
        actual = [p for p in fig.axes[0].patches
                  if (p.get_gid() or '').startswith('region-area-dcx-')]
        self.assertEqual({int(p.get_gid().rsplit('-', 1)[1]) for p in actual}, set(range(1, 48)))
        self.assertEqual(len(actual), 47, 'Batch islands into one artist per received region')
        self.assertEqual(sum(np.count_nonzero(p.get_path().codes == Path.MOVETO)
                             for p in actual), 114461)  # 114448 pinned polygons and 13 holes.
        self.assertEqual(sum(len(p.get_path().vertices) for p in actual), 520176)
        for polygon in actual:
            self.assert_visible(fig, polygon.get_path().vertices)
        self.assertIn('全国（47都道府県）', self.text(fig))

    def test_all_clear_is_release_and_never_cancellation(self):
        sentence, at = self.cases['dcx-municipality'].records[0]
        records = ((change_fields(sentence, ((24, 2, 3),)), at),)
        report, = base._decode(records)
        self.assertEqual(report.camf.a1, 3)
        _, fig = self.capture(records)
        self.assertIn('【解除】', self.text(fig))
        self.assertNotIn('取消', self.text(fig))


if __name__ == '__main__':
    unittest.main()
