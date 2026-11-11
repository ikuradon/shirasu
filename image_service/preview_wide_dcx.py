"""DCX: complete received geometry, complete named areas, then a topic card.

The ordered fallback belongs to this unpublished renderer. Neither a map
failure nor a missing boundary turns a municipality into its parent prefecture.
"""

from dataclasses import replace

import numpy as np
from azarashi.reports import dcx
from matplotlib.patches import Ellipse as SymbolEllipse, PathPatch, Polygon
from matplotlib.path import Path

from image_service import preview_dcx_areas, region_map
from image_service import preview_geometry_cache as cached_geometry
from image_service import preview_wide_layout as wide
from image_service import render as base
from image_service.preview_dcx_geometry import ellipse_ring, secondary_ellipse_ring
from image_service.preview_dcx_reports import Ellipse, Point, SecondaryEllipse, dcx_plan
from image_service.preview_diagnostics import PreviewFallback, error_reason, unique_reasons


MAP_CREDIT = '情報：みちびき DCX / 地形：Natural Earth'
AREA_CREDIT = '情報：みちびき DCX / 区域：気象庁（加工） / 地形：Natural Earth'
SECONDARY_COLOUR = '#d1b6f6'
EVACUATION_COLOUR = '#ffd166'
LEAVE_COLOUR = '#f5a66b'
CHANNELS = {dcx.LAlert: 'Lアラート', dcx.JAlert: 'Jアラート',
            dcx.MTInfo: '自治体からの情報', dcx.OutsideJapan: '海外の災害情報'}


def _colour(report):
    # CAMF severity is not a Japanese meteorological warning colour scale.
    return '#8ca7ad' if report.camf.a1 == 3 else '#77cbd9'


def _sidebar(fig, report, plan, *, mode):
    rows = [wide.Row('通報区分', (plan.status,))]
    if mode != 'topic':
        rows.append(wide.Row('災害種別', (plan.headline,)))
    country = '日本' if report.camf.a2 == 111 else str(report.a2_country_region_name)
    rows.append(wide.Row('国・地域', (country,)))
    severity = {1: 'Moderate', 2: 'Severe', 3: 'Extreme'}.get(report.camf.a5)
    if severity is not None:
        rows.append(wide.Row('重大度（通報値）', (severity,)))
    if mode == 'area':
        areas = plan.areas
        if {area.code for area in areas if area.layer == 'prefectures'} == set(range(1, 48)):
            names = ('全国（47都道府県）',)
        elif len(areas) <= 5:
            names = tuple(area.name for area in areas)
        else:
            names = (f'{len(areas)}区域', '区域名の一覧は投稿本文を参照')
        rows.append(wide.Row('通知対象（実線）', names, color=_colour(report))
                    if plan.additional_ellipse else wide.Row('受信した対象区域', names))
    if mode == 'geometry':
        if any(isinstance(item, SecondaryEllipse) for item in plan.geometries):
            evacuation = any(isinstance(item, SecondaryEllipse) and item.role == 'evacuation_destination'
                             for item in plan.geometries)
            rows.extend((wide.Row('主範囲（実線）', color=_colour(report)),
                         wide.Row('避難先の指定（破線）', ('通報で指定された範囲',), color=EVACUATION_COLOUR)
                         if evacuation else
                         wide.Row('第2範囲（破線）', ('個別指示は投稿本文へ',), color=SECONDARY_COLOUR)))
        elif any(isinstance(item, Ellipse) for item in plan.geometries):
            rows.append(wide.Row('通知対象（実線）', color=_colour(report))
                        if plan.additional_ellipse else
                        wide.Row('地図の表示', ('通報の対象範囲',)))
        if any(isinstance(item, Point) for item in plan.geometries):
            rows.append(wide.Row('受信した地点', ('災害中心位置',)))
    if mode != 'topic' and plan.additional_ellipse:
        leave = plan.additional_ellipse.role == 'evacuate_from'
        rows.append(wide.Row('追加範囲（破線）',
                             ('範囲から離れる指示' if leave else '範囲へ向かう指示',),
                             color=LEAVE_COLOUR if leave else EVACUATION_COLOUR))
    footnote = None if mode == 'topic' else '詳細・指示内容は投稿本文を参照'
    if mode == 'geometry' and any(isinstance(item, SecondaryEllipse) for item in plan.geometries):
        footnote = '重なる区域は主範囲の指示を優先\n詳細・指示内容は投稿本文を参照'
    wide.sidebar(fig, '受信した情報', rows, footnote=footnote)


def _nearby_locator(items):
    """An overseas locator shows the surrounding region, not a fixed Japan."""
    lon, lat = items[0].lon, items[0].lat
    return (np.asarray(((lon - 25, float(base._my(max(-84., lat - 20)))),
                        (lon + 25, float(base._my(min(84., lat + 20)))))),)


