"""Northwest Pacific tsunami forecasts for the unpublished image preview.

IS-QZSS-DCR-017 tables 4.1.2-26, -27, -27a and -28:
https://qzss.go.jp/technical/download/is_qzss_dcr_017_agree.html

These are forecast points, not Japanese tsunami warning/advisory areas.
The arrival day is relative to the UTC issue date; 31:63 means either already
arrived or unknown, never a confirmed observation or a fabricated time.
"""

from datetime import datetime, timedelta, timezone

from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.coastal_region import coastal_region_en
from azarashi.definitions.qzss.dcr.tsunamigenic_potential import tsunamigenic_potential_en

from image_service.regional_reports import RegionMap, RegionNotice, _reports

POINT_CODES = frozenset(range(1, 42)) | (frozenset(range(66, 87)) - {73})
FORECAST_COLOR = '#6ce2ec'
HEIGHTS = {
    1: '0.3〜1m', 2: '1〜3m', 3: '3〜5m', 4: '5〜10m',
    508: '10m超', 509: '巨大（Huge）', 510: '高い（High）', 511: '不明',
}


def _arrival(raw, value, report_time):
    if (not isinstance(raw, dict) or set(raw) != {'day', 'hour', 'minute'}
            or any(type(number) is not int for number in raw.values())
            or raw['day'] not in (0, 1)):
        raise ValueError('Invalid Northwest Pacific arrival fields')
    if (raw['hour'], raw['minute']) == (31, 63):
        if value is not None:
            raise ValueError('Ambiguous arrival must not have a resolved time')
        return '到達済み、または到達時刻不明'
    if (not 0 <= raw['hour'] <= 23 or not 0 <= raw['minute'] <= 59
            or not isinstance(value, datetime) or value.utcoffset() is None):
        raise ValueError('Undefined Northwest Pacific arrival time')
    expected = (report_time.astimezone(timezone.utc) + timedelta(days=raw['day'])).replace(
        hour=raw['hour'], minute=raw['minute'], second=0, microsecond=0)
    if value != expected:
        raise ValueError('Arrival time disagrees with its UTC issue date and fields')
    # A forecast can precede an updated bulletin's issue time. Preserve the
    # transmitted date instead of silently advancing it or calling it observed.
    return '到達予想 ' + expected.strftime('%m/%d %H:%M UTC')


def nwp_plan(reports):
    reports = _reports(reports, dcr.NorthwestPacificTsunami)
    first = reports[0]
    if first.report_classification_no not in (2, 7) or first.information_type_no != 0:
        raise ValueError('Unsupported Northwest Pacific classification or information type')
    if not isinstance(first.report_time, datetime) or first.report_time.utcoffset() is None:
        raise ValueError('Missing Northwest Pacific issue time')
    potential = first.tsunamigenic_potential_raw
    # No-possibility and other-potential messages remain complete text cards;
    # neither is converted into an active point-forecast map or an all-clear.
    if type(potential) is not int or potential not in (1, 2, 3, 4):
        raise ValueError('This potential requires the complete bulletin text')
    points = {}
    for report in reports:
        if type(report.tsunamigenic_potential_raw) is not int or report.tsunamigenic_potential_raw != potential:
            raise ValueError('Different tsunami potentials must not be combined')
        if not 1 <= len(report.coastal_regions_raw) <= 5:
            raise ValueError('No received forecast points or too many entries')
        for code, height, raw_time, arrival in zip(
                report.coastal_regions_raw, report.tsunami_heights_raw,
                report.expected_tsunami_arrival_times_raw, report.expected_tsunami_arrival_times,
                strict=True):
            if (type(code) is not int or code not in POINT_CODES
                    or type(height) is not int or height not in HEIGHTS):
                raise ValueError('Unknown Northwest Pacific point or tsunami height')
            lines = ('予想高 ' + HEIGHTS[height], _arrival(raw_time, arrival, report.report_time))
            if code in points and points[code] != lines:
                raise ValueError('Conflicting forecasts for the same point')
            points[code] = lines
    notices = tuple(RegionNotice(code, coastal_region_en[code].split(' (', 1)[0], lines, FORECAST_COLOR)
                    for code, lines in sorted(points.items()))
    return RegionMap('northwest_pacific_tsunami', '北西太平洋津波情報',
        '予測地点ごとの津波高・到達予想 / UTC', notices,
        '点は受信した予測地点。浸水範囲や国内の津波警報区分、未掲載地域の安全を示すものではありません。',
        (), (tsunamigenic_potential_en[potential] + '.',))
