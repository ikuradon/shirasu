"""Unpublished point maps with prefectural context and an inset locator.

The published v1-v4 functions remain unchanged. Typhoon label placement keeps
its explicit track-intersection checks while also reserving the locator box.
"""

from datetime import datetime, timezone
import math

from azarashi.reports import dcr
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, TextArea, VPacker
import numpy as np

from image_service import map_base_v3 as terrain
from image_service import preview_context as context
from image_service import region_map as layout
from image_service import render as base
from image_service import volcano_map
from image_service.regional_reports import _reports


def _place_track_labels(points, sizes, *, obstacles=()):
    """Find a bounded, deterministic layout with clear boxes and leader lines.

    Coordinates are figure fractions; distances and marker clearances use
    pixels. Search several partial layouts so an early label cannot trap later
    ones. The header/footer stay outside the placement area.
    """
    points = [tuple(point) for point in points]
    tracks = [(a, b) for a, b in zip(points, points[1:]) if base._pixel_distance(a, b) > 1e-6]
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
            if any(base._overlap(box, obstacle) > 0 for obstacle in obstacles):
                continue
            if any(box[0] - 12 / base.WIDTH < qx < box[2] + 12 / base.WIDTH
                   and box[1] - 12 / base.HEIGHT < qy < box[3] + 12 / base.HEIGHT for qx, qy in points):
                continue
            segment = base._leader_segment((px, py), box)
            if any(base._segment_hits_box(segment, obstacle) for obstacle in obstacles):
                continue
            if any(base._segment_hits_box(track, box) or base._segments_touch(segment, track) for track in tracks):
                continue
            # Avoid unrelated markers too. Coincident targets share only their
            # marker; their leaders start outside it and may not overlap.
            if any(base._pixel_distance((px, py), point) > 1e-6 and base._segment_hits_box(segment,
                   (point[0] - 7 / base.WIDTH, point[1] - 7 / base.HEIGHT,
                    point[0] + 7 / base.WIDTH, point[1] + 7 / base.HEIGHT)) for point in points):
                continue
            length = base._pixel_distance(*segment)
            score = length ** 2 + .2 * base._pixel_distance((px, py), (x, y)) ** 2
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


def _track_annotations(fig, ax, positions, labels, *, obstacles=()):
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    points = [fig.transFigure.inverted().transform(ax.transData.transform(point)) for point in positions]
    for scale in (1, .85, .7):
        boxes, sizes = [], []
        for lines in labels:
            children = [TextArea(text, textprops={'fontproperties': base._font(size * scale, bold),
                        'color': color, 'parse_math': False}) for text, size, color, bold in lines]
            box = VPacker(children=children, align='left', pad=0, sep=8)
            box.set_figure(fig)
            extent = box.get_window_extent(renderer)
            boxes.append(box)
            sizes.append(((extent.width + 24) / base.WIDTH, (extent.height + 24) / base.HEIGHT))
        try:
            rectangles = _place_track_labels(points, sizes, obstacles=obstacles)
        except ValueError:
            continue
        for point, projected, box, rectangle in zip(positions, points, boxes, rectangles, strict=True):
            centre = ((rectangle[0] + rectangle[2]) / 2, (rectangle[1] + rectangle[3]) / 2)
            start, end = base._leader_segment(projected, rectangle)
            ax.add_line(Line2D(*zip(start, end), transform=fig.transFigure,
                              color='#899cac', linewidth=1, zorder=7, gid='label-leader'))
            ax.add_artist(AnnotationBbox(box, point, xycoords='data', xybox=centre,
                          boxcoords=fig.transFigure, frameon=False, box_alignment=(.5, .5), zorder=9))
        return rectangles
    raise ValueError('Typhoon labels do not fit outside the locator')


def _axes(fig, positions, *, minimum):
    limits, locator = context.limits([np.asarray(positions)], minimum=minimum)
    ax = terrain.axes(fig, limits)
    context.draw_prefecture_boundaries(ax)
    if locator:
        context.draw_locator(fig, limits)
    return ax, (context.LOCATOR_OBSTACLE,) if locator else ()


def _point_header(fig, report, subtitle):
    base._header(fig, report, subtitle)
    # The frozen helper's final text is its source credit. Extend the preview's
    # own text artist without altering any published function or global state.
    fig.texts[-1].set_text('情報・県境：気象庁（加工） / 受信：みちびき / 地形：Natural Earth')


