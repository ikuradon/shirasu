"""Bounded preview geometry reuse must preserve source scope and fresh styling."""

from copy import deepcopy
import gc
import importlib.util
import io
import re
import unittest
from unittest.mock import patch
import weakref

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import to_rgba
    from matplotlib.figure import Figure
    from matplotlib.path import Path
    from image_service import preview_geometry_cache as cached
    from image_service import region_map as frozen


def feature(longitude=120.):
    return {'geometry': {'type': 'Polygon', 'coordinates': [
        [[longitude, 30.], [longitude + 4., 30.], [longitude + 4., 34.],
         [longitude, 34.], [longitude, 30.]],
        [[longitude + 1., 31.], [longitude + 2., 31.], [longitude + 2., 32.],
         [longitude + 1., 32.], [longitude + 1., 31.]],
    ]}}


@unittest.skipUnless(HAS_RENDERER, 'Install the isolated image renderer dependencies')
class GeometryCacheTests(unittest.TestCase):
    def cache(self, **kwargs):
        return cached.GeometryCache(**kwargs)

    def assert_geometry_equal(self, actual, expected, *, lines=False):
        self.assertEqual(len(actual), len(expected))
        for part, original in zip(actual, expected, strict=True):
            if lines:
                np.testing.assert_array_equal(part, original)
            else:
                self.assertEqual(len(part), len(original))
                for ring, source in zip(part, original, strict=True):
                    np.testing.assert_array_equal(ring, source)

    def expected_path(self, polygons):
        return Path.make_compound_path(*(frozen._polygon_path(polygon) for polygon in polygons))

    def test_equal_content_reuses_projection_but_exact_reference_and_layer_are_keys(self):
        store, source = self.cache(), feature()
        original = deepcopy(source)
        with patch.object(frozen, '_geometry', wraps=frozen._geometry) as project:
            first = store.geometry(source, 'prefectures', 139.)
            second = store.geometry(deepcopy(source), 'prefectures', 139.)
            self.assertEqual(project.call_count, 1)
            self.assert_geometry_equal(second, first)
            store.geometry(source, 'prefectures', 139. + 1e-10)
            self.assertEqual(project.call_count, 2)
            store.geometry(source, 'marine', 139.)
            self.assertEqual(project.call_count, 3)
            with self.assertRaisesRegex(ValueError, 'coastline geometry'):
                store.geometry(source, 'tsunami', 139.)
        self.assertEqual(source, original)
        self.assertGreaterEqual(store.info()['hits'], 1)

    def test_in_place_change_invalidates_a_hit_and_failed_geometry_can_be_repaired(self):
        store, source = self.cache(), feature()
        before = store.geometry(source, 'prefectures', 139.)
        source['geometry']['coordinates'][0][-1][0] += .25
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'Unclosed regional polygon'):
                store.geometry(source, 'prefectures', 139.)
        for ring in source['geometry']['coordinates']:
            for point in ring:
                point[0] += 5
        source['geometry']['coordinates'][0][-1] = source['geometry']['coordinates'][0][0].copy()
        after = store.geometry(source, 'prefectures', 139.)
        self.assert_geometry_equal(after, frozen._geometry(source, 'prefectures', 139.))
        self.assertFalse(np.array_equal(before[0][0], after[0][0]))

    def test_json_normalization_cannot_turn_invalid_input_into_a_warm_hit(self):
        store, source = self.cache(), feature()
        expected = store.geometry(source, 'prefectures', 139.)
        tuple_ring = deepcopy(source)
        tuple_ring['geometry']['coordinates'][0] = tuple(tuple_ring['geometry']['coordinates'][0])
        tuple_polygons = deepcopy(source)
        tuple_polygons['geometry']['coordinates'] = tuple(tuple_polygons['geometry']['coordinates'])
        for invalid in (tuple_ring, tuple_polygons):
            with self.assertRaises(ValueError) as error:
                frozen._geometry(invalid, 'prefectures', 139.)
            with self.assertRaisesRegex(ValueError, re.escape(str(error.exception))):
                store.geometry(invalid, 'prefectures', 139.)
        # A tuple at the point level is explicitly accepted by _position, so
        # bypassing its noncanonical JSON representation must still render it.
        tuple_point = deepcopy(source)
        tuple_point['geometry']['coordinates'][0][0] = tuple(tuple_point['geometry']['coordinates'][0][0])
        self.assert_geometry_equal(store.geometry(tuple_point, 'prefectures', 139.), expected)
        for numeric_type in (int, float):
            class Coordinate(numeric_type):
                pass
            raw = feature(numeric_type(120))
            store.geometry(raw, 'prefectures', 139.)
            raw['geometry']['coordinates'][0][0][0] = Coordinate(120)
            with self.assertRaisesRegex(ValueError, 'Invalid regional coordinate'):
                store.geometry(raw, 'prefectures', 139.)

    def test_dateline_holes_lines_and_ring_structure_match_frozen_helpers(self):
        store = self.cache()
        source = {'geometry': {'type': 'Polygon', 'coordinates': [
            [[179., 20.], [-179., 20.], [-179., 24.], [179., 24.], [179., 20.]],
            [[179.5, 21.], [-179.5, 21.], [-179.5, 22.], [179.5, 22.], [179.5, 21.]],
        ]}}
        outcomes = []
        for reference in (170., -170., 0., 360.):
            parts = store.geometry(source, 'prefectures', reference)
            self.assert_geometry_equal(parts, frozen._geometry(source, 'prefectures', reference))
            actual, expected = store.compound_path(parts), self.expected_path(parts)
            np.testing.assert_array_equal(actual.vertices, expected.vertices)
            np.testing.assert_array_equal(actual.codes, expected.codes)
            outcomes.append(parts)
        self.assertFalse(np.array_equal(outcomes[0][0][0], outcomes[1][0][0]))
        outer, hole = outcomes[0][0]
        # The same flattened vertices may mean one polygon with a hole or two
        # independent polygons. Ring grouping must remain part of the path key.
        for polygons in (((outer, hole),), ((outer,), (hole,)), ((hole,), (outer,))):
            actual, expected = store.compound_path(polygons), self.expected_path(polygons)
            np.testing.assert_array_equal(actual.vertices, expected.vertices)
            np.testing.assert_array_equal(actual.codes, expected.codes)
        coastline = {'geometry': {'type': 'MultiLineString', 'coordinates': [
            [[179., 20.], [-179., 21.]], [[170., 25.], [172., 26.]],
        ]}}
        self.assert_geometry_equal(store.geometry(coastline, 'tsunami', -170.),
                                   frozen._geometry(coastline, 'tsunami', -170.), lines=True)

    def test_cached_buffers_cannot_be_made_writeable_and_path_attributes_are_fresh(self):
        store = self.cache()
        parts = store.geometry(feature(), 'prefectures', 139.)
        with patch.object(frozen, '_polygon_path', wraps=frozen._polygon_path) as compile_path:
            first = store.compound_path(parts)
            second = store.compound_path(parts)
            self.assertEqual(compile_path.call_count, len(parts))
        self.assertIsNot(first, second)
        for array in (*parts[0], first.vertices, first.codes):
            self.assertFalse(array.flags.writeable)
            with self.assertRaises(ValueError):
                array.setflags(write=True)
            with self.assertRaises(ValueError):
                array.flat[0] = 0
        expected = second.should_simplify, second.simplify_threshold
        first.should_simplify = not expected[0]
        first.simplify_threshold = .75
        third = store.compound_path(parts)
        self.assertEqual((second.should_simplify, second.simplify_threshold), expected)
        self.assertEqual((third.should_simplify, third.simplify_threshold), expected)

    def test_lru_reuse_changes_eviction_order(self):
        store = self.cache(max_entries=2, max_bytes=1_000_000)
        a, b, c = (feature(lon) for lon in (120., 125., 130.))
        with patch.object(frozen, '_geometry', wraps=frozen._geometry) as project:
            store.geometry(a, 'prefectures', 139.)
            store.geometry(b, 'prefectures', 139.)
            store.geometry(a, 'prefectures', 139.)
            store.geometry(c, 'prefectures', 139.)
            self.assertEqual(project.call_count, 3)
            store.geometry(b, 'prefectures', 139.)
            self.assertEqual(project.call_count, 4)
        self.assertLessEqual(store.info()['entries'], 2)
        self.assertGreaterEqual(store.info()['evictions'], 1)

    def test_byte_budget_includes_objects_and_oversized_results_are_not_retained(self):
        probe = self.cache(max_entries=10, max_bytes=1_000_000)
        parts = probe.geometry(feature(), 'prefectures', 139.)
        size = probe.info()['retained_bytes']
        self.assertGreater(size, sum(ring.nbytes for polygon in parts for ring in polygon))
        budget = size + size // 2
        store = self.cache(max_entries=10, max_bytes=budget)
        store.geometry(feature(120.), 'prefectures', 139.)
        store.geometry(feature(125.), 'prefectures', 139.)
        self.assertLessEqual(store.info()['retained_bytes'], budget)
        self.assertEqual(store.info()['entries'], 1)
        self.assertGreaterEqual(store.info()['evictions'], 1)
        tiny = self.cache(max_entries=10, max_bytes=1)
        with patch.object(frozen, '_geometry', wraps=frozen._geometry) as project:
            for _ in range(2):
                self.assert_geometry_equal(tiny.geometry(feature(), 'prefectures', 139.), parts)
            self.assertEqual(project.call_count, 2)
        self.assertEqual(tiny.info()['entries'], 0)
        self.assertEqual(tiny.info()['retained_bytes'], 0)
        self.assertGreaterEqual(tiny.info()['skipped'], 2)

    def test_projection_and_paths_share_limits_and_clear_releases_both(self):
        store = self.cache(max_entries=1, max_bytes=1_000_000)
        source = feature()
        parts = store.geometry(source, 'prefectures', 139.)
        store.compound_path(parts)
        with patch.object(frozen, '_geometry', wraps=frozen._geometry) as project:
            store.geometry(source, 'prefectures', 139.)
            self.assertEqual(project.call_count, 1)
        self.assertEqual(store.info()['entries'], 1)
        self.assertGreaterEqual(store.info()['evictions'], 2)
        store.clear()
        self.assertEqual(store.info()['entries'], 0)
        self.assertEqual(store.info()['retained_bytes'], 0)

    def test_invalid_serialization_and_degenerate_paths_keep_frozen_errors_uncached(self):
        store = self.cache()
        invalid = feature()
        invalid['geometry']['coordinates'] = np.asarray([[120., 30.], [121., 31.]])
        with self.assertRaises(ValueError) as error:
            frozen._geometry(invalid, 'prefectures', 139.)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, re.escape(str(error.exception))):
                store.geometry(invalid, 'prefectures', 139.)
        polygons = ((np.asarray(((120., 30.), (121., 31.), (122., 32.), (120., 30.))),),)
        with patch.object(frozen, '_polygon_path', wraps=frozen._polygon_path) as compile_path:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'Degenerate regional polygon'):
                    store.compound_path(polygons)
            self.assertEqual(compile_path.call_count, 2)
        self.assertEqual(store.info()['entries'], 0)

    def test_geometry_only_validation_and_empty_paths_keep_original_semantics(self):
        store = self.cache()
        source = {'geometry': {'type': 'Polygon', 'coordinates': [
            [[120., 30.], [121., 30.], [122., 30.], [120., 30.]],
        ]}}
        parts = store.geometry(source, 'prefectures', 139.)
        self.assert_geometry_equal(parts, frozen._geometry(source, 'prefectures', 139.))
        with self.assertRaisesRegex(ValueError, 'Degenerate regional polygon'):
            store.compound_path(parts)
        expected = self.expected_path(())
        for _ in range(2):
            empty = store.compound_path(())
            np.testing.assert_array_equal(empty.vertices, expected.vertices)
            self.assertIsNone(empty.codes)

    def test_cache_does_not_retain_source_feature_objects(self):
        class Feature(dict):
            pass
        source = Feature(feature())
        reference = weakref.ref(source)
        store = self.cache()
        store.geometry(source, 'prefectures', 139.)
        del source
        gc.collect()
        self.assertIsNone(reference())
        self.assertGreater(store.info()['entries'], 0)

    def axes(self):
        fig = Figure(figsize=(4, 3), dpi=80)
        FigureCanvasAgg(fig)
        self.addCleanup(fig.clear)
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_xlim(118, 136)
        ax.set_ylim(30, 44)
        ax.set_aspect('equal')
        ax.set_axis_off()
        return fig, ax

    def png(self, figure):
        stream = io.BytesIO()
        figure.savefig(stream, format='png', metadata={'Software': 'geometry-cache-regression'})
        return stream.getvalue()

    def test_cached_paint_matches_png_and_keeps_each_reports_colours_independent(self):
        source = feature()
        first_artist = None
        with patch.object(cached, '_CACHE', self.cache()):
            for layer, colour in (('marine', '#dc3344'), ('marine', '#4488dd'),
                                  ('prefectures', '#dc3344'), ('tsunami', '#4488dd')):
                current = (source if layer != 'tsunami' else
                           {'geometry': {'type': 'LineString',
                                         'coordinates': [[120., 30.], [123., 33.], [125., 30.]]}})
                parts = cached.geometry(current, layer, 139.)
                leaves = {1: {'color': colour, 'parts': [(current, parts)]}}
                actual_figure, actual = self.axes()
                expected_figure, expected = self.axes()
                cached.paint_regions(actual, leaves, layer)
                frozen._paint_regions(expected, leaves, layer)
                self.assertEqual(self.png(actual_figure), self.png(expected_figure))
                if layer == 'marine':
                    self.assertEqual(actual.patches[0].get_edgecolor(), to_rgba(colour))
                    if first_artist is None:
                        first_artist = actual.patches[0]
                    else:
                        self.assertIsNot(first_artist, actual.patches[0])
                        self.assertEqual(first_artist.get_edgecolor(), to_rgba('#dc3344'))


if __name__ == '__main__':
    unittest.main()
