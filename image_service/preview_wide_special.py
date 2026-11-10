"""Wide offline maps for river reaches, ash forecasts and NWPTAC points.

The semantic adapters and pinned geographic catalogues remain unchanged. Map
numbers identify the corresponding sidebar rows, not observation stations or
newly inferred hazard footprints. All distinct received forecasts are retained.
"""

import re

import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

from image_service import ash_map, volcano_map
from image_service import preview_wide_layout as wide
from image_service import region_map as geometry
from image_service import render as base
from image_service.ash_reports import ash_plan, STYLES as ASH_STYLES
from image_service.preview_wide_flood import draw_flood
from image_service.preview_wide_tsunami import draw_nwp

def _numbered_markers(fig, view, points, notices, *, prefix):
    """Use bounded numeric labels to associate real map anchors with rows.

    Marker positions stay at the supplied source anchors. A small numeric
    label may be offset when anchors are close; its leader preserves the
    association. Failure to place every label rejects the complete image.
    """
    fig.canvas.draw()
    projected = [fig.transFigure.inverted().transform(view.ax.transData.transform(point))
                 for point in points]
    # A square bounds the circular number, including its text and padding.
    half_x, half_y = 20 / wide.WIDTH, 20 / wide.HEIGHT
    obstacles = tuple(view.obstacles)
    selected, leaders = [], []
    offsets = [(0, 0)] + [(dx, dy) for radius in (34, 52, 76, 108)
                         for dx, dy in ((radius, 0), (-radius, 0), (0, radius), (0, -radius),
                                        (radius, radius), (-radius, radius),
                                        (radius, -radius), (-radius, -radius))]
    for index, (point, notice, source) in enumerate(zip(points, notices, projected, strict=True), 1):
        for dx, dy in offsets:
            x, y = source[0] + dx / wide.WIDTH, source[1] + dy / wide.HEIGHT
            box = (x - half_x, y - half_y, x + half_x, y + half_y)
            if not (.015 <= box[0] and box[2] < wide.PANEL_X - .01
                    and .055 <= box[1] and box[3] <= .78):
                continue
            if any(base._overlap(box, other) > 0 for other in (*obstacles, *selected)):
                continue
            segment = (tuple(source), (x, y))
            if (dx or dy) and any(base._segment_hits_box(segment, other)
                                  for other in (*obstacles, *selected)):
                continue
            if any(base._segment_hits_box(leader, box) for leader in leaders):
                continue
            # Do not hide a neighbouring anchor beneath this row's number.
            if any(other_index != index - 1 and box[0] <= other[0] <= box[2]
                   and box[1] <= other[1] <= box[3]
                   for other_index, other in enumerate(projected)):
                continue
            break
        else:
            raise ValueError('Forecast numbers do not fit outside the locator')
        selected.append(box)
        view.ax.scatter(*point, s=24, facecolor=notice.color, edgecolor=wide.WHITE,
                        linewidth=.8, zorder=8, gid=f'{prefix}-{notice.code}')
        if dx or dy:
            leaders.append(segment)
            view.ax.add_line(Line2D((source[0], x), (source[1], y), transform=fig.transFigure,
                                  color=wide.MUTED, linewidth=.8, zorder=8,
                                  gid=f'wide-number-leader-{prefix}-{notice.code}'))
        wide.text(fig, x, y, str(index), 13, wide.WHITE, bold=True, ha='center',
                  va='center', gid=f'wide-number-{prefix}-{notice.code}',
                  bbox={'boxstyle': 'circle,pad=.22', 'facecolor': wide.SEA,
                        'edgecolor': notice.color, 'linewidth': 1.3})
    return tuple(selected)


def _neutral_areas(ax, leaves):
    geometry._paint_regions(ax, leaves, 'prefectures')
    for artist in ax.patches:
        if (artist.get_gid() or '').startswith('region-area-'):
            artist.set_facecolor('none')
            artist.set_edgecolor('#9ba8b4')
            artist.set_linewidth(1.5)
            artist.set_alpha(1.)


