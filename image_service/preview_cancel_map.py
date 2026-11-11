"""Neutral cancellation backgrounds; association decisions belong to the caller.

A reference drawer is run in a disposable figure. Only its map frame and
geographic place-name candidates survive: no forecast artists or text are
copied into the cancellation image. This is an unpublished rendering helper,
not a rule for matching a cancellation to a previous announcement.
"""

from dataclasses import dataclass, replace
import math
from numbers import Real

from matplotlib.patches import Rectangle

from image_service import preview_context as context
from image_service import preview_wide_layout as wide


@dataclass(frozen=True)
class MapFrame:
    limits: tuple
    visible_limits: tuple
    position: tuple = (0., 0., 1., 1.)
    locator_limits: tuple | None = None
    locator_position: tuple | None = None
    places: tuple = ()


def _numbers(values, label):
    if (not isinstance(values, (tuple, list)) or len(values) != 4
            or any(isinstance(value, bool) or not isinstance(value, Real)
                   or not math.isfinite(value) for value in values)):
        raise ValueError(f'Invalid cancellation map {label}')
    return tuple(float(value) for value in values)


def _bounds(values, label):
    left, right, bottom, top = _numbers(values, label)
    if not left < right or not bottom < top:
        raise ValueError(f'Invalid cancellation map {label}')
    return left, right, bottom, top


def _position(values):
    x, y, width, height = _numbers(values, 'position')
    if min(x, y) < 0 or min(width, height) <= 0 or max(x + width, y + height) > 1 + 1e-12:
        raise ValueError('Invalid cancellation map position')
    return x, y, width, height


def _obstacles(values):
    result = []
    for value in values:
        left, bottom, right, top = _numbers(value, 'obstacle')
        if not 0 <= left < right <= 1 or not 0 <= bottom < top <= 1:
            raise ValueError('Invalid cancellation map obstacle')
        result.append((left, bottom, right, top))
    return tuple(result)


def _locator_limits(limits, main, position):
    """Keep the former locator coverage and include the now-visible full map."""
    if (limits[0] <= main[0] and main[1] <= limits[1]
            and limits[2] <= main[2] and main[3] <= limits[3]):
        return limits
    corners = lambda bounds: ((bounds[0], bounds[2]), (bounds[1], bounds[3]))
    return wide._limits((corners(limits), corners(main)), (.05, .05, .95, .95),
                        minimum=.1, position=position)


def _locator_obstacle(position):
    left, bottom, width, height = position
    return (max(0., left - .01), max(0., bottom - .01),
            min(1., left + width + .01), min(1., bottom + height + .04))


def _right_locator(position):
    """Move the locator to the full canvas' right edge without resizing it."""
    _, bottom, width, height = position
    return (max(0., .96 - width), bottom, width, height)


def frame_from_view(view):
    """Extract finite drawing bounds without retaining an Axes or Figure."""
    locator = view.locator
    return MapFrame(
        tuple(view.ax.get_xlim()) + tuple(view.ax.get_ylim()),
        tuple(view.visible_limits), tuple(view.ax.get_position(original=True).bounds),
        tuple(locator.get_xlim()) + tuple(locator.get_ylim()) if locator is not None else None,
        tuple(locator.get_position(original=True).bounds) if locator is not None else None,
        tuple(view.places),
    )


def capture_frame(drawer, reports):
    """Capture the complete original map after the caller verifies association.

    The caller supplies the normal drawer and already decoded original report
    bundle. A card, failed render, or multiple main maps is not a usable frame.
    The original reports are passed through unchanged.
    """
    fig = wide.figure()
    try:
        drawer(fig, tuple(reports))
        wide.validate(fig)
        views = [getattr(ax, '_wide_map_view') for ax in fig.axes
                 if getattr(ax, '_wide_map_view', None) is not None]
        if len(views) != 1:
            raise ValueError('Cancellation reference has no unique map frame')
        view, = views
        if view.ax not in fig.axes or (view.locator is not None and view.locator not in fig.axes):
            raise ValueError('Cancellation reference map frame is incomplete')
        return frame_from_view(view)
    finally:
        fig.clear()


