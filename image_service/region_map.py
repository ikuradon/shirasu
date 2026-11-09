"""Versioned regional geometry and labels for the v2 report adapters.

Coordinates and code relationships come only from the pinned JMA assets. Missing
regions reject the entire plan; callers can then show the original report text.
"""

from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

from matplotlib.colors import to_rgba
from matplotlib.backends.backend_agg import RendererAgg
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, TextArea, VPacker
from matplotlib.path import Path as ShapePath
from matplotlib.patches import PathPatch, Polygon, Rectangle
from matplotlib.transforms import IdentityTransform
import numpy as np

from image_service import render as base

ASSETS = Path(__file__).parent / 'assets' / 'v2'
LAYERS = ('marine', 'tsunami', 'prefectures')
ASSET_NAMES = tuple(f'{layer}.geojson' for layer in LAYERS) + ('groups.json',)
LABEL_BOTTOM, LABEL_TOP = .18, .79
OVERVIEW_POSITION = (.705, .20, .26, .30)
OVERVIEW_OBSTACLE = (.695, .19, .975, .56)


@lru_cache(maxsize=1)
def verify_assets():
    base.verify_assets()
    manifest = json.loads((ASSETS / 'manifest.json').read_text())
    for name in ASSET_NAMES:
        if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('Regional assets do not match their frozen version')
    return True


def _code(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0:
        return int(value)
    raise ValueError('Invalid regional code')


@lru_cache(maxsize=3)
def _layer(name):
    if name not in LAYERS:
        raise ValueError('Unknown regional layer')
    verify_assets()
    data = json.loads((ASSETS / f'{name}.geojson').read_text())
    if data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
        raise ValueError('Invalid regional feature collection')
    features = {}
    for feature in data['features']:
        if feature.get('type') != 'Feature' or not isinstance(feature.get('properties'), dict):
            raise ValueError('Invalid regional feature')
        features.setdefault(_code(feature['properties'].get('code')), []).append(feature)
    if not features:
        raise ValueError('Empty regional feature collection')
    return features


@lru_cache(maxsize=1)
def _groups():
    verify_assets()
    data = json.loads((ASSETS / 'groups.json').read_text())
    if not isinstance(data, dict) or any(name not in LAYERS for name in data):
        raise ValueError('Invalid regional grouping')
    result = {}
    for layer, values in data.items():
        if not isinstance(values, dict):
            raise ValueError('Invalid regional grouping')
        result[layer] = {}
        for parent, children in values.items():
            if not isinstance(children, list) or not children:
                raise ValueError('Invalid regional group members')
            result[layer][_code(parent)] = tuple(_code(child) for child in children)
    return result


def _expand(layer, code):
    features, groups = _layer(layer), _groups().get(layer, {})
    def visit(current, seen):
        if current in seen:
            raise ValueError('Cyclic regional grouping')
        if current in groups:
            result = set()
            for child in groups[current]:
                result.update(visit(child, seen | {current}))
            return result
        if current not in features:
            raise ValueError('Unknown geographic region')
        return {current}
    return tuple(sorted(visit(_code(code), set())))


def _position(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(part) not in (float, int) or not math.isfinite(part) for part in value)
            or not -180 <= value[0] <= 180 or not -85 < value[1] < 85):
        raise ValueError('Invalid regional coordinate')
    return tuple(value)


def _sequence(values, reference, *, ring=False):
    if not isinstance(values, list) or len(values) < (4 if ring else 2):
        raise ValueError('Invalid regional geometry coordinates')
    positions = [_position(value) for value in values]
    if ring and positions[0] != positions[-1]:
        raise ValueError('Unclosed regional polygon')
    result = np.asarray(positions, dtype=float)
    result[:, 0] = np.degrees(np.unwrap(np.radians(result[:, 0])))
    result[:, 0] += round((reference - float(result[:, 0].mean())) / 360) * 360
    result[:, 1] = base._my(result[:, 1])
    return result


