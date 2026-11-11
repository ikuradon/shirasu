"""A dedicated, unpublished cancellation card over an optional plain map.

The caller establishes geographic context before drawing this card. Body
values (forecast positions, levels, storm numbers) are deliberately not read
here: a cancellation never turns those values into a current warning.
"""

from datetime import datetime

from azarashi.reports import dcr

from image_service import preview_wide_layout as wide
from image_service import render as base


# Geographic names must leave the central message, header and footer clear.
# The existing bottom-right locator remains below the central message.
CANCEL_OBSTACLES = ((0., .815, 1., 1.), (.14, .32, .86, .78),
                    (0., 0., 1., .085), (0., .075, .54, .16))

_TITLES = {
    dcr.EarthquakeEarlyWarning: '緊急地震速報',
    dcr.Hypocenter: '震源情報',
    dcr.SeismicIntensity: '震度情報',
    dcr.NankaiTroughEarthquake: '南海トラフ地震関連情報',
    dcr.Tsunami: '津波情報',
    dcr.NorthwestPacificTsunami: '北西太平洋津波情報',
    dcr.Volcano: '火山情報',
    dcr.AshFall: '降灰予報',
    dcr.Weather: '気象情報',
    dcr.Flood: '指定河川洪水予報',
    dcr.Typhoon: '台風情報',
    dcr.Marine: '海上警報',
}
_BACKGROUNDS = {
    'none': None,
    'area': '背景：取消報に記載された地域',
    'reference': '背景：元情報と同じ表示範囲',
}
_SOURCE = '情報：気象庁 / 受信：みちびき'
_MAP_SOURCE = _SOURCE + ' / 区域：気象庁（加工） / 地形：Natural Earth'


def _report(reports):
    reports = tuple(reports)
    if not reports or not all(isinstance(report, dcr.Base) for report in reports):
        raise ValueError('Cancellation cards require DCR reports')
    first = reports[0]
    if any(report.information_type_no != 2 for report in reports):
        raise ValueError('Cancellation cards require cancellation reports')
    at = first.report_time
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError('Cancellation time must be timezone-aware')
    if first.report_classification_no not in (1, 2, 3, 7):
        raise ValueError('Unknown cancellation classification')
    if any(type(report) is not type(first)
           or report.report_time != at
           or report.report_classification_no != first.report_classification_no
           for report in reports[1:]):
        raise ValueError('Mixed cancellation announcements')
    return first


def validate(fig):
    """Check only this card's labels, without changing common map validation."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    labels = [label for label in fig.texts
              if (label.get_gid() or '').startswith('wide-cancel-')]
    for index, label in enumerate(labels):
        box = label.get_window_extent(renderer)
        if box.x0 < 0 or box.y0 < 0 or box.x1 > wide.WIDTH or box.y1 > wide.HEIGHT:
            raise ValueError('Cancellation text leaves the image')
        if any(box.overlaps(other.get_window_extent(renderer))
               for other in labels[index + 1:]):
            raise ValueError('Cancellation text overlaps')


def draw_card(fig, reports, *, background='none'):
    """Draw a cancellation prominently; never infer a former report or area.

    ``area`` means independently validated geography in the cancellation.
    ``reference`` is reserved for an association already established by the
    caller. A background must already be a plain map with no hazard layers.
    """
    if background not in _BACKGROUNDS:
        raise ValueError('Unknown cancellation background')
    first = _report(reports)
    if background == 'none' and fig.axes:
        raise ValueError('An unlocated cancellation card must not retain a map')
    if background != 'none' and not fig.axes:
        raise ValueError('A geographic cancellation card requires a plain map')

    if background != 'none':
        veil = wide._rect(fig, 0, 0, 1, 1, wide.SEA, alpha=.46, zorder=20)
        veil.set_gid('cancel-background-veil')

    def text(x, y, value, size, *, kind, **kwargs):
        return wide.text(fig, x, y, value, size, gid='wide-cancel-' + kind, **kwargs)

    if first.report_classification_no == 7:
        text(.04, .94, '【訓練・試験】', 23, kind='training', color=wide.ALERT,
             bold=True, va='center')
    text(.96, .94, 'SHIRASU', 14, kind='brand', color=wide.MUTED,
         ha='right', va='center')
    at = first.report_time.astimezone(base.JST)
    text(.04, .875, f'取消報 発表日時：{at:%Y/%m/%d %H:%M} JST', 17,
         kind='time', color=wide.MUTED, va='center')
    text(.5, .64, '取消', 104, kind='status', bold=True, ha='center', va='center')
    title = _TITLES.get(type(first), '防災気象情報')
    for size in (32, 30, 28):
        if wide._width(fig, title, size, True) <= wide.WIDTH * .70:
            break
    else:
        raise ValueError('Cancellation title does not fit')
    text(.5, .475, title, size, kind='category', bold=True, ha='center', va='center')
    text(.5, .37, 'この情報は取り消されました', 22, kind='description',
         ha='center', va='center')
    if _BACKGROUNDS[background] is not None:
        text(.04, .115, _BACKGROUNDS[background], 14, kind='context',
             color=wide.MUTED, va='center')
    text(.04, .065, '詳細は投稿本文を参照', 15, kind='footer',
         color=wide.MUTED, va='center')
    text(.022, .022, _SOURCE if background == 'none' else _MAP_SOURCE, 10,
         kind='credit', color=wide.MUTED, va='center')
    validate(fig)
