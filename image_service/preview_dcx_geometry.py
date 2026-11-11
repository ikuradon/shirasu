"""WGS84 boundaries of transmitted DCX target ellipses, for preview only.

IS-QZSS-DCX-004 section 5.9 defines containment in the east/north components
of a local ENU frame at the ellipse centre, with points on WGS84 at height
zero. Inverting that construction avoids turning kilometre axes into map
degrees, geodesic distances, or a different rotation convention.
https://qzss.go.jp/en/technical/download/pdf/ps-is-qzss/is-qzss-dcx-004.pdf

CAMF 1.2 section 3.7.3 defines B3 by translating, scaling and rotating in
the main ellipse's frame. The C9 rotation acts on both the secondary centre
and its axes, about the main centre; it is not an independent geodesic ellipse.
https://www.gsc-europa.eu/sites/default/files/sites/all/files/EWSS-CAMF_in_force.pdf
"""

import math

import numpy as np


_A = 6378.137  # WGS84 semi-major axis, kilometres
_F = 1 / 298.257223563
_B = _A * (1 - _F)
_E2 = _F * (2 - _F)
_INVERSE_SQUARE_AXES = np.array((1 / _A ** 2, 1 / _A ** 2, 1 / _B ** 2))
_STEPS = 512


def _simple_ring(points):
    """Reject nonadjacent crossing or overlapping edges before painting."""
    start, end = points[:-1], points[1:]
    if np.any(np.linalg.norm(end - start, axis=1) <= 1e-12):
        return False
    for i in range(len(start) - 2):
        last = len(start) - (1 if i == 0 else 0)
        others = slice(i + 2, last)
        q, s = start[others], end[others] - start[others]
        if not len(q):
            continue
        p, r = start[i], end[i] - start[i]
        delta = q - p
        cross = r[0] * s[:, 1] - r[1] * s[:, 0]
        parallel = np.abs(cross) <= 1e-14
        if np.any(parallel):
            collinear = parallel & (np.abs(delta[:, 0] * r[1] - delta[:, 1] * r[0]) <= 1e-14)
            if np.any(collinear):
                rr = np.dot(r, r)
                first = (delta[collinear] @ r) / rr
                second = ((delta[collinear] + s[collinear]) @ r) / rr
                if np.any((np.minimum(first, second) <= 1) & (np.maximum(first, second) >= 0)):
                    return False
        safe = ~parallel
        t = (delta[safe, 0] * s[safe, 1] - delta[safe, 1] * s[safe, 0]) / cross[safe]
        u = (delta[safe, 0] * r[1] - delta[safe, 1] * r[0]) / cross[safe]
        if np.any((t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)):
            return False
    return True


def _ellipse_parameters(ellipse, reference):
    try:
        lon, lat, major, minor, azimuth = map(float, (
            ellipse.lon, ellipse.lat, ellipse.semi_major_km,
            ellipse.semi_minor_km, ellipse.azimuth))
        target = lon if reference is None else float(reference)
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        raise ValueError('Invalid DCX ellipse parameters') from error
    if not all(math.isfinite(value) for value in (lon, lat, major, minor, azimuth, target)):
        raise ValueError('Non-finite DCX ellipse parameters')
    if not -85 < lat < 85 or not 0 < minor <= major <= 2500:
        raise ValueError('DCX ellipse is outside the supported projection or axis range')
    if not -90 <= azimuth < 90:
        raise ValueError('Invalid DCX ellipse azimuth')
    return lon, lat, major, minor, azimuth, target


def ellipse_ring(ellipse, reference=None):
    """Return a closed lon/Mercator-y ring, or reject unsupported geometry.

    ``ellipse`` supplies lon, lat, semi_major_km, semi_minor_km and azimuth.
    Azimuth is positive from east towards north; a centre is a parameter of
    the target area, not the reported location of a disaster. A caller must
    fall back as a whole on ValueError rather than truncate the boundary.
    """
    lon, lat, major, minor, azimuth, target = _ellipse_parameters(ellipse, reference)
    angle = np.radians(azimuth)
    phase = np.linspace(0, 2 * np.pi, _STEPS + 1)
    x, y = major * np.cos(phase), minor * np.sin(phase)
    e = x * np.cos(angle) - y * np.sin(angle)
    n = x * np.sin(angle) + y * np.cos(angle)
    return _project_ring(lon, lat, e, n, target)


