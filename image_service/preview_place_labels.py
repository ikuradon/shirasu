"""Optional, quiet place names below the unpublished forecast annotations.

These names explain the base map only. They never identify a warning area,
move a received observation, or displace a forecast label to make room.
"""

import numpy as np
from matplotlib.collections import PathCollection
from matplotlib.offsetbox import AnnotationBbox
from matplotlib.path import Path
from matplotlib.transforms import Bbox

from image_service import preview_wide_layout as wide
from image_service import render as base


COLOR = '#b8c2c9'
FONT_SIZE = 14
MAX_LABELS = 5


def _obstacles(fig, view, renderer):
    """Measure existing forecast objects in display pixels, once per map."""
    boxes = [Bbox.from_extents(*box).transformed(fig.transFigure) for box in view.obstacles]
    paths = []
    for label in (*fig.texts, *view.ax.texts):
        if not label.get_visible() or not label.get_text():
            continue
        patch = label.get_bbox_patch()
        box = (patch.get_window_extent(renderer) if patch else label.get_window_extent(renderer))
        boxes.append(box.padded(5))
    for artist in view.ax.artists:
        if isinstance(artist, AnnotationBbox) and artist.get_visible():
            boxes.append(artist.get_window_extent(renderer).padded(5))
    for patch in view.ax.patches:
        identity = patch.get_gid() or ''
        if identity.startswith('seismic-badge-'):
            boxes.append(patch.get_window_extent(renderer).padded(5))
        elif ((identity.startswith('weather-outline-')
               or (identity.startswith('region-area-') and patch.get_linewidth() >= 1))
              and patch.get_linewidth() > 0 and patch.get_edgecolor()[3] > 0):
            # Neutral municipality/prefecture outlines still identify a
            # received scope. Avoid prominent strokes, not filled interiors
            # or EEW's thin neutral subdivision boundaries.
            paths.append((patch.get_transform().transform_path(patch.get_path()),
                          patch.get_linewidth() * fig.dpi / 144 + 3))
    for collection in view.ax.collections:
        if not isinstance(collection, PathCollection) or not collection.get_visible():
            continue
        points = collection.get_offset_transform().transform(collection.get_offsets())
        sizes = collection.get_sizes()
        strokes = collection.get_linewidths()
        for index, (x, y) in enumerate(points):
            size = sizes[index % len(sizes)] if len(sizes) else 36
            stroke = strokes[index % len(strokes)] if len(strokes) else 1
            radius = (np.sqrt(size) + stroke) * fig.dpi / 144 + 6
            boxes.append(Bbox.from_bounds(x - radius, y - radius, 2 * radius, 2 * radius))
    for line in view.ax.lines:
        identity = line.get_gid() or ''
        # Untagged graticules and the river's redundant casing are not
        # separate forecast information. The foreground flow line is checked.
        if not identity or identity.startswith(('flood-shadow-', 'flood-casing-')):
            continue
        if line.get_visible():
            paths.append((line.get_transform().transform_path(line.get_path()),
                          line.get_linewidth() * fig.dpi / 144 + 4))
    return boxes, paths


def _inside_component(point, polygon):
    if not polygon:
        return True
    return (Path(polygon[0]).contains_point(point)
            and not any(Path(hole).contains_point(point) for hole in polygon[1:]))


def draw_place_labels(fig, view, places):
    """Place at most five geographic names after every forecast was drawn.

    ``places`` supplies ``name``, ``position`` (projected lon/my), ``code``,
    ``kind`` and the local ``polygon`` including holes. Prefecture-name
    centres stay inside their own component. Island names may sit immediately
    beside their island, where a tiny land area cannot hold its name and the
    received marker. All movement is bounded in display pixels, with no leader.
    An obstructed name is simply omitted; forecast information takes priority.
    """
    places = tuple(places)
    if not places:
        return ()
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes, paths = _obstacles(fig, view, renderer)
    occupied, labels, centres, used_names = [], [], [], set()
    offsets = ((0, 0), (0, 42), (0, -42), (52, 0), (-52, 0),
               (42, 36), (-42, 36), (42, -36), (-42, -36),
               (0, 68), (0, -68), (72, 0), (-72, 0))
    fixed_boxes = [Bbox.from_extents(*box).transformed(fig.transFigure) for box in view.obstacles]
    pending = []
    for index, place in enumerate(places):
        name = str(place.name)
        if not name or len(name) > 40:
            continue
        anchor = np.asarray(view.ax.transData.transform(place.position), dtype=float)
        if (not np.isfinite(anchor).all() or not view.ax.bbox.contains(*anchor)
                or any(box.contains(*anchor) for box in fixed_boxes)):
            continue
        label = view.ax.text(*place.position, name, fontproperties=base._font(FONT_SIZE),
                             color=COLOR, alpha=.82, ha='center', va='center',
                             zorder=3.8, parse_math=False,
                             gid=f'preview-place-{place.code}')
        pending.append((index, place, label, anchor))

    def candidate(place, label, anchor):
        for dx, dy in offsets:
            centre = anchor + (dx, dy)
            position = view.ax.transData.inverted().transform(centre)
            if (getattr(place, 'kind', 'prefecture') != 'island'
                    and not _inside_component(position, getattr(place, 'polygon', ()))):
                continue
            label.set_position(position)
            box = label.get_window_extent(renderer).padded(4)
            if not (12 <= box.x0 and box.x1 <= wide.PANEL_X * wide.WIDTH - 12
                    and 12 <= box.y0 and box.y1 <= wide.HEIGHT - 12):
                continue
            if not (view.ax.bbox.contains(box.x0, box.y0)
                    and view.ax.bbox.contains(box.x1, box.y1)):
                continue
            if any(box.overlaps(other) for other in (*boxes, *occupied)):
                continue
            if any(path.intersects_bbox(box.padded(padding), filled=False) for path, padding in paths):
                continue
            return box, centre, position
        return None

    while pending and len(labels) < MAX_LABELS:
        choices, rejected = [], set()
        for index, place, label, anchor in pending:
            if label.get_text() in used_names:
                rejected.add(index)
                label.remove()
                continue
            fit = candidate(place, label, anchor)
            if fit is None:
                rejected.add(index)
                label.remove()
                continue
            box, centre, position = fit
            distance = min((float(np.linalg.norm(centre - other)) for other in centres), default=0.)
            choices.append((distance, -index, place, label, box, centre, position))
            if not labels:
                # The core/nearest place supplied first remains the priority.
                break
        pending = [item for item in pending if item[0] not in rejected]
        if not choices:
            break
        # Thereafter spread successful names across the visible map instead
        # of spending all five slots on neighbouring eastern/western units.
        _, negative_index, place, label, box, centre, position = max(choices, key=lambda item: item[:2])
        label.set_position(position)
        labels.append(label)
        centres.append(centre)
        occupied.append(box.padded(12))
        used_names.add(label.get_text())
        pending = [item for item in pending if item[0] != -negative_index]
    for _, _, label, _ in pending:
        label.remove()
    return tuple(labels)
