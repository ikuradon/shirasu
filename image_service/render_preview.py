"""Unpublished 16:9 renderer; never registered as a signed URL generation."""

import io

from azarashi.reports import dcr
from matplotlib import patheffects
from matplotlib.text import Text

from image_service import render as base
from image_service import render_v4 as published
from image_service import nwp_map, ash_map, flood_map
from image_service import preview_wide_layout as layout
from image_service import preview_wide_regions as regions
from image_service import preview_wide_points as points
from image_service import preview_wide_special as special

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
}


def verify_assets():
    published.verify_assets()
    nwp_map.verify_assets()
    ash_map.verify_assets()
    flood_map.verify_assets()
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
            try:
                if drawer is None or cancelled:
                    layout.summary(fig, reports)
                else:
                    drawer(fig, reports)
                layout.validate(fig)
            except ValueError:
                # Never create a plausible partial map by dropping values.
                fig.clear()
                layout.summary(fig, reports)
                layout.validate(fig)
            for label in fig.findobj(Text):
                if label.get_bbox_patch() is None:
                    label.set_path_effects([patheffects.withStroke(
                        linewidth=1.1, foreground=layout.SEA)])
            output = io.BytesIO()
            fig.savefig(output, format='png', dpi=layout.DPI, facecolor=layout.SEA,
                        metadata={'Software': 'shirasu-image-preview'})
            return base.RenderedImage(output.getvalue(), type(first).__name__)
        finally:
            if fig is not None:
                fig.clear()
            base._reset_assembly()
