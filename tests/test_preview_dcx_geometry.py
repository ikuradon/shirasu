"""DCX boundaries must retain the transmitted WGS84/ENU ellipse meaning."""

from types import SimpleNamespace
import importlib.util
import unittest

HAS_NUMPY = importlib.util.find_spec('numpy') is not None
if HAS_NUMPY:
    import numpy as np
    from image_service.preview_dcx_geometry import ellipse_ring


def ellipse(lon=140.717397, lat=41.765507, major=3.413, minor=2.524, azimuth=39.375):
    return SimpleNamespace(lon=lon, lat=lat, semi_major_km=major,
                           semi_minor_km=minor, azimuth=azimuth)


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


if __name__ == '__main__':
    unittest.main()
