"""Wide offline maps for river reaches, ash forecasts and NWPTAC points.

The semantic adapters and pinned geographic catalogues remain unchanged. Map
numbers identify the corresponding sidebar rows, not observation stations or
newly inferred hazard footprints. All distinct received forecasts are retained.
"""

import re

import numpy as np
from matplotlib.lines import Line2D

from image_service import ash_map, flood_map, nwp_map, volcano_map
from image_service import preview_wide_layout as wide
from image_service import region_map as geometry
from image_service import render as base
from image_service.ash_reports import ash_plan
from image_service.flood_reports import flood_plan
from image_service.nwp_reports import nwp_plan

FLOOD_CREDIT = ('流路：国土数値情報を基に気象庁加工 / 県境：気象庁 / '
                '画像：Shirasu / 受信：みちびき / 地形：Natural Earth')
NWP_POTENTIALS = {
    1: '破壊的津波の可能性：海洋全域',
    2: '破壊的津波の可能性：地域規模',
    3: '破壊的津波の可能性：震源付近（局地的）',
    4: '局地的な破壊的津波の可能性はごく小さい',
}


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


def _ash_sidebar_lines(notice):
    """Compact repeated phenomena without inventing a continuous time range."""
    grouped = {}
    for line, style in zip(notice.lines, notice.styles, strict=True):
        match = re.fullmatch(r'基点＋([1-6])時間：(.+)', line)
        if match is None:
            raise ValueError('Unknown ash forecast line format')
        hour, phenomenon = int(match[1]), match[2]
        grouped.setdefault((phenomenon, style), set()).add(hour)
    lines, styles = [], []
    for (phenomenon, style), hours in grouped.items():
        hours_text = '・'.join(str(hour) for hour in sorted(hours))
        lines.append(f'基点＋{hours_text}時間：{phenomenon}')
        styles.append(style)
    return tuple(lines), tuple(styles)


def draw_flood(fig, reports):
    reports = tuple(reports)
    plan = flood_plan(reports)
    if not 1 <= len(plan.notices) <= 16:
        raise ValueError('Unsupported number of flood forecast reaches')
    catalogue = flood_map._rivers()
    reaches, sequences, anchors, seen, segment_levels = [], [], [], set(), {}
    for notice in plan.notices:
        feature = catalogue.get(notice.code)
        if feature is None or notice.code in seen:
            raise ValueError('Missing or duplicated flood forecast geometry')
        seen.add(notice.code)
        parts = flood_map._parts(feature)
        flood_map._check_shared_segments(feature, notice.warning_level, segment_levels)
        reaches.append(parts)
        sequences.extend(parts)
        anchors.append(flood_map._anchor(parts))
    # Validate the complete bundle, including shared source segments, before
    # adding any artists. A conflicting reach must not become a partial map.
    view = wide.map_axes(fig, sequences, full=sequences, minimum=.6)
    for notice, parts in zip(plan.notices, reaches, strict=True):
        def dash(width):
            return (0, (12 / width, 8 / width)) if notice.warning_level == 1 else '-'
        for index, part in enumerate(parts):
            identity = f'{notice.code}-{index}'
            for kind, color, width, zorder in (
                    ('shadow', wide.SEA, 7, 4), ('casing', flood_map.CASING, 5, 5),
                    ('reach', notice.color, 3.2, 6)):
                view.ax.plot(*part.T, color=color, linewidth=width, linestyle=dash(width),
                             solid_capstyle='round', zorder=zorder,
                             gid=f'flood-{kind}-{identity}')
    _numbered_markers(fig, view, anchors, plan.notices, prefix='flood-anchor')
    wide.header(fig, reports[0], plan.title, '線は洪水予報の対象区間 / 浸水範囲ではありません',
                credit=FLOOD_CREDIT)
    rows = [wide.Row(f'{index} {notice.title}', notice.lines, styles=notice.styles,
                     gid=f'flood-hazard-{notice.code}')
            for index, notice in enumerate(plan.notices, 1)]
    wide.sidebar(fig, '河川の予報区間', rows,
                 footnote='解除は氾濫注意情報への切替を含む'
                 if any(notice.warning_level == 1 for notice in plan.notices) else None)
    return plan


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
    rows = []
    for index, notice in enumerate(plan.notices, 1):
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
        lines, styles = _ash_sidebar_lines(notice)
        rows.append(wide.Row(f'{index} {notice.title}', lines, styles=styles,
                             gid=f'ash-hazard-{notice.code}'))
    points = np.asarray([*anchors, volcano_point])
    full.append(points)
    principal.append(points)
    view = wide.map_axes(fig, principal, full=full, minimum=1.2,
                         force_locator=True)
    _neutral_areas(view.ax, leaves)
    _numbered_markers(fig, view, anchors, plan.notices, prefix='municipality-anchor')
    view.ax.scatter(*volcano_point, marker='^', s=155, facecolor=wide.WHITE,
                    edgecolor=wide.SEA, linewidth=1.1, zorder=8,
                    gid=f'volcano-point-{plan.volcano_code}')
    wide.header(fig, reports[0], plan.title, '市町村別の予報 / ▲ 火山の代表位置')
    wide.sidebar(fig, plan.volcano_name, rows,
                 intro=(f'基点 {base._time(plan.base_time, full=True)} JST',))
    return plan


def draw_nwp(fig, reports):
    reports = tuple(reports)
    plan = nwp_plan(reports)
    if not 1 <= len(plan.notices) <= 16:
        raise ValueError('Unsupported number of NWPTAC forecast points')
    catalogue, positions, seen = nwp_map._positions(), [], set()
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
    sequences = (np.asarray(positions),)
    view = wide.map_axes(fig, sequences, full=sequences, minimum=14.)
    _numbered_markers(fig, view, positions, plan.notices, prefix='nwp-point')
    wide.header(fig, reports[0], plan.title, '予測地点ごとの津波高・到達予想 / UTC', utc=True)
    rows = [wide.Row(f'{index} {notice.title}', notice.lines,
                     gid=f'nwp-forecast-{notice.code}')
            for index, notice in enumerate(plan.notices, 1)]
    wide.sidebar(fig, '予測地点 / UTC', rows,
                 intro=(NWP_POTENTIALS[reports[0].tsunamigenic_potential_raw],))
    return plan