def secondary_ellipse_ring(secondary, reference=None):
    """Project all of a B3 secondary boundary in the *main* ENU frame.

    The transformed centre is a mathematical construction, not a disaster
    pin. C9 rotates the shift as well as the axes; angles differing by 180
    degrees therefore generally produce different regions. A failed surface
    intersection rejects the complete ring, never its offending points.
    """
    try:
        main = secondary.main
        shift, scale, rotation = map(float, (secondary.shift, secondary.scale, secondary.rotation))
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        raise ValueError('Invalid DCX secondary ellipse parameters') from error
    lon, lat, major, minor, azimuth, target = _ellipse_parameters(main, reference)
    if (not all(math.isfinite(value) for value in (shift, scale, rotation))
            or shift not in (0, 1, 2, 3) or scale not in tuple(i / 4 for i in range(1, 9))
            or not 0 <= rotation < 360 or not (rotation / 11.25).is_integer()):
        raise ValueError('Invalid DCX secondary ellipse transform')
    angle = np.radians(azimuth + rotation)
    phase = np.linspace(0, 2 * np.pi, _STEPS + 1)
    x = major * scale * np.cos(phase) + major * shift
    y = minor * scale * np.sin(phase)
    e = x * np.cos(angle) - y * np.sin(angle)
    n = x * np.sin(angle) + y * np.cos(angle)
    return _project_ring(lon, lat, e, n, target)


def _project_ring(lon, lat, e, n, target):
    """Invert main-centred east/north positions onto the near WGS84 surface."""
    latitude, longitude = np.radians((lat, lon))
    sin_lat, cos_lat = np.sin(latitude), np.cos(latitude)
    sin_lon, cos_lon = np.sin(longitude), np.cos(longitude)
    normal = _A / np.sqrt(1 - _E2 * sin_lat ** 2)
    centre = normal * np.array((cos_lat * cos_lon, cos_lat * sin_lon, (1 - _E2) * sin_lat))
    east = np.array((-sin_lon, cos_lon, 0.))
    north = np.array((-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat))
    up = np.array((cos_lat * cos_lon, cos_lat * sin_lon, sin_lat))

    tangent = centre + e[:, None] * east + n[:, None] * north

    # Intersect the line through each EN position, parallel to geodetic up,
    # with WGS84. Choose the nearer surface, not the far side of the Earth.
    a = np.dot(up * _INVERSE_SQUARE_AXES, up)
    b = 2 * (tangent * _INVERSE_SQUARE_AXES) @ up
    c = np.sum(tangent ** 2 * _INVERSE_SQUARE_AXES, axis=1) - 1
    discriminant = b ** 2 - 4 * a * c
    if np.any(discriminant <= 0) or not np.all(np.isfinite(discriminant)):
        raise ValueError('DCX ellipse has no unique near WGS84 surface')
    # Stable quadratic root: avoid cancellation for small ellipses.
    q = -.5 * (b + np.copysign(np.sqrt(discriminant), b))
    root_one, root_two = q / a, c / q
    height = np.where(np.abs(root_one) < np.abs(root_two), root_one, root_two)
    surface = tangent + height[:, None] * up
    if not np.all(np.isfinite(surface)):
        raise ValueError('Invalid DCX WGS84 boundary')
    distance = np.hypot(surface[:, 0], surface[:, 1])
    # The intersection is on the ellipsoid (height zero), so this inversion
    # is exact without an iterative general ECEF-to-geodetic conversion.
    latitude = np.degrees(np.arctan2(surface[:, 2], (1 - _E2) * distance))
    if np.any(np.abs(latitude) >= 85):
        raise ValueError('DCX ellipse crosses the Mercator latitude limit')
    longitude = np.degrees(np.unwrap(np.arctan2(surface[:, 1], surface[:, 0])))
    if abs(longitude[-1] - longitude[0]) > 1e-7 or np.ptp(longitude) >= 180:
        raise ValueError('DCX ellipse surrounds a pole or cannot be unwrapped locally')
    longitude += 360 * round((target - float(np.mean(longitude[:-1]))) / 360)
    mercator = np.degrees(np.log(np.tan(np.pi / 4 + np.radians(latitude) / 2)))
    ring = np.column_stack((longitude, mercator))
    ring[-1] = ring[0]
    if not np.all(np.isfinite(ring)) or not _simple_ring(ring):
        raise ValueError('DCX ellipse has an invalid projected boundary')
    return ring
