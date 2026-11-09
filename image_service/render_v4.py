"""Version 4: per-information weather colours; older categories stay identical."""

import io

from azarashi.reports import dcr
from matplotlib import patheffects
from matplotlib.text import Text

from image_service import render as v1
from image_service import render_v2 as v2
from image_service import render_v3 as v3
from image_service import map_v4
from image_service.weather_map_v4 import weather_plan


def verify_assets():
    return v3.verify_assets()


def render(records):
    records = tuple(records)
    with v1._LOCK:
        verify_assets()
        v1._reset_assembly()
        fig = None
        try:
            reports = v1._decode(records)
            first = reports[0]
            if type(first) is not dcr.Weather or first.information_type_no == 2:
                return v3.render(records)
            try:
                plan = weather_plan(reports)
                fig = v1._figure()
                map_v4.draw_map(fig, plan)
                v3._header(fig, first, plan)
                v3._footer(fig, plan)
            except ValueError:
                # Preserve the complete report as text if any value or label
                # cannot be represented; never revert to v3's category colours.
                return v2.render(records)
            for label in fig.findobj(Text):
                if not (label.get_gid() or '').startswith('weather-hazard-'):
                    label.set_path_effects([patheffects.withStroke(linewidth=1.2, foreground=v1.SEA)])
            output = io.BytesIO()
            fig.savefig(output, format='png', dpi=v1.DPI, facecolor=v1.SEA,
                        metadata={'Software': 'shirasu-image-v4'})
            return v1.RenderedImage(output.getvalue(), type(first).__name__)
        finally:
            if fig is not None:
                fig.clear()
            v1._reset_assembly()