def _geometry(feature, layer, reference):
    geometry = feature.get('geometry')
    if not isinstance(geometry, dict):
        raise ValueError('Missing regional geometry')
    kind, coordinates = geometry.get('type'), geometry.get('coordinates')
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError('Empty regional geometry')
    if layer == 'tsunami':
        if kind == 'LineString':
            coordinates = [coordinates]
        elif kind != 'MultiLineString':
            raise ValueError('Tsunami regions require coastline geometry')
        return tuple(_sequence(line, reference) for line in coordinates)
    if kind == 'Polygon':
        coordinates = [coordinates]
    elif kind != 'MultiPolygon':
        raise ValueError('Regional area requires polygon geometry')
    polygons = []
    for polygon in coordinates:
        if not isinstance(polygon, list) or not polygon:
            raise ValueError('Empty regional polygon')
        polygons.append(tuple(_sequence(ring, reference, ring=True) for ring in polygon))
    return tuple(polygons)


def _signed_area(ring):
    relative = ring - ring[0]
    return float(np.sum(relative[:-1, 0] * relative[1:, 1] - relative[1:, 0] * relative[:-1, 1])) / 2


def _polygon_path(rings):
    vertices, commands = [], []
    for index, original in enumerate(rings):
        ring = original
        signed_area = _signed_area(ring)
        if signed_area == 0:
            raise ValueError('Degenerate regional polygon')
        # Nonzero winding: exterior counterclockwise, every hole clockwise.
        if (signed_area > 0) != (index == 0):
            ring = ring[::-1]
        vertices.extend(ring)
        commands.extend([ShapePath.MOVETO, *([ShapePath.LINETO] * (len(ring) - 2)), ShapePath.CLOSEPOLY])
    return ShapePath(vertices, commands)


def _limits(sequences, *, position=(0, 0, 1, 1), centre_at=(.5, .47), span=(.84, .52), minimum=4.):
    vertices = np.concatenate(sequences)
    low, high = vertices.min(axis=0), vertices.max(axis=0)
    aspect = position[2] * base.WIDTH / (position[3] * base.HEIGHT)
    width = max(minimum, float(high[0] - low[0]) / span[0],
                float(high[1] - low[1]) / span[1] * aspect)
    height = width / aspect
    centre = (low + high) / 2
    return (centre[0] - width * centre_at[0], centre[0] + width * (1 - centre_at[0]),
            centre[1] - height * centre_at[1], centre[1] + height * (1 - centre_at[1]))


def _axes(fig, limits, *, position=(0, 0, 1, 1)):
    # Main maps fill the image. An optional prefecture overview shares the same
    # dark map style and keeps every remote island visible at a smaller scale.
    width = limits[1] - limits[0]
    centre_x = (limits[0] + limits[1]) / 2
    ax = fig.add_axes(position, facecolor=base.SEA)
    ax.set_xlim(*limits[:2])
    ax.set_ylim(*limits[2:])
    ax.set_aspect('equal')
    for polygon in base._land():
        shift = round((centre_x - float(polygon[:, 0].mean())) / 360) * 360
        for delta in (shift - 360, shift, shift + 360):
            if (polygon[:, 0].max() + delta < limits[0] or polygon[:, 0].min() + delta > limits[1]
                    or polygon[:, 1].max() < limits[2] or polygon[:, 1].min() > limits[3]):
                continue
            part = polygon.copy()
            part[:, 0] += delta
            ax.add_patch(Polygon(part, facecolor=base.LAND, edgecolor=base.COAST, linewidth=.55, zorder=2))
    spacing = 2 if width < 15 else 5 if width < 40 else 10
    for lon in np.arange(math.floor(limits[0] / spacing) * spacing, limits[1], spacing):
        ax.axvline(lon, color='#2a3743', linewidth=.5, zorder=1)
    for lat in range(-80, 85, spacing):
        y = base._my(lat)
        if limits[2] <= y <= limits[3]:
            ax.axhline(y, color='#2a3743', linewidth=.5, zorder=1)
    ax.set_axis_off()
    return ax


