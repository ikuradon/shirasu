"""Point maps for the unpublished wide layout; published helpers stay immutable.

Only received typhoon centres are connected. The sidebar distinguishes the
latest received analysis/estimate from forecasts and never derives motion or
wind-field geometry from the track. Earthquakes and volcanoes use their actual
source positions, with full advisory text remaining in the accompanying post.
"""

from datetime import datetime
import math

from azarashi.reports import dcr
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, TextArea, VPacker
import numpy as np

from image_service import render as base
from image_service import region_map, volcano_map
from image_service import preview_wide_layout as wide
from image_service.regional_reports import _reports


def _distance(a, b):
    return math.hypot((a[0] - b[0]) * wide.WIDTH, (a[1] - b[1]) * wide.HEIGHT)


def _leader_segment(point, box):
    end = (min(max(point[0], box[0]), box[2]), min(max(point[1], box[1]), box[3]))
    length = _distance(point, end)
    if length <= 11:
        raise ValueError('Label is too close to its marker')
    start = tuple(p + (q - p) * 11 / length for p, q in zip(point, end, strict=True))
    return start, end


def _place_track_labels(points, sizes, *, obstacles=()):
    """Keep labels and leaders clear of the track, header, panel and locator."""
    points = [tuple(point) for point in points]
    if not 1 <= len(points) <= 16 or len(points) != len(sizes):
        raise ValueError('Too many dated centres for a readable map')
    tracks = [(a, b) for a, b in zip(points, points[1:]) if _distance(a, b) > 1e-6]
    candidates = []
    for (px, py), (width, height) in zip(points, sizes, strict=True):
        centres = []
        for radius in (.045, .075, .12, .18, .25, .34, .45):
            for step in range(16):
                angle = step * math.pi / 8
                centres.append((px + radius * math.cos(angle), py + radius * math.sin(angle)))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            centres.append((px + dx * (width / 2 + .020), py + dy * (height / 2 + .020)))
        centres.extend((float(x), float(y))
                       for x in np.linspace(.035 + width / 2, wide.PANEL_X - .025 - width / 2, 18)
                       for y in np.linspace(.065 + height / 2, .775 - height / 2, 15))
        valid = {}
        for x, y in centres:
            box = (x - width / 2, y - height / 2, x + width / 2, y + height / 2)
            if (box[0] < .035 or box[2] > wide.PANEL_X - .025
                    or box[1] < .065 or box[3] > .775):
                continue
            if any(base._overlap(box, obstacle) > 0 for obstacle in obstacles):
                continue
            if any(box[0] - 12 / wide.WIDTH < qx < box[2] + 12 / wide.WIDTH
                   and box[1] - 12 / wide.HEIGHT < qy < box[3] + 12 / wide.HEIGHT for qx, qy in points):
                continue
            try:
                segment = _leader_segment((px, py), box)
            except ValueError:
                continue
            if any(base._segment_hits_box(segment, obstacle) for obstacle in obstacles):
                continue
            if any(base._segment_hits_box(track, box) or base._segments_touch(segment, track)
                   for track in tracks):
                continue
            if any(_distance((px, py), point) > 1e-6 and base._segment_hits_box(segment,
                   (point[0] - 7 / wide.WIDTH, point[1] - 7 / wide.HEIGHT,
                    point[0] + 7 / wide.WIDTH, point[1] + 7 / wide.HEIGHT)) for point in points):
                continue
            score = _distance(*segment) ** 2 + .2 * _distance((px, py), (x, y)) ** 2
            valid[box] = (score, box, segment)
        candidates.append(sorted(valid.values(), key=lambda choice: (choice[0], choice[1])))
    if any(not choices for choices in candidates):
        raise ValueError('Dated centres do not fit the map')

    def search(order):
        beams = [(0., ())]
        for index in order:
            following = []
            for cost, placed in beams:
                for score, box, segment in candidates[index]:
                    if any(base._overlap(box, previous[1]) > 0
                           or base._segment_hits_box(segment, previous[1])
                           or base._segment_hits_box(previous[2], box)
                           or base._segments_touch(segment, previous[2]) for previous in placed):
                        continue
                    following.append((cost + score, (*placed, (index, box, segment))))
            if not following:
                return None
            following.sort(key=lambda state: (state[0], state[1]))
            beams, seen = [], set()
            for state in following:
                geometry = tuple(sorted((points[i], box) for i, box, _ in state[1]))
                if geometry not in seen:
                    seen.add(geometry)
                    beams.append(state)
                if len(beams) == 32:
                    break
        result = [None] * len(points)
        for index, box, _ in beams[0][1]:
            result[index] = box
        return result

    order = list(range(len(points)))
    result = search(order)
    if result is None:
        constrained = sorted(order, key=lambda i: (len(candidates[i]), -sizes[i][0], i))
        if constrained != order:
            result = search(constrained)
    if result is None:
        raise ValueError('Dated centres have no clear label layout')
    return result


