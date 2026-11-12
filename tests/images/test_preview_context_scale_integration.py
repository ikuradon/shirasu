"""Geographic context must not replace or hide the received map information."""

import importlib.util
import unittest

from tests.images import test_flood_renderer as flood_helpers
from tests.images import test_image_renderer as image_helpers
from tests.images.test_image_renderer_v3 import changed, sample

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from matplotlib.transforms import Bbox
    from image_service import preview_wide_layout as wide
    from image_service import region_map
    from image_service import render as base
    from image_service import flood_map
    from tools.preview.gallery_catalog import load_cases


@unittest.skipUnless(HAS_RENDERER, 'Context-scale integration uses the isolated image renderer')
class ContextScaleIntegrationTests(unittest.TestCase):
    capture = flood_helpers.FloodRendererTests.capture

    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def assert_visible_source(self, figure, points):
        main = figure.axes[0]
        projected = figure.transFigure.inverted().transform(main.transData.transform(points))
        locators = [ax for ax in figure.axes if ax.get_gid() == 'preview-locator']
        for x, y in projected:
            self.assertTrue(.015 <= x < wide.PANEL_X, (x, y))
            self.assertTrue(.055 <= y <= .78, (x, y))
            if locators:
                left, bottom, right, top = wide.LOCATOR_OBSTACLE
                self.assertFalse(left <= x <= right and bottom <= y <= top, (x, y))

    def assert_context_names(self, figure, *, required=False):
        main = figure.axes[0]
        names = [label for label in main.texts
                 if (label.get_gid() or '').startswith('preview-place-')]
        self.assertLessEqual(len(names), 5)
        if required:
            self.assertTrue(names, 'This regional map has room for geographic context names')
        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        boxes = [label.get_window_extent(renderer) for label in names]
        obstacles = [Bbox.from_extents(*box).transformed(figure.transFigure)
                     for box in wide.OBSTACLES]
        if any(ax.get_gid() == 'preview-locator' for ax in figure.axes):
            obstacles.append(Bbox.from_extents(*wide.LOCATOR_OBSTACLE).transformed(figure.transFigure))
        for index, box in enumerate(boxes):
            self.assertFalse(any(box.overlaps(other) for other in (*boxes[index + 1:], *obstacles)))
        for locator in (ax for ax in figure.axes if ax.get_gid() == 'preview-locator'):
            self.assertFalse(locator.texts, 'The locator remains an unlabelled base map')
        return names

    def principal_prefecture(self, code, reference):
        polygons = [polygon for feature in region_map._layer('prefectures')[code]
                    for polygon in region_map._geometry(feature, 'prefectures', reference)]
        return max(polygons, key=lambda polygon: abs(region_map._signed_area(polygon[0])))[0]

    def assert_geography_in_view(self, figure, points):
        """Context may approach the header but must remain in the main viewport."""
        projected = figure.transFigure.inverted().transform(figure.axes[0].transData.transform(points))
        self.assertTrue(np.all((projected[:, 0] >= 0) & (projected[:, 0] < wide.PANEL_X)))
        self.assertTrue(np.all((projected[:, 1] >= 0) & (projected[:, 1] <= 1)))

    def test_inland_aso_keeps_county_context_and_all_eighteen_forecasts(self):
        _, figure = self.capture(self.cases['ash-detail'].records)
        self.assertTrue(figure.axes)
        self.assert_geography_in_view(figure, self.principal_prefecture(43, 131.1))
        points = [artist.get_offsets()[0] for artist in figure.axes[0].collections
                  if (artist.get_gid() or '').startswith(('municipality-anchor-', 'volcano-point-'))]
        self.assertEqual(len(points), 4)
        self.assert_visible_source(figure, points)
        cells = [artist for artist in figure.artists
                 if (artist.get_gid() or '').startswith('ash-cell-')]
        self.assertEqual(len(cells), 18)
        self.assert_context_names(figure, required=True)

    def test_actual_tiny_river_retains_every_vertex_and_regional_context(self):
        # The pinned Hikimi forecast reach is only about 0.012 degrees long.
        # It must not determine a street-scale viewport on its own.
        code = 870706001700
        _, figure = self.capture(flood_helpers.forecast((code, 3)))
        self.assertTrue(figure.axes)
        self.assert_geography_in_view(figure, self.principal_prefecture(32, 131.8))
        source = flood_map._parts(flood_map._rivers()[code])
        actual = {line.get_gid(): line for line in figure.axes[0].lines
                  if (line.get_gid() or '').startswith('flood-reach-')}
        self.assertEqual(len(actual), len(source))
        for index, part in enumerate(source):
            np.testing.assert_array_equal(actual[f'flood-reach-{code}-{index}'].get_xydata(), part)
            self.assert_visible_source(figure, part)
        self.assert_context_names(figure, required=True)

    def test_remote_island_volcano_does_not_pull_in_parent_tokyo_mainland(self):
        # True island-only source, unlike the Tokyo-prefecture gallery case.
        # Clear the old Aso municipality list when selecting Iwojima.
        records = changed('volcano', (76, 12, 329), (88, 115, 0))
        _, figure = self.capture(records)
        self.assertTrue(figure.axes)
        main = figure.axes[0]
        point, = [artist for artist in main.collections if artist.get_gid() == 'volcano-point-329']
        np.testing.assert_allclose(point.get_offsets()[0], (141.289, base._my(24.751)))
        self.assert_visible_source(figure, point.get_offsets())
        self.assertLess(main.get_ylim()[1], base._my(30), 'Tokyo mainland must not shrink an island view')
        names = self.assert_context_names(figure, required=True)
        self.assertTrue(any('小笠原' in label.get_text() for label in names))
        self.assertFalse(any(label.get_text() in ('東京都', '東京地方') for label in names))

    def test_offshore_hypocenter_keeps_exact_epicentre_and_nearby_coast(self):
        records = self.cases['hypocenter'].records
        _, figure = self.capture(records)
        self.assertTrue(figure.axes)
        report, = base._decode(records)
        lon, lat = base._coordinates(report.coordinates_of_hypocenter_raw)
        marker, = [artist for artist in figure.axes[0].collections if artist.get_gid() == 'hypocenter-point']
        np.testing.assert_allclose(marker.get_offsets()[0], (lon, base._my(lat)))
        self.assert_visible_source(figure, marker.get_offsets())
        self.assert_geography_in_view(figure, self.principal_prefecture(45, lon))
        self.assert_context_names(figure, required=True)

    def test_dateline_typhoon_retains_nearby_centres_without_a_worldwide_view(self):
        records = []
        for (sentence, received), latitude, west in zip(image_helpers.TYPHOON[:2], (25, 26), (0, 1), strict=True):
            # Latitude/longitude fields occupy 41 bits before central pressure.
            fields = [(14, 3, 7), (102, 41, 0), (103, 7, latitude), (122, 1, west), (123, 8, 179)]
            records.append((image_helpers.change_fields(sentence, fields), received))
        reports = base._decode(records)
        self.assertEqual([base._coordinates(report.coordinates_of_typhoon_raw) for report in reports],
                         [(179., 25.), (-179., 26.)])
        _, figure = self.capture(records)
        self.assertTrue(figure.axes)
        main = figure.axes[0]
        markers = [artist.get_offsets()[0] for artist in main.collections
                   if (artist.get_gid() or '').startswith('typhoon-point-')]
        self.assertEqual(len(markers), 2)
        self.assertAlmostEqual(abs(markers[0][0] - markers[1][0]), 2.)
        self.assertLess(main.get_xlim()[1] - main.get_xlim()[0], 30)
        self.assert_visible_source(figure, markers)
        self.assert_context_names(figure)

    def test_broad_pacific_overview_keeps_all_official_points_without_extra_locator(self):
        _, figure = self.capture(sample('northwest_pacific_tsunami'))
        self.assertTrue(figure.axes)
        self.assertFalse(any(ax.get_gid() == 'preview-locator' for ax in figure.axes))
        points = [artist.get_offsets()[0] for artist in figure.axes[0].collections
                  if (artist.get_gid() or '').startswith('nwp-point-')]
        self.assertEqual(len(points), 5)
        self.assert_visible_source(figure, points)
        self.assert_context_names(figure)


if __name__ == '__main__':
    unittest.main()
