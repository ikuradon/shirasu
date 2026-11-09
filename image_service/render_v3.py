"""Version 3: weather regions, earthquake early warnings and volcano locations."""

import io

from azarashi.reports import dcr
from matplotlib import patheffects
from matplotlib.text import Text

from image_service import render as v1
from image_service import render_v2 as v2
from image_service import map_v3
from image_service import map_base_v3
from image_service.eew_map import eew_plan
from image_service.weather_map import weather_plan
from image_service.volcano_map import draw_volcano, verify_assets as verify_volcano_assets

ADAPTERS = {dcr.Weather: weather_plan, dcr.EarthquakeEarlyWarning: eew_plan}


def verify_assets():
    v2.verify_assets()
    map_v3.verify_assets()
    map_base_v3.verify_assets()
    verify_volcano_assets()
    return True


def _header(fig, report, plan):
    markers, details, training, _ = v1._metadata(report)
    if report.information_type_no == 1:
        markers += '【訂正】'
    if isinstance(report, dcr.Weather) and report.weather_warning_state_raw == 2:
        markers += '【解除】'
    if markers:
        v1._text(fig, .045, .958, markers, 21, v1.ALERT, bold=True)
    v1._text(fig, .042, .89 if markers else .925, plan.title, 38, bold=True)
    v1._text(fig, .045, .846 if markers else .88,
             v1._time(report.report_time, full=True) + ' 発表 / JST', 14, v1.MUTED)
    v1._text(fig, .045, .812 if markers else .846, details, 13,
             v1.ALERT if training else v1.MUTED)
    v1._text(fig, .955, .956, 'SHIRASU', 14, bold=True, ha='right')
    v1._text(fig, .955, .914, plan.subtitle, 12, v1.MUTED, ha='right')
    headline = getattr(plan, 'headline', '')
    if headline:
        if len(v1._wrap(headline, 34)) != 1:
            raise ValueError('Forecast headline does not fit')
        v1._text(fig, .955, .85, headline, 25, v1.ALERT, bold=True, ha='right')
    legend = []
    for label, color in plan.legend:
        legend.append((label, color))
    # The legend is measured as text and stays in the upper-right map overlay.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    x, y = .955, .812 if headline else .857
    for label, color in reversed(legend):
        text = '■ ' + label
        width, _, _ = renderer.get_text_width_height_descent(text, v1._font(12), False)
        if x - width / v1.WIDTH < .48:
            x, y = .955, y - .03
        if y < .80:
            raise ValueError('Legend does not fit')
        v1._text(fig, x, y, text, 12, color, ha='right')
        x -= (width + 22) / v1.WIDTH


def _footer(fig, plan):
    notes = v1._wrap(plan.note, 112)
    notices = v1._wrap('\n'.join(plan.notice_lines), 112)
    if len(notes) > 2 or len(notices) > 3:
        raise ValueError('Map notices do not fit')
    v1._text(fig, .045, .025, '情報・地理データ：気象庁（加工） / 受信：みちびき / 地形：Natural Earth',
             10, v1.MUTED)
    for index, line in enumerate(reversed(notes)):
        v1._text(fig, .045, .052 + index * .023, line, 11, v1.MUTED)
    for index, line in enumerate(reversed(notices)):
        v1._text(fig, .045, .10 + index * .023, line, 12, v1.ALERT)


def render(records):
    records = tuple(records)
    with v1._LOCK:
        verify_assets()
        v1._reset_assembly()
        fig = None
        try:
            reports = v1._decode(records)
            first = reports[0]
            if (type(first) not in (*ADAPTERS, dcr.Volcano)
                    or first.information_type_no == 2):
                return v2.render(records)
            try:
                fig = v1._figure()
                if type(first) is dcr.Volcano:
                    plan = draw_volcano(fig, reports)
                else:
                    plan = ADAPTERS[type(first)](reports)
                    map_v3.draw_map(fig, plan)
                _header(fig, first, plan)
                _footer(fig, plan)
            except ValueError:
                # Preserve the complete source bundle when a map cannot retain
                # every value or the geography is not supported.
                return v2.render(records)
            # Detailed coastlines remain visible around the labels, while a
            # narrow halo keeps them from cutting through individual glyphs.
            for label in fig.findobj(Text):
                label.set_path_effects([patheffects.withStroke(linewidth=1.2, foreground=v1.SEA)])
            output = io.BytesIO()
            fig.savefig(output, format='png', dpi=v1.DPI, facecolor=v1.SEA,
                        metadata={'Software': 'shirasu-image-v3'})
            return v1.RenderedImage(output.getvalue(), type(first).__name__)
        finally:
            if fig is not None:
                fig.clear()
            v1._reset_assembly()