def _track_annotations(fig, ax, positions, labels, *, obstacles=()):
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    points = [fig.transFigure.inverted().transform(ax.transData.transform(point)) for point in positions]
    for scale in (1, .90, .80):
        boxes, sizes = [], []
        for label, colour, actual in labels:
            child = TextArea(label, textprops={'fontproperties': base._font((21 if actual else 19) * scale, True),
                                               'color': colour, 'parse_math': False})
            box = VPacker(children=[child], align='left', pad=0, sep=0)
            box.set_figure(fig)
            extent = box.get_window_extent(renderer)
            boxes.append(box)
            sizes.append(((extent.width + 24) / wide.WIDTH, (extent.height + 24) / wide.HEIGHT))
        try:
            rectangles = _place_track_labels(points, sizes, obstacles=obstacles)
        except ValueError:
            continue
        for point, projected, box, rectangle in zip(positions, points, boxes, rectangles, strict=True):
            centre = ((rectangle[0] + rectangle[2]) / 2, (rectangle[1] + rectangle[3]) / 2)
            start, end = _leader_segment(projected, rectangle)
            ax.add_line(Line2D(*zip(start, end), transform=fig.transFigure,
                              color='#899cac', linewidth=1, zorder=7, gid='label-leader'))
            ax.add_artist(AnnotationBbox(box, point, xycoords='data', xybox=centre,
                          boxcoords=fig.transFigure, frameon=False, box_alignment=(.5, .5), zorder=9))
        return rectangles
    raise ValueError('Dated centres do not fit outside the locator')


def _date(report):
    full = report.reference_time.astimezone(base.JST).year != report.report_time.astimezone(base.JST).year
    return base._time(report.reference_time, full=full)


def _coordinates(raw):
    values = []
    for axis, positive, negative in (('lat', '北緯', '南緯'), ('lon', '東経', '西経')):
        hemisphere = raw['lat_ns' if axis == 'lat' else 'lon_ew']
        value = f'{negative if hemisphere else positive}{raw[axis + "_d"]}°{raw[axis + "_m"]:02d}′'
        if raw[axis + '_s']:
            value += f'{raw[axis + "_s"]}″'
        values.append(value)
    return tuple(values)


def _metric(label, value, unit):
    # Keep decoder-defined unknowns and open bounds as text, never raw sentinels.
    value = str(value)
    return wide.Metric(label, value.removesuffix(unit), unit) if value.endswith(unit) else wide.Metric(label, value)


def draw_typhoon(fig, reports):
    reports = _reports(reports, dcr.Typhoon)
    first = reports[0]
    if any(not isinstance(report.reference_time, datetime) or report.reference_time.utcoffset() is None
           or type(report.reference_time_type_raw) is not int or report.reference_time_type_raw not in (1, 2, 3)
           or base._coordinates(report.coordinates_of_typhoon_raw) is None
           or report.typhoon_number_raw != first.typhoon_number_raw for report in reports):
        raise ValueError('Unknown or mixed typhoon position, time or identity')
    seen = {}
    for report in reports:
        key = (report.reference_time, report.reference_time_type_raw)
        if key in seen and bytes(report.raw) != seen[key]:
            raise ValueError('Conflicting typhoon centres at one reference time')
        seen[key] = bytes(report.raw)
    points = sorted(reports, key=lambda report: (report.reference_time, report.reference_time_type_raw, report.nmea))
    positions = [base._coordinates(report.coordinates_of_typhoon_raw) for report in points]
    longitudes = np.degrees(np.unwrap(np.radians([point[0] for point in positions])))
    projected = list(zip(longitudes, base._my([point[1] for point in positions]), strict=True))
    view = wide.map_axes(fig, [np.asarray(projected)], minimum=20.)
    ax = view.ax
    ax.plot(*zip(*projected), color=wide.WHITE, linewidth=2.1, linestyle=(0, (5, 5)),
            zorder=5, gid='typhoon-track')
    labels = []
    for index, (report, point) in enumerate(zip(points, projected, strict=True)):
        actual = report.reference_time_type_raw == 1
        kind = {1: '実況 ', 2: '推定 ', 3: ''}[report.reference_time_type_raw]
        ax.scatter(*point, s=150 if actual else 90, facecolor=wide.ACCENT if actual else wide.SEA,
                   edgecolor=wide.WHITE, linewidth=1.8, zorder=8, gid=f'typhoon-point-{index}')
        labels.append((kind + _date(report), wide.ACCENT if actual else wide.WHITE, actual))
    _track_annotations(fig, ax, projected, labels, obstacles=view.obstacles)
    wide.header(fig, first, f'台風第{first.typhoon_number} 実況・予報',
                '受信した中心位置 / 時刻はJST')
    current = [report for report in points if report.reference_time_type_raw in (1, 2)]
    if current:
        latest = max(current, key=lambda report: (report.reference_time, report.reference_time_type_raw == 1))
        status = {1: '実況', 2: '推定'}[latest.reference_time_type_raw]
        intro = [*_coordinates(latest.coordinates_of_typhoon_raw)]
        for label, value in (('大きさ', latest.typhoon_scale_category), ('強さ', latest.typhoon_intensity_category)):
            if str(value) != 'なし':
                intro.append(f'{label}：{value}')
        wide.sidebar(fig, f'{_date(latest)} {status}', intro=tuple(intro), metrics=(
            _metric('中心気圧', latest.central_pressure, 'hPa'),
            _metric('最大風速', latest.maximum_wind_speed, 'm/s'),
            _metric('最大瞬間風速', latest.maximum_gust_wind_speed, 'm/s'),
        ))
    else:
        # A forecast-only bundle must not promote any future value to "current".
        rows = tuple(wide.Row(_date(report) + ' 予報', (
            '中心気圧 ' + str(report.central_pressure),
            '最大風速 ' + str(report.maximum_wind_speed),
            '最大瞬間風速 ' + str(report.maximum_gust_wind_speed),
        )) for report in points)
        wide.sidebar(fig, '受信した予報', rows, intro=('実況・推定の受信なし',))
    return view


