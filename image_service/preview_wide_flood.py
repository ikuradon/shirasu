"""Unpublished flood presentation with a named single forecast reach.

The names, warning states and complete flow lines still come from the frozen
flood adapters. A map label is a reference to the forecast reach, not a gauge
location or a depiction of the extent of flooding.
"""

import re

from matplotlib.lines import Line2D
from matplotlib.transforms import Bbox

from image_service import flood_map
from image_service import preview_wide_layout as wide
from image_service.flood_reports import flood_plan


FLOOD_CREDIT = ('流路：国土数値情報を基に気象庁加工 / 県境：気象庁 / '
                '画像：Shirasu / 受信：みちびき / 地形：Natural Earth')


def _name_and_jurisdiction(title):
    """Move only a terminal parenthetical qualifier onto its own line.

    Upstream/downstream and other reach names remain intact. Names without a
    terminal qualifier are displayed in full rather than guessed from a river
    catalogue or shortened to a potentially different forecast interval.
    """
    match = re.fullmatch(r'(.+?)[(（]([^()（）]+)[)）]', title)
    return (match[1], match[2]) if match else (title, None)


def _single_sidebar(fig, notice):
    name, jurisdiction = _name_and_jurisdiction(notice.title)
    width = (wide.PANEL_RIGHT - wide.PANEL_LEFT) * wide.WIDTH - 12
    y = .929 * wide.HEIGHT

    def add_lines(value, size, *, color=wide.WHITE, bold=False, identity):
        nonlocal y
        for line in wide._wrap(fig, value, size, width, bold):
            label = wide.text(fig, wide.PANEL_LEFT, y / wide.HEIGHT, line, size,
                              color, bold=bold, va='top', gid=identity)
            label._wide_sidebar = True
            y -= size * wide.DPI / 72 * 1.3

    add_lines(name, 28, bold=True, identity=f'flood-name-{notice.code}')
    if jurisdiction:
        y -= 10
        add_lines(jurisdiction, 17, color=wide.MUTED,
                  identity=f'flood-jurisdiction-{notice.code}')
    y -= 46
    add_lines('受信した情報', 16, color=wide.MUTED,
              identity='wide-sidebar-flood-state')
    y -= 14
    style, = notice.styles
    value, = notice.lines
    size = 30
    while size > 18 and wide._width(fig, value, size, True) > width - 20:
        size -= 1
    if wide._width(fig, value, size, True) > width - 20:
        raise ValueError('Complete flood warning does not fit')
    label = wide.text(fig, wide.PANEL_LEFT + .005, y / wide.HEIGHT, value,
                      size, style.foreground, bold=True, va='top',
                      gid=f'flood-hazard-{notice.code}')
    label._wide_sidebar = True
    label.set_bbox(dict(facecolor=style.background, edgecolor=style.edge,
                        linewidth=1.5, boxstyle='square,pad=.20'))
    y -= size * wide.DPI / 72 * 1.4 + 32
    if notice.warning_level == 1:
        add_lines('解除は氾濫注意情報への切替を含む', 14, color=wide.MUTED,
                  identity='wide-sidebar-flood-release')
    if y < .055 * wide.HEIGHT:
        raise ValueError('Complete flood sidebar does not fit')


def _single_reference(fig, view, anchor, notice):
    """Place a complete river name beside, never over, the source flow lines."""
    name, _ = _name_and_jurisdiction(notice.title)
    fig.canvas.draw()
    source = fig.transFigure.inverted().transform(view.ax.transData.transform(anchor))
    paths = [line.get_transform().transform_path(line.get_path()) for line in view.ax.lines
             if (line.get_gid() or '').startswith('flood-reach-')]
    for size in (20, 18, 16):
        width = wide._width(fig, name, size, True) + 28
        height = size * wide.DPI / 72 * 1.3 + 20
        for gap in (25, 50, 90, 140, 200):
            offsets = ((width / 2 + gap, 0), (-width / 2 - gap, 0),
                       (0, height / 2 + gap), (0, -height / 2 - gap),
                       (width / 2 + gap, height / 2 + gap),
                       (-width / 2 - gap, height / 2 + gap),
                       (width / 2 + gap, -height / 2 - gap),
                       (-width / 2 - gap, -height / 2 - gap))
            for dx, dy in offsets:
                centre = (source[0] * wide.WIDTH + dx, source[1] * wide.HEIGHT + dy)
                box = Bbox.from_bounds(centre[0] - width / 2, centre[1] - height / 2,
                                       width, height)
                normalized = box.transformed(fig.transFigure.inverted())
                if not (.015 <= normalized.x0 and normalized.x1 < wide.PANEL_X - .01
                        and .055 <= normalized.y0 and normalized.y1 <= .78):
                    continue
                if any(normalized.overlaps(Bbox.from_extents(*obstacle))
                       for obstacle in view.obstacles):
                    continue
                if any(path.intersects_bbox(box.padded(6), filled=False) for path in paths):
                    continue
                break
            else:
                continue
            break
        else:
            continue
        break
    else:
        raise ValueError('Complete river name does not fit outside the forecast reach')
    x, y = centre[0] / wide.WIDTH, centre[1] / wide.HEIGHT
    view.ax.add_line(Line2D((source[0], x), (source[1], y), transform=fig.transFigure,
                           color=wide.MUTED, linewidth=1, zorder=8,
                           gid=f'flood-name-leader-{notice.code}'))
    view.ax.scatter(*anchor, s=27, facecolor=notice.color, edgecolor=flood_map.CASING,
                    linewidth=1, zorder=9, gid=f'flood-anchor-{notice.code}')
    wide.text(fig, x, y, name, size, bold=True, ha='center', va='center',
              gid=f'flood-map-name-{notice.code}',
              bbox={'boxstyle': 'square,pad=.22', 'facecolor': wide.SEA,
                    'edgecolor': wide.COAST, 'linewidth': .8})


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
    # Reject incomplete or contradictory geography before painting any part.
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
    wide.header(fig, reports[0], plan.title, '線は洪水予報の対象区間 / 浸水範囲ではありません',
                credit=FLOOD_CREDIT)
    if len(plan.notices) == 1:
        _single_reference(fig, view, anchors[0], plan.notices[0])
        _single_sidebar(fig, plan.notices[0])
    else:
        # The established numbered layout remains useful when distinct named
        # forecast intervals share a river or have neighbouring source anchors.
        from image_service.preview_wide_special import _numbered_markers
        _numbered_markers(fig, view, anchors, plan.notices, prefix='flood-anchor')
        rows = [wide.Row(f'{index} {notice.title}', notice.lines, styles=notice.styles,
                         gid=f'flood-hazard-{notice.code}')
                for index, notice in enumerate(plan.notices, 1)]
        wide.sidebar(fig, '河川の予報区間', rows,
                     footnote='解除は氾濫注意情報への切替を含む'
                     if any(notice.warning_level == 1 for notice in plan.notices) else None)
    return plan
