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
from image_service.nwp_reports import nwp_plan
from image_service.ash_reports import ash_plan


def verify_assets():
    published.verify_assets()
    nwp_map.verify_assets()
    ash_map.verify_assets()
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


def render(records):
    records = tuple(records)
    with base._LOCK:
        verify_assets()
        base._reset_assembly()
        fig = None
        try:
            reports = base._decode(records)
            first = reports[0]
            if type(first) not in (dcr.NorthwestPacificTsunami, dcr.AshFall):
                return published.render(records)
            try:
                if type(first) is dcr.AshFall:
                    plan = ash_plan(reports)
                    fig = base._figure()
                    ash_map.draw_map(fig, plan)
                    render_v3._header(fig, first, plan)
                else:
                    plan = nwp_plan(reports)
                    fig = base._figure()
                    nwp_map.draw_map(fig, plan)
                    _header(fig, first, plan)
                render_v3._footer(fig, plan)
            except ValueError:
                # Keep the complete bundle in the published summary if a
                # geographic or semantic value cannot be represented faithfully.
                return published.render(records)
            for label in fig.findobj(Text):
                if not (label.get_gid() or '').startswith('ash-hazard-'):
                    label.set_path_effects([patheffects.withStroke(linewidth=1.2, foreground=base.SEA)])
            output = io.BytesIO()
            fig.savefig(output, format='png', dpi=base.DPI, facecolor=base.SEA,
                        metadata={'Software': 'shirasu-image-preview'})
            return base.RenderedImage(output.getvalue(), type(first).__name__)
        finally:
            if fig is not None:
                fig.clear()
            base._reset_assembly()