def _boundary_mask(paths):
    """Index visible outline pixels once instead of scanning every island per label."""
    renderer = RendererAgg(base.WIDTH, base.HEIGHT, base.DPI)
    graphics = renderer.new_gc()
    graphics.set_foreground((1, 1, 1, 1))
    for path, linewidth in paths:
        graphics.set_linewidth(linewidth)
        renderer.draw_path(graphics, path, IdentityTransform())
    graphics.restore()
    pixels = np.asarray(renderer.buffer_rgba())[::-1, :, 3] > 0
    total = np.zeros((base.HEIGHT + 1, base.WIDTH + 1), dtype=np.uint32)
    total[1:, 1:] = pixels.cumsum(axis=0, dtype=np.uint32).cumsum(axis=1, dtype=np.uint32)
    return total


def _hits_boundary(total, box):
    if total is None:
        return False
    x0 = max(0, math.floor(box[0] * base.WIDTH - 4))
    y0 = max(0, math.floor(box[1] * base.HEIGHT - 4))
    x1 = min(base.WIDTH, math.ceil(box[2] * base.WIDTH + 4))
    y1 = min(base.HEIGHT, math.ceil(box[3] * base.HEIGHT + 4))
    return int(total[y1, x1]) + int(total[y0, x0]) > int(total[y0, x1]) + int(total[y1, x0])


def _place_labels(points, sizes, *, boundaries=None, obstacles=()):
    """Bounded v2 placement without inventing tracks between unrelated regions."""
    points = [tuple(point) for point in points]
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
                       for y in np.linspace(LABEL_BOTTOM + height / 2, LABEL_TOP - height / 2, 15))
        valid = {}
        for x, y in centres:
            box = (x - width / 2, y - height / 2, x + width / 2, y + height / 2)
            if box[0] < .035 or box[2] > .965 or box[1] < LABEL_BOTTOM or box[3] > LABEL_TOP:
                continue
            if any(base._overlap(box, obstacle) > 0 for obstacle in obstacles):
                continue
            if _hits_boundary(boundaries, box):
                continue
            if any(box[0] - 12 / base.WIDTH < qx < box[2] + 12 / base.WIDTH
                   and box[1] - 12 / base.HEIGHT < qy < box[3] + 12 / base.HEIGHT for qx, qy in points):
                continue
            segment = base._leader_segment((px, py), box)
            if any(base._segment_hits_box(segment, obstacle) for obstacle in obstacles):
                continue
            if any(base._pixel_distance((px, py), point) > 1e-6 and base._segment_hits_box(segment,
                   (point[0] - 7 / base.WIDTH, point[1] - 7 / base.HEIGHT,
                    point[0] + 7 / base.WIDTH, point[1] + 7 / base.HEIGHT)) for point in points):
                continue
            length = base._pixel_distance(*segment)
            score = length ** 2 + .2 * base._pixel_distance((px, py), (x, y)) ** 2
            valid[box] = (score, box, segment)
        candidates.append(sorted(valid.values(), key=lambda choice: (choice[0], choice[1])))
    if any(not choices for choices in candidates):
        raise ValueError('Regional labels do not fit')

    def search(order):
        beams = [(0., ())]
        for index in order:
            following = []
            for cost, placed in beams:
                for score, box, segment in candidates[index]:
                    if any(base._overlap(box, old[1]) > 0
                           or base._segment_hits_box(segment, old[1])
                           or base._segment_hits_box(old[2], box)
                           or base._segments_touch(segment, old[2]) for old in placed):
                        continue
                    following.append((cost + score, (*placed, (index, box, segment))))
            if not following:
                return None
            following.sort(key=lambda state: (state[0], state[1]))
            beams, seen = [], set()
            for state in following:
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

    original = list(range(len(points)))
    result = search(original)
    constrained = sorted(original, key=lambda index: (len(candidates[index]), -sizes[index][0], index))
    if result is None and constrained != original:
        result = search(constrained)
    if result is None:
        raise ValueError('Regional labels do not fit')
    return result


