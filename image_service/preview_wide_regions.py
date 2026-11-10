"""Regional maps for the unpublished wide layout.

The semantic adapters and geographic catalogues remain frozen. This module
changes presentation only: named values go in the right panel, while the map
retains the complete received scope. The locator stays a plain base map.
"""

from dataclasses import replace
import string

from matplotlib.colors import to_rgba
from matplotlib.patches import PathPatch, Rectangle
from matplotlib.path import Path
from matplotlib.transforms import Bbox
import numpy as np

from azarashi.definitions.qzss.dcr.seismic_intensity import seismic_intensity

from image_service import preview_regions as geometry
from image_service import region_map as regions
from image_service import render as base
from image_service import preview_wide_layout as wide
from image_service.eew_map import eew_plan
from image_service.marine_regions import marine_map
from image_service.regional_reports import seismic_intensity_map
from image_service.weather_map_v4 import weather_plan


EEW_NAMES = {66: '中国地方', 67: '四国地方', 68: '九州地方'}
EEW_FILL = '#647a84'
EEW_EDGE = '#94a3ad'
SEISMIC_LABELS = {1: '4未満', 2: '4', 3: '5−', 4: '5＋', 5: '6−', 6: '6＋', 7: '7'}
# Preview-only palette: the published semantic adapter's colours stay frozen.
SEISMIC_COLORS = {1: '#b8cad7', 2: '#e58b3a', 3: '#d84b45', 4: '#d84b45',
                  5: '#9f4fb1', 6: '#9f4fb1', 7: '#723e89'}
SEISMIC_FOREGROUNDS = {value: '#161a1f' if value <= 2 else '#ffffff' for value in SEISMIC_LABELS}
SEISMIC_BADGE_SIZE = 60


def compact_eew(plan):
    """Remove only names wholly covered by an equally valued received name."""
    expanded = {notice.code: set(geometry.geography._expand('eew', notice.code))
                for notice in plan.notices}
    selected = tuple(notice for notice in plan.notices
                     if not any(expanded[notice.code] < expanded[other.code]
                                and notice.color == other.color
                                and notice.lines == other.lines
                                for other in plan.notices))
    original_scope = set().union(*expanded.values())
    selected_scope = set().union(*(expanded[notice.code] for notice in selected))
    if original_scope != selected_scope:
        raise ValueError('EEW presentation lost a received warning area')
    return replace(plan, notices=tuple(replace(notice, title=EEW_NAMES.get(notice.code, notice.title))
                                      for notice in selected))


def _primary_polygons(leaves):
    result = {}
    for code, leaf in leaves.items():
        polygons = [polygon for _, parts in leaf['parts'] for polygon in parts]
        result[code] = max(polygons, key=lambda polygon: abs(regions._signed_area(polygon[0]))
                          - sum(abs(regions._signed_area(ring)) for ring in polygon[1:]))
    return result


def _principal_point(polygon):
    """An interior reference for a named area, never an observation location."""
    ring = polygon[0]
    a, b = ring[:-1], ring[1:]
    cross = a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1]
    centre = ((a + b) * cross[:, None]).sum(axis=0) / (3 * cross.sum())
    outer = Path(ring)
    holes = tuple(Path(hole) for hole in polygon[1:])

    def inside(point):
        return outer.contains_point(point) and not any(hole.contains_point(point) for hole in holes)

    if inside(centre):
        return tuple(map(float, centre))
    low, high = ring.min(axis=0), ring.max(axis=0)
    candidates = [np.asarray((x, y)) for x in np.linspace(low[0], high[0], 31)[1:-1]
                  for y in np.linspace(low[1], high[1], 31)[1:-1] if inside((x, y))]
    if not candidates:
        raise ValueError('No readable reference point inside the received region')
    return tuple(map(float, min(candidates, key=lambda point: np.linalg.norm(point - centre))))


