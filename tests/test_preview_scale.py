"""Context is local geography, never a change to received forecast scope."""

import importlib.util
import unittest

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'numpy', 'matplotlib'))
if HAS_RENDERER:
    import numpy as np
    from image_service import preview_scale as scale
    from image_service import render as base


@unittest.skipUnless(HAS_RENDERER, 'Context policy uses the isolated image dependencies')
class PreviewScaleTests(unittest.TestCase):
    def point(self, lon, lat):
        return np.array(((lon, float(base._my(lat))),))

    def bounds(self, result):
        points = np.concatenate(result.sequences)
        return points.min(axis=0), points.max(axis=0)

    def test_inland_aso_adds_one_local_county_without_mutating_forecast_geometry(self):
        source = self.point(131.1, 32.88)
        original = source.copy()
        result = scale.context_for((source,), minimum=3.)
        np.testing.assert_array_equal(source, original)
        self.assertEqual(result.places[0].name, '熊本県')
        self.assertEqual(result.places[0].kind, 'prefecture')
        low, high = self.bounds(result)
        self.assertLess(low[0], 130.5, 'Keep Kumamoto coastal context')
        self.assertGreater(high[1], base._my(33.))
        self.assertLess(high[1], base._my(34.), 'Do not add all neighbouring prefectures')
        self.assertTrue(all(not np.shares_memory(source, ring) for ring in result.sequences))

    def test_short_hikimi_reach_gets_shimane_context(self):
        source = np.array(((131.80265, base._my(34.62003)),
                           (131.81431, base._my(34.62270))))
        result = scale.context_for((source,), minimum=.6)
        self.assertEqual(result.places[0].name, '島根県')
        low, high = self.bounds(result)
        self.assertGreater(high[0] - low[0], 1.)
        self.assertGreater(result.minimum, .6)

    def test_island_context_does_not_pull_izu_or_iwo_back_to_tokyo_mainland(self):
        for lon, lat, name, north in ((139.394, 34.724, '伊豆諸島北部', 35.2),
                                      (141.289, 24.751, '小笠原諸島', 26.)):
            with self.subTest(name=name):
                result = scale.context_for((self.point(lon, lat),), minimum=4.)
                self.assertEqual(result.places[0].name, name)
                self.assertEqual(result.places[0].kind, 'island')
                low, high = self.bounds(result)
                self.assertLess(high[1], base._my(north))
                self.assertGreater(low[1], base._my(lat - 1.))
                self.assertLess(result.minimum, 4., 'Remote islands have their own sensible scale')

    def test_longitude_wrapping_preserves_local_context_and_source_positions(self):
        source = self.point(139.394, 34.724)
        ordinary = scale.context_for((source,), minimum=4.)
        wrapped = scale.context_for((source + (360., 0.),), minimum=4.)
        self.assertEqual(ordinary.minimum, wrapped.minimum)
        self.assertEqual(len(ordinary.sequences), len(wrapped.sequences))
        for a, b in zip(ordinary.sequences, wrapped.sequences, strict=True):
            np.testing.assert_allclose(a + (360., 0.), b)
        self.assertEqual([p.name for p in ordinary.places], [p.name for p in wrapped.places])
        for a, b in zip(ordinary.places, wrapped.places, strict=True):
            np.testing.assert_allclose(np.asarray(a.position) + (360., 0.), b.position)

    def test_basin_and_distant_ocean_views_do_not_drag_in_japan(self):
        source = np.array(((170., base._my(10.)), (190., base._my(30.))))
        result = scale.context_for((source,), minimum=20.)
        self.assertEqual(result.sequences, ())
        self.assertEqual(result.minimum, 20.)
        self.assertEqual(result.places, ())
        remote = scale.context_for((self.point(-150., 10.),), minimum=4.)
        self.assertEqual(remote.sequences, ())
        self.assertEqual(remote.minimum, 4.)
        self.assertEqual(remote.places, ())

    def test_existing_regional_or_wide_minima_are_not_overridden(self):
        for minimum in (12., 14., 20.):
            with self.subTest(minimum=minimum):
                result = scale.context_for((self.point(131.1, 32.88),), minimum=minimum)
                self.assertEqual(result.minimum, minimum)
                self.assertEqual(result.sequences, ())
        broad = np.array(((130., base._my(32.)), (140., base._my(40.))))
        self.assertEqual(scale.context_for((broad,)).sequences, ())

    def test_small_mainland_units_are_not_misclassified_by_their_area(self):
        for lon, lat, name in ((139.65, 35.7, '東京地方'),
                               (139.45, 35.45, '神奈川県'),
                               (135.5, 34.7, '大阪府'),
                               (134., 34.25, '香川県')):
            with self.subTest(name=name):
                result = scale.context_for((self.point(lon, lat),), minimum=4.)
                self.assertEqual(result.minimum, 4.)
                self.assertEqual(result.places[0].name, name)
                self.assertEqual(result.places[0].kind, 'prefecture')

    def test_invalid_source_geometry_is_rejected_without_guessed_context(self):
        for sequences in ((), (np.empty((0, 2)),), (np.array(((float('nan'), 30.),)),),
                          (np.array(((130., 30., 5.),)),)):
            with self.subTest(sequences=sequences), self.assertRaises(ValueError):
                scale.context_for(sequences)


if __name__ == '__main__':
    unittest.main()
