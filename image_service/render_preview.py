"""Unpublished 16:9 renderer; never registered as a signed URL generation."""

import io
from dataclasses import dataclass

from azarashi.reports import dcr, dcx
from matplotlib import patheffects
from matplotlib.text import Text

from image_service import render as base
from image_service import render_v4 as published
from image_service import nwp_map, ash_map, flood_map, preview_dcx_areas
from image_service import preview_wide_layout as layout
from image_service import preview_wide_regions as regions
from image_service import preview_wide_points as points
from image_service import preview_wide_special as special
from image_service import preview_wide_dcx as dcx_images
from image_service.preview_diagnostics import Decision, error_reasons, reason, unique_reasons


@dataclass(frozen=True)
class RenderedPreview(base.RenderedImage):
    decision: Decision

DRAWERS = {
    dcr.Weather: regions.draw_weather,
    dcr.EarthquakeEarlyWarning: regions.draw_eew,
    dcr.Marine: regions.draw_marine,
    dcr.SeismicIntensity: regions.draw_seismic,
    dcr.Tsunami: regions.draw_tsunami,
    dcr.Typhoon: points.draw_typhoon,
    dcr.Hypocenter: points.draw_hypocenter,
    dcr.Volcano: points.draw_volcano,
    dcr.Flood: special.draw_flood,
    dcr.AshFall: special.draw_ash,
    dcr.NorthwestPacificTsunami: special.draw_nwp,
    dcx.LAlert: dcx_images.draw_dcx,
    dcx.JAlert: dcx_images.draw_dcx,
    dcx.MTInfo: dcx_images.draw_dcx,
    dcx.OutsideJapan: dcx_images.draw_dcx,
}


def verify_assets():
    published.verify_assets()
    nwp_map.verify_assets()
    ash_map.verify_assets()
    flood_map.verify_assets()
    preview_dcx_areas.verify_assets()
    return True


def render(records):
    records = tuple(records)
    with base._LOCK:
        verify_assets()
        base._reset_assembly()
        fig = None
        try:
            reports = base._decode(records)
            first = reports[0]
            fig = layout.figure()
            drawer = DRAWERS.get(type(first))
            _, _, _, cancelled = base._metadata(first)
            decision = Decision('map')
            phase = 'selection'
            try:
                if drawer is None or cancelled:
                    code = 'cancelled' if cancelled else 'unsupported_report'
                    decision = Decision('summary', (reason(code),))
                    layout.summary(fig, reports)
                else:
                    phase = 'render'
                    plan = drawer(fig, reports)
                    if isinstance(first, dcx.AlertBase):
                        decision = Decision(plan.scope_kind, unique_reasons(plan.reasons))
                    phase = 'layout'
                    layout.context_names(fig)
                phase = 'layout'
                layout.validate(fig)
            except ValueError as error:
                # Never create a plausible partial map by dropping values.
                reasons = unique_reasons((*decision.reasons, *error_reasons(error, stage=phase)))
                fig.clear()
                if isinstance(first, dcx.AlertBase):
                    plan = dcx_images.draw_topic(fig, reports, reasons=reasons)
                    decision = Decision('topic', unique_reasons(plan.reasons))
                else:
                    layout.summary(fig, reports)
                    decision = Decision('summary', reasons)
                layout.validate(fig)
            for label in fig.findobj(Text):
                if label.get_bbox_patch() is None:
                    label.set_path_effects([patheffects.withStroke(
                        linewidth=1.1, foreground=layout.SEA)])
            output = io.BytesIO()
            fig.savefig(output, format='png', dpi=layout.DPI, facecolor=layout.SEA,
                        metadata={'Software': 'shirasu-image-preview'})
            return RenderedPreview(output.getvalue(), type(first).__name__, decision)
        finally:
            if fig is not None:
                fig.clear()
            base._reset_assembly()