def draw_typhoon(fig, reports):
    reports = _reports(reports, dcr.Typhoon)
    points = sorted(reports, key=lambda report: (
        report.reference_time or datetime.max.replace(tzinfo=timezone.utc),
        report.reference_time_type_raw, report.nmea))
    if any(report.reference_time is None or report.reference_time_type_raw not in (1, 2, 3)
           or base._coordinates(report.coordinates_of_typhoon_raw) is None for report in points):
        raise ValueError('Unknown typhoon position or reference time')
    positions = [base._coordinates(report.coordinates_of_typhoon_raw) for report in points]
    longitudes = np.degrees(np.unwrap(np.radians([point[0] for point in positions])))
    projected = list(zip(longitudes, base._my([point[1] for point in positions]), strict=True))
    ax, obstacles = _axes(fig, projected, minimum=20.)
    _point_header(fig, points[0], '中心位置の実況・予報')
    # Received centres are connected as in the published renderer; the line is
    # not an uncertainty cone or a radius of strong wind.
    ax.plot(*zip(*projected), color=base.WHITE, linewidth=2.1, linestyle=(0, (4, 4)),
            zorder=5, gid='typhoon-track')
    labels = []
    for index, (report, point) in enumerate(zip(points, projected, strict=True)):
        actual = report.reference_time_type == '実況'
        ax.scatter(*point, s=105 if actual else 65, facecolor=base.ACCENT if actual else base.SEA,
                   edgecolor=base.WHITE, linewidth=1.8, zorder=8, gid=f'typhoon-point-{index}')
        date = base._time(report.reference_time)
        if report.reference_time.astimezone(base.JST).year != report.report_time.astimezone(base.JST).year:
            date = base._time(report.reference_time, full=True)
        kind = '' if report.reference_time_type == '予報' else report.reference_time_type + ' '
        lines = [(kind + date, 19, base.ACCENT if actual else base.WHITE, True),
                 (report.central_pressure.replace('hPa', ' hPa'), 29 if actual else 22,
                  base.WHITE if actual else base.MUTED, actual)]
        if actual:
            intensity = report.typhoon_intensity_category
            prefix = '' if intensity == 'なし' else intensity + ' / '
            lines.append((prefix + '最大風速 ' + report.maximum_wind_speed.replace('m/s', ' m/s'),
                          13, base.MUTED, False))
        labels.append(lines)
    _track_annotations(fig, ax, projected, labels, obstacles=obstacles)
    base._text(fig, .955, .025, f'受信した{len(points)}時点 / 時刻はJST', 11, base.MUTED, ha='right')
    return ax


def draw_hypocenter(fig, reports):
    reports = _reports(reports, dcr.Hypocenter)
    if len(reports) != 1:
        raise ValueError('Hypocenter maps accept one report')
    report = reports[0]
    position = base._coordinates(report.coordinates_of_hypocenter_raw)
    if position is None:
        raise ValueError('Unknown hypocenter position')
    notifications = '\n'.join(text for code, text in zip(report.notifications_on_disaster_prevention_raw,
                                 report.notifications_on_disaster_prevention, strict=True) if code != 0)
    notice_lines = base._wrap(notifications, 44)
    if len(notice_lines) > 5:
        raise ValueError('Hypocenter notification text does not fit')
    point = (position[0], float(base._my(position[1])))
    ax, obstacles = _axes(fig, [point], minimum=12.)
    _point_header(fig, report, '震央の位置')
    ax.scatter(*point, marker='x', s=160, color=base.ACCENT, linewidth=3, zorder=8,
               gid='hypocenter-point')
    labels = [[(str(report.seismic_epicenter), 23, base.WHITE, True),
               ('M ' + str(report.magnitude) + ' / 深さ ' + str(report.depth_of_hypocenter),
                20, base.MUTED, False),
               (base._time(report.occurrence_time_of_earthquake, full=True) + ' 発生',
                14, base.MUTED, False)]]
    labels[0].extend((line, 13, base.ALERT, False) for line in notice_lines)
    _track_annotations(fig, ax, [point], labels, obstacles=obstacles)
    return ax


def draw_volcano(fig, reports):
    plan = volcano_map.volcano_plan(reports)
    notice = plan.notices[0]
    lon, lat = layout._position(volcano_map._positions()[str(notice.code)]['coordinates'])
    point = (lon, float(base._my(lat)))
    ax, obstacles = _axes(fig, [point], minimum=4.)
    ax.scatter(*point, marker='^', s=130, facecolor=notice.color,
               edgecolor=base.WHITE, linewidth=1.1, zorder=8,
               gid=f'volcano-point-{notice.code}')
    layout._annotations(fig, ax, [point], plan.notices, obstacles=obstacles)
    return plan