def _additional_ring(plan, reference):
    item = plan.additional_ellipse
    if item is None:
        return None
    if not isinstance(item, Ellipse) or item.role not in ('evacuate_from', 'evacuate_to'):
        raise PreviewFallback('unsupported_geometry', stage='geometry', detail='未対応の追加避難範囲の役割',
                              technical='Unsupported DCX additional evacuation ellipse')
    if isinstance(item.lat, (int, float)) and 85 <= abs(item.lat) <= 90:
        # These are valid transmitted latitudes outside this map projection.
        # ellipse_ring's combined axis/projection error cannot distinguish
        # them from malformed axes, so state the known limitation here.
        raise PreviewFallback('projection_unavailable', stage='geometry', detail='EX3-EX7: 追加範囲の中心緯度',
                              technical='Unprojectable MTInfo additional ellipse latitude')
    # EX3-EX7 define an independent centre and ENU frame. A B3 transform
    # about the primary ellipse would change the transmitted region.
    try:
        return ellipse_ring(item, reference=reference)
    except ValueError as error:
        # Its failure remains a geometry issue even when the notification
        # target is a municipality rather than the primary ellipse.
        failure = error_reason(error, stage='geometry')
        raise PreviewFallback(failure.code, stage=failure.stage, detail=failure.detail,
                              technical=str(error)) from error


def _draw_additional(view, plan, ring):
    if ring is not None:
        colour = LEAVE_COLOUR if plan.additional_ellipse.role == 'evacuate_from' else EVACUATION_COLOUR
        # A destination is a received instruction, not a guarantee of safety.
        # Keep both full boundaries without inventing routes or mixed fills.
        view.ax.add_patch(Polygon(ring, closed=True, facecolor='none', edgecolor=colour,
                                  linewidth=2.3, linestyle='--', zorder=5.2,
                                  gid='dcx-additional-ellipse'))


def _geometry(fig, report, plan):
    reference = plan.geometries[0].lon
    rings, points, secondaries = [], [], []
    for item in plan.geometries:
        if isinstance(item, Ellipse) and item.role == 'target':
            rings.append(ellipse_ring(item, reference=reference))
        elif isinstance(item, SecondaryEllipse) and item.role in ('secondary_target', 'evacuation_destination'):
            secondaries.append((item, secondary_ellipse_ring(item, reference=reference)))
        elif isinstance(item, Point) and item.role == 'hazard_center':
            if not -85 < item.lat < 85 or not np.isfinite((item.lon, item.lat)).all():
                raise PreviewFallback('projection_unavailable', stage='geometry', detail='災害中心位置',
                                      technical='Unprojectable DCX hazard position')
            points.append((item.lon + round((reference - item.lon) / 360) * 360,
                           float(base._my(item.lat))))
        else:
            raise PreviewFallback('unsupported_geometry', stage='geometry', detail='未対応の位置・範囲の役割',
                                  technical='Unsupported DCX geometry role')
    sequences = (*rings, *(ring for _, ring in secondaries), *((np.asarray(points),) if points else ()))
    if not sequences:
        raise PreviewFallback('no_geometry', stage='geometry', technical='No DCX geometry')
    additional = _additional_ring(plan, reference)
    if additional is not None:
        sequences = (*sequences, additional)
    locator = _nearby_locator(plan.geometries) if isinstance(report, dcx.OutsideJapan) else ()
    view = wide.map_axes(fig, sequences, full=sequences, locator_context=locator)
    colour = _colour(report)
    for index, (item, ring) in enumerate(secondaries):
        # Do not blend fills into a third apparent warning level in the
        # intersection. The main range's instructions take priority there.
        view.ax.add_patch(Polygon(ring, closed=True, facecolor='none',
                                  edgecolor=EVACUATION_COLOUR if item.role == 'evacuation_destination' else SECONDARY_COLOUR,
                                  linewidth=2.3,
                                  linestyle='--', zorder=4.9, gid=f'dcx-secondary-ellipse-{index}'))
    for index, ring in enumerate(rings):
        view.ax.add_patch(Polygon(ring, closed=True, facecolor=colour + '24',
                                  edgecolor=colour, linewidth=2.3,
                                  linestyle='--' if report.camf.a1 == 3 and not secondaries and additional is None else '-',
                                  zorder=5, gid=f'dcx-target-ellipse-{index}'))
    _draw_additional(view, plan, additional)
    for index, point in enumerate(points):
        view.ax.scatter(*point, marker='x', s=170, linewidth=2.2, color=colour,
                        zorder=7, gid=f'dcx-hazard-point-{index}')
    scope = ('通報対象範囲・災害中心位置' if rings and points else
             '通報の対象範囲 / 災害の被害範囲ではありません' if rings else '受信した災害中心位置')
    if secondaries:
        scope = ('通報の主範囲・避難先の指定'
                 if any(item.role == 'evacuation_destination' for item, _ in secondaries)
                 else '通報の主範囲・第2範囲 / 被害範囲ではありません')
    if additional is not None:
        scope = '通知対象・追加の避難指示範囲'
    wide.header(fig, report, CHANNELS[type(report)], scope, credit=MAP_CREDIT)
    _sidebar(fig, report, plan, mode='geometry')


