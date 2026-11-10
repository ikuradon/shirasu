"""Shared, unpublished 16:9 presentation; published renderer state is untouched.

The right panel holds received values. The locator belongs to the map, with a
reserved box that cannot cover the geometry used to fit the main viewport.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import PathPatch, Rectangle
from matplotlib.path import Path

from image_service import render as base
from image_service import map_base_v3 as terrain
from image_service import preview_context as context
from image_service import preview_scale as scale
from image_service import region_map as geometry

WIDTH, HEIGHT, DPI = 1920, 1080, 120
SEA, LAND, COAST = '#191c21', '#363c42', '#737d86'
WHITE, MUTED, ALERT, ACCENT = base.WHITE, base.MUTED, base.ALERT, base.ACCENT
PANEL_X, PANEL_LEFT, PANEL_RIGHT = .755, .778, .978
LOCATOR_POSITION = (.565, .07, .17, .23)
LOCATOR_OBSTACLE = (.555, .06, .745, .34)
BODY = (.04, .11, .71, .76)
LOCATOR_BODY = (.04, .11, .55, .76)
OBSTACLES = ((0, .78, PANEL_X, 1), (0, 0, PANEL_X, .055), (.745, 0, 1, 1))
CREDIT = '情報・区域：気象庁（加工） / 受信：みちびき / 地形：Natural Earth'


@dataclass(frozen=True)
class Row:
    title: str
    lines: tuple[str, ...] = ()
    color: str = WHITE
    styles: tuple = ()
    gid: str | None = None


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    unit: str = ''
    color: str = WHITE


@dataclass(frozen=True)
class MapView:
    ax: object
    locator: object | None
    limits: tuple
    visible_limits: tuple
    obstacles: tuple
    places: tuple = ()


def figure():
    fig = Figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI, facecolor=SEA)
    FigureCanvasAgg(fig)
    return fig


def text(fig, x, y, value, size=18, color=WHITE, bold=False, ha='left',
         gid='wide-text', **kwargs):
    kwargs.setdefault('zorder', 30)
    result = fig.text(x, y, str(value), fontproperties=base._font(size, bold),
                      color=color, ha=ha, parse_math=False, **kwargs)
    result.set_gid(gid)
    return result


def _rect(fig, x, y, width, height, color, *, alpha=1., zorder=20):
    patch = Rectangle((x, y), width, height, transform=fig.transFigure,
                      facecolor=color, edgecolor='none', alpha=alpha, zorder=zorder)
    fig.add_artist(patch)
    return patch


def panel(fig):
    _rect(fig, PANEL_X, 0, 1 - PANEL_X, 1, '#090c10', alpha=.90)
    _rect(fig, PANEL_X, 0, .0012, 1, '#8d969f', alpha=.25)
    text(fig, PANEL_RIGHT, .976, 'SHIRASU', 11, MUTED, ha='right', gid='wide-header')


def _width(fig, value, size, bold=False):
    return fig.canvas.get_renderer().get_text_width_height_descent(
        str(value), base._font(size, bold), False)[0]


def _wrap(fig, value, size, width, bold=False):
    """Wrap measured glyphs, never silently abbreviate a received value."""
    value = str(value)
    if len(value) > 20000:
        raise ValueError('Unbounded presentation text')
    output = []
    renderer, font = fig.canvas.get_renderer(), base._font(size, bold)
    widths = {}

    def measure(line):
        if line not in widths:
            widths[line] = renderer.get_text_width_height_descent(line, font, False)[0]
        return widths[line]

    for paragraph in value.splitlines():
        current = ''
        for char in paragraph:
            if not char.isprintable():
                continue
            if current and measure(current + char) > width:
                # Keep a word together where a whitespace boundary exists.
                # Japanese closing punctuation stays with a preceding glyph.
                split = current.rfind(' ') if char.isascii() and char.isalnum() else -1
                if split > 0 and current[split + 1:].isascii():
                    output.append(current[:split])
                    current = current[split + 1:]
                elif char in '、。，．：；！？)]）］｝」』】〉》':
                    output.append(current[:-1])
                    current = current[-1:]
                elif current[-1] in '([（［｛「『【〈《':
                    output.append(current[:-1])
                    current = current[-1:]
                else:
                    output.append(current)
                    current = ''
            current += char
        if current:
            output.append(current)
    return output


def header(fig, report, title, scope, *, utc=False, credit=None, markers=()):
    panel(fig)
    marker, _, _, _ = base._metadata(report)
    if getattr(report, 'information_type_no', None) == 1:
        marker += '【訂正】'
    for extra in markers:
        token = extra if extra.startswith('【') else '【' + extra + '】'
        if token not in marker:
            marker += token
    if marker:
        text(fig, .026, .957, marker, 17, ALERT, bold=True, gid='wide-header')
    for size in (35, 32, 29, 26, 23):
        if _width(fig, title, size, True) <= WIDTH * .69:
            break
    else:
        raise ValueError('Wide title does not fit')
    text(fig, .024, .887, title, size, bold=True, gid='wide-header')
    is_report = hasattr(report, 'report_time')
    at = report.report_time if is_report else report.timestamp
    zone = timezone.utc if utc else base.JST
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError('Missing presentation timestamp')
    stamp = at.astimezone(zone).strftime('%Y/%m/%d %H:%M')
    text(fig, .026, .841, stamp + (' 発表 / ' if is_report else ' 受信 / ')
         + ('UTC' if utc else 'JST'), 15, gid='wide-header')
    if _width(fig, scope, 13) > WIDTH * .69:
        raise ValueError('Wide scope does not fit')
    text(fig, .026, .800, scope, 13, MUTED, gid='wide-header')
    text(fig, .022, .022, credit or CREDIT, 10, MUTED, gid='wide-header')


def sidebar(fig, heading, rows=(), *, intro=(), metrics=(), footnote=None):
    """Fit every row before drawing. Oversized input falls back as one bundle."""
    rows, metrics = tuple(rows), tuple(metrics)
    intro = (intro,) if isinstance(intro, str) else tuple(intro)
    if len(rows) > 70 or len(metrics) > 10:
        raise ValueError('Too many sidebar entries')
    width = (PANEL_RIGHT - PANEL_LEFT) * WIDTH - 10
    heading_lines = _wrap(fig, heading, 21, width, True)
    if len(heading_lines) > 2:
        raise ValueError('Sidebar heading does not fit')
    top = .929 * HEIGHT
    heading_height = len(heading_lines) * 40
    available = top - heading_height - .055 * HEIGHT
    blocks = None
    for size in (18, 17, 16, 15, 14):
        candidate = []
        step = size * DPI / 72 * 1.24
        for value in intro:
            for line in _wrap(fig, value, size - 1, width):
                candidate.append(('line', line, size - 1, MUTED, False, None, step, None))
        if intro:
            candidate.append(('gap', '', size, WHITE, False, None, 12, None))
        for metric in metrics:
            for line in _wrap(fig, metric.label, size, width - 22):
                candidate.append(('line', line, size, MUTED, False, None, step, None))
            value = str(metric.value) + (' ' + str(metric.unit) if metric.unit else '')
            value_size = 38
            while value_size > 20 and _width(fig, value, value_size, True) > width - 18:
                value_size -= 1
            if _width(fig, value, value_size, True) > width - 18:
                raise ValueError('Metric does not fit')
            candidate.append(('metric', value, value_size, metric.color, True, None,
                              value_size * DPI / 72 * 1.2 + 18, None))
        for row in rows:
            if row.styles and len(row.styles) != len(row.lines):
                raise ValueError('Sidebar hazard styles must match all values')
            for line in _wrap(fig, row.title, size + 1, width - 12, True):
                candidate.append(('title', line, size + 1, row.color, True, None,
                                  step + 6, row.gid))
            for index, value in enumerate(row.lines):
                style = row.styles[index] if row.styles else None
                for line in _wrap(fig, value, size, width - 16):
                    candidate.append(('line', line, size,
                                      style.foreground if style else WHITE, False, style,
                                      step + (8 if style else 0), row.gid))
            candidate.append(('gap', '', size, WHITE, False, None, 10, None))
            if sum(block[6] for block in candidate) > available:
                break
        if footnote:
            candidate.append(('gap', '', size, WHITE, False, None, 8, None))
            for line in _wrap(fig, footnote, 13, width):
                candidate.append(('line', line, 13, MUTED, False, None, 27, None))
        if sum(block[6] for block in candidate) <= available:
            blocks = candidate
            break
    if blocks is None:
        raise ValueError('Complete sidebar does not fit')
    y = top
    for line in heading_lines:
        text(fig, PANEL_LEFT, y / HEIGHT, line, 21, bold=True,
             va='top', gid='wide-sidebar-heading')
        y -= 40
    for kind, value, size, color, bold, style, height, identity in blocks:
        if kind == 'gap':
            y -= height
            continue
        if kind == 'title':
            _rect(fig, PANEL_LEFT - .004, (y - height + 5) / HEIGHT,
                  PANEL_RIGHT - PANEL_LEFT + .004, height / HEIGHT,
                  '#9ba4ac', alpha=.10, zorder=22)
        if kind == 'metric':
            _rect(fig, PANEL_LEFT, (y - height + 8) / HEIGHT, .0035,
                  (height - 10) / HEIGHT, '#ff913e', zorder=22)
        label = text(fig, PANEL_RIGHT if kind == 'metric' else PANEL_LEFT + .005,
                     y / HEIGHT, value, size, color, bold=bold,
                     ha='right' if kind == 'metric' else 'left', va='top',
                     gid=identity or ('wide-hazard' if style else 'wide-sidebar'))
        label._wide_sidebar = True
        if style:
            label.set_bbox(dict(facecolor=style.background, edgecolor=style.edge,
                                linewidth=1.1, boxstyle='square,pad=.13'))
        y -= height


def _limits(sequences, rectangle, *, minimum=4., position=(0, 0, 1, 1)):
    points = np.concatenate(tuple(sequences))
    if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all():
        raise ValueError('Invalid wide map coordinates')
    low, high = points.min(axis=0), points.max(axis=0)
    left, bottom, right, top = rectangle
    aspect = position[2] * WIDTH / (position[3] * HEIGHT)
    width = max(minimum, float(high[0] - low[0]) / (right - left),
                float(high[1] - low[1]) / (top - bottom) * aspect)
    height = width / aspect
    centre = (low + high) / 2
    x, y = (left + right) / 2, (bottom + top) / 2
    return (centre[0] - width * x, centre[0] + width * (1 - x),
            centre[1] - height * y, centre[1] + height * (1 - y))


def _axes(fig, limits, *, position=(0, 0, 1, 1), graticule=True):
    ax = fig.add_axes(position, facecolor=SEA)
    ax.set_xlim(*limits[:2])
    ax.set_ylim(*limits[2:])
    ax.set_aspect('equal')
    centre, paths = (limits[0] + limits[1]) / 2, []
    for rings, (left, right, bottom, top) in terrain._land():
        if top < limits[2] or bottom > limits[3]:
            continue
        shift = round((centre - (left + right) / 2) / 360) * 360
        for delta in (shift - 360, shift, shift + 360):
            if right + delta >= limits[0] and left + delta <= limits[1]:
                paths.append(geometry._polygon_path([ring + np.array((delta, 0)) for ring in rings]))
    if paths:
        ax.add_patch(PathPatch(Path.make_compound_path(*paths), facecolor=LAND,
                               edgecolor=COAST, linewidth=.55, zorder=2, gid='v3-land'))
    if graticule:
        width = limits[1] - limits[0]
        spacing = 2 if width < 15 else 5 if width < 40 else 10
        for lon in np.arange(math.floor(limits[0] / spacing) * spacing, limits[1], spacing):
            ax.axvline(lon, color='#2a3743', linewidth=.5, zorder=1)
        for lat in range(-80, 85, spacing):
            value = base._my(lat)
            if limits[2] <= value <= limits[3]:
                ax.axhline(value, color='#2a3743', linewidth=.5, zorder=1)
    ax.set_axis_off()
    return ax


def map_axes(fig, sequences, *, full=(), minimum=4., force_locator=False, locator_context=()):
    sequences, full = tuple(sequences), tuple(full)
    surroundings = scale.context_for(sequences, minimum=minimum)
    fitted = (*sequences, *surroundings.sequences)
    limits = _limits(fitted, BODY, minimum=surroundings.minimum)
    def visible(bounds):
        return (bounds[0], bounds[0] + PANEL_X * (bounds[1] - bounds[0]), *bounds[2:])
    show = force_locator or context.is_zoomed(visible(limits))
    if show:
        limits = _limits(fitted, LOCATOR_BODY, minimum=surroundings.minimum)
    ax = _axes(fig, limits)
    context.draw_prefecture_boundaries(ax)
    viewport = visible(limits)
    inset = None
    if show:
        shift = round(((viewport[0] + viewport[1]) / 2 - 135) / 360) * 360
        locator_land = tuple(locator_context) or (
            np.array(((122 + shift, float(base._my(23))), (148 + shift, float(base._my(47))))),)
        extent = np.array(((viewport[0], viewport[2]), (viewport[1], viewport[3])))
        inset_limits = _limits((*locator_land, extent, *full), (.05, .05, .95, .95),
                               minimum=.1, position=LOCATOR_POSITION)
        # The locator is a plain base map. Forecast geometry, administrative
        # borders, tracks and reference markers belong only on the main map.
        inset = _axes(fig, inset_limits, position=LOCATOR_POSITION, graticule=False)
        inset.set_gid('preview-locator')
        inset.set_zorder(10)
        inset.add_patch(Rectangle((0, 0), 1, 1, transform=inset.transAxes,
                                  facecolor=SEA, edgecolor='#536777', linewidth=.7, zorder=0))
        inset.add_patch(Rectangle((viewport[0], viewport[2]), viewport[1] - viewport[0],
                                  viewport[3] - viewport[2], fill=False, edgecolor=WHITE,
                                  linewidth=1.1, zorder=8, gid='preview-main-extent'))
        text(fig, LOCATOR_POSITION[0], .315, '表示位置', 12, MUTED, gid='wide-locator')
        text(fig, LOCATOR_POSITION[0] + LOCATOR_POSITION[2], .315,
             '枠は主図範囲', 10, MUTED, ha='right', gid='wide-locator')
    view = MapView(ax, inset, limits, viewport,
                   OBSTACLES + ((LOCATOR_OBSTACLE,) if show else ()), surroundings.places)
    ax._wide_map_view = view
    return view


def context_names(fig):
    """Place quiet geographic names only after every forecast artist exists."""
    from image_service.preview_place_labels import draw_place_labels
    for ax in fig.axes:
        view = getattr(ax, '_wide_map_view', None)
        if view is not None:
            draw_place_labels(fig, view, view.places)


def summary(fig, reports):
    first = reports[0]
    _, details, _, cancelled = base._metadata(first)
    header(fig, first, base._title(first), '受信した通報の要旨',
           credit='情報：みちびき / Shirasu' if not hasattr(first, 'report_time') else None)
    sidebar(fig, '通報情報', (Row('配信区分', (details,)),),
            footnote='全文は投稿本文を参照')
    sources = reports[:1] if isinstance(first, base.dcr.NankaiTroughEarthquake) else reports
    body = ('この通報は取り消されました。\n対象：' + base._title(first) if cancelled
            else '\n\n'.join(str(report) for report in sources))
    lines = _wrap(fig, body, 18, WIDTH * .68)
    for index, line in enumerate(lines[:19]):
        text(fig, .04, .745 - index * .034, line, 18, va='top', gid='wide-summary')
    if len(lines) > 19:
        text(fig, .72, .06, '抜粋 / 全文は投稿本文を参照', 13, ALERT,
             ha='right', gid='wide-summary')


def validate(fig):
    """Check shared chrome and panel text after the complete renderer ran."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    labels = [label for label in fig.texts if getattr(label, '_wide_sidebar', False)
              or (label.get_gid() or '').startswith(
                  ('wide-header', 'wide-sidebar', 'wide-hazard', 'wide-locator'))]
    for index, label in enumerate(labels):
        box = (label.get_bbox_patch().get_window_extent(renderer) if label.get_bbox_patch()
               else label.get_window_extent(renderer))
        if box.x0 < 0 or box.x1 > WIDTH or box.y0 < 0 or box.y1 > HEIGHT:
            raise ValueError('Presentation text leaves the image')
        for other in labels[index + 1:]:
            other_box = (other.get_bbox_patch().get_window_extent(renderer) if other.get_bbox_patch()
                         else other.get_window_extent(renderer))
            if box.overlaps(other_box):
                raise ValueError('Presentation text overlaps')
