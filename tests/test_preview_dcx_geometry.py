"""DCX boundaries must retain the transmitted WGS84/ENU ellipse meaning."""

from types import SimpleNamespace
import importlib.util
import unittest

HAS_NUMPY = importlib.util.find_spec('numpy') is not None
if HAS_NUMPY:
    import numpy as np
    from image_service.preview_dcx_geometry import ellipse_ring, secondary_ellipse_ring


def ellipse(lon=140.717397, lat=41.765507, major=3.413, minor=2.524, azimuth=39.375):
    return SimpleNamespace(lon=lon, lat=lat, semi_major_km=major,
                           semi_minor_km=minor, azimuth=azimuth)


def secondary(main=None, shift=2, scale=1., rotation=90.):
    return SimpleNamespace(main=ellipse() if main is None else main,
                           shift=shift, scale=scale, rotation=rotation)


def forward_enu(ring, source):
    """Independent forward formula in metres, from DCX-004 equations 5.9.1-3."""
    lon = np.radians(ring[:, 0])
    lat = 2 * np.arctan(np.exp(np.radians(ring[:, 1]))) - np.pi / 2
    a = 6378137.
    flattening = 1 / 298.257223563
    eccentricity = 2 * flattening - flattening ** 2

    def ecef(longitude, latitude):
        radius = a / np.sqrt(1 - eccentricity * np.sin(latitude) ** 2)
        return np.column_stack((radius * np.cos(latitude) * np.cos(longitude),
                                radius * np.cos(latitude) * np.sin(longitude),
                                radius * (1 - eccentricity) * np.sin(latitude)))

    points = ecef(lon, lat)
    lon0, lat0 = np.radians((source.lon, source.lat))
    origin = ecef(np.array((lon0,)), np.array((lat0,)))[0]
    delta = points - origin
    east = -delta[:, 0] * np.sin(lon0) + delta[:, 1] * np.cos(lon0)
    north = (-delta[:, 0] * np.sin(lat0) * np.cos(lon0)
             - delta[:, 1] * np.sin(lat0) * np.sin(lon0) + delta[:, 2] * np.cos(lat0))
    up = (delta[:, 0] * np.cos(lat0) * np.cos(lon0)
          + delta[:, 1] * np.cos(lat0) * np.sin(lon0) + delta[:, 2] * np.sin(lat0))
    polar = a * (1 - flattening)
    on_surface = (points[:, 0] ** 2 + points[:, 1] ** 2) / a ** 2 + points[:, 2] ** 2 / polar ** 2
    np.testing.assert_allclose(on_surface, 1, atol=2e-15, rtol=0)
    return east / 1000, north / 1000, up / 1000