def _annotations(fig, ax, anchors, notices, *, obstacles=()):
    if len(notices) > 16:
        raise ValueError('Too many regional labels for a readable image')
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    points = [fig.transFigure.inverted().transform(ax.transData.transform(point)) for point in anchors]
    outlines = [(artist.get_transform().transform_path(artist.get_path()), artist.get_linewidth())
                for artist in (*ax.patches, *ax.lines)
                if (artist.get_gid() or '').startswith(('region-area-', 'region-coast-'))]
    boundaries = _boundary_mask(outlines) if outlines else None
    for scale in (1, .85, .7):
        boxes, sizes = [], []
        for notice in notices:
            lines = [(text, 21, notice.color, True) for text in base._wrap(notice.title, 36)]
            for detail in notice.lines:
                lines.extend((text, 16, base.WHITE, False) for text in base._wrap(detail, 44))
            if not lines:
                raise ValueError('Empty regional label')
            children = [TextArea(text, textprops={'fontproperties': base._font(size * scale, bold),
                        'color': color, 'parse_math': False}) for text, size, color, bold in lines]
            box = VPacker(children=children, align='left', pad=0, sep=6)
            box.set_figure(fig)
            extent = box.get_window_extent(renderer)
            boxes.append(box)
            sizes.append(((extent.width + 24) / base.WIDTH, (extent.height + 24) / base.HEIGHT))
        try:
            rectangles = _place_labels(points, sizes, boundaries=boundaries, obstacles=obstacles)
        except ValueError:
            continue
        for point, projected, box, rectangle, notice in zip(anchors, points, boxes, rectangles, notices, strict=True):
            centre = ((rectangle[0] + rectangle[2]) / 2, (rectangle[1] + rectangle[3]) / 2)
            start, end = base._leader_segment(projected, rectangle)
            ax.add_line(Line2D(*zip(start, end), transform=fig.transFigure, color='#899cac', linewidth=1,
                              zorder=7, gid='label-leader'))
            ax.add_artist(AnnotationBbox(box, point, xycoords='data', xybox=centre, boxcoords=fig.transFigure,
                          frameon=False, box_alignment=(.5, .5), zorder=9))
        return rectangles
    raise ValueError('Regional labels do not fit')


def _paint_regions(ax, leaves, layer):
    for code, leaf in sorted(leaves.items()):
        area_paths = []
        for _, parts in leaf['parts']:
            if layer == 'tsunami':
                for line in parts:
                    ax.add_line(Line2D(line[:, 0], line[:, 1], color=leaf['color'], linewidth=4.6,
                                      solid_capstyle='round', zorder=4, gid=f'region-coast-{code}'))
            else:
                area_paths.extend(_polygon_path(polygon) for polygon in parts)
        if area_paths:
            # Batch even thousands of small islands into one artist per region.
            # Prefectures only provide context: intensity stays at the marker
            # and label rather than implying a uniform field over the area.
            face = to_rgba(leaf['color'], .18) if layer == 'marine' else to_rgba('#a3b3c2', .035)
            edge = leaf['color'] if layer == 'marine' else '#8b98a5'
            ax.add_patch(PathPatch(ShapePath.make_compound_path(*area_paths), facecolor=face,
                                   edgecolor=edge, linewidth=1.25, zorder=3, gid=f'region-area-{code}'))


def _inside_limits(sequences, limits):
    vertices = np.concatenate(sequences)
    return (vertices[:, 0].min() >= limits[0] and vertices[:, 0].max() <= limits[1]
            and vertices[:, 1].min() >= limits[2] and vertices[:, 1].max() <= limits[3])


def _body_limits(limits):
    left, right, bottom, top = limits
    height = top - bottom
    return left, right, bottom + height * LABEL_BOTTOM, bottom + height * LABEL_TOP