def _area(fig, report, plan):
    paths, sequences = {}, []
    for area in plan.areas:
        if area.layer == 'prefectures':
            features = region_map._layer('prefectures').get(area.code)
        elif area.layer == 'municipalities':
            feature = preview_dcx_areas.municipality(area.code)
            features = (feature,) if feature else None
        else:
            raise PreviewFallback('unknown_area', stage='area', detail='未対応の区域レイヤー',
                                  technical='Unsupported DCX target area layer')
        if not features:
            detail = (f'{area.layer}: code={area.code}'
                      if type(area.code) is int and 0 <= area.code < 10_000_000 else area.layer)
            raise PreviewFallback('missing_boundary', stage='area', detail=detail,
                                  technical='A received DCX target area is missing from the pinned map')
        for feature in features:
            parts = cached_geometry.geometry(feature, 'prefectures', 139.)
            # Compile each feature before visiting the next one, preserving
            # which invalid boundary is reported first. Only the final cheap
            # concatenation across features remains specific to this render.
            paths.setdefault(area.code, []).append(cached_geometry.compound_path(parts))
            sequences.extend(ring for part in parts for ring in part)
    if not sequences:
        raise PreviewFallback('no_area', stage='area', technical='No DCX target areas')
    additional = _additional_ring(plan, 139.)
    if additional is not None:
        sequences.append(additional)
    # Every component belongs to the received scope. Fit the complete union,
    # including remote islands, instead of silently showing its largest part.
    view = wide.map_axes(fig, sequences, full=sequences)
    colour = _colour(report)
    # Japan contains over 114,000 island polygons. Batch by received region,
    # retaining every subpath and hole without allocating one artist per island.
    for code, parts in paths.items():
        view.ax.add_patch(PathPatch(Path.make_compound_path(*parts),
                                   facecolor=colour + '24', edgecolor=colour,
                                   linewidth=1.4, zorder=5, gid=f'region-area-dcx-{code}'))
    _draw_additional(view, plan, additional)
    scope = '通知対象・追加の避難指示範囲' if additional is not None else '受信した通報の対象区域'
    wide.header(fig, report, CHANNELS[type(report)], scope, credit=AREA_CREDIT)
    _sidebar(fig, report, plan, mode='area')


def _topic(fig, report, plan):
    wide.header(fig, report, CHANNELS[type(report)], '詳細・指示内容は投稿本文を参照',
                credit='情報：みちびき DCX')
    colour = _colour(report)
    centre = (.375, .54)
    # A fixed information symbol carries no geographic or severity meaning.
    fig.add_artist(SymbolEllipse(centre, .10, .10 * wide.WIDTH / wide.HEIGHT, transform=fig.transFigure,
                          facecolor='none', edgecolor=colour, linewidth=2.2,
                          gid='dcx-topic-symbol'))
    wide.text(fig, *centre, 'i', 49, colour, ha='center', va='center', bold=True)
    for size in (48, 42, 36, 30, 25, 21):
        lines = wide._wrap(fig, plan.headline, size, wide.WIDTH * .63, True)
        if len(lines) <= 2:
            break
    else:
        raise PreviewFallback('layout_unavailable', stage='layout', detail='DCXトピック見出し',
                              technical='DCX topic title does not fit')
    for index, line in enumerate(lines):
        wide.text(fig, .375, .365 - index * .075, line, size,
                  ha='center', va='center', bold=True, gid='dcx-topic')
    _sidebar(fig, report, plan, mode='topic')


def draw_dcx(fig, reports):
    reports = tuple(reports)
    plan = dcx_plan(reports)
    report, = reports
    # Record only decisions needed to reach the selected mode. For example,
    # an absent administrative fallback is irrelevant when geometry succeeds.
    reasons = [item for item in plan.reasons if item.stage == 'selection']
    candidates = (('geometry', plan.geometries, _geometry),
                  ('area', plan.areas, _area), ('topic', True, _topic))
    for mode, available, draw in candidates:
        if not available:
            reasons.extend(item for item in plan.reasons if item.stage == mode)
            continue
        stage = mode if mode != 'topic' else 'render'
        try:
            draw(fig, report, plan)
            stage = 'layout'
            wide.validate(fig)
        except ValueError as error:
            reasons.append(error_reason(error, stage=stage))
            fig.clear()
            if mode == 'topic':
                # The outer renderer may retry a clean topic card. Preserve
                # every failed candidate even though this drawer cannot return
                # a plan; arbitrary exception text stays technical-only.
                last = reasons[-1]
                raise PreviewFallback(last.code, stage=last.stage, detail=last.detail,
                                      previous_reasons=reasons[:-1], technical=str(error)) from error
        else:
            return replace(plan, scope_kind=mode, reasons=unique_reasons(reasons))


def draw_topic(fig, reports, *, reasons=()):
    reports = tuple(reports)
    plan = dcx_plan(reports)
    _topic(fig, reports[0], plan)
    return replace(plan, scope_kind='topic', reasons=unique_reasons((*plan.reasons, *reasons)))
