"""Geographic context for offline maps, independent of the received hazards.

The pinned JMA weather geography supplies named local units: ordinary
prefectures, Hokkaido's regions, and the separate Izu/Ogasawara/Amami/Okinawa
units. A compact subject gains one nearby unit's local land component. This
does not expand its warning scope, move a reported point, or replace any of
the sequences the caller must fit. Remote components of a named unit never
pull an island view back to the mainland.
"""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from matplotlib.path import Path

from image_service import map_v3 as geography
from image_service import region_map as geometry
from image_service import render as base


# Separate island forecast units in the pinned JMA catalogue. Other units'
# principal components remain mainland context even when their areas are
# small (Tokyo, Kanagawa, Osaka and Kagawa in particular).
ISLAND_UNITS = frozenset((130020, 130030, 130040, 460040,
                         471000, 472000, 473000, 474000))


@dataclass(frozen=True)
class Place:
    name: str
    position: tuple[float, float]
    polygon: tuple
    code: int
    kind: str = 'prefecture'


@dataclass(frozen=True)
class ScaleContext:
    sequences: tuple
    minimum: float
    places: tuple[Place, ...]


@dataclass(frozen=True)
class _Part:
    code: int
    rings: list
    low: tuple
    high: tuple
    area: float


@lru_cache(maxsize=1)
def _catalogue():
    # Reuse the verified decoder/cache rather than load a second copy of the
    # twenty-megabyte geographic catalogue for context rendering.
    units = geography._layer('weather')
    parts, main = [], {}
    for code, feature in sorted(units.items()):
        raw = feature['geometry']
        polygons = [raw['coordinates']] if raw['type'] == 'Polygon' else raw['coordinates']
        for rings in polygons:
            outer = np.asarray(rings[0], dtype=float)
            low, high = outer.min(axis=0), outer.max(axis=0)
            area = abs(geometry._signed_area(outer))
            part = _Part(code, rings, (float(low[0]), float(base._my(low[1]))),
                         (float(high[0]), float(base._my(high[1]))), area)
            index = len(parts)
            parts.append(part)
            if code not in main or area > parts[main[code]].area:
                main[code] = index
    low = np.asarray([part.low for part in parts])
    high = np.asarray([part.high for part in parts])
    return units, tuple(parts), main, low, high


@lru_cache(maxsize=256)
def _polygon(index):
    _, parts, _, _, _ = _catalogue()
    feature = {'geometry': {'type': 'Polygon', 'coordinates': parts[index].rings}}
    polygon, = geometry._geometry(feature, 'prefectures', 139.)
    for ring in polygon:
        ring.setflags(write=False)
    return polygon


def _inside(point, polygon):
    return (Path(polygon[0]).contains_point(point)
            and not any(Path(hole).contains_point(point) for hole in polygon[1:]))


def _distance(point, polygon):
    if _inside(point, polygon):
        return 0.
    distance = float('inf')
    for ring in polygon:
        start, delta = ring[:-1], ring[1:] - ring[:-1]
        lengths = np.einsum('ij,ij->i', delta, delta)
        along = np.divide(np.einsum('ij,ij->i', point - start, delta), lengths,
                          out=np.zeros(len(delta)), where=lengths > 0)
        closest = start + np.clip(along, 0, 1)[:, None] * delta
        distance = min(distance, float(np.linalg.norm(closest - point, axis=1).min()))
    return distance


def _nearest(point):
    _, parts, _, low, high = _catalogue()
    distances = np.linalg.norm(np.maximum(np.maximum(low - point, point - high), 0), axis=1)
    best, best_distance = None, float('inf')
    # Bounds are lower bounds on actual polygon distance. Stop when no later
    # component can be closer, including small offshore islands.
    for index in np.argsort(distances, kind='stable'):
        if distances[index] > min(best_distance, .9):
            break
        distance = _distance(point, _polygon(int(index)))
        if distance < best_distance:
            best, best_distance = int(index), distance
            if distance == 0:
                break
    return best if best_distance <= .9 else None