def _overview(fig, sequences, leaves, main_limits):
    left, right, bottom, top = main_limits
    full_extent = [*sequences, np.asarray(((left, bottom), (right, top)))]
    limits = _limits(full_extent, position=OVERVIEW_POSITION, centre_at=(.5, .5), span=(.88, .88), minimum=1.)
    ax = _axes(fig, limits, position=OVERVIEW_POSITION)
    ax.set_gid('region-overview')
    ax.set_zorder(10)
    # set_axis_off also hides the axes patch, so keep the opaque sea background
    # explicit; otherwise the main map would show through the overview ocean.
    ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes, facecolor=base.SEA,
                           edgecolor='#536777', linewidth=.6, zorder=0))
    _paint_regions(ax, leaves, 'prefectures')
    ax.add_patch(Rectangle((left, bottom), right - left, top - bottom, fill=False,
                           edgecolor=base.WHITE, linewidth=.9, linestyle=(0, (4, 3)),
                           zorder=6, gid='region-main-extent'))
    base._text(fig, OVERVIEW_POSITION[0], .54, '離島を含む全域', size=13, color=base.WHITE, zorder=11)
    base._text(fig, OVERVIEW_POSITION[0], .515, '破線は主図範囲', size=11, color='#9cabb8', zorder=11)
    return ax


def draw_map(fig, plan):
    """Draw every requested region and its full label, or reject the entire plan."""
    layer = plan.layer
    if layer not in LAYERS or not plan.notices:
        raise ValueError('Unknown or empty regional map')
    if len(plan.notices) > 16:
        raise ValueError('Too many regional labels for a readable image')
    features = _layer(layer)
    groups, leaves = [], {}
    reference = None
    for notice in plan.notices:
        to_rgba(notice.color)  # Validate before drawing anything.
        selected = _expand(layer, notice.code)
        anchors = []
        for code in selected:
            if code in leaves and leaves[code]['color'] != notice.color:
                raise ValueError('Overlapping regions have conflicting colors')
            for feature in features[code]:
                anchor = _position(feature['properties'].get('anchor'))
                if reference is None:
                    reference = anchor[0]
                lon = anchor[0] + round((reference - anchor[0]) / 360) * 360
                anchors.append((lon, float(base._my(anchor[1]))))
                if code not in leaves:
                    leaves[code] = {'color': notice.color, 'parts': []}
                # A repeated parent/child request must not multiply the opacity.
                if not any(old is feature for old, _ in leaves[code]['parts']):
                    leaves[code]['parts'].append((feature, _geometry(feature, layer, reference)))
        # Keep a parent label at one actual member's representative point.
        centre = np.mean(anchors, axis=0)
        groups.append(min(anchors, key=lambda point: (math.dist(point, centre), point)))
    sequences, primary = [], []
    for leaf in leaves.values():
        polygons = []
        for _, parts in leaf['parts']:
            sequences.extend(parts if layer == 'tsunami' else [ring for polygon in parts for ring in polygon])
            if layer == 'prefectures':
                polygons.extend(parts)
        if polygons:
            # A prefecture's principal land mass keeps the main intensity map
            # readable; all its other components remain in the overview.
            primary.extend(max(polygons, key=lambda polygon: abs(_signed_area(polygon[0]))
                               - sum(abs(_signed_area(hole)) for hole in polygon[1:])))
    sequences.append(np.asarray(groups))
    primary.append(np.asarray(groups))
    limits = _limits(primary if layer == 'prefectures' else sequences)
    overview = layer == 'prefectures' and not _inside_limits(sequences, _body_limits(limits))
    if overview:
        limits = _limits(primary, centre_at=(.38, .47), span=(.55, .52))
    ax = _axes(fig, limits)
    _paint_regions(ax, leaves, layer)
    if layer == 'prefectures':
        visible = Rectangle((0, LABEL_BOTTOM), 1, LABEL_TOP - LABEL_BOTTOM, transform=fig.transFigure)
        for artist in ax.patches:
            if (artist.get_gid() or '').startswith('region-area-'):
                artist.set_clip_path(visible)
    if overview:
        _overview(fig, sequences, leaves, limits)
    for point, notice in zip(groups, plan.notices, strict=True):
        ax.scatter(*point, s=35, facecolor=base.SEA, edgecolor=notice.color, linewidth=1.5,
                   zorder=8, gid=f'region-anchor-{notice.code}')
    _annotations(fig, ax, groups, plan.notices, obstacles=(OVERVIEW_OBSTACLE,) if overview else ())
    return ax
