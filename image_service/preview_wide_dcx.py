"""DCX: complete received geometry, complete named areas, then a topic card.

The ordered fallback belongs to this unpublished renderer. Neither a map
failure nor a missing boundary turns a municipality into its parent prefecture.
"""

from dataclasses import replace

import numpy as np
from azarashi.reports import dcx
from matplotlib.patches import Ellipse as SymbolEllipse, PathPatch, Polygon
from matplotlib.path import Path

from image_service import ash_map, region_map
from image_service import preview_wide_layout as wide
from image_service import render as base
from image_service.preview_dcx_geometry import ellipse_ring
from image_service.preview_dcx_reports import Ellipse, Point, dcx_plan


MAP_CREDIT = '情報：みちびき DCX / 地形：Natural Earth'
AREA_CREDIT = '情報：みちびき DCX / 区域：気象庁（加工） / 地形：Natural Earth'
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
        rows.append(wide.Row('受信した対象区域', names))
    if mode == 'geometry':
        if any(isinstance(item, Ellipse) for item in plan.geometries):
            rows.append(wide.Row('地図の表示', ('通報の対象範囲',)))
        if any(isinstance(item, Point) for item in plan.geometries):
            rows.append(wide.Row('受信した地点', ('災害中心位置',)))
    wide.sidebar(fig, '受信した情報', rows,
                 footnote=None if mode == 'topic' else '詳細・指示内容は投稿本文を参照')


def _nearby_locator(items):
    """An overseas locator shows the surrounding region, not a fixed Japan."""
    lon, lat = items[0].lon, items[0].lat
    return (np.asarray(((lon - 25, float(base._my(max(-84., lat - 20)))),
                        (lon + 25, float(base._my(min(84., lat + 20)))))),)


def _geometry(fig, report, plan):
    reference = plan.geometries[0].lon
    rings, points = [], []
    for item in plan.geometries:
        if isinstance(item, Ellipse) and item.role == 'target':
            rings.append(ellipse_ring(item, reference=reference))
        elif isinstance(item, Point) and item.role == 'hazard_center':
            if not -85 < item.lat < 85 or not np.isfinite((item.lon, item.lat)).all():
                raise ValueError('Unprojectable DCX hazard position')
            points.append((item.lon + round((reference - item.lon) / 360) * 360,
                           float(base._my(item.lat))))
        else:
            raise ValueError('Unsupported DCX geometry role')
    sequences = (*rings, *((np.asarray(points),) if points else ()))
    if not sequences:
        raise ValueError('No DCX geometry')
    locator = _nearby_locator(plan.geometries) if isinstance(report, dcx.OutsideJapan) else ()
    view = wide.map_axes(fig, sequences, full=sequences, locator_context=locator)
    colour = _colour(report)
    for index, ring in enumerate(rings):
        view.ax.add_patch(Polygon(ring, closed=True, facecolor=colour + '24',
                                  edgecolor=colour, linewidth=2.3,
                                  linestyle='--' if report.camf.a1 == 3 else '-',
                                  zorder=5, gid=f'dcx-target-ellipse-{index}'))
    for index, point in enumerate(points):
        view.ax.scatter(*point, marker='x', s=170, linewidth=2.2, color=colour,
                        zorder=7, gid=f'dcx-hazard-point-{index}')
    scope = ('通報対象範囲・災害中心位置' if rings and points else
             '通報の対象範囲 / 災害の被害範囲ではありません' if rings else '受信した災害中心位置')
    wide.header(fig, report, CHANNELS[type(report)], scope, credit=MAP_CREDIT)
    _sidebar(fig, report, plan, mode='geometry')


def _area(fig, report, plan):
    paths, sequences = {}, []
    for area in plan.areas:
        if area.layer == 'prefectures':
            features = region_map._layer('prefectures').get(area.code)
        elif area.layer == 'municipalities':
            feature = ash_map._municipalities().get(area.code)
            features = (feature,) if feature else None
        else:
            raise ValueError('Unsupported DCX target area layer')
        if not features:
            raise ValueError('A received DCX target area is missing from the pinned map')
        for feature in features:
            parts = region_map._geometry(feature, 'prefectures', 139.)
            paths.setdefault(area.code, []).extend(region_map._polygon_path(part) for part in parts)
            sequences.extend(ring for part in parts for ring in part)
    if not sequences:
        raise ValueError('No DCX target areas')
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
    wide.header(fig, report, CHANNELS[type(report)], '受信した通報の対象区域', credit=AREA_CREDIT)
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
        raise ValueError('DCX topic title does not fit')
    for index, line in enumerate(lines):
        wide.text(fig, .375, .365 - index * .075, line, size,
                  ha='center', va='center', bold=True, gid='dcx-topic')
    _sidebar(fig, report, plan, mode='topic')


def draw_dcx(fig, reports):
    reports = tuple(reports)
    plan = dcx_plan(reports)
    report, = reports
    candidates = []
    if plan.geometries:
        candidates.append(('geometry', _geometry))
    if plan.areas:
        candidates.append(('area', _area))
    candidates.append(('topic', _topic))
    for mode, draw in candidates:
        try:
            draw(fig, report, plan)
            wide.validate(fig)
        except ValueError:
            fig.clear()
            if mode == 'topic':
                raise
        else:
            return replace(plan, scope_kind=mode)


def draw_topic(fig, reports):
    reports = tuple(reports)
    plan = dcx_plan(reports)
    _topic(fig, reports[0], plan)
    return replace(plan, scope_kind='topic')
