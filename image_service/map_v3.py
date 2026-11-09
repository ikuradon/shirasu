"""Weather and EEW geography, isolated from the published v2 renderer.

Only the pinned local GIS assets are used. The v2 drawing helpers are reused
without changing their globals, caches or asset lookup paths.
"""

from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import zlib

from matplotlib.colors import to_rgba
from matplotlib.patches import Rectangle
import numpy as np

from image_service import region_map as style
from image_service import render as base
from image_service import map_base_v3 as terrain

ASSETS = Path(__file__).parent / 'assets' / 'v3'
LAYERS = ('weather', 'eew')
ASSET_NAMES = tuple(name + extension for name in LAYERS
                    for extension in ('.geojson.zlib', '_groups.json'))


@lru_cache(maxsize=1)
def verify_assets():
    base.verify_assets()
    manifest = json.loads((ASSETS / 'regional_manifest.json').read_text())
    for name in ASSET_NAMES:
        if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('V3 regional assets do not match their frozen version')
    return True


@lru_cache(maxsize=2)
def _layer(name):
    if name not in LAYERS:
        raise ValueError('Unknown forecast layer')
    verify_assets()
    compressed = (ASSETS / (name + '.geojson.zlib')).read_bytes()
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, 64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise RuntimeError('Invalid compressed regional asset')
    data = json.loads(raw)
    if data.get('type') != 'FeatureCollection':
        raise RuntimeError('Invalid regional feature collection')
    result = {}
    for feature in data['features']:
        code = style._code(feature['properties']['code'])
        if code in result or feature.get('type') != 'Feature':
            raise RuntimeError('Duplicate or invalid regional feature')
        result[code] = feature
    return result


@lru_cache(maxsize=2)
def _groups(layer):
    if layer not in LAYERS:
        raise ValueError('Unknown forecast layer')
    verify_assets()
    raw = json.loads((ASSETS / (layer + '_groups.json')).read_text())
    return {style._code(parent): tuple(style._code(child) for child in children)
            for parent, children in raw.items()}


def _expand(layer, code):
    features, groups = _layer(layer), _groups(layer)
    def visit(current, seen):
        if current in seen:
            raise ValueError('Cyclic forecast groups')
        if current in groups:
            if not groups[current]:
                raise ValueError('Empty forecast group')
            return set().union(*(visit(child, seen | {current}) for child in groups[current]))
        if current not in features:
            raise ValueError('Unknown forecast region')
        return {current}
    return tuple(sorted(visit(style._code(code), set())))


def draw_map(fig, plan):
    if plan.layer not in LAYERS or not 1 <= len(plan.notices) <= 16:
        raise ValueError('Unsupported forecast map')
    features, leaves, anchors = _layer(plan.layer), {}, []
    reference = None
    for notice in plan.notices:
        to_rgba(notice.color)
        members = []
        for code in _expand(plan.layer, notice.code):
            feature = features[code]
            lon, lat = style._position(feature['properties']['anchor'])
            if reference is None:
                reference = lon
            point = (lon + round((reference - lon) / 360) * 360, float(base._my(lat)))
            members.append(point)
            if code in leaves:
                if leaves[code]['color'] != notice.color:
                    raise ValueError('Overlapping forecast regions have conflicting colours')
            else:
                leaves[code] = {'color': notice.color, 'parts': [(feature,
                    style._geometry(feature, 'prefectures', reference))]}
        centre = np.mean(members, axis=0)
        anchors.append(min(members, key=lambda point: (math.dist(point, centre), point)))
    full, principal = [], []
    for leaf in leaves.values():
        polygons = [polygon for _, parts in leaf['parts'] for polygon in parts]
        full.extend(ring for polygon in polygons for ring in polygon)
        principal.extend(max(polygons, key=lambda polygon: abs(style._signed_area(polygon[0]))
                             - sum(abs(style._signed_area(hole)) for hole in polygon[1:])))
    full.append(np.asarray(anchors))
    principal.append(np.asarray(anchors))
    limits = style._limits(principal)
    overview = not style._inside_limits(full, style._body_limits(limits))
    if overview:
        limits = style._limits(principal, centre_at=(.38, .47), span=(.55, .52))
    ax = terrain.axes(fig, limits)
    # Forecast areas describe the code's scope. They are not a municipal warning
    # footprint or a spatially uniform shaking field, so retain neutral outlines.
    style._paint_regions(ax, leaves, 'prefectures')
    clip = Rectangle((0, style.LABEL_BOTTOM), 1, style.LABEL_TOP - style.LABEL_BOTTOM,
                     transform=fig.transFigure)
    for artist in ax.patches:
        if (artist.get_gid() or '').startswith('region-area-'):
            artist.set_clip_path(clip)
    if overview:
        terrain.overview(fig, full, leaves, limits)
    for point, notice in zip(anchors, plan.notices, strict=True):
        ax.scatter(*point, s=35, facecolor=base.SEA, edgecolor=notice.color, linewidth=1.5,
                   zorder=8, gid=f'region-anchor-{notice.code}')
    style._annotations(fig, ax, anchors, plan.notices,
                       obstacles=(style.OVERVIEW_OBSTACLE,) if overview else ())
    return ax