def _ash_hours(notice):
    """Retain discrete hours and simultaneous ash/cinders in separate lanes."""
    hours = {hour: {} for hour in range(1, 7)}
    for line, style in zip(notice.lines, notice.styles, strict=True):
        match = re.fullmatch(r'基点＋([1-6])時間：(.+)', line)
        if match is None or style not in ASH_STYLES.values() or match[2] != style.label:
            raise ValueError('Unknown ash forecast line format')
        code = next(code for code, expected in ASH_STYLES.items() if style == expected)
        lane = 'cinders' if code == 4 else 'ash'
        if lane in hours[int(match[1])] and hours[int(match[1])][lane] != code:
            raise ValueError('Conflicting ash forecasts in one time cell')
        hours[int(match[1])][lane] = code
    return hours


def _ash_timeline(fig, plan):
    """Fit the entire municipality/time matrix before drawing any sidebar row."""
    rows = [(notice, _ash_hours(notice)) for notice in plan.notices]
    width = (wide.PANEL_RIGHT - wide.PANEL_LEFT) * wide.WIDTH
    active = {code for _, hours in rows for value in hours.values() for code in value.values()}
    start = .929 * wide.HEIGHT
    # Reserve the volcano, full base date, column headings, and a complete key.
    legend_height = 28 * len(active) + 72
    for size in (18, 17, 16, 15, 14):
        step = (size + 1) * wide.DPI / 72 * 1.24
        cell_height = 52 if size >= 17 else 46
        layout = []
        for notice, hours in rows:
            titles = wide._wrap(fig, notice.title, size + 1, width - 40, True)
            cinders = any('cinders' in value for value in hours.values())
            height = len(titles) * step + 12 + cell_height + 24
            if cinders:
                height += 27 + cell_height
            layout.append((notice, hours, titles, cinders, height))
        if start - 155 - sum(row[4] for row in layout) - legend_height >= .06 * wide.HEIGHT:
            break
    else:
        raise ValueError('Complete ash time matrix does not fit')

    def label(x, y, value, font_size, color=wide.WHITE, **kwargs):
        item = wide.text(fig, x, y / wide.HEIGHT, value, font_size, color,
                         va='top', **kwargs)
        item._wide_sidebar = True
        return item

    label(wide.PANEL_LEFT, start, plan.volcano_name, 21, bold=True)
    label(wide.PANEL_LEFT, start - 43,
          f'基点 {base._time(plan.base_time, full=True)} JST', 14, wide.MUTED)
    label(wide.PANEL_LEFT, start - 79, '基点からの時間', 14, wide.MUTED)
    gap = 5
    cell_width = (width - 5 * gap) / 6
    centres = [wide.PANEL_LEFT + (index * (cell_width + gap) + cell_width / 2) / wide.WIDTH
               for index in range(6)]
    for hour, x in enumerate(centres, 1):
        label(x, start - 113, f'＋{hour}', 14, ha='center', bold=True)
    label(wide.PANEL_RIGHT, start - 79, '単位：時間', 12, wide.MUTED, ha='right')
    y = start - 155
    for index, (notice, hours, titles, cinders, height) in enumerate(layout, 1):
        title_y = y
        label(wide.PANEL_LEFT, y, str(index), size + 1, bold=True)
        for line in titles:
            label(wide.PANEL_LEFT + 32 / wide.WIDTH, y, line, size + 1,
                  bold=True, gid=f'ash-municipality-{notice.code}')
            y -= step
        y -= 12
        for lane in ('ash', 'cinders') if cinders else ('ash',):
            if lane == 'cinders':
                label(wide.PANEL_LEFT, y, '小さな噴石', 12, wide.MUTED)
                y -= 27
            for hour, x in enumerate(centres, 1):
                code = hours[hour].get(lane)
                style = ASH_STYLES.get(code)
                face = style.background if style and code != 4 else '#22282e'
                edge = style.edge if style else '#53606b'
                identity = f'ash-cell-{notice.code}-{hour}-{lane}'
                patch = Rectangle((x - cell_width / 2 / wide.WIDTH, (y - cell_height) / wide.HEIGHT),
                                  cell_width / wide.WIDTH, cell_height / wide.HEIGHT,
                                  transform=fig.transFigure, facecolor=face, edgecolor=edge,
                                  linewidth=1.5 if code == 4 else .65, zorder=23, gid=identity)
                fig.add_artist(patch)
                value = {1: '少量', 2: 'やや\n多量', 3: '多量', 4: '◆'}.get(code, '—')
                text = wide.text(fig, x, (y - cell_height / 2) / wide.HEIGHT, value, 12,
                                 style.foreground if style else wide.MUTED, bold=bool(style),
                                 ha='center', va='center', linespacing=1.03, zorder=24,
                                 gid=f'{identity}-{code or "missing"}')
                text._wide_sidebar = True
                # The cell already provides contrast; suppress the generic
                # dark text halo which would muddy black-on-yellow labels.
                text.set_bbox(dict(facecolor='none', edgecolor='none', pad=0))
            y -= cell_height + 8
        y = title_y - height
    y -= 8
    for code in sorted(active):
        style = ASH_STYLES[code]
        fig.add_artist(Rectangle((wide.PANEL_LEFT, (y - 20) / wide.HEIGHT),
                                 18 / wide.WIDTH, 18 / wide.HEIGHT, transform=fig.transFigure,
                                 facecolor=style.background, edgecolor=style.edge,
                                 linewidth=1.5, zorder=23, gid=f'ash-legend-{code}'))
        label(wide.PANEL_LEFT + 28 / wide.WIDTH, y, style.label, 12)
        y -= 28
    label(wide.PANEL_LEFT, y - 8, '—：この時間の記載なし', 12, wide.MUTED)


