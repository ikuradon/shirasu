"""Frozen v1 image renderer. Changing visual/decoder/assets requires a new version.

No current clock, remote resource, user-supplied font or output file is used.
The URL's receipt timestamps are the decoder's sole date context.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import unicodedata

# Matplotlib may cache fonts in temporary storage; rendered PNGs remain in memory.
os.environ.setdefault('MPLCONFIGDIR', '/tmp/shirasu-matplotlib-v1')

import azarashi
from azarashi.reports import dcr, dcx
from azarashi.definitions.qzss.dcr.latitude_and_longitude import is_position
import matplotlib
matplotlib.use('Agg')
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.offsetbox import AnnotationBbox, TextArea, VPacker
from matplotlib.lines import Line2D
from matplotlib.patches import Polygon
import numpy as np

ASSETS = Path(__file__).parent / 'assets' / 'v1'
JST = timezone(timedelta(hours=9))
WIDTH, HEIGHT, DPI = 1440, 1200, 120
SEA, LAND, COAST = '#10171e', '#29343e', '#677684'
WHITE, MUTED, ACCENT = '#f2f6fa', '#a3b3c2', '#6ce2ec'
ALERT = '#ffce6c'
_LOCK = threading.RLock()
_ASSET_NAMES = ('land.geojson', 'NotoSansCJKjp-Regular.otf', 'NotoSansCJKjp-Bold.otf')


@lru_cache(maxsize=1)
def verify_assets():
    manifest = json.loads((ASSETS / 'manifest.json').read_text())
    for name in _ASSET_NAMES:
        actual = hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()
        if actual != manifest[name]['sha256']:
            raise RuntimeError('Renderer assets do not match the frozen version')
    return True


@lru_cache(maxsize=1)
def _land():
    verify_assets()
    polygons = []
    for feature in json.loads((ASSETS / 'land.geojson').read_text())['features']:
        geometry = feature['geometry']
        groups = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
        for polygon in groups:
            if polygon:
                data = np.asarray(polygon[0], dtype=float)
                # Keep dateline-crossing rings continuous before shifting them
                # into the view centred on the report's actual coordinates.
                longitudes = np.degrees(np.unwrap(np.radians(data[:, 0])))
                polygons.append(np.column_stack((longitudes, _my(data[:, 1]))))
    return polygons


def _font(size=18, bold=False):
    name = 'NotoSansCJKjp-Bold.otf' if bold else 'NotoSansCJKjp-Regular.otf'
    return FontProperties(fname=str(ASSETS / name), size=size)


def _text(fig, x, y, text, size=18, color=WHITE, bold=False, **kwargs):
    return fig.text(x, y, text, fontproperties=_font(size, bold), color=color,
                    parse_math=False, **kwargs)


def _my(latitude):
    return np.degrees(np.log(np.tan(np.pi / 4 + np.radians(np.clip(latitude, -85, 85)) / 2)))


def _coordinates(raw):
    if not isinstance(raw, dict) or not is_position(raw):
        return None
    lat = (raw['lat_d'] + raw['lat_m'] / 60 + raw['lat_s'] / 3600) * (-1 if raw['lat_ns'] else 1)
    lon = (raw['lon_d'] + raw['lon_m'] / 60 + raw['lon_s'] / 3600) * (-1 if raw['lon_ew'] else 1)
    # Mercator has no representation at the poles: retain them in text instead.
    return (lon, lat) if abs(lat) < 85 else None


def _time(value, *, full=False):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return '時刻不明'
    local = value.astimezone(JST)
    return local.strftime('%Y/%m/%d %H:%M') if full else f'{local.month}/{local.day} {local:%H:%M}'


def _reset_assembly():
    # azarashi keeps multipart Nankai text on its class. Never mix separate URLs.
    dcr.NankaiTroughEarthquake.completed = False
    dcr.NankaiTroughEarthquake.reports = {}
    dcr.NankaiTroughEarthquake.announcement = None


def _metadata(report):
    if isinstance(report, dcr.Base):
        training = report.report_classification_no == 7
        cancelled = report.information_type_no == 2
        markers = ('【訓練・試験】' if training else '') + ('【取消】' if cancelled else '')
        details = f'{report.information_type} / {report.report_classification}'
        return markers, details, training, cancelled
    training = report.camf.a1 == 0
    # CAMF's All Clear is a解除, not a cancellation of an erroneous message.
    marker = '【訓練・試験】' if training else '【解除】' if report.camf.a1 == 3 else ''
    return marker, report.a1_message_type, training, False


def _decode(records):
    if not 1 <= len(records) <= 32:
        raise ValueError('Invalid report count')
    reports = []
    for nmea, milliseconds in records:
        if not isinstance(milliseconds, int) or isinstance(milliseconds, bool):
            raise ValueError('Invalid reception time')
        report = azarashi.decode(nmea, timestamp=datetime.fromtimestamp(milliseconds / 1000, timezone.utc))
        if isinstance(report, (dcx.NullMsg, dcx.Unknown)):
            raise ValueError('No renderable report')
        if not isinstance(report, (dcr.Base, dcx.AlertBase)):
            raise ValueError('Unsupported report')
        reports.append(report)
    first = reports[0]
    if any(type(report) is not type(first) for report in reports):
        raise ValueError('Mixed report categories')
    if isinstance(first, dcr.Base):
        key = lambda report: (report.report_time, report.report_classification_no, report.information_type_no)
        if any(key(report) != key(first) for report in reports):
            raise ValueError('Mixed report announcements')
    elif len(reports) != 1:
        raise ValueError('DCX images accept one report')
    if isinstance(first, dcr.Typhoon):
        if any(report.typhoon_number_raw != first.typhoon_number_raw for report in reports):
            raise ValueError('Mixed typhoons')
        # Conflicting points at the same time cannot silently overwrite each other.
        seen = {}
        for report in reports:
            key = (report.reference_time, report.reference_time_type_raw)
            if key in seen and bytes(report.raw) != seen[key]:
                raise ValueError('Conflicting forecast points')
            seen[key] = bytes(report.raw)
    elif isinstance(first, dcr.Hypocenter) and len(reports) != 1:
        raise ValueError('Hypocenter images accept one report')
    return reports


def _title(report):
    if isinstance(report, dcr.Typhoon):
        return f'台風{report.typhoon_number}'
    if isinstance(report, dcr.Hypocenter):
        return '震源情報'
    if isinstance(report, dcx.AlertBase):
        return 'DCX / ' + report.dcx_message_type
    return str(getattr(report, 'disaster_category', type(report).__name__))


def _header(fig, report, subtitle):
    markers, details, training, cancelled = _metadata(report)
    if markers:
        _text(fig, .045, .958, markers, 21, ALERT, bold=True)
    title = _title(report)
    title_size = min(40, 650 / max(1, sum(1 if unicodedata.east_asian_width(c) in ('W', 'F') else .55 for c in title)))
    _text(fig, .042, .89 if markers else .925, title, title_size, bold=True)
    is_dcr = isinstance(report, dcr.Base)
    published = _time(report.report_time if is_dcr else report.timestamp, full=True)
    time_kind = '発表' if is_dcr else '受信'
    _text(fig, .045, .846 if markers else .88, f'{published} {time_kind} / JST', 14, MUTED)
    _text(fig, .045, .812 if markers else .846, details, 13, ALERT if training or cancelled else MUTED)
    _text(fig, .955, .956, 'SHIRASU', 14, bold=True, ha='right')
    _text(fig, .955, .914, subtitle, 12, MUTED, ha='right')
    source = '気象庁・みちびき' if is_dcr else 'みちびき（DCX）'
    _text(fig, .045, .025, f'情報：{source} / 地形：Natural Earth', 10, MUTED)


def _figure():
    fig = Figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI, facecolor=SEA)
    FigureCanvasAgg(fig)
    return fig


def _map(fig, positions, *, japan_context=False):
    longitudes = np.degrees(np.unwrap(np.radians([position[0] for position in positions])))
    latitudes = [position[1] for position in positions]
    xs, ys = list(longitudes), list(_my(latitudes))
    map_x, map_y = xs.copy(), ys.copy()
    if japan_context and all(100 <= x <= 190 for x in xs) and all(0 <= y <= 65 for y in latitudes):
        map_x.extend([128, 145])
        map_y.extend(_my([31, 45]))
    min_x, max_x, min_y, max_y = min(map_x), max(map_x), min(map_y), max(map_y)
    span_x = max(20 if japan_context else 12, (max_x - min_x) * 1.4)
    span_y = max(14 if japan_context else 10, (max_y - min_y) * 1.5)
    # Leave map space around the points for labels and the overlaid header.
    width = max(span_x, span_y * WIDTH / HEIGHT)
    height = width * HEIGHT / WIDTH
    centre_x = (min_x + max_x) / 2
    centre_y = (min_y + max_y) / 2 + height * .08
    limits = (centre_x - width / 2, centre_x + width / 2,
              centre_y - height / 2, centre_y + height / 2)
    ax = fig.add_axes([0, 0, 1, 1], facecolor=SEA)
    ax.set_xlim(*limits[:2])
    ax.set_ylim(*limits[2:])
    ax.set_aspect('equal')
    for polygon in _land():
        shift = round((centre_x - float(np.mean(polygon[:, 0]))) / 360) * 360
        for delta in (shift - 360, shift, shift + 360):
            xmin, xmax = float(polygon[:, 0].min()) + delta, float(polygon[:, 0].max()) + delta
            if xmax < limits[0] or xmin > limits[1] or polygon[:, 1].max() < limits[2] or polygon[:, 1].min() > limits[3]:
                continue
            copy = polygon.copy()
            copy[:, 0] += delta
            ax.add_patch(Polygon(copy, facecolor=LAND, edgecolor=COAST, linewidth=.55, zorder=2))
    spacing = 5 if width < 30 else 10
    for lon in np.arange(math.floor(limits[0] / spacing) * spacing, limits[1], spacing):
        ax.axvline(lon, color='#2a3743', linewidth=.5, zorder=1)
    for lat in range(-80, 85, spacing):
        y = _my(lat)
        if limits[2] <= y <= limits[3]:
            ax.axhline(y, color='#2a3743', linewidth=.5, zorder=1)
    ax.set_axis_off()
    return ax, list(zip(xs, ys))


def _overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _segment_hits_box(segment, box):
    """Closed segment/rectangle intersection, including boundary contact."""
    start, end = segment
    lower, upper = 0., 1.
    for axis in (0, 1):
        delta = end[axis] - start[axis]
        if abs(delta) < 1e-12:
            if not box[axis] <= start[axis] <= box[axis + 2]:
                return False
            continue
        first = (box[axis] - start[axis]) / delta
        last = (box[axis + 2] - start[axis]) / delta
        lower, upper = max(lower, min(first, last)), min(upper, max(first, last))
        if lower > upper:
            return False
    return True


def _segments_touch(first, second):
    """Detect crossings, endpoint contacts and collinear overlap."""
    a, b = first
    c, d = second
    if (max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0])
            or max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1])):
        return False
    def side(p, q, r):
        value = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(value) < 1e-12 else (1 if value > 0 else -1)
    return side(a, b, c) * side(a, b, d) <= 0 and side(c, d, a) * side(c, d, b) <= 0


def _pixel_distance(a, b):
    return math.hypot((a[0] - b[0]) * WIDTH, (a[1] - b[1]) * HEIGHT)


def _leader_segment(point, box):
    # The nearest point on the padded box produces short, usually vertical or
    # horizontal leaders. Use these exact endpoints when drawing too.
    end = (min(max(point[0], box[0]), box[2]), min(max(point[1], box[1]), box[3]))
    length = _pixel_distance(point, end)
    if length <= 11:
        raise ValueError('Label is too close to its marker')
    start = tuple(p + (q - p) * 11 / length for p, q in zip(point, end, strict=True))
    return start, end


def place_labels(points, sizes):
    """Find a bounded, deterministic layout with clear boxes and leader lines.

    Coordinates are figure fractions; distances and marker clearances use
    pixels. Search several partial layouts so an early label cannot trap later
    ones. The header/footer stay outside the placement area.
    """
    points = [tuple(point) for point in points]
    tracks = [(a, b) for a, b in zip(points, points[1:]) if _pixel_distance(a, b) > 1e-6]
    candidates = []
    for (px, py), (width, height) in zip(points, sizes, strict=True):
        centres = []
        for radius in (.075, .12, .18, .25, .34, .45):
            for step in range(16):
                angle = step * math.pi / 8
                centres.append((px + radius * math.cos(angle), py + radius * math.sin(angle)))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            centres.append((px + dx * (width / 2 + .025), py + dy * (height / 2 + .025)))
        centres.extend((float(x), float(y))
                       for x in np.linspace(.035 + width / 2, .965 - width / 2, 20)
                       for y in np.linspace(.085 + height / 2, .79 - height / 2, 15))
        valid = []
        for x, y in centres:
            box = (x - width / 2, y - height / 2, x + width / 2, y + height / 2)
            if box[0] < .035 or box[2] > .965 or box[1] < .085 or box[3] > .79:
                continue
            if any(box[0] - 12 / WIDTH < qx < box[2] + 12 / WIDTH
                   and box[1] - 12 / HEIGHT < qy < box[3] + 12 / HEIGHT for qx, qy in points):
                continue
            segment = _leader_segment((px, py), box)
            if any(_segment_hits_box(track, box) or _segments_touch(segment, track) for track in tracks):
                continue
            # Avoid unrelated markers too. Coincident targets share only their
            # marker; their leaders start outside it and may not overlap.
            if any(_pixel_distance((px, py), point) > 1e-6 and _segment_hits_box(segment,
                   (point[0] - 7 / WIDTH, point[1] - 7 / HEIGHT,
                    point[0] + 7 / WIDTH, point[1] + 7 / HEIGHT)) for point in points):
                continue
            length = _pixel_distance(*segment)
            score = length ** 2 + .2 * _pixel_distance((px, py), (x, y)) ** 2
            valid.append((score, box, segment))
        # Keep the outer candidates: nearby placements alone can enclose a
        # cluster and leave no clear route for its remaining labels.
        unique = {candidate[1]: candidate for candidate in valid}
        candidates.append(sorted(unique.values(), key=lambda candidate: (candidate[0], candidate[1])))
    if any(not options for options in candidates):
        raise ValueError('Labels do not fit the image')

    def search(order):
        beams = [(0., ())]
        for index in order:
            following = []
            for cost, placed in beams:
                for score, box, segment in candidates[index]:
                    if any(_overlap(box, previous[1]) > 0
                           or _segment_hits_box(segment, previous[1])
                           or _segment_hits_box(previous[2], box)
                           or _segments_touch(segment, previous[2]) for previous in placed):
                        continue
                    following.append((cost + score, (*placed, (index, box, segment))))
            if not following:
                return None
            following.sort(key=lambda state: (state[0], state[1]))
            beams, seen = [], set()
            for state in following:
                # Coincident points can otherwise fill the entire beam with
                # permutations of the same few occupied rectangles.
                geometry = tuple(sorted((points[index], box) for index, box, _ in state[1]))
                if geometry not in seen:
                    seen.add(geometry)
                    beams.append(state)
                if len(beams) == 32:
                    break
        result = [None] * len(points)
        for index, box, _ in beams[0][1]:
            result[index] = box
        return result

    chronological = list(range(len(points)))
    result = search(chronological)
    if result is None:
        constrained = sorted(chronological, key=lambda i: (len(candidates[i]), -sizes[i][0], i))
        if constrained != chronological:
            result = search(constrained)
    if result is None:
        raise ValueError('Labels do not fit the image')
    return result


def _annotations(fig, ax, positions, labels):
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    points = [fig.transFigure.inverted().transform(ax.transData.transform(point)) for point in positions]
    for scale in (1, .85, .7):
        boxes, sizes = [], []
        for lines in labels:
            children = [TextArea(text, textprops={'fontproperties': _font(size * scale, bold),
                        'color': color, 'parse_math': False}) for text, size, color, bold in lines]
            box = VPacker(children=children, align='left', pad=0, sep=8)
            box.set_figure(fig)
            extent = box.get_window_extent(renderer)
            boxes.append(box)
            # Reserve space between text and leaders. VPacker spacing is in
            # points, so measuring its real extent avoids unit/descender errors.
            sizes.append(((extent.width + 24) / WIDTH, (extent.height + 24) / HEIGHT))
        try:
            rectangles = place_labels(points, sizes)
        except ValueError:
            continue
        for point, projected, box, rect in zip(positions, points, boxes, rectangles, strict=True):
            centre = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
            start, end = _leader_segment(projected, rect)
            ax.add_line(Line2D(*zip(start, end), transform=fig.transFigure,
                              color='#899cac', linewidth=1, zorder=7, gid='label-leader'))
            ax.add_artist(AnnotationBbox(box, point, xycoords='data', xybox=centre,
                          boxcoords=fig.transFigure, frameon=False, box_alignment=(.5, .5), zorder=9))
        return rectangles
    raise ValueError('Labels do not fit the image')


def _typhoon(fig, reports):
    points = sorted(reports, key=lambda r: (r.reference_time or datetime.max.replace(tzinfo=timezone.utc),
                                           r.reference_time_type_raw, r.nmea))
    if any(report.reference_time is None or report.reference_time_type_raw not in (1, 2, 3)
           or _coordinates(report.coordinates_of_typhoon_raw) is None for report in points):
        return _summary(fig, reports, note='位置・時刻が未定義のため受信内容を表示')
    positions = [_coordinates(report.coordinates_of_typhoon_raw) for report in points]
    ax, projected = _map(fig, positions, japan_context=True)
    _header(fig, points[0], '中心位置の実況・予報')
    # These are forecast-centre connections, never uncertainty or wind radii.
    ax.plot(*zip(*projected), color=WHITE, linewidth=2.1, linestyle=(0, (4, 4)), zorder=5)
    labels = []
    for report, point in zip(points, projected, strict=True):
        actual = report.reference_time_type == '実況'
        ax.scatter(*point, s=105 if actual else 65, facecolor=ACCENT if actual else SEA,
                   edgecolor=WHITE, linewidth=1.8, zorder=8)
        date = _time(report.reference_time)
        if report.reference_time.astimezone(JST).year != report.report_time.astimezone(JST).year:
            date = _time(report.reference_time, full=True)
        kind = '' if report.reference_time_type == '予報' else report.reference_time_type + ' '
        lines = [(kind + date, 19, ACCENT if actual else WHITE, True),
                 (report.central_pressure.replace('hPa', ' hPa'), 29 if actual else 22,
                  WHITE if actual else MUTED, actual)]
        if actual:
            intensity = report.typhoon_intensity_category
            prefix = '' if intensity == 'なし' else intensity + ' / '
            lines.append((prefix + '最大風速 ' + report.maximum_wind_speed.replace('m/s', ' m/s'), 13, MUTED, False))
        labels.append(lines)
    try:
        _annotations(fig, ax, projected, labels)
    except ValueError:
        fig.clear()
        return _summary(fig, reports, note='受信した各時点の情報')
    _text(fig, .955, .025, f'受信した{len(points)}時点 / 時刻はJST', 11, MUTED, ha='right')


def _hypocenter(fig, reports):
    report = reports[0]
    position = _coordinates(report.coordinates_of_hypocenter_raw)
    if position is None:
        return _summary(fig, reports, note='震源位置が未定義のため受信内容を表示')
    notifications = '\n'.join(text for code, text in zip(report.notifications_on_disaster_prevention_raw,
                                 report.notifications_on_disaster_prevention, strict=True) if code != 0)
    notice_lines = _wrap(notifications, 44)
    if len(notice_lines) > 5:
        return _summary(fig, reports, note='防災上の留意事項を含む受信内容')
    ax, projected = _map(fig, [position])
    _header(fig, report, '震央の位置')
    ax.scatter(*projected[0], marker='x', s=160, color=ACCENT, linewidth=3, zorder=8)
    labels = [[(str(report.seismic_epicenter), 23, WHITE, True),
               ('M ' + str(report.magnitude) + ' / 深さ ' + str(report.depth_of_hypocenter), 20, MUTED, False),
               (_time(report.occurrence_time_of_earthquake, full=True) + ' 発生', 14, MUTED, False)]]
    labels[0].extend((line, 13, ALERT, False) for line in notice_lines)
    try:
        _annotations(fig, ax, projected, labels)
    except ValueError:
        fig.clear()
        return _summary(fig, reports)


def _wrap(text, width=50):
    output = []
    for line in text.splitlines():
        if not line.strip():
            continue
        current, used = '', 0
        for char in line:
            if not char.isprintable():
                continue
            size = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
            if used + size > width:
                output.append(current)
                current, used = '', 0
            current, used = current + char, used + size
        if current:
            output.append(current)
    return output


def _summary(fig, reports, note='受信した通報の要旨'):
    first = reports[0]
    _header(fig, first, note)
    _, _, _, cancelled = _metadata(first)
    if cancelled:
        text = 'この通報は取り消されました。\n対象：' + _title(first)
    else:
        # Avoid repeating a multipart Nankai assembly once per source page.
        sources = reports[:1] if isinstance(first, dcr.NankaiTroughEarthquake) else reports
        text = '\n\n'.join(str(report) for report in sources)
    lines = _wrap(text, 66)
    truncated = len(lines) > 17
    for index, line in enumerate(lines[:17]):
        _text(fig, .06, .745 - index * .035, line, 17, WHITE)
    if truncated:
        _text(fig, .955, .07, '抜粋 / 全文は投稿本文を参照', 12, ALERT, ha='right')


@dataclass(frozen=True)
class RenderedImage:
    png: bytes
    category: str


def render(records):
    """Render a verified bundle; callers must authenticate untrusted input first."""
    with _LOCK:
        verify_assets()
        _reset_assembly()
        fig = None
        try:
            reports = _decode(tuple(records))
            fig = _figure()
            first = reports[0]
            _, _, _, cancelled = _metadata(first)
            if cancelled:
                _summary(fig, reports)
            elif isinstance(first, dcr.Typhoon):
                _typhoon(fig, reports)
            elif isinstance(first, dcr.Hypocenter):
                _hypocenter(fig, reports)
            else:
                _summary(fig, reports)
            output = io.BytesIO()
            fig.savefig(output, format='png', dpi=DPI, facecolor=SEA,
                        metadata={'Software': 'shirasu-image-v1'})
            return RenderedImage(output.getvalue(), type(first).__name__)
        finally:
            if fig is not None:
                fig.clear()
            _reset_assembly()
