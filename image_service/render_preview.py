"""Unpublished renderer for offline review; never registered as a signed URL."""

import io
from datetime import timezone

from azarashi.reports import dcr
from matplotlib import patheffects
from matplotlib.text import Text

from image_service import render as base
from image_service import render_v3
from image_service import render_v4 as published
from image_service import nwp_map
from image_service import ash_map
from image_service import flood_map
from image_service import preview_regions, preview_points
from image_service.nwp_reports import nwp_plan
from image_service.ash_reports import ash_plan
from image_service.flood_reports import flood_plan
from image_service.weather_map_v4 import weather_plan
from image_service.eew_map import eew_plan
from image_service.marine_regions import marine_map
from image_service.regional_reports import seismic_intensity_map, tsunami_map

REGIONAL_PLANS = {
    dcr.Weather: weather_plan,
    dcr.EarthquakeEarlyWarning: eew_plan,
    dcr.Marine: marine_map,
    dcr.SeismicIntensity: seismic_intensity_map,
    dcr.Tsunami: tsunami_map,
}
POINT_DRAWERS = {dcr.Typhoon: preview_points.draw_typhoon,
                 dcr.Hypocenter: preview_points.draw_hypocenter}


def verify_assets():
    published.verify_assets()
    nwp_map.verify_assets()
    ash_map.verify_assets()
    flood_map.verify_assets()
    return True


def _header(fig, report, plan):
    markers, details, training, _ = base._metadata(report)
    if markers:
        base._text(fig, .045, .958, markers, 21, base.ALERT, bold=True)
    base._text(fig, .042, .89 if markers else .925, plan.title, 34, bold=True)
    issued = report.report_time.astimezone(timezone.utc).strftime('%Y/%m/%d %H:%M')
    base._text(fig, .045, .846 if markers else .88, issued + ' 発表 / UTC', 14, base.MUTED)
    base._text(fig, .045, .812 if markers else .846, details, 13,
               base.ALERT if training else base.MUTED)
    base._text(fig, .955, .956, 'SHIRASU', 14, bold=True, ha='right')
    base._text(fig, .955, .914, 'NWPTAC / 予測地点', 12, base.MUTED, ha='right')
    base._text(fig, .955, .812 if markers else .846, plan.subtitle, 12, base.MUTED, ha='right')


def _flood_footer(fig, plan):
    notes = base._wrap(plan.note, 112)
    notices = base._wrap('\n'.join(plan.notice_lines), 112)
    if len(notes) > 2 or len(notices) > 3:
        raise ValueError('Flood map notices do not fit')
    base._text(fig, .045, .025,
               '流路：国土数値情報を基に気象庁加工／県境：気象庁／画像：Shirasu／受信：みちびき／地形：Natural Earth',
               10, base.MUTED)
    for index, line in enumerate(reversed(notes)):
        base._text(fig, .045, .052 + index * .023, line, 11, base.MUTED)
    for index, line in enumerate(reversed(notices)):
        base._text(fig, .045, .10 + index * .023, line, 12, base.ALERT)


def render(records):
    records = tuple(records)
    with base._LOCK:
        verify_assets()
        base._reset_assembly()
        fig = None
        try:
            reports = base._decode(records)
            first = reports[0]
            if (type(first) not in (*REGIONAL_PLANS, *POINT_DRAWERS, dcr.Volcano,
                                   dcr.NorthwestPacificTsunami, dcr.AshFall, dcr.Flood)
                    or first.information_type_no == 2):
                return published.render(records)
            try:
                if type(first) in POINT_DRAWERS:
                    fig = base._figure()
                    POINT_DRAWERS[type(first)](fig, reports)
                elif type(first) is dcr.Volcano:
                    fig = base._figure()
                    plan = preview_points.draw_volcano(fig, reports)
                    render_v3._header(fig, first, plan)
                elif type(first) in REGIONAL_PLANS:
                    plan = REGIONAL_PLANS[type(first)](reports)
                    fig = base._figure()
                    preview_regions.draw_map(fig, plan)
                    render_v3._header(fig, first, plan)
                elif type(first) is dcr.Flood:
                    plan = flood_plan(reports)
                    fig = base._figure()
                    flood_map.draw_map(fig, plan)
                    render_v3._header(fig, first, plan)
                elif type(first) is dcr.AshFall:
                    plan = ash_plan(reports)
                    fig = base._figure()
                    ash_map.draw_map(fig, plan)
                    render_v3._header(fig, first, plan)
                else:
                    plan = nwp_plan(reports)
                    fig = base._figure()
                    nwp_map.draw_map(fig, plan)
                    _header(fig, first, plan)
                if type(first) in POINT_DRAWERS:
                    pass  # These adapters retain the original point-map header/footer.
                elif type(first) is dcr.Flood:
                    _flood_footer(fig, plan)
                else:
                    render_v3._footer(fig, plan)
            except ValueError:
                # Keep the complete bundle in the published summary if a
                # geographic or semantic value cannot be represented faithfully.
                if type(first) in (*REGIONAL_PLANS, *POINT_DRAWERS, dcr.Volcano):
                    if fig is None:
                        fig = base._figure()
                    fig.clear()
                    base._summary(fig, reports)
                else:
                    return published.render(records)
            for label in fig.findobj(Text):
                if not (label.get_gid() or '').startswith(('ash-hazard-', 'flood-hazard-', 'weather-hazard-')):
                    label.set_path_effects([patheffects.withStroke(linewidth=1.2, foreground=base.SEA)])
            output = io.BytesIO()
            fig.savefig(output, format='png', dpi=base.DPI, facecolor=base.SEA,
                        metadata={'Software': 'shirasu-image-preview'})
            return base.RenderedImage(output.getvalue(), type(first).__name__)
        finally:
            if fig is not None:
                fig.clear()
            base._reset_assembly()
