"""Quiet geographic context for unpublished maps, using pinned existing assets."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import zlib

from matplotlib.collections import LineCollection
from matplotlib.patches import Rectangle
import numpy as np

from image_service import region_map as layout
from image_service import render as base

LOCATOR_POSITION = (.755, .19, .205, .22)
LOCATOR_OBSTACLE = (.745, .18, .975, .45)
ASSETS = Path(__file__).parent / 'assets' / 'preview'


@lru_cache(maxsize=1)
def verify_assets():
    manifest = json.loads((ASSETS / 'prefecture_boundaries_manifest.json').read_text())
    for name in ('prefecture_boundaries.geojson.zlib', 'PREFECTURE_BOUNDARIES_NOTICE.txt'):
        if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('Prefecture boundaries do not match their reviewed source')
    return True


@lru_cache(maxsize=1)
def _prefecture_lines():
    verify_assets()
    decoder = zlib.decompressobj()
    raw = decoder.decompress((ASSETS / 'prefecture_boundaries.geojson.zlib').read_bytes(),
                             4 * 1024 * 1024 + 1)
    if (len(raw) > 4 * 1024 * 1024 or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail):
        raise RuntimeError('Invalid compressed prefecture boundaries')
    collection = json.loads(raw)
    if collection.get('type') != 'FeatureCollection' or not collection.get('features'):
        raise RuntimeError('Invalid prefecture boundary collection')
    lines = []
    for feature in collection['features']:
        for line in layout._geometry(feature, 'tsunami', 139.):
            bounds = (float(line[:, 0].min()), float(line[:, 0].max()),
                      float(line[:, 1].min()), float(line[:, 1].max()))
            lines.append((line, bounds))
    return tuple(lines)


def draw_prefecture_boundaries(ax):
    left, right = ax.get_xlim()
    bottom, top = ax.get_ylim()
    centre = (left + right) / 2
    lines = []
    for line, (x0, x1, y0, y1) in _prefecture_lines():
        shift = round((centre - (x0 + x1) / 2) / 360) * 360
        if x1 + shift >= left and x0 + shift <= right and y1 >= bottom and y0 <= top:
            lines.append(line + np.asarray((shift, 0)))
    # Shared prefecture boundaries are neutral reference lines, never filled
    # warning areas or obstacles for forecast labels.
    boundaries = LineCollection(lines, colors='#8596a7', linewidths=.6, alpha=.38,
                                zorder=3, gid='preview-prefecture-boundaries')
    ax.add_collection(boundaries, autolim=False)
    return boundaries


def is_zoomed(main_limits):
    left, right, bottom, top = main_limits
    # A locator adds useful context to regional views. Wide basin/nationwide
    # maps already supply it, and should keep their space for received details.
    if right - left > 32 or top - bottom > 40:
        return False
    shift = round(((left + right) / 2 - 135) / 360) * 360
    japan_visible = (left <= 122 + shift and right >= 148 + shift
                     and bottom <= float(base._my(23)) and top >= float(base._my(47)))
    return not japan_visible


def limits(sequences, *, minimum=4.):
    sequences = tuple(sequences)
    main_limits = layout._limits(sequences, minimum=minimum)
    show_locator = is_zoomed(main_limits)
    if show_locator:
        main_limits = layout._limits(sequences, centre_at=(.39, .47), span=(.64, .52),
                                      minimum=minimum)
    return main_limits, show_locator


def draw_locator(fig, main_limits, *, extent_sequences=()):
    left, right, bottom, top = main_limits
    # Keep the Japanese archipelago recognizable even for a very small reach.
    # Expand further when needed to retain the complete main-map viewport.
    shift = round(((left + right) / 2 - 135) / 360) * 360
    japan = np.asarray(((122. + shift, float(base._my(23.))),
                        (148. + shift, float(base._my(47.)))))
    viewport = np.asarray(((left, bottom), (right, top)))
    locator_limits = layout._limits([japan, viewport, *extent_sequences], position=LOCATOR_POSITION,
                                    centre_at=(.5, .5), span=(.88, .88), minimum=0.)
    ax = layout._axes(fig, locator_limits, position=LOCATOR_POSITION)
    ax.set_gid('preview-locator')
    ax.set_zorder(10)
    ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes,
                           facecolor=base.SEA, edgecolor='#536777', linewidth=.7,
                           zorder=0, gid='preview-locator-frame'))
    ax.add_patch(Rectangle((left, bottom), right - left, top - bottom,
                           facecolor='#cce8f2', edgecolor=base.WHITE, alpha=.18,
                           linewidth=0, zorder=4, gid='preview-main-area'))
    ax.add_patch(Rectangle((left, bottom), right - left, top - bottom,
                           fill=False, edgecolor=base.WHITE, linewidth=1.2,
                           zorder=5, gid='preview-main-extent'))
    title_y = LOCATOR_POSITION[1] + LOCATOR_POSITION[3] + .015
    base._text(fig, LOCATOR_POSITION[0], title_y, '表示位置', 12, base.MUTED, zorder=11)
    base._text(fig, LOCATOR_POSITION[0] + LOCATOR_POSITION[2], title_y,
               '枠は拡大範囲', 10, base.MUTED, ha='right', zorder=11)
    return ax