def _weather_mainland_cluster(primary, codes):
    """Fit neighbouring subdivisions of a received group without remote islands.

    Ishikari/Sorachi/Shiribeshi are three adjacent mainland subdivisions; the
    largest alone does not represent their group's full map extent. Tokyo's
    mainland/Izu/Ogasawara subdivisions, by contrast, are separated by sea.
    Expand a component from the largest subdivision through touching or very
    nearby outer bounds, allowing small source-boundary simplification gaps.
    All excluded remote geometry remains available to the plain locator.
    """
    codes = tuple(codes)
    bounds = {code: (primary[code][0].min(axis=0), primary[code][0].max(axis=0))
              for code in codes}
    seed = max(codes, key=lambda code: abs(regions._signed_area(primary[code][0])))
    selected = {seed}
    while True:
        added = {code for code in codes if code not in selected
                 and any(np.linalg.norm(np.maximum(np.maximum(bounds[code][0] - bounds[other][1],
                                                                bounds[other][0] - bounds[code][1]), 0)) <= .1
                         for other in selected)}
        if not added:
            break
        selected.update(added)
    return tuple(primary[code] for code in codes if code in selected)


def _prepare(fig, plan):
    if plan.layer not in geometry.LAYERS or not plan.notices:
        raise ValueError('Unsupported wide regional map')
    painted, leaves, anchors, full, _ = geometry._regions(plan)
    if painted == 'prefectures':
        primary = _primary_polygons(leaves)
        source = geometry.geography if plan.layer in ('weather', 'eew') else regions
        # Fit adjacent mainland subdivisions of each received weather group,
        # while keeping remote Izu/Ogasawara units in the locator extent.
        main_polygons = primary.values()
        if plan.layer == 'weather':
            main_polygons = [polygon for notice in plan.notices
                             for polygon in _weather_mainland_cluster(
                                 primary, source._expand(plan.layer, notice.code))]
        sequences = [ring for polygon in main_polygons for ring in polygon]
        values = np.concatenate(sequences)
        low, high = values.min(axis=0), values.max(axis=0)
        remote = not regions._inside_limits(full, (low[0], high[0], low[1], high[1]))
        # DCR prefecture anchors can lie on an island. Reference letters should
        # remain beside the visible principal land. The locator includes the
        # remote parts' extent without thematic overlays. References do not
        # imply a station or a per-point forecast.
        anchors = []
        for notice in plan.notices:
            polygons = [primary[code] for code in source._expand(plan.layer, notice.code)]
            polygon = max(polygons, key=lambda value: abs(regions._signed_area(value[0])))
            anchors.append(_principal_point(polygon))
    else:
        sequences, remote = full, False
    view = wide.map_axes(fig, sequences, full=full, force_locator=remote)
    return view, painted, leaves, anchors


def _weather_bands(plan):
    """Union each transmitted hazard on its exact expanded forecast scope."""
    per_leaf = {}
    for notice in plan.notices:
        if len(notice.lines) != len(notice.styles):
            raise ValueError('Weather label and style counts differ')
        colours = []
        for style in notice.styles:
            colour = style.background if style.background != 'none' else style.edge
            to_rgba(colour)
            if colour not in colours:
                colours.append(colour)
        if not colours:
            raise ValueError('Weather notice has no category colour')
        for code in geometry.geography._expand('weather', notice.code):
            combined = per_leaf.setdefault(code, [])
            combined.extend(colour for colour in colours if colour not in combined)
    # The outer black band remains visible when a purple inner band is present.
    order = {'#0c000c': 0, '#aa00aa': 1}
    return {code: tuple(sorted(colours, key=lambda colour: (order.get(colour, 2), colour)))
            for code, colours in per_leaf.items()}


def _weather_outlines(ax, leaves, bands, *, scale=1., released=False):
    regions._paint_regions(ax, leaves, 'prefectures')
    areas = {int(artist.get_gid().removeprefix('region-area-')): artist
             for artist in ax.patches if (artist.get_gid() or '').startswith('region-area-')}
    for area in areas.values():
        area.set_facecolor('none')
        area.set_edgecolor('none')
        area.set_linewidth(0)
        area.set_alpha(1)
    distinct = len(set(bands.values())) > 1
    for code, area in areas.items():
        colours = bands[code]
        layers = [('#f2f5f7', 8.4), *zip(colours, np.linspace(6.8, 2.8, len(colours)))]
        for index, (colour, width) in enumerate(layers):
            border = PathPatch(area.get_path(), transform=area.get_transform(),
                               facecolor='none', edgecolor=colour, linewidth=width * scale,
                               capstyle='round', joinstyle='round', zorder=4 + index * .1,
                               linestyle=(0, (4, 3)) if released else '-',
                               gid=f'weather-outline-{code}-{index}')
            # Different neighbouring warning sets must not overwrite one
            # another's colours at a shared administrative boundary. Constrain
            # those bands to their own polygon side; common sets retain the
            # reviewed symmetric two-colour stroke.
            if distinct:
                border.set_clip_path(area)
            ax.add_patch(border)


