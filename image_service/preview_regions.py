"""Unpublished regional maps with geographic context and complete island scope.

Semantic adapters and geometry stay in their published modules. This local
orchestration adds context without changing old assets, helpers or globals.
"""

import math

from matplotlib.colors import to_rgba
from matplotlib.patches import Rectangle
import numpy as np

from image_service import map_base_v3 as terrain
from image_service import map_v3 as geography
from image_service import map_v4 as weather_labels
from image_service import preview_context as context
from image_service import region_map as layout
from image_service import render as base

LAYERS = ('weather', 'eew', 'marine', 'prefectures', 'tsunami')


def _regions(plan):
    forecast = plan.layer in ('weather', 'eew')
    source = geography if forecast else layout
    features, leaves, anchors = source._layer(plan.layer), {}, []
    painted_layer = 'prefectures' if forecast else plan.layer
    reference = None
    for notice in plan.notices:
        to_rgba(notice.color)
        members = []
        for code in source._expand(plan.layer, notice.code):
            selected = [features[code]] if forecast else features[code]
            if code in leaves and leaves[code]['color'] != notice.color:
                raise ValueError('Overlapping regions have conflicting colours')
            for feature in selected:
                lon, lat = layout._position(feature['properties'].get('anchor'))
                if reference is None:
                    reference = lon
                members.append((lon + round((reference - lon) / 360) * 360,
                                float(base._my(lat))))
                leaf = leaves.setdefault(code, {'color': notice.color, 'parts': []})
                if not any(old is feature for old, _ in leaf['parts']):
                    leaf['parts'].append((feature, layout._geometry(feature, painted_layer, reference)))
        centre = np.mean(members, axis=0)
        anchors.append(min(members, key=lambda point: (math.dist(point, centre), point)))
    full, principal = [], []
    for leaf in leaves.values():
        polygons = []
        for _, parts in leaf['parts']:
            full.extend(parts if painted_layer == 'tsunami'
                        else [ring for polygon in parts for ring in polygon])
            if painted_layer == 'prefectures':
                polygons.extend(parts)
        if polygons:
            principal.extend(max(polygons, key=lambda polygon: abs(layout._signed_area(polygon[0]))
                                 - sum(abs(layout._signed_area(hole)) for hole in polygon[1:])))
    points = np.asarray(anchors)
    full.append(points)
    principal.append(points)
    return painted_layer, leaves, anchors, full, principal


def draw_map(fig, plan):
    """Retain every received area and label, or reject the complete map."""
    if plan.layer not in LAYERS or not 1 <= len(plan.notices) <= 16:
        raise ValueError('Unsupported regional preview map')
    painted_layer, leaves, anchors, full, principal = _regions(plan)
    sequences = principal if painted_layer == 'prefectures' else full
    limits, locator = context.limits(sequences)
    # A small remote island may fall outside the principal land mass even when
    # the whole main viewport is broad. Preserve it in the same locator rather
    # than placing a second inset over the first.
    visible = list(layout._body_limits(limits))
    if locator:
        # A minor component can lie inside the main body yet underneath the
        # locator. Principal geometry fits left of the reserved inset, and all
        # other components outside that safe corridor belong in the locator.
        visible[1] = limits[0] + (limits[1] - limits[0]) * context.LOCATOR_OBSTACLE[0]
    remote = painted_layer == 'prefectures' and not layout._inside_limits(full, visible)
    if remote and not locator:
        limits = layout._limits(sequences, centre_at=(.39, .47), span=(.64, .52))
        locator = True
    ax = terrain.axes(fig, limits)
    context.draw_prefecture_boundaries(ax)
    layout._paint_regions(ax, leaves, painted_layer)
    if painted_layer == 'prefectures':
        clip = Rectangle((0, layout.LABEL_BOTTOM), 1, layout.LABEL_TOP - layout.LABEL_BOTTOM,
                         transform=fig.transFigure)
        for artist in ax.patches:
            if (artist.get_gid() or '').startswith('region-area-'):
                artist.set_clip_path(clip)
    if locator:
        overview = context.draw_locator(fig, limits, extent_sequences=full)
        if remote:
            # The complete source geometry remains visible at small scale,
            # including every island cropped from the main map's body.
            layout._paint_regions(overview, leaves, painted_layer)
    for point, notice in zip(anchors, plan.notices, strict=True):
        ax.scatter(*point, s=35, facecolor=base.SEA, edgecolor=notice.color, linewidth=1.5,
                   zorder=8, gid=f'region-anchor-{notice.code}')
    annotations = weather_labels._annotations if plan.layer == 'weather' else layout._annotations
    annotations(fig, ax, anchors, plan.notices,
                obstacles=(context.LOCATOR_OBSTACLE,) if locator else ())
    return ax
