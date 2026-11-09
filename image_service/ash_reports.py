"""Received Ash Fall forecast entries for the unpublished image preview.

DCR-017 tables 4.1.2-34..36 define a volcanic-activity base time and up to
four (hours from base, phenomenon, municipality) entries in each report.
The hour is retained as a relative forecast value, not an invented onset or
time-interval boundary. Unknown fields require the complete text fallback.
https://qzss.go.jp/en/technical/download/pdf/ps-is-qzss/is-qzss-dcr-017.pdf

Label hues follow JMA's ash-class table and preliminary-forecast example:
https://www.jma.go.jp/jma/kishou/know/kazan/qvaf/class.png
https://www.jma.go.jp/jma/kishou/know/kazan/qvaf/howto_sok_fig.png
The small-cinder label is outlined red, distinct from the filled heavy-ash
label. No colour is assigned to a whole municipality as a hazard footprint.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.ash_fall_warning_code import ash_fall_warning_code
from azarashi.definitions.qzss.dcr.ash_fall_warning_type import ash_fall_warning_type
from azarashi.definitions.qzss.dcr.local_government import local_government
from azarashi.definitions.qzss.dcr.volcano_name import volcano_name as volcano_names

from image_service.regional_reports import RegionMap, RegionNotice, _date, _reports
from image_service.weather_map_v4 import LabelStyle

STYLES = {
    1: LabelStyle('少量の降灰', '#d9d9d9', '#000000', '#d9d9d9'),
    2: LabelStyle('やや多量の降灰', '#ffff00', '#000000', '#ffff00'),
    3: LabelStyle('多量の降灰', '#ff0000', '#ffffff', '#ff0000'),
    4: LabelStyle('小さな噴石の落下', 'none', '#ffffff', '#ff0000'),
}


@dataclass(frozen=True)
class AshNotice(RegionNotice):
    styles: tuple[LabelStyle, ...]


@dataclass(frozen=True)
class AshPlan(RegionMap):
    volcano_code: int = 0
    volcano_name: str = ''
    base_time: datetime | None = None
    warning_type: int = 0


def _base_time(report):
    raw, value = report.activity_time_raw, report.activity_time
    if (not isinstance(raw, dict) or set(raw) != {'day', 'hour', 'minute'}
            or any(type(number) is not int for number in raw.values())
            or not 1 <= raw['day'] <= 31 or not 0 <= raw['hour'] <= 23
            or not 0 <= raw['minute'] <= 59
            or not isinstance(value, datetime) or value.utcoffset() is None):
        raise ValueError('Unknown or invalid ash forecast base time')
    if not isinstance(report.report_time, datetime) or report.report_time.utcoffset() is None:
        raise ValueError('Missing ash issue time')
    utc = value.astimezone(timezone.utc)
    if ((utc.day, utc.hour, utc.minute) != (raw['day'], raw['hour'], raw['minute'])
            or utc.second or utc.microsecond or value > report.report_time):
        raise ValueError('Ash activity time is inconsistent or in the future')
    return value


def ash_plan(reports):
    reports = _reports(reports, dcr.AshFall)
    first = reports[0]
    if first.report_classification_no not in (2, 7):
        raise ValueError('Unsupported ash report classification')
    volcano, kind = first.volcano_name_raw, first.ash_fall_warning_type_raw
    if (type(volcano) is not int or volcano not in volcano_names
            or volcano in (900, 901, 902, 4000)
            or type(kind) is not int or kind not in (1, 2)):
        raise ValueError('Unknown ash volcano or forecast type')
    base_time = _base_time(first)
    identity = (volcano, kind, base_time)
    municipalities, amounts = {}, {}
    for report in reports:
        if (type(report.volcano_name_raw) is not int
                or type(report.ash_fall_warning_type_raw) is not int
                or (report.volcano_name_raw, report.ash_fall_warning_type_raw,
                    _base_time(report)) != identity):
            raise ValueError('Different volcanoes, forecast types or activity times')
        if not 1 <= len(report.local_governments_raw) <= 4:
            raise ValueError('No ash forecast entries or too many entries')
        for municipality, hours, phenomenon in zip(
                report.local_governments_raw, report.expected_ash_fall_times_raw,
                report.ash_fall_warning_codes_raw, strict=True):
            if (type(municipality) is not int or municipality not in local_government
                    or municipality % 100000 == 99999
                    or type(hours) is not int or not 1 <= hours <= 6
                    or type(phenomenon) is not int or phenomenon not in STYLES):
                raise ValueError('Unknown municipality, ash forecast time or phenomenon')
            key = municipality, hours
            if phenomenon in (1, 2, 3):
                if key in amounts and amounts[key] != phenomenon:
                    raise ValueError('Conflicting ash amounts for one municipality and time')
                amounts[key] = phenomenon
            municipalities.setdefault(municipality, set()).add((hours, phenomenon))
    notices = []
    for code, entries in sorted(municipalities.items()):
        entries = sorted(entries)
        lines = tuple(f'基点＋{hours}時間：{ash_fall_warning_code[phenomenon]}'
                      for hours, phenomenon in entries)
        notices.append(AshNotice(code, local_government[code], lines, '#ffffff',
                                 tuple(STYLES[phenomenon] for _, phenomenon in entries)))
    return AshPlan('ash_fall', f'降灰予報（{ash_fall_warning_type[kind]}）',
        '基点時刻からの予想時間 / 市町村別', tuple(notices),
        '輪郭は受信した市町村。市町村全域の降灰分布や噴石の落下範囲、未掲載地域の安全を示すものではありません。',
        (), (f'火山：{volcano_names[volcano]} / 基点時刻：{_date(base_time)}',),
        volcano_code=volcano, volcano_name=volcano_names[volcano], base_time=base_time,
        warning_type=kind)
