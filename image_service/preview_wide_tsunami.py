"""Unpublished tsunami maps with received values beside their source anchors.

The domestic coastal forecast areas and international forecast points retain
their different meanings. No height, arrival state, warning class or geography
is inferred by this presentation layer.
"""

import re

from matplotlib.lines import Line2D
import numpy as np

from image_service import nwp_map
from image_service import preview_geometry_cache as cached_geometry
from image_service import preview_wide_layout as wide
from image_service import render as base
from image_service.nwp_reports import nwp_plan
from image_service.regional_reports import tsunami_map


NWP_POTENTIALS = {
    1: '破壊的津波の可能性：海洋全域',
    2: '破壊的津波の可能性：地域規模',
    3: '破壊的津波の可能性：震源付近（局地的）',
    4: '局地的な破壊的津波の可能性はごく小さい',
}


def _heights(notice):
    """Extract every distinct displayed value, including nonnumeric states."""
    heights = []
    if not notice.lines or len(notice.lines) % 2:
        raise ValueError('Tsunami heights must retain their paired arrival states')
    for line in notice.lines[::2]:
        match = re.fullmatch(r'(?:受信値 \d+：)?予想高 (.+)', line)
        if match is None:
            raise ValueError('Unrecognised tsunami height line')
        if match[1] not in heights:
            heights.append(match[1])
    return tuple(heights)


def _value_markers(fig, view, points, notices, labels, *, prefix):
    """Keep anchors fixed and place measured value labels without collisions.

    Leaders terminate at the label edge and cannot cross another label, leader
    or the plain locator. An unreadable complete set is rejected, never pruned.
    """
    points, notices, labels = tuple(points), tuple(notices), tuple(labels)
    if not points or len(points) != len(notices) or len(points) != len(labels):
        raise ValueError('Tsunami values and source anchors differ')
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    sources = tuple(tuple(fig.transFigure.inverted().transform(view.ax.transData.transform(point)))
                    for point in points)
    marker_half_x, marker_half_y = 7 / wide.WIDTH, 7 / wide.HEIGHT
    marker_boxes = tuple((x - marker_half_x, y - marker_half_y,
                          x + marker_half_x, y + marker_half_y) for x, y in sources)
    occupied, leaders, planned = [], [], []
    for point, source, notice, value in zip(points, sources, notices, labels, strict=True):
        size = 23 if len(value) <= 8 else 19
        probe = wide.text(fig, .5, .5, value, size, bold=True, ha='center', va='center')
        extent = probe.get_window_extent(renderer)
        probe.remove()
        padding = size * wide.DPI / 72 * .24 + 4
        half_x, half_y = ((extent.width / 2 + padding) / wide.WIDTH,
                          (extent.height / 2 + padding) / wide.HEIGHT)
        if half_x * 2 > .23 or half_y * 2 > .24:
            raise ValueError('Tsunami value label is too large')
        gaps = (14, 34, 64, 104, 154, 224)
        offsets = [(dx, dy) for gap in gaps for dx, dy in (
            (0, half_y + gap / wide.HEIGHT),
            (half_x + gap / wide.WIDTH, 0),
            (-half_x - gap / wide.WIDTH, 0),
            (0, -half_y - gap / wide.HEIGHT),
            (half_x + gap / wide.WIDTH, half_y + gap / wide.HEIGHT),
            (-half_x - gap / wide.WIDTH, half_y + gap / wide.HEIGHT),
            (half_x + gap / wide.WIDTH, -half_y - gap / wide.HEIGHT),
            (-half_x - gap / wide.WIDTH, -half_y - gap / wide.HEIGHT))]
        for dx, dy in offsets:
            x, y = source[0] + dx, source[1] + dy
            box = (x - half_x, y - half_y, x + half_x, y + half_y)
            if not (.012 <= box[0] and box[2] < wide.PANEL_X - .012
                    and .06 <= box[1] and box[3] <= .78):
                continue
            if any(base._overlap(box, other) > 0 for other in (*view.obstacles, *occupied)):
                continue
            if any(base._overlap(box, other) > 0 for other in marker_boxes):
                continue
            end = (min(max(source[0], box[0]), box[2]), min(max(source[1], box[1]), box[3]))
            distance = np.hypot((end[0] - source[0]) * wide.WIDTH,
                                (end[1] - source[1]) * wide.HEIGHT)
            start = tuple(value + (target - value) * 6 / distance
                          for value, target in zip(source, end, strict=True))
            segment = (start, end)
            if any(base._segment_hits_box(segment, other) for other in (*view.obstacles, *occupied)):
                continue
            # Include anchors whose labels have not been placed yet: a leader
            # through one of their dots would attach this height to that point.
            # Coincident anchors share a dot and cannot obstruct one another.
            if any(other_source != source and base._segment_hits_box(segment, other_box)
                   for other_source, other_box in zip(sources, marker_boxes, strict=True)):
                continue
            if any(base._segment_hits_box(old, box) or
                   base._segments_touch(old, segment) for old in leaders):
                continue
            break
        else:
            raise ValueError('Complete tsunami values do not fit outside the locator')
        occupied.append(box)
        leaders.append(segment)
        planned.append((point, notice, value, size, x, y, segment))
    # A failed layout adds no partial collection of forecast markers.
    for point, notice, value, size, x, y, segment in planned:
        view.ax.scatter(*point, s=36, facecolor=notice.color, edgecolor=wide.WHITE,
                        linewidth=.9, zorder=8, gid=f'{prefix}-{notice.code}')
        view.ax.add_line(Line2D((segment[0][0], segment[1][0]),
                               (segment[0][1], segment[1][1]), transform=fig.transFigure,
                               color=notice.color, linewidth=1.1, zorder=8,
                               gid=f'wide-tsunami-leader-{prefix}-{notice.code}'))
        wide.text(fig, x, y, value, size, wide.WHITE, bold=True, ha='center', va='center',
                  gid=f'wide-tsunami-value-{prefix}-{notice.code}',
                  bbox={'boxstyle': 'round,pad=.24,rounding_size=.08',
                        'facecolor': wide.SEA, 'edgecolor': notice.color, 'linewidth': 1.6})
    return tuple(occupied)


