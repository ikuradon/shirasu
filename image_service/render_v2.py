"""Version 2: regional warning maps, retaining the immutable v1 renderer."""

import io

from azarashi.reports import dcr

from image_service import render as v1
from image_service.marine_regions import marine_map
from image_service.regional_reports import seismic_intensity_map, tsunami_map
from image_service.region_map import draw_map, verify_assets as verify_region_assets

ADAPTERS = {dcr.Marine: marine_map, dcr.SeismicIntensity: seismic_intensity_map,
            dcr.Tsunami: tsunami_map}


def verify_assets():
    v1.verify_assets()
    verify_region_assets()
    return True


def _header(fig, report, plan):
    markers, details, training, _ = v1._metadata(report)
    if report.information_type_no == 1:
        markers += '【訂正】'
    if markers:
        v1._text(fig, .045, .958, markers, 21, v1.ALERT, bold=True)
    v1._text(fig, .042, .89 if markers else .925, plan.title, 38, bold=True)
    v1._text(fig, .045, .846 if markers else .88,
             v1._time(report.report_time, full=True) + ' 発表 / JST', 14, v1.MUTED)
    v1._text(fig, .045, .812 if markers else .846, details, 13,
             v1.ALERT if training else v1.MUTED)
    v1._text(fig, .955, .956, 'SHIRASU', 14, bold=True, ha='right')
    v1._text(fig, .955, .914, plan.subtitle, 12, v1.MUTED, ha='right')
    legend = []
    for label, color in plan.legend:
        legend.append((label, color))
    # The legend is measured as text and stays in the upper-right map overlay.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    x, y = .955, .857
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
    v1._text(fig, .045, .025, '情報・区域：気象庁（区域データを加工） / 受信：みちびき / 地形：Natural Earth',
             10, v1.MUTED)
    for index, line in enumerate(reversed(notes)):
        v1._text(fig, .045, .052 + index * .023, line, 11, v1.MUTED)
    for index, line in enumerate(reversed(notices)):
        v1._text(fig, .045, .10 + index * .023, line, 12, v1.ALERT)


def render(records):
    records = tuple(records)
    # v1 and v2 share the same pinned decoder and Matplotlib process. Use the
    # same lock so multipart state and font rendering never cross requests.
    with v1._LOCK:
        verify_assets()
        v1._reset_assembly()
        fig = None
        try:
            reports = v1._decode(records)
            first = reports[0]
            adapter = ADAPTERS.get(type(first))
            if adapter is None or first.information_type_no == 2:
                return v1.render(records)
            try:
                plan = adapter(reports)
                fig = v1._figure()
                draw_map(fig, plan)
                _header(fig, first, plan)
                _footer(fig, plan)
            except ValueError:
                # Unknown regions, conflicting overlaps or unreadable layouts
                # must not produce a misleading partial map.
                return v1.render(records)
            output = io.BytesIO()
            fig.savefig(output, format='png', dpi=v1.DPI, facecolor=v1.SEA,
                        metadata={'Software': 'shirasu-image-v2'})
            return v1.RenderedImage(output.getvalue(), type(first).__name__)
        finally:
            if fig is not None:
                fig.clear()
            v1._reset_assembly()