def draw_hypocenter(fig, reports):
    reports = _reports(reports, dcr.Hypocenter)
    if len(reports) != 1:
        raise ValueError('Hypocenter maps accept one report')
    report = reports[0]
    position = base._coordinates(report.coordinates_of_hypocenter_raw)
    if position is None:
        raise ValueError('Unknown hypocenter position')
    if len(report.notifications_on_disaster_prevention_raw) != len(report.notifications_on_disaster_prevention):
        raise ValueError('Inconsistent hypocenter advisory fields')
    point = (position[0], float(base._my(position[1])))
    view = wide.map_axes(fig, [np.asarray([point])], minimum=12.)
    view.ax.scatter(*point, marker='x', s=230, color=wide.ACCENT, linewidth=3.5,
                    zorder=8, gid='hypocenter-point')
    wide.header(fig, report, '震源情報', '受信した震央の位置')
    occurred = base._time(report.occurrence_time_of_earthquake, full=True)
    rows = [wide.Row('発生時刻', (occurred + (' JST' if occurred != '時刻不明' else ''),)),
            wide.Row('震央位置', _coordinates(report.coordinates_of_hypocenter_raw))]
    notices = []
    for code, value in zip(report.notifications_on_disaster_prevention_raw,
                           report.notifications_on_disaster_prevention, strict=True):
        if code and value:
            # An exact short heading can accompany the map; the full text is
            # retained by the posting formatter instead of being copied here.
            first_line = next((line for line in str(value).splitlines() if line.strip()), '')
            if first_line and first_line not in notices:
                notices.append(first_line)
    if notices:
        rows.append(wide.Row('防災情報', tuple(notices), color=wide.ALERT))
    wide.sidebar(fig, str(report.seismic_epicenter), tuple(rows), metrics=(
        wide.Metric('マグニチュード（M）', str(report.magnitude)),
        _metric('深さ', report.depth_of_hypocenter, 'km'),
    ))
    return view


def draw_volcano(fig, reports):
    reports = tuple(reports)
    plan = volcano_map.volcano_plan(reports)
    notice = plan.notices[0]
    lon, lat = region_map._position(volcano_map._positions()[str(notice.code)]['coordinates'])
    point = (lon, float(base._my(lat)))
    view = wide.map_axes(fig, [np.asarray([point])], minimum=4.)
    view.ax.scatter(*point, marker='^', s=180, facecolor=notice.color,
                    edgecolor=wide.WHITE, linewidth=1.1, zorder=8,
                    gid=f'volcano-point-{notice.code}')
    wide.header(fig, reports[0], plan.title, '火山の代表位置 / 警戒範囲ではありません')
    rows = (wide.Row('警戒情報', (notice.lines[0],), color=notice.color),
            wide.Row('活動時刻', (notice.lines[1].removeprefix('活動時刻：'),)),
            wide.Row(notice.lines[2], notice.lines[3:]))
    wide.sidebar(fig, notice.title, rows)
    return view