def draw_frame(fig, frame, *, obstacles=()):
    """Keep the original camera and show its full map without the old panel."""
    if not isinstance(frame, MapFrame):
        raise ValueError('Invalid cancellation map frame')
    limits = _bounds(frame.limits, 'limits')
    visible = _bounds(frame.visible_limits, 'visible limits')
    position = _position(frame.position)
    if (visible[0] < limits[0] or visible[1] > limits[1]
            or visible[2] < limits[2] or visible[3] > limits[3]):
        raise ValueError('Cancellation visible map leaves the original frame')
    extra_obstacles = _obstacles(obstacles)
    if (frame.locator_limits is None) != (frame.locator_position is None):
        raise ValueError('Incomplete cancellation locator frame')
    locator_limits = (_bounds(frame.locator_limits, 'locator limits')
                      if frame.locator_limits is not None else None)
    locator_position = (_right_locator(_position(frame.locator_position))
                        if frame.locator_position is not None else None)
    # Cancellation cards have no right panel. Its former underlying geography
    # is visible now, so the locator must describe the full main axes extent.
    visible = limits
    if locator_limits is not None:
        locator_limits = _locator_limits(locator_limits, visible, locator_position)
    ax = wide._axes(fig, limits, position=position)
    context.draw_prefecture_boundaries(ax)
    locator = None
    fixed = ()
    if locator_limits is not None:
        locator = wide._axes(fig, locator_limits, position=locator_position, graticule=False)
        locator.set_gid('preview-locator')
        locator.set_zorder(10)
        locator.add_patch(Rectangle((0, 0), 1, 1, transform=locator.transAxes,
                                    facecolor=wide.SEA, edgecolor='#536777', linewidth=.7,
                                    zorder=0, gid='preview-locator-frame'))
        locator.add_patch(Rectangle((visible[0], visible[2]), visible[1] - visible[0],
                                    visible[3] - visible[2], fill=False, edgecolor=wide.WHITE,
                                    linewidth=1.1, zorder=8, gid='preview-main-extent'))
        left, bottom, width, height = locator_position
        fixed += (_locator_obstacle(locator_position),)
        # These belong to the background, below the cancellation card's veil.
        wide.text(fig, left, bottom + height + .015, '表示位置', 12, wide.MUTED,
                  gid='wide-locator', zorder=12)
        wide.text(fig, left + width, bottom + height + .015, '枠は主図範囲', 10, wide.MUTED,
                  ha='right', gid='wide-locator', zorder=12)
    view = wide.MapView(ax, locator, limits, visible, fixed + extra_obstacles, tuple(frame.places))
    ax._wide_map_view = view
    return view


def draw_scope(fig, sequences, *, full=(), obstacles=()):
    """Fit all caller-validated region components, including remote islands.

    ``full`` also participates in fitting the main map. No target outline,
    point, warning colour, or forecast value is painted from either input.
    """
    extra_obstacles = _obstacles(obstacles)
    complete = (*tuple(sequences), *tuple(full))
    if not complete:
        raise ValueError('Cancellation background has no confirmed geographic scope')
    view = wide.map_axes(fig, complete, full=complete)
    visible = tuple(view.ax.get_xlim()) + tuple(view.ax.get_ylim())
    fixed = ()
    if view.locator is not None:
        locator = view.locator
        position = _right_locator(tuple(locator.get_position(original=True).bounds))
        locator.set_position(position)
        limits = tuple(locator.get_xlim()) + tuple(locator.get_ylim())
        fitted = _locator_limits(limits, visible, position)
        locator.set_xlim(*fitted[:2])
        locator.set_ylim(*fitted[2:])
        extent, = [patch for patch in locator.patches if patch.get_gid() == 'preview-main-extent']
        extent.set_xy((visible[0], visible[2]))
        extent.set_width(visible[1] - visible[0])
        extent.set_height(visible[3] - visible[2])
        fixed = (_locator_obstacle(position),)
    view = replace(view, visible_limits=visible, obstacles=fixed + extra_obstacles)
    view.ax._wide_map_view = view
    for label in fig.texts:
        if label.get_gid() == 'wide-locator':
            label.set_zorder(12)
            if view.locator is not None:
                left, bottom, width, height = position
                label.set_position((left + width if label.get_ha() == 'right' else left,
                                    bottom + height + .015))
    return view