def draw_ash(fig, reports):
    reports = tuple(reports)
    plan = ash_plan(reports)
    if not 1 <= len(plan.notices) <= 15:
        raise ValueError('Unsupported number of ash forecast municipalities')
    catalogue = ash_map._municipalities()
    location = volcano_map._positions().get(str(plan.volcano_code))
    if location is None:
        raise ValueError('No official position for this volcano')
    lon, lat = geometry._position(location['coordinates'])
    reference, volcano_point = lon, (lon, float(base._my(lat)))
    leaves, anchors, full, principal = {}, [], [], []
    for notice in plan.notices:
        feature = catalogue.get(notice.code)
        if feature is None or notice.code in leaves:
            raise ValueError('Missing or duplicated municipality geometry')
        polygons = geometry._geometry(feature, 'prefectures', reference)
        leaves[notice.code] = {'color': notice.color, 'parts': [(feature, polygons)]}
        full.extend(ring for polygon in polygons for ring in polygon)
        principal.extend(max(polygons, key=lambda polygon: abs(geometry._signed_area(polygon[0]))
                             - sum(abs(geometry._signed_area(hole)) for hole in polygon[1:])))
        x, y = geometry._position(feature['properties']['anchor'])
        anchors.append((x + round((reference - x) / 360) * 360, float(base._my(y))))
    points = np.asarray([*anchors, volcano_point])
    full.append(points)
    principal.append(points)
    # Compact inland municipalities (for example Aso) must not fill the
    # viewport with anonymous land. Keep enough surrounding coastal context
    # while still fitting every received municipality and the volcano.
    view = wide.map_axes(fig, principal, full=full, minimum=3.,
                         force_locator=True)
    _neutral_areas(view.ax, leaves)
    _numbered_markers(fig, view, anchors, plan.notices, prefix='municipality-anchor')
    view.ax.scatter(*volcano_point, marker='^', s=155, facecolor=wide.WHITE,
                    edgecolor=wide.SEA, linewidth=1.1, zorder=8,
                    gid=f'volcano-point-{plan.volcano_code}')
    wide.header(fig, reports[0], plan.title, '市町村別の予報 / ▲ 火山の代表位置')
    _ash_timeline(fig, plan)
    return plan
