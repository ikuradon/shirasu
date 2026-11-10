"""Recorded DCX and explicit training variants follow geometry → area → topic."""

from dataclasses import replace
import importlib.util
import unittest
from unittest.mock import patch

import test_flood_renderer as helpers
from test_image_renderer import change_fields

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from matplotlib.path import Path
    from image_service import ash_map, region_map
    from image_service import render as base
    from image_service import preview_wide_dcx as dcx_images
    from image_service import preview_dcx_areas as dcx_areas
    from image_service import preview_wide_layout as wide
    from image_service.preview_dcx_reports import Point, dcx_plan
    from scripts.gallery_catalog import load_cases


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