def _neutral_areas(ax, leaves):
    regions._paint_regions(ax, leaves, 'prefectures')
    for artist in ax.patches:
        if (artist.get_gid() or '').startswith('region-area-'):
            artist.set_facecolor('none')
            artist.set_edgecolor('#aab6bf')
            artist.set_linewidth(1.8)


def _eew_areas(ax, leaves):
    """Show the received warning scope with one fill, never an intensity map."""
    regions._paint_regions(ax, leaves, 'prefectures')
    for artist in ax.patches:
        if (artist.get_gid() or '').startswith('region-area-'):
            artist.set_alpha(None)
            artist.set_facecolor(to_rgba(EEW_FILL, .72))
            artist.set_edgecolor(to_rgba(EEW_EDGE, .75))
            artist.set_linewidth(.45)


def _references(fig, view, anchors, notices):
    if len(notices) > len(string.ascii_uppercase):
        raise ValueError('Too many regional references')
    texts = []
    for reference, point, notice in zip(string.ascii_uppercase, anchors, notices):
        view.ax.scatter(*point, s=275, facecolor=wide.SEA, edgecolor=notice.color,
                        linewidth=1.4, zorder=7, gid=f'region-anchor-{notice.code}')
        text = view.ax.text(*point, reference, fontproperties=base._font(11, True),
                            color=wide.WHITE, ha='center', va='center', zorder=8,
                            gid=f'region-reference-{notice.code}', parse_math=False)
        texts.append(text)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes = [text.get_window_extent(renderer).expanded(1.25, 1.25) for text in texts]
    if any(box.overlaps(other) for index, box in enumerate(boxes) for other in boxes[index + 1:]):
        raise ValueError('Regional reference markers overlap')
    return tuple(f'{letter}  {notice.title}' for letter, notice in zip(string.ascii_uppercase, notices))


def draw_weather(fig, reports):
    reports = tuple(reports)
    plan = weather_plan(reports)
    bands = _weather_bands(plan)
    view, _, leaves, _ = _prepare(fig, plan)
    released = reports[0].weather_warning_state_raw == 2
    _weather_outlines(view.ax, leaves, bands, released=released)
    wide.header(fig, reports[0], '気象警報・注意報', '受信した府県予報区を表示',
                markers=('解除',) if released else ())
    first = plan.notices[0]
    common = all((notice.lines, notice.styles) == (first.lines, first.styles) for notice in plan.notices)
    if common:
        rows = [wide.Row(f'下記の{len(plan.notices)}区域に共通', first.lines, styles=first.styles)]
        rows.extend(wide.Row(notice.title, gid=f'weather-region-{notice.code}') for notice in plan.notices)
    else:
        rows = [wide.Row(notice.title, notice.lines, styles=notice.styles,
                         gid=f'weather-region-{notice.code}') for notice in plan.notices]
    wide.sidebar(fig, '発表対象区域', rows)
    return plan


def draw_eew(fig, reports):
    reports = tuple(reports)
    plan = compact_eew(eew_plan(reports))
    view, _, leaves, _ = _prepare(fig, plan)
    _eew_areas(view.ax, leaves)
    wide.header(fig, reports[0], plan.title, '警報対象区域（震度分布ではありません）')
    rows = []
    for line in plan.notice_lines:
        if line.startswith('最大予測長周期地震動：'):
            rows.append(wide.Row('最大予測長周期地震動',
                                 (line.removeprefix('最大予測長周期地震動：'),)))
        elif line.startswith('地震発生 '):
            rows.append(wide.Row('地震の情報', tuple(line.split(' / '))))
        elif line.startswith('仮定震源（PLUM法） / '):
            # The semantic adapter has already removed PLUM's fixed depth/M.
            # Preserve its assumed place/time wording without implying an
            # estimated epicentre or estimated earthquake magnitude.
            rows.append(wide.Row('仮定震源（PLUM法）', tuple(line.split(' / ')[1:])))
    rows.append(wide.Row('警報対象区域', tuple(notice.title for notice in plan.notices)))
    wide.sidebar(fig, '受信した情報', rows,
                 metrics=(wide.Metric('通報全体の予測震度', plan.headline.removeprefix('予測震度 '),
                                      color=wide.ALERT),))
    return plan