@unittest.skipUnless(HAS_NUMPY, 'Geometry tests use the isolated image renderer dependencies')
class DcxGeometryTests(unittest.TestCase):
    def assert_transmitted_ellipse(self, source):
        ring = ellipse_ring(source)
        self.assertEqual(ring.shape, (513, 2))
        np.testing.assert_array_equal(ring[0], ring[-1])
        self.assertTrue(np.all(np.isfinite(ring)))
        e, n, up = forward_enu(ring, source)
        angle = np.radians(source.azimuth)
        major = e * np.cos(angle) + n * np.sin(angle)
        minor = -e * np.sin(angle) + n * np.cos(angle)
        np.testing.assert_allclose((major / source.semi_major_km) ** 2
                                   + (minor / source.semi_minor_km) ** 2, 1,
                                   atol=2e-9, rtol=0)
        self.assertAlmostEqual(major[0], source.semi_major_km, places=8)
        self.assertAlmostEqual(minor[128], source.semi_minor_km, places=8)
        self.assertTrue(np.all(up < 0), 'Use the nearby ellipsoid surface below the tangent plane')
        self.assertLess(float(np.max(np.abs(up))), source.semi_major_km,
                        'Reject the other quadratic root on the far side of the Earth')
        return ring

    def test_refined_hakodate_ellipse_matches_independent_forward_enu(self):
        self.assert_transmitted_ellipse(ellipse())

    def test_mtinfo_official_additional_ellipse_uses_its_own_enu_frame(self):
        # DCX-004 EX3-EX7 example, independent of any primary ellipse/B1
        # refinement. Verify the boundary with the forward WGS84 equations.
        self.assert_transmitted_ellipse(ellipse(139.689138, 35.687299, 10.933, 5.979, 45.))

    def test_overseas_165km_ellipse_is_not_replaced_by_geodesic_radii(self):
        self.assert_transmitted_ellipse(ellipse(95.769926, 5.11284, 165.324, 90.407, -45))

    def test_azimuth_zero_points_east_and_positive_angles_turn_north(self):
        for angle in (0, 45, -90):
            with self.subTest(angle=angle):
                source = ellipse(139.5, 35., 10., 2., angle)
                ring = self.assert_transmitted_ellipse(source)
                east, north, _ = forward_enu(ring, source)
                self.assertAlmostEqual(east[0], 10 * np.cos(np.radians(angle)), places=8)
                self.assertAlmostEqual(north[0], 10 * np.sin(np.radians(angle)), places=8)

    def test_dateline_ring_and_requested_reference_stay_contiguous(self):
        source = ellipse(179.8, -20, 165.324, 90.407, 20)
        ring = self.assert_transmitted_ellipse(source)
        self.assertGreater(ring[:, 0].max(), 180)
        self.assertLess(np.max(np.abs(np.diff(ring[:, 0]))), 1)
        shifted = ellipse_ring(source, reference=-180)
        np.testing.assert_allclose(shifted, ring - (360, 0), atol=1e-12)
        self.assertLess(np.ptp(ring[:, 0]), 5)

    def test_smallest_refined_axis_is_valid_but_extreme_or_nonfinite_values_fail(self):
        self.assert_transmitted_ellipse(ellipse(130, 30, .027, .027, 0))
        for source in (ellipse(lat=85), ellipse(lat=84.9, major=100, minor=100),
                       ellipse(lat=80, major=2500, minor=2500),
                       ellipse(major=7000), ellipse(major=2, minor=3),
                       ellipse(minor=0), ellipse(azimuth=90), ellipse(lon=float('nan'))):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    ellipse_ring(source)

    def assert_secondary_in_main_frame(self, source):
        ring = secondary_ellipse_ring(source)
        self.assertEqual(ring.shape, (513, 2))
        np.testing.assert_array_equal(ring[0], ring[-1])
        self.assertTrue(np.all(np.isfinite(ring)))
        east, north, up = forward_enu(ring, source.main)
        angle = np.radians(source.main.azimuth + source.rotation)
        distance = source.shift * source.main.semi_major_km
        centre = distance * np.array((np.cos(angle), np.sin(angle)))
        # Uniform parameter samples give the centre independently of the
        # projected lon/lat bounding box or any inferred geographic centre.
        np.testing.assert_allclose((east[:-1].mean(), north[:-1].mean()), centre,
                                   atol=3e-9, rtol=0)
        x, y = east - centre[0], north - centre[1]
        major = x * np.cos(angle) + y * np.sin(angle)
        minor = -x * np.sin(angle) + y * np.cos(angle)
        a, b = source.main.semi_major_km * source.scale, source.main.semi_minor_km * source.scale
        np.testing.assert_allclose((major / a) ** 2 + (minor / b) ** 2, 1,
                                   atol=2e-8, rtol=0)
        self.assertAlmostEqual(major[0], a, places=8)
        self.assertAlmostEqual(minor[128], b, places=8)
        self.assertTrue(np.all(up <= 1e-9))
        self.assertLess(float(np.max(np.abs(up))), 6378., 'The far surface is not the target')
        return ring, centre

    def test_secondary_shift_scale_and_rotation_preserve_the_main_enu_frame(self):
        main = ellipse(95.769926, 5.11284, 165.324, 90.407, -45)
        for shift, scale, rotation in ((2, 1., 90.), (1, .25, 11.25), (3, 2., 348.75)):
            with self.subTest(shift=shift, scale=scale, rotation=rotation):
                self.assert_secondary_in_main_frame(secondary(main, shift, scale, rotation))

    def test_secondary_rotation_moves_the_centre_as_well_as_the_axes(self):
        main = ellipse(139.5, 35., 10., 2., 0)
        for rotation, expected in ((0, (20, 0)), (180, (-20, 0)), (270, (0, -20))):
            with self.subTest(rotation=rotation):
                _, centre = self.assert_secondary_in_main_frame(secondary(main, rotation=rotation))
                np.testing.assert_allclose(centre, expected, atol=1e-12, rtol=0)

    def test_zero_transform_codes_define_a_concentric_quarter_size_ellipse(self):
        main = ellipse(130, 30, .216, .216, -90)
        result, centre = self.assert_secondary_in_main_frame(secondary(main, 0, .25, 0))
        np.testing.assert_array_equal(centre, (0, 0))
        expected = ellipse_ring(ellipse(130, 30, .054, .054, -90))
        np.testing.assert_array_equal(result, expected)

    def test_maximum_secondary_scale_is_allowed_when_the_complete_surface_exists(self):
        # The main-axis 2500 km limit is not also a 2500 km secondary-axis
        # limit. The scaled 5000 km boundary still has a unique near surface.
        self.assert_secondary_in_main_frame(secondary(ellipse(10, 0, 2500, 1200, 0), 0, 2, 0))

    def test_secondary_dateline_boundary_uses_the_same_reference_as_the_main(self):
        source = secondary(ellipse(179.8, -20, 165.324, 90.407, 20), 2, 1, 0)
        ring, _ = self.assert_secondary_in_main_frame(source)
        self.assertGreater(ring[:, 0].max(), 180)
        self.assertLess(np.max(np.abs(np.diff(ring[:, 0]))), 1)
        shifted = secondary_ellipse_ring(source, reference=-180)
        np.testing.assert_allclose(shifted, ring - (360, 0), atol=1e-12)

    def test_invalid_or_unprojectable_secondary_is_rejected_as_a_complete_ring(self):
        sources = (secondary(shift=.5), secondary(shift=4), secondary(scale=0),
                   secondary(scale=.3), secondary(scale=2.25), secondary(rotation=360),
                   secondary(rotation=1), secondary(rotation=float('nan')),
                   secondary(scale=float('inf')), secondary(shift=None),
                   secondary(ellipse(130, 20, 2500, 1000, 0), 3, 2, 0),
                   secondary(ellipse(130, 84, 100, 50, 0), 2, 1, 90))
        for source in sources:
            with self.subTest(source=source), self.assertRaises(ValueError):
                secondary_ellipse_ring(source)
        with self.assertRaises(ValueError):
            secondary_ellipse_ring(secondary(), reference=float('nan'))


if __name__ == '__main__':
    unittest.main()
