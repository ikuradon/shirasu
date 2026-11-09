"""Semantic adapters for v2 regional maps; no geography or drawing side effects.

Codes and decoded wording come from the pinned azarashi / IS-QZSS-DCR tables.
A map must preserve every distinct received value. Unknown geographic or warning
codes raise ValueError so the caller can show the complete text instead.

QZSS's 2026-04-28 notice defines the release-message midnight placeholder:
https://qzss.go.jp/info/information/dcr_260428.html
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.prefecture import prefecture
from azarashi.definitions.qzss.dcr.seismic_intensity import seismic_intensity
from azarashi.definitions.qzss.dcr.tsunami_forecast_region import tsunami_forecast_region
from azarashi.definitions.qzss.dcr.tsunami_height import tsunami_height
from azarashi.definitions.qzss.dcr.tsunami_warning_code import tsunami_warning_code

JST = timezone(timedelta(hours=9))
INTENSITY_COLORS = {
    1: '#8cb9dc', 2: '#f2df86', 3: '#ffb65b', 4: '#f58048',
    5: '#ec5561', 6: '#c777c9', 7: '#ab82e6',
}
TSUNAMI_COLORS = {1: '#8cb9dc', 2: '#6ce2ec', 3: '#ff6970', 4: '#ca91e8', 5: '#ca91e8'}


@dataclass(frozen=True)
class RegionNotice:
    code: int
    title: str
    lines: tuple[str, ...]
    color: str


@dataclass(frozen=True)
class RegionMap:
    layer: str
    title: str
    subtitle: str
    notices: tuple[RegionNotice, ...]
    note: str
    legend: tuple[tuple[str, str], ...] = ()  # (label, color)
    notice_lines: tuple[str, ...] = ()


def _reports(reports, expected):
    reports = tuple(reports)
    if not 1 <= len(reports) <= 32 or any(type(report) is not expected for report in reports):
        raise ValueError('Unsupported regional report bundle')
    first = reports[0]
    key = lambda report: (report.report_time, report.report_classification_no, report.information_type_no)
    if (first.report_classification_no not in (1, 2, 3, 7)
            or first.information_type_no not in (0, 1)
            or any(key(report) != key(first) for report in reports)):
        # Cancellations use the renderer's prominent cancellation card, not a map.
        raise ValueError('Mixed or cancelled regional announcement')
    return reports


def _date(value, reference=None):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('Missing regional timestamp')
    local = value.astimezone(JST)
    include_year = reference is None or local.year != reference.astimezone(JST).year
    return local.strftime('%Y/%m/%d %H:%M JST' if include_year else '%m/%d %H:%M JST')


def seismic_intensity_map(reports):
    """Show the received prefectural values, never an inferred intensity field."""
    reports = _reports(reports, dcr.SeismicIntensity)
    first = reports[0]
    occurrence = (first.occurrence_time_of_earthquake,
                  first.occurrence_time_of_earthquake_raw)
    values = {}
    for report in reports:
        if (report.occurrence_time_of_earthquake,
                report.occurrence_time_of_earthquake_raw) != occurrence:
            raise ValueError('Mixed earthquake occurrence times')
        for code, intensity in zip(report.prefectures_raw, report.seismic_intensities_raw, strict=True):
            if type(code) is not int or code not in prefecture or intensity not in INTENSITY_COLORS:
                raise ValueError('Unknown prefecture or seismic intensity')
            values.setdefault(code, set()).add(intensity)
    if not values:
        raise ValueError('No received seismic intensity regions')
    notices = tuple(RegionNotice(code, prefecture[code],
        ('受信震度 ' + ' / '.join(seismic_intensity[value] for value in sorted(intensities, reverse=True)),),
        INTENSITY_COLORS[max(intensities)]) for code, intensities in sorted(values.items()))
    used = sorted({value for intensities in values.values() for value in intensities})
    when = ('地震発生 ' + _date(occurrence[0]) if occurrence[0] is not None else '地震発生時刻：不明')
    return RegionMap('prefectures', '震度情報', '都道府県別の受信震度', notices,
        '色は受信値の最大。県全体の揺れを示さず、未掲載地域の「揺れなし」も意味しません。',
        tuple(('震度 ' + seismic_intensity[value], INTENSITY_COLORS[value]) for value in used), (when,))


def _arrival(forecast, report):
    """Preserve special arrival states and the official release-time exception."""
    raw = forecast.arrival_time_raw
    if set(raw) != {'day', 'hour', 'minute'} or any(type(raw[key]) is not int for key in raw):
        raise ValueError('Invalid tsunami arrival fields')
    if raw['day'] not in (0, 1):
        raise ValueError('Invalid tsunami arrival day')
    if (raw['hour'], raw['minute']) == (31, 63):
        return '到達状況：津波到達中と推測', False
    if (raw['day'], raw['hour'], raw['minute']) == (0, 30, 62):
        return '到達予想：該当情報なし', False
    if not (0 <= raw['hour'] <= 23 and 0 <= raw['minute'] <= 59):
        raise ValueError('Unknown tsunami arrival code')
    arrival = forecast.arrival_time
    if arrival is None or arrival.utcoffset() is None:
        raise ValueError('Missing tsunami arrival date')
    placeholder = (report.tsunami_warning_code_raw == 2 and raw['hour'] == raw['minute'] == 0
                   and arrival <= report.report_time)
    if placeholder:
        return '到達予想：該当情報なし', True
    return '到達予想 ' + _date(arrival, report.report_time), False


def tsunami_map(reports):
    """Attach each received height/time pair to its exact forecast-region code."""
    reports = _reports(reports, dcr.Tsunami)
    first = reports[0]
    warning = first.tsunami_warning_code_raw
    if warning not in TSUNAMI_COLORS or any(report.tsunami_warning_code_raw != warning for report in reports):
        raise ValueError('Unknown or mixed tsunami warning types')
    values, advisory_lines = {}, []
    contains_placeholder = False
    for report in reports:
        for code, text in zip(report.notifications_on_disaster_prevention_raw,
                              report.notifications_on_disaster_prevention, strict=True):
            if code:
                for line in text.splitlines():
                    if line and line not in advisory_lines:
                        advisory_lines.append(line)
        for forecast in report.forecasts:
            if (type(forecast.region_code) is not int or forecast.region_code not in tsunami_forecast_region
                    or forecast.region_code == 1000 or forecast.height_code not in tsunami_height):
                raise ValueError('Unknown tsunami forecast region or height')
            arrival, placeholder = _arrival(forecast, report)
            contains_placeholder = contains_placeholder or placeholder
            value = ('予想高 ' + tsunami_height[forecast.height_code], arrival)
            regional = values.setdefault(forecast.region_code, [])
            if value not in regional:
                regional.append(value)
    if not values:
        raise ValueError('No received tsunami regions')
    notices = []
    for code, alternatives in sorted(values.items()):
        lines = []
        for index, (height, arrival) in enumerate(alternatives, 1):
            lines.extend(((f'受信値 {index}：' if len(alternatives) > 1 else '') + height, arrival))
        notices.append(RegionNotice(code, tsunami_forecast_region[code], tuple(lines), TSUNAMI_COLORS[warning]))
    if contains_placeholder:
        advisory_lines.append('解除通報の欠測代用時刻は「該当情報なし」と表示')
    return RegionMap('tsunami', tsunami_warning_code[warning], '津波予報区ごとの受信情報', tuple(notices),
        '対象は受信した予報区のみ。浸水範囲を示すものではありません。',
        ((tsunami_warning_code[warning], TSUNAMI_COLORS[warning]),), tuple(advisory_lines))