def _draw_values(fig, reports, adapter, scope):
    reports = tuple(reports)
    plan = adapter(reports)
    view, painted, leaves, anchors = _prepare(fig, plan)
    if painted == 'prefectures':
        _neutral_areas(view.ax, leaves)
    else:
        regions._paint_regions(view.ax, leaves, painted)
    names = _references(fig, view, anchors, plan.notices)
    released = plan.layer == 'tsunami' and reports[0].tsunami_warning_code_raw == 2
    title = '津波 警報解除' if released else plan.title
    wide.header(fig, reports[0], title, scope + ' / 記号は右欄と対応',
                markers=('解除',) if released else ())
    rows = [wide.Row(title, notice.lines, color=notice.color,
                     gid=f'region-values-{notice.code}')
            for title, notice in zip(names, plan.notices, strict=True)]
    # The quake's occurrence time is distinct from the report time in the
    # header. Keep that short received value; advice remains in the post text.
    intro = plan.notice_lines if plan.layer == 'prefectures' else ()
    wide.sidebar(fig, '受信した区域と情報', rows, intro=intro)
    return plan


def draw_marine(fig, reports):
    return _draw_values(fig, reports, marine_map, '地方海上予報区')


def _seismic_values(reports):
    """Keep distinct received values; the raw 3-bit code is not the intensity."""
    values = {}
    for report in reports:
        for code, value in zip(report.prefectures_raw, report.seismic_intensities_raw, strict=True):
            if type(code) is not int or not 1 <= code <= 47 or value not in SEISMIC_LABELS:
                raise ValueError('Unknown seismic badge value or prefecture')
            values.setdefault(code, set()).add(value)
    if not values:
        raise ValueError('No seismic badge values')
    return {code: tuple(sorted(group, reverse=True)) for code, group in sorted(values.items())}


