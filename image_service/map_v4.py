"""V4 weather badges, using the immutable v3 geography and v2 label placement.

The local draw orchestration keeps published renderers free of new behaviour;
shared helpers are called without changing their globals, classes or caches.
"""

import math

from matplotlib.colors import to_rgba
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, PaddedBox, TextArea, VPacker
from matplotlib.patches import Rectangle
import numpy as np

from image_service import region_map as style
from image_service import render as base
from image_service import map_base_v3 as terrain
from image_service import map_v3 as geography


def _badge(text, palette, size, gid):
    label = TextArea(text, textprops={'fontproperties': base._font(size, True),
                     'color': palette.foreground, 'parse_math': False, 'gid': gid})
    return PaddedBox(label, pad=4, draw_frame=True,
                     patch_attrs={'facecolor': palette.background, 'edgecolor': palette.edge,
                                  'linewidth': 1.1, 'boxstyle': 'square,pad=0'})


def _annotations(fig, ax, anchors, notices, *, obstacles=()):
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    points = [fig.transFigure.inverted().transform(ax.transData.transform(point)) for point in anchors]
    outlines = [(artist.get_transform().transform_path(artist.get_path()), artist.get_linewidth())
                for artist in (*ax.patches, *ax.lines)
                if (artist.get_gid() or '').startswith(('region-area-', 'region-coast-'))]
    boundaries = style._boundary_mask(outlines) if outlines else None
    for scale in (1, .85, .7):
        boxes, sizes = [], []
        for notice in notices:
            children = [TextArea(text, textprops={'fontproperties': base._font(21 * scale, True),
                        'color': notice.color, 'parse_math': False})
                        for text in base._wrap(notice.title, 36)]
            for index, (detail, palette) in enumerate(zip(notice.lines, notice.styles, strict=True)):
                # Measure the padded badge as part of the placement geometry,
                # so borders cannot overlap neighbouring labels or leaders.
                children.append(_badge('\n'.join(base._wrap(detail, 44)), palette, 16 * scale,
                                       f'weather-hazard-{notice.code}-{index}'))
            box = VPacker(children=children, align='left', pad=0, sep=6)
            box.set_figure(fig)
            extent = box.get_window_extent(renderer)
            boxes.append(box)
            sizes.append(((extent.width + 24) / base.WIDTH, (extent.height + 24) / base.HEIGHT))
        try:
            rectangles = style._place_labels(points, sizes, boundaries=boundaries, obstacles=obstacles)
        except ValueError:
            continue
        for point, projected, box, rectangle in zip(anchors, points, boxes, rectangles, strict=True):
            centre = ((rectangle[0] + rectangle[2]) / 2, (rectangle[1] + rectangle[3]) / 2)
            start, end = base._leader_segment(projected, rectangle)
            ax.add_line(Line2D(*zip(start, end), transform=fig.transFigure, color='#899cac', linewidth=1,
                              zorder=7, gid='label-leader'))
            ax.add_artist(AnnotationBbox(box, point, xycoords='data', xybox=centre, boxcoords=fig.transFigure,
                          frameon=False, box_alignment=(.5, .5), zorder=9))
        return rectangles
    raise ValueError('Weather labels do not fit')


def draw_map(fig, plan):
    if plan.layer != 'weather' or not 1 <= len(plan.notices) <= 16:
        raise ValueError('Unsupported forecast map')
    features, leaves, anchors = geography._layer(plan.layer), {}, []
    reference = None
    for notice in plan.notices:
        to_rgba(notice.color)
        members = []
        for code in geography._expand(plan.layer, notice.code):
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
    _annotations(fig, ax, anchors, plan.notices,
                       obstacles=(style.OVERVIEW_OBSTACLE,) if overview else ())
    return ax