def _domestic_rows(plan):
    """Only identical complete height/arrival pairs share a sidebar block."""
    groups = {}
    for notice in plan.notices:
        groups.setdefault((notice.lines, notice.color), []).append(notice)
    rows = []
    for (lines, color), notices in groups.items():
        names = tuple(notice.title for notice in notices)
        if len(lines) == 2:
            rows.append(wide.Row(lines[0], (*names, lines[1]), color=color,
                                 gid=f'tsunami-group-{notices[0].code}'))
        else:
            # Distinct values for one region keep their numbered pairs; do not
            # imply that the maximum height arrived at the earliest time.
            rows.append(wide.Row('・'.join(names), lines, color=color,
                                 gid=f'tsunami-group-{notices[0].code}'))
    return tuple(rows)


def draw_tsunami(fig, reports):
    # Import locally so the regional module can expose this drawer as a wrapper.
    from image_service import preview_wide_regions as regions

    reports = tuple(reports)
    plan = tsunami_map(reports)
    if not 1 <= len(plan.notices) <= 16:
        raise ValueError('Unsupported number of tsunami forecast regions')
    warning = reports[0].tsunami_warning_code_raw
    view, painted, leaves, anchors = regions._prepare(fig, plan)
    cached_geometry.paint_regions(view.ax, leaves, painted)
    labels = [('解除' if warning == 2 else '津波なし') if warning in (1, 2)
              else '\n'.join(_heights(notice)) for notice in plan.notices]
    _value_markers(fig, view, anchors, plan.notices, labels, prefix='tsunami-region')
    wide.header(fig, reports[0], '津波 警報解除' if warning == 2 else plan.title,
                '津波予報区・浸水範囲ではありません', markers=('解除',) if warning == 2 else ())
    heading = '警報解除' if warning == 2 else '津波なし' if warning == 1 else '予想高と到達予想'
    intro = ('解除通報に含まれる受信値',) if warning == 2 else (
        ('「津波なし」通報に含まれる受信値',) if warning == 1 else ())
    wide.sidebar(fig, heading, _domestic_rows(plan), intro=intro)
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
    _value_markers(fig, view, positions, plan.notices,
                   ['\n'.join(_heights(notice)) for notice in plan.notices], prefix='nwp-point')
    wide.header(fig, reports[0], plan.title, '予測地点ごとの津波高・到達予想 / UTC', utc=True)
    rows = [wide.Row(notice.title, notice.lines, gid=f'nwp-forecast-{notice.code}')
            for notice in plan.notices]
    potential = NWP_POTENTIALS[reports[0].tsunamigenic_potential_raw]
    # Keep the short geographic qualifier whole instead of stranding「域」
    # on its own line. This changes only the break, not the received meaning.
    potential = (potential.replace('：', '：\n', 1) if '：' in potential
                 else potential.replace('可能性は', '\n可能性は', 1))
    wide.sidebar(fig, '予測地点 / UTC', rows, intro=(potential,))
    return plan