def _seismic_positions(fig, view, leaves, values):
    """Place bounded groups of square badges at prefectural representatives.

    Every badge centre stays inside the largest land component of its own
    prefecture. Alternative positions are cartographic representatives, never
    estimated observation stations. If a complete placement cannot be found,
    reject the map instead of omitting a prefecture or a distinct value.
    """
    fig.canvas.draw()
    primary = _primary_polygons(leaves)
    if set(primary) != set(values):
        raise ValueError('Seismic badge scope differs from the received map')
    obstacles = [Bbox.from_extents(*box).transformed(fig.transFigure) for box in view.obstacles]
    occupied, placed = [], []
    half, spacing = SEISMIC_BADGE_SIZE / 2, SEISMIC_BADGE_SIZE + 8
    # Give small prefectures with more values their limited space first.
    order = sorted(values, key=lambda code: (abs(regions._signed_area(primary[code][0]))
                                             / len(values[code]), code))
    for code in order:
        polygon = [view.ax.transData.transform(ring) for ring in primary[code]]
        outer, holes = Path(polygon[0]), [Path(ring) for ring in polygon[1:]]
        anchor = view.ax.transData.transform(_principal_point(primary[code]))
        low, high = polygon[0].min(axis=0), polygon[0].max(axis=0)
        grid = np.array([(x, y) for x in np.linspace(low[0], high[0], 33)[1:-1]
                         for y in np.linspace(low[1], high[1], 33)[1:-1]])
        candidates = np.vstack((anchor, grid))
        inside = outer.contains_points(candidates)
        for hole in holes:
            inside &= ~hole.contains_points(candidates)
        candidates = candidates[inside]
        candidates = candidates[np.argsort(np.linalg.norm(candidates - anchor, axis=1), kind='stable')]
        count = len(values[code])
        layouts = []
        for columns in range(count, 0, -1):
            offsets = np.array([(index % columns, -(index // columns)) for index in range(count)], dtype=float)
            offsets -= (offsets.min(axis=0) + offsets.max(axis=0)) / 2
            layouts.append(offsets * spacing)
        chosen = None
        for centre in candidates:
            for offsets in layouts:
                centres = centre + offsets
                if not outer.contains_points(centres).all() or any(hole.contains_points(centres).any() for hole in holes):
                    continue
                boxes = [Bbox.from_bounds(x - half - 3, y - half - 3,
                                          SEISMIC_BADGE_SIZE + 6, SEISMIC_BADGE_SIZE + 6)
                         for x, y in centres]
                if any(box.x0 < 0 or box.y0 < 0 or box.x1 > wide.WIDTH or box.y1 > wide.HEIGHT
                       or any(box.overlaps(blocked) for blocked in (*obstacles, *occupied))
                       for box in boxes):
                    continue
                chosen = centres, boxes
                break
            if chosen is not None:
                break
        if chosen is None:
            raise ValueError('Complete seismic badges do not fit inside their prefectures')
        centres, boxes = chosen
        occupied.extend(boxes)
        placed.extend((code, value, float(point[0]), float(point[1]))
                      for value, point in zip(values[code], centres, strict=True))
    return tuple(sorted(placed, key=lambda item: (item[0], -item[1])))


def _seismic_badges(fig, view, leaves, values):
    positions = _seismic_positions(fig, view, leaves, values)
    renderer = fig.canvas.get_renderer()
    for code, value, x, y in positions:
        badge = Rectangle(((x - SEISMIC_BADGE_SIZE / 2) / wide.WIDTH,
                           (y - SEISMIC_BADGE_SIZE / 2) / wide.HEIGHT),
                          SEISMIC_BADGE_SIZE / wide.WIDTH, SEISMIC_BADGE_SIZE / wide.HEIGHT,
                          transform=fig.transFigure, facecolor=SEISMIC_COLORS[value],
                          edgecolor='#d8dfe5', linewidth=.8, zorder=7,
                          gid=f'seismic-badge-{code}-{value}')
        view.ax.add_patch(badge)
        label = SEISMIC_LABELS[value]
        for size in range(25, 10, -1):
            width, height, _ = renderer.get_text_width_height_descent(label, base._font(size, True), False)
            if width <= SEISMIC_BADGE_SIZE - 10 and height <= SEISMIC_BADGE_SIZE - 10:
                break
        else:
            raise ValueError('Seismic intensity label does not fit its badge')
        view.ax.text(x / wide.WIDTH, y / wide.HEIGHT, label, transform=fig.transFigure,
                     fontproperties=base._font(size, True), color=SEISMIC_FOREGROUNDS[value],
                     ha='center', va='center', zorder=8, parse_math=False,
                     gid=f'seismic-intensity-{code}-{value}')
    return positions


def draw_seismic(fig, reports):
    reports = tuple(reports)
    plan = seismic_intensity_map(reports)
    values = _seismic_values(reports)
    view, _, leaves, _ = _prepare(fig, plan)
    _neutral_areas(view.ax, leaves)
    _seismic_badges(fig, view, leaves, values)
    wide.header(fig, reports[0], plan.title, '都道府県別の最大震度 / 記号は県の代表位置')
    received = sorted({value for group in values.values() for value in group}, reverse=True)
    rows = [wide.Row('震度 ' + seismic_intensity[value],
                     ('・'.join(notice.title for notice in plan.notices if value in values[notice.code]),),
                     color=SEISMIC_COLORS[value], gid=f'seismic-group-{value}')
            for value in received]
    wide.sidebar(fig, '都道府県別の震度', rows, intro=plan.notice_lines,
                 metrics=(wide.Metric('受信した最大震度', seismic_intensity[received[0]],
                                      color=SEISMIC_COLORS[received[0]]),),
                 footnote='−は弱、＋は強')
    return plan


def draw_tsunami(fig, reports):
    from image_service.preview_wide_tsunami import draw_tsunami as draw_values
    return draw_values(fig, reports)
