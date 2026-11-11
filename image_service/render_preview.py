"""Unpublished 16:9 renderer; never registered as a signed URL generation."""

import io
from dataclasses import dataclass

from azarashi.reports import dcr, dcx
from matplotlib import patheffects
from matplotlib.text import Text
from nankai_image import header as nankai_header, is_nankai_entry

from image_service import render as base
from image_service import render_v4 as published
from image_service import nwp_map, ash_map, flood_map, preview_dcx_areas
from image_service import preview_wide_layout as layout
from image_service import preview_wide_regions as regions
from image_service import preview_wide_points as points
from image_service import preview_wide_special as special
from image_service import preview_wide_dcx as dcx_images
from image_service import preview_cancellation as cancellation
from image_service import preview_nankai as nankai
from image_service.preview_diagnostics import Decision, error_reasons, reason, unique_reasons


@dataclass(frozen=True)
class RenderedPreview(base.RenderedImage):
    decision: Decision


@dataclass(frozen=True)
class RenderedWebP:
    data: bytes
    category: str
    decision: Decision
    mime: str = 'image/webp'

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


def _decorate(fig):
    for label in fig.findobj(Text):
        if label.get_bbox_patch() is None:
            label.set_path_effects([patheffects.withStroke(
                linewidth=1.1, foreground=layout.SEA)])


def _finish(fig, category, decision):
    _decorate(fig)
    output = io.BytesIO()
    fig.savefig(output, format='png', dpi=layout.DPI, facecolor=layout.SEA,
                metadata={'Software': 'shirasu-image-preview'})
    return RenderedPreview(output.getvalue(), category, decision)


def verify_webp_encoder():
    from image_service.webp_w1 import verify_encoder
    return verify_encoder()


def _finish_webp(fig, category, decision):
    from image_service.webp_w1 import encode
    _decorate(fig)
    return RenderedWebP(encode(fig, dpi=layout.DPI, facecolor=layout.SEA), category, decision)


def render(records, *, cancel_reference=None):
    return _render(records, cancel_reference=cancel_reference, finish=_finish)


def render_webp(records, *, cancel_reference=None):
    """Encode the same scene using the frozen w1 profile, without a PNG step."""
    verify_webp_encoder()
    return _render(records, cancel_reference=cancel_reference, finish=_finish_webp)


def _render(records, *, cancel_reference, finish):
    records = tuple(records)
    with base._LOCK:
        verify_assets()
        if any(is_nankai_entry(entry) for entry in records):
            # A selected original page proves the headline. Do not invoke or
            # reset azarashi's process-wide multipart text assembler here.
            if len(records) != 1:
                raise ValueError('Nankai headline images require one representative page')
            if cancel_reference is not None:
                raise ValueError('Nankai headline images do not accept cancellation references')
            header = nankai_header(records[0])
            fig = layout.figure()
            try:
                nankai.draw_card(fig, header)
                nankai.validate(fig)
                decision = Decision('topic', (reason('cancelled'),) if header.information == 2 else ())
                return finish(fig, 'NankaiTroughEarthquake', decision)
            finally:
                fig.clear()
        base._reset_assembly()
        fig = None
        try:
            reports = base._decode(records)
            first = reports[0]
            fig = layout.figure()
            drawer = DRAWERS.get(type(first))
            _, _, _, cancelled = base._metadata(first)
            if cancel_reference is not None and not cancelled:
                raise ValueError('A cancellation reference requires a cancellation report')
            decision = Decision('map')
            phase = 'selection'
            try:
                if cancelled:
                    decision = cancellation.draw(fig, reports, DRAWERS, cancel_reference=cancel_reference)
                elif drawer is None:
                    decision = Decision('summary', (reason('unsupported_report'),))
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
                if cancelled:
                    cancellation.card.draw_card(fig, reports)
                    decision = Decision('summary', unique_reasons((reason('cancelled'), *reasons)))
                elif isinstance(first, dcx.AlertBase):
                    plan = dcx_images.draw_topic(fig, reports, reasons=reasons)
                    decision = Decision('topic', unique_reasons(plan.reasons))
                else:
                    layout.summary(fig, reports)
                    decision = Decision('summary', reasons)
                layout.validate(fig)
            return finish(fig, type(first).__name__, decision)
        finally:
            if fig is not None:
                fig.clear()
            base._reset_assembly()
