"""Pinned 10m land for v3 close-up maps; v1/v2 keep their original terrain."""

from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import zlib

from matplotlib.path import Path as ShapePath
from matplotlib.patches import PathPatch, Rectangle
import numpy as np

from image_service import render as base
from image_service import region_map as style

ASSETS = Path(__file__).parent / 'assets' / 'v3'


@lru_cache(maxsize=1)
def verify_assets():
    manifest = json.loads((ASSETS/'land_manifest.json').read_text())
    for name in ('land.geojson.zlib', 'LAND_NOTICE.txt'):
        if hashlib.sha256((ASSETS/name).read_bytes()).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('V3 land does not match its frozen version')
    return True


@lru_cache(maxsize=1)
def _land():
    verify_assets()
    decoder = zlib.decompressobj()
    raw = decoder.decompress((ASSETS/'land.geojson.zlib').read_bytes(), 64*1024*1024+1)
    if len(raw) > 64*1024*1024 or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise RuntimeError('Invalid compressed land asset')
    result = []
    for feature in json.loads(raw)['features']:
        geometry = feature['geometry']
        polygons = ([geometry['coordinates']] if geometry['type'] == 'Polygon'
                    else geometry['coordinates'])
        for polygon in polygons:
            rings = []
            for ring in polygon:
                values = np.asarray(ring, dtype=float)
                values[:, 0] = np.degrees(np.unwrap(np.radians(values[:, 0])))
                if rings:
                    values[:, 0] += round((float(rings[0][:, 0].mean())
                                           - float(values[:, 0].mean()))/360)*360
                values[:, 1] = base._my(values[:, 1])
                rings.append(values)
            exterior = rings[0]
            bounds = (float(exterior[:, 0].min()), float(exterior[:, 0].max()),
                      float(exterior[:, 1].min()), float(exterior[:, 1].max()))
            result.append((rings, bounds))
    return tuple(result)


def axes(fig, limits, *, position=(0, 0, 1, 1)):
    ax = fig.add_axes(position, facecolor=base.SEA)
    ax.set_xlim(*limits[:2])
    ax.set_ylim(*limits[2:])
    ax.set_aspect('equal')
    centre, paths = (limits[0]+limits[1])/2, []
    for rings, (left, right, bottom, top) in _land():
        if top < limits[2] or bottom > limits[3]:
            continue
        shift = round((centre-(left+right)/2)/360)*360
        for delta in (shift-360, shift, shift+360):
            if right+delta < limits[0] or left+delta > limits[1]:
                continue
            shifted = [ring + np.asarray((delta, 0)) for ring in rings]
            paths.append(style._polygon_path(shifted))
    if paths:
        ax.add_patch(PathPatch(ShapePath.make_compound_path(*paths), facecolor=base.LAND,
                               edgecolor=base.COAST, linewidth=.55, zorder=2, gid='v3-land'))
    width = limits[1]-limits[0]
    spacing = 2 if width < 15 else 5 if width < 40 else 10
    for lon in np.arange(math.floor(limits[0]/spacing)*spacing, limits[1], spacing):
        ax.axvline(lon, color='#2a3743', linewidth=.5, zorder=1)
    for lat in range(-80, 85, spacing):
        y = base._my(lat)
        if limits[2] <= y <= limits[3]:
            ax.axhline(y, color='#2a3743', linewidth=.5, zorder=1)
    ax.set_axis_off()
    return ax


def overview(fig, sequences, leaves, main_limits):
    left, right, bottom, top = main_limits
    limits = style._limits([*sequences, np.asarray(((left, bottom), (right, top)))],
        position=style.OVERVIEW_POSITION, centre_at=(.5, .5), span=(.88, .88), minimum=1.)
    ax = axes(fig, limits, position=style.OVERVIEW_POSITION)
    ax.set_gid('region-overview')
    ax.set_zorder(10)
    ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes, facecolor=base.SEA,
                           edgecolor='#536777', linewidth=.6, zorder=0))
    style._paint_regions(ax, leaves, 'prefectures')
    ax.add_patch(Rectangle((left, bottom), right-left, top-bottom, fill=False,
                           edgecolor=base.WHITE, linewidth=.9, linestyle=(0, (4, 3)),
                           zorder=6, gid='region-main-extent'))
    base._text(fig, style.OVERVIEW_POSITION[0], .54, '離島を含む全域', 13, zorder=11)
    base._text(fig, style.OVERVIEW_POSITION[0], .515, '破線は主図範囲', 11, '#9cabb8', zorder=11)
    return ax
