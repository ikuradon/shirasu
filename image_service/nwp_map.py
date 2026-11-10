"""Offline NWPTAC forecast points; positions never imply a coastal warning area."""

from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from image_service import map_base_v3 as terrain
from image_service import region_map as layout
from image_service import render as base
from image_service import preview_context as context

ASSETS = Path(__file__).parent / 'assets' / 'preview'
POINT_CODES = frozenset(range(1, 42)) | (frozenset(range(66, 87)) - {73})


@lru_cache(maxsize=1)
def verify_assets():
    terrain.verify_assets()
    context.verify_assets()
    manifest = json.loads((ASSETS / 'nwp_manifest.json').read_text())
    for name in ('nwp_points.json', 'NWP_NOTICE.txt'):
        if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('NWPTAC assets do not match the reviewed catalogue')
    return True


@lru_cache(maxsize=1)
def _positions():
    verify_assets()
    rows = json.loads((ASSETS / 'nwp_points.json').read_text())['points']
    points = {}
    for row in rows:
        code = row['code']
        if type(code) is not int or code not in POINT_CODES or code in points:
            raise RuntimeError('Invalid or duplicated NWPTAC forecast point')
        lon, lat = row['longitude'], row['latitude']
        if (type(lon) not in (int, float) or type(lat) not in (int, float)
                or not math.isfinite(lon) or not math.isfinite(lat)
                or not -180 <= lon <= 180 or not -85 < lat < 85):
            raise RuntimeError('Invalid NWPTAC forecast position')
        points[code] = row
    if set(points) != POINT_CODES:
        raise RuntimeError('Incomplete NWPTAC forecast catalogue')
    return points


def draw_map(fig, plan):
    if plan.layer != 'northwest_pacific_tsunami' or not 1 <= len(plan.notices) <= 16:
        raise ValueError('Unsupported NWPTAC map')
    catalogue, positions, seen = _positions(), [], set()
    reference = None
    for notice in plan.notices:
        if notice.code not in catalogue or notice.code in seen:
            raise ValueError('Unknown or repeated NWPTAC forecast point')
        seen.add(notice.code)
        row = catalogue[notice.code]
        lon, lat = row['longitude'], row['latitude']
        if reference is None:
            reference = lon
        positions.append((lon + round((reference - lon) / 360) * 360, float(base._my(lat))))
    limits, locator = context.limits([np.asarray(positions)], minimum=14.)
    ax = terrain.axes(fig, limits)
    context.draw_prefecture_boundaries(ax)
    for point, notice in zip(positions, plan.notices, strict=True):
        ax.scatter(*point, s=65, facecolor=notice.color, edgecolor=base.WHITE,
                   linewidth=1.1, zorder=8, gid=f'nwp-point-{notice.code}')
    # There is no earthquake location, propagation path, coastal extent or
    # domestic warning class in these received point forecasts.
    if locator:
        context.draw_locator(fig, limits, extent_sequences=(np.asarray(positions),))
    layout._annotations(fig, ax, positions, plan.notices,
                        obstacles=(context.LOCATOR_OBSTACLE,) if locator else ())
    return ax
