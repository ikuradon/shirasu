"""Flood forecasts drawn on JMA's exact forecast-reach flow lines.

Every component of a received forecast reach remains visible. These lines are
neither river banks nor inundation polygons, and the label anchors are points
on the source flow lines rather than invented observation stations.
"""

from dataclasses import replace
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import zlib

import numpy as np

from image_service import map_base_v3 as terrain
from image_service import region_map as layout
from image_service import render as base
from image_service.preview_labels import draw_labels
from image_service import preview_context as context

ASSETS = Path(__file__).parent / 'assets' / 'preview'
MAX_GEOMETRY_BYTES = 8 * 1024 * 1024
CASING = '#e8edf1'


@lru_cache(maxsize=1)
def verify_assets():
    terrain.verify_assets()
    context.verify_assets()
    manifest = json.loads((ASSETS / 'flood_manifest.json').read_text())
    for name in ('flood.geojson.zlib', 'FLOOD_NOTICE.txt'):
        if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('Flood assets do not match their reviewed source')
    return True


@lru_cache(maxsize=1)
def _rivers():
    verify_assets()
    decoder = zlib.decompressobj()
    raw = decoder.decompress((ASSETS / 'flood.geojson.zlib').read_bytes(), MAX_GEOMETRY_BYTES + 1)
    if (len(raw) > MAX_GEOMETRY_BYTES or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail):
        raise RuntimeError('Invalid compressed flood geometry')
    collection = json.loads(raw)
    if collection.get('type') != 'FeatureCollection' or not isinstance(collection.get('features'), list):
        raise RuntimeError('Invalid flood feature collection')
    features = {}
    for feature in collection['features']:
        code = feature.get('properties', {}).get('code')
        if (feature.get('type') != 'Feature' or not isinstance(code, str)
                or len(code) != 12 or not code.isascii() or not code.isdecimal()
                or int(code) <= 0 or int(code) in features):
            raise RuntimeError('Invalid or duplicated flood forecast code')
        # Validate all parts, not just the line picked as a label anchor.
        _parts(feature)
        features[int(code)] = feature
    if not features:
        raise RuntimeError('Empty flood forecast catalogue')
    return features


def _parts(feature):
    # The shared line decoder retains disconnected parts and validates finite
    # geographic coordinates. Japanese reaches share a single longitude frame.
    parts = layout._geometry(feature, 'tsunami', 139.)
    if any(not np.any(np.linalg.norm(np.diff(part, axis=0), axis=1) > 0) for part in parts):
        raise ValueError('Degenerate flood flow line')
    return parts


def _anchor(parts):
    """Halfway along the longest component, always on a source line segment."""
    lengths = [np.linalg.norm(np.diff(part, axis=0), axis=1) for part in parts]
    index = max(range(len(parts)), key=lambda i: float(lengths[i].sum()))
    points, lengths = parts[index], lengths[index]
    distance = float(lengths.sum()) / 2
    for start, end, length in zip(points[:-1], points[1:], lengths, strict=True):
        if length > 0 and distance <= length:
            return tuple(start + (end - start) * (distance / length))
        distance -= float(length)
    raise ValueError('Cannot place a flood label on the flow line')


def _check_shared_segments(feature, level, levels):
    """Reject conflicting colours on the pinned source's shared flow segments.

    All five positive-length overlaps in the reviewed catalogue share identical
    source edges. Compare their original vertices, independent of direction;
    touching endpoints and duplicate zero-length vertices are not overlaps.
    """
    geometry = feature['geometry']
    lines = ([geometry['coordinates']] if geometry['type'] == 'LineString'
             else geometry['coordinates'])
    for line in lines:
        for start, end in zip(line[:-1], line[1:], strict=True):
            segment = tuple(sorted((tuple(start), tuple(end))))
            if segment[0] == segment[1]:
                continue
            if levels.setdefault(segment, level) != level:
                raise ValueError('Overlapping flood reaches have different warning levels')


def draw_map(fig, plan):
    if plan.layer != 'flood' or not 1 <= len(plan.notices) <= 16:
        raise ValueError('Unsupported flood forecast map')
    catalogue = _rivers()
    reaches, sequences, anchors, seen = [], [], [], set()
    segment_levels = {}
    for notice in plan.notices:
        feature = catalogue.get(notice.code)
        if feature is None or notice.code in seen:
            raise ValueError('Missing or duplicated flood forecast geometry')
        seen.add(notice.code)
        parts = _parts(feature)
        _check_shared_segments(feature, notice.warning_level, segment_levels)
        reaches.append(parts)
        sequences.extend(parts)
        anchors.append(_anchor(parts))
    # Bounds use ALL vertices, including short or disconnected source parts.
    # The map body leaves the existing title/footer space clear without cropping
    # any warning reach or substituting a municipality/river-wide outline.
    # Keep all warning lines left of the locator; labels and their leaders also
    # avoid its reserved rectangle. No received reach is hidden by the inset.
    limits, show_locator = context.limits(sequences, minimum=.6)
    ax = terrain.axes(fig, limits)
    context.draw_prefecture_boundaries(ax)
    for notice, parts, anchor in zip(plan.notices, reaches, anchors, strict=True):
        def dash(width):
            # Matplotlib scales dashes by linewidth. Compensate so the casing,
            # shadow and release stroke have exactly the same gaps.
            return (0, (12 / width, 8 / width)) if notice.warning_level == 1 else '-'
        for index, part in enumerate(parts):
            identity = f'{notice.code}-{index}'
            ax.plot(*part.T, color=base.SEA, linewidth=7, linestyle=dash(7),
                    solid_capstyle='round', zorder=4, gid='flood-shadow-' + identity)
            # A pale casing keeps the official near-black occurrence colour
            # legible against the dark terrain; it does not expand a hazard area.
            ax.plot(*part.T, color=CASING, linewidth=5, linestyle=dash(5),
                    solid_capstyle='round', zorder=5, gid='flood-casing-' + identity)
            ax.plot(*part.T, color=notice.color, linewidth=3.2, linestyle=dash(3.2),
                    solid_capstyle='round', zorder=6, gid='flood-reach-' + identity)
        ax.scatter(*anchor, s=27, facecolor=notice.color, edgecolor=CASING,
                   linewidth=1, zorder=8, gid=f'flood-anchor-{notice.code}')
    # Severity belongs to the line and badge; every title remains readable,
    # including black occurrence and neutral release entries.
    labels = [replace(notice, color=base.WHITE) for notice in plan.notices]
    if show_locator:
        context.draw_locator(fig, limits)
    draw_labels(fig, ax, anchors, labels, prefix='flood-hazard',
                obstacles=(context.LOCATOR_OBSTACLE,) if show_locator else ())
    return ax