def _representative(index, preferred):
    polygon = _polygon(index)
    if _inside(preferred, polygon):
        return tuple(map(float, preferred))
    ring = polygon[0]
    a, b = ring[:-1], ring[1:]
    cross = a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1]
    centre = ((a + b) * cross[:, None]).sum(axis=0) / (3 * cross.sum())
    if _inside(centre, polygon):
        return tuple(map(float, centre))
    low, high = ring.min(axis=0), ring.max(axis=0)
    candidates = np.asarray([(x, y) for x in np.linspace(low[0], high[0], 25)[1:-1]
                             for y in np.linspace(low[1], high[1], 25)[1:-1]])
    valid = Path(ring).contains_points(candidates)
    for hole in polygon[1:]:
        valid &= ~Path(hole).contains_points(candidates)
    candidates = candidates[valid]
    if not len(candidates):
        # A very narrow feature may have no useful label location. The caller
        # still gets its geometry; omitting a context name loses no forecast.
        return None
    return tuple(map(float, min(candidates, key=lambda value: np.linalg.norm(value - centre))))


def _island(index):
    _, parts, main, _, _ = _catalogue()
    code = parts[index].code
    return code in ISLAND_UNITS or index != main[code]


def _local_components(index):
    _, parts, _, low, high = _catalogue()
    if not _island(index):
        return (index,)
    seed = parts[index]
    distance = np.linalg.norm(np.maximum(np.maximum(low - high[index], low[index] - high), 0), axis=1)
    return tuple(i for i, part in enumerate(parts)
                 if part.code == seed.code and _island(i) and distance[i] <= .45)


def _place(index, shift):
    units, parts, _, _, _ = _catalogue()
    part = parts[index]
    feature = units[part.code]
    lon, lat = geometry._position(feature['properties']['anchor'])
    position = _representative(index, (lon, float(base._my(lat))))
    if position is None:
        return None
    polygon = tuple(ring + np.asarray((shift, 0.)) for ring in _polygon(index))
    return Place(feature['properties']['name'], (position[0] + shift, position[1]),
                 polygon, part.code, 'island' if _island(index) else 'prefecture')


def context_for(sequences, *, minimum=4.):
    """Return additional context only; callers must retain all source geometry.

    Compact local views fit one containing/nearby named region, plus the
    original geometry. Ocean/basin-wide minima (>= 8 degrees) are retained.
    Islands use local components and a 2.2-degree floor instead of applying a
    mainland minimum: their distant parent prefecture is never added.
    Coordinates are the caller's already-unwrapped longitude/Mercator degrees.
    """
    sequences = tuple(np.asarray(sequence, dtype=float) for sequence in sequences)
    if (not sequences or any(sequence.ndim != 2 or sequence.shape[1] != 2
                              or not len(sequence) or not np.isfinite(sequence).all()
                              for sequence in sequences)
            or not np.isfinite(minimum) or minimum <= 0):
        raise ValueError('Invalid source geometry for contextual map scale')
    points = np.concatenate(sequences)
    low, high = points.min(axis=0), points.max(axis=0)
    centre = (low + high) / 2
    shift = round((centre[0] - 139.) / 360) * 360
    reference = centre - np.asarray((shift, 0.))
    units, parts, main, part_low, part_high = _catalogue()
    compact = minimum < 8 and high[0] - low[0] <= 2.2 and high[1] - low[1] <= 2.8
    core = _nearest(reference) if compact else None
    additional = ()
    if core is not None:
        indices = _local_components(core)
        additional = tuple(ring + np.asarray((shift, 0.))
                           for index in indices for ring in _polygon(index))
        minimum = 2.2 if _island(core) else max(3., minimum)
    # Supplying names does not force their regions into the main viewport.
    # Offer the closest normal region anchors; final placement is performed
    # only after the thematic map has reserved its text and marker positions.
    width = max(minimum, float(high[0] - low[0]) / .51,
                float(high[1] - low[1]) / .65 * 16 / 9)
    places = []
    if width <= 16:
        candidates = []
        for code, principal in main.items():
            index = core if core is not None and parts[core].code == code else principal
            distance = float(np.linalg.norm(np.maximum(
                np.maximum(part_low[index] - reference, reference - part_high[index]), 0)))
            if distance <= max(width * .55, 1.8):
                candidates.append((index != core, distance, code, index))
        for _, _, _, index in sorted(candidates):
            place = _place(index, shift)
            if place is not None:
                places.append(place)
    return ScaleContext(additional, float(minimum), tuple(places))
