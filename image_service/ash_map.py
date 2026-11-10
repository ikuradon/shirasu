"""Ash forecasts over municipal outlines; no inferred plume or deposition field."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import zlib

from matplotlib.patches import Rectangle
import numpy as np

from image_service import map_base_v3 as terrain
from image_service import region_map as layout
from image_service import render as base
from image_service import volcano_map
from image_service import preview_context as context
from image_service.ash_reports import AshNotice
from image_service.preview_labels import draw_labels

ASSETS = Path(__file__).parent / 'assets' / 'preview'


@lru_cache(maxsize=1)
def verify_assets():
    terrain.verify_assets()
    volcano_map.verify_assets()
    context.verify_assets()
    manifest = json.loads((ASSETS / 'municipalities_manifest.json').read_text())
    for name in ('municipalities.geojson.zlib', 'MUNICIPALITIES_NOTICE.txt'):
        if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('Municipal assets do not match their reviewed source')
    return True


@lru_cache(maxsize=1)
def _municipalities():
    verify_assets()
    decoder = zlib.decompressobj()
    data = decoder.decompress((ASSETS / 'municipalities.geojson.zlib').read_bytes(), 64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024 or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise RuntimeError('Invalid compressed municipal geometry')
    collection = json.loads(data)
    if collection.get('type') != 'FeatureCollection':
        raise RuntimeError('Invalid municipal feature collection')
    features = {}
    for feature in collection['features']:
        code = layout._code(feature['properties']['code'])
        if (code in features or feature.get('type') != 'Feature'
                or feature['geometry']['type'] not in ('Polygon', 'MultiPolygon')):
            raise RuntimeError('Duplicate or invalid municipal feature')
        features[code] = feature
    return features


def draw_map(fig, plan):
    if plan.layer != 'ash_fall' or not 1 <= len(plan.notices) <= 15:
        raise ValueError('Unsupported ash forecast map')
    catalogue = _municipalities()
    location = volcano_map._positions().get(str(plan.volcano_code))
    if location is None:
        raise ValueError('No official position for this volcano')
    lon, lat = layout._position(location['coordinates'])
    reference, volcano_point = lon, (lon, float(base._my(lat)))
    leaves, anchors, full, principal = {}, [], [], []
    for notice in plan.notices:
        feature = catalogue.get(notice.code)
        if feature is None or notice.code in leaves:
            raise ValueError('Missing or duplicated municipality geometry')
        polygons = layout._geometry(feature, 'prefectures', reference)
        leaves[notice.code] = {'color': notice.color, 'parts': [(feature, polygons)]}
        full.extend(ring for polygon in polygons for ring in polygon)
        principal.extend(max(polygons, key=lambda polygon: abs(layout._signed_area(polygon[0]))
                             - sum(abs(layout._signed_area(hole)) for hole in polygon[1:])))
        x, y = layout._position(feature['properties']['anchor'])
        anchors.append((x + round((reference - x) / 360) * 360, float(base._my(y))))
    points = np.asarray([*anchors, volcano_point])
    full.append(points)
    principal.append(points)
    limits, locator = context.limits(principal, minimum=1.2)
    overview = not layout._inside_limits(full, layout._body_limits(limits))
    if overview and not locator:
        locator = True
        limits = layout._limits(principal, centre_at=(.39, .47), span=(.64, .52), minimum=1.2)
    ax = terrain.axes(fig, limits)
    context.draw_prefecture_boundaries(ax)
    # A municipal warning code specifies a named area, not uniform ash fall
    # over every part of its boundary. Keep the polygons neutral.
    layout._paint_regions(ax, leaves, 'prefectures')
    clip = Rectangle((0, layout.LABEL_BOTTOM), 1, layout.LABEL_TOP - layout.LABEL_BOTTOM,
                     transform=fig.transFigure)
    for artist in ax.patches:
        if (artist.get_gid() or '').startswith('region-area-'):
            artist.set_clip_path(clip)
    if locator:
        inset = context.draw_locator(fig, limits, extent_sequences=full)
        # The locator doubles as the complete-area overview. Keep every island
        # of each received municipality even when the principal land is enlarged.
        layout._paint_regions(inset, leaves, 'prefectures')
    for point, notice in zip(anchors, plan.notices, strict=True):
        ax.scatter(*point, s=35, facecolor=base.SEA, edgecolor=notice.color, linewidth=1.5,
                   zorder=8, gid=f'municipality-anchor-{notice.code}')
    ax.scatter(*volcano_point, marker='^', s=130, facecolor=base.WHITE, edgecolor=base.SEA,
               linewidth=1.1, zorder=8, gid=f'volcano-point-{plan.volcano_code}')
    volcano_label = AshNotice(plan.volcano_code, '火山：' + plan.volcano_name, (), base.WHITE, ())
    draw_labels(fig, ax, [*anchors, volcano_point], [*plan.notices, volcano_label],
                prefix='ash-hazard', obstacles=(context.LOCATOR_OBSTACLE,) if locator else ())
    return ax
