"""Interpret QZQSM EEW without inventing a per-region intensity or epicentre.

IS-QZSS-DCR-017, 4.1.2.3.2 / tables 4.1.2-5, -8, -9, -10 and -11:
https://qzss.go.jp/technical/download/is_qzss_dcr_017_agree.html
The payload has one intensity range, one long-period ground-motion range,
and flags for prefectural/local warning areas. It contains no coordinates.

Assumptive hypocentre / PLUM semantics:
https://www.data.jma.go.jp/svd/eew/data/nc/katsuyou/reference.pdf
The fixed depth 10 km and magnitude 1.0 are not estimated earthquake values.
"""

from dataclasses import dataclass
from datetime import datetime

from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.depth_of_hypocenter import depth_of_hypocenter
from azarashi.definitions.qzss.dcr.eew_forecast_region import eew_forecast_region
from azarashi.definitions.qzss.dcr.eew_magnitude import eew_magnitude
from azarashi.definitions.qzss.dcr.epicenter_and_hypocenter import epicenter_and_hypocenter
from azarashi.definitions.qzss.dcr.long_period_ground_motion_lower_limit import long_period_ground_motion_lower_limit
from azarashi.definitions.qzss.dcr.long_period_ground_motion_upper_limit import long_period_ground_motion_upper_limit
from azarashi.definitions.qzss.dcr.notification_on_disaster_prevention import notification_on_disaster_prevention
from azarashi.definitions.qzss.dcr.seismic_intensity_lower_limit import seismic_intensity_lower_limit
from azarashi.definitions.qzss.dcr.seismic_intensity_upper_limit import seismic_intensity_upper_limit

from image_service.regional_reports import RegionMap, RegionNotice, _date, _reports

WARNING_COLOR = '#ffce6c'


@dataclass(frozen=True)
class EEWPlan(RegionMap):
    headline: str = ''


def _known(code, table):
    if type(code) is not int or code not in table:
        raise ValueError('Unknown EEW value')
    return table[code]


def _range(lower, upper, *, long_period=False):
    low_table = long_period_ground_motion_lower_limit if long_period else seismic_intensity_lower_limit
    high_table = long_period_ground_motion_upper_limit if long_period else seismic_intensity_upper_limit
    low, high = _known(lower, low_table), _known(upper, high_table)
    prefix = '長周期地震動階級' if long_period else '震度'
    low, high = low.removeprefix(prefix), high.removeprefix(prefix)
    maximum, or_greater = (5, 6) if long_period else (10, 11)
    if 1 <= lower <= maximum and 1 <= upper <= maximum and lower > upper:
        raise ValueError('Reversed EEW prediction bounds')
    if not long_period and (lower == 14) != (upper == 14):
        raise ValueError('EEW intensity none conflicts with another bound')
    if upper == or_greater:
        if not 1 <= lower <= maximum:
            raise ValueError('EEW open upper bound needs a defined lower bound')
        return low + '程度以上'
    if lower == upper:
        return low
    if 1 <= lower <= maximum and 1 <= upper <= maximum:
        return low + '〜' + high
    # Preserve distinct, explicitly defined missing/unknown limits. They cannot
    # be collapsed into a numerical range or silently assigned to each area.
    return '下限 ' + low + ' / 上限 ' + high


def _occurrence(report):
    raw = report.occurrence_time_of_earthquake_raw
    if (not isinstance(raw, dict) or set(raw) != {'day', 'hour', 'minute'}
            or any(type(value) is not int for value in raw.values())):
        raise ValueError('Invalid EEW occurrence fields')
    value = report.occurrence_time_of_earthquake
    raw_tuple = tuple(raw[key] for key in ('day', 'hour', 'minute'))
    if raw_tuple == (0, 31, 63) and value is None:
        return '不明'
    if (not 1 <= raw['day'] <= 31 or not 0 <= raw['hour'] <= 23
            or not 0 <= raw['minute'] <= 59 or not isinstance(value, datetime)
            or value.utcoffset() is None or value > report.report_time):
        raise ValueError('Invalid or future EEW occurrence time')
    return _date(value)


def _identity(report):
    return (report.occurrence_time_of_earthquake,
            tuple(report.occurrence_time_of_earthquake_raw[key] for key in ('day', 'hour', 'minute')),
            report.seismic_epicenter_raw, report.depth_of_hypocenter_raw, report.magnitude_raw,
            report.seismic_intensity_lower_limit_raw, report.seismic_intensity_upper_limit_raw,
            report.long_period_ground_motion_lower_limit_raw, report.long_period_ground_motion_upper_limit_raw,
            report.assumptive)


def eew_plan(reports):
    """Return the complete set of received warning areas, or reject the bundle."""
    reports = _reports(reports, dcr.EarthquakeEarlyWarning)
    first = reports[0]
    # Unlike several other DCR categories, EEW permits only issue/cancellation
    # and maximum-priority/training. Correction is not a defined EEW format.
    if first.information_type_no != 0 or first.report_classification_no not in (1, 7):
        raise ValueError('Unsupported EEW report classification or information type')
    regions, advice = set(), []
    for report in reports:
        _occurrence(report)
        _known(report.seismic_epicenter_raw, epicenter_and_hypocenter)
        _known(report.depth_of_hypocenter_raw, depth_of_hypocenter)
        _known(report.magnitude_raw, eew_magnitude)
        _range(report.seismic_intensity_lower_limit_raw, report.seismic_intensity_upper_limit_raw)
        _range(report.long_period_ground_motion_lower_limit_raw,
               report.long_period_ground_motion_upper_limit_raw, long_period=True)
        if (type(report.assumptive) is not bool
                or report.assumptive != (report.depth_of_hypocenter_raw == report.magnitude_raw == 10)):
            raise ValueError('Inconsistent assumptive EEW hypocentre')
        if _identity(report) != _identity(first):
            raise ValueError('Different EEW earthquakes or predictions must not be combined')
        if not report.eew_forecast_regions_raw:
            raise ValueError('No EEW warning areas')
        for code in report.eew_forecast_regions_raw:
            _known(code, eew_forecast_region)
            if not 1 <= code <= 70:
                raise ValueError('No geometry for this EEW warning area')
            regions.add(code)
        for code in report.notifications_on_disaster_prevention_raw:
            text = _known(code, notification_on_disaster_prevention)
            if code:
                for line in text.splitlines():
                    if line and line not in advice:
                        advice.append(line)

    prediction = _range(first.seismic_intensity_lower_limit_raw, first.seismic_intensity_upper_limit_raw)
    headline = '予測震度 ' + prediction
    extra = []
    if (first.long_period_ground_motion_lower_limit_raw, first.long_period_ground_motion_upper_limit_raw) != (0, 0):
        extra.append('最大予測長周期地震動：階級 ' + _range(
            first.long_period_ground_motion_lower_limit_raw,
            first.long_period_ground_motion_upper_limit_raw, long_period=True))
    epicentre = _known(first.seismic_epicenter_raw, epicenter_and_hypocenter)
    note = '輪郭は受信した警報対象区域。震度分布ではなく、輪郭のない地域も安全を意味しません。'
    if first.assumptive:
        extra.append('仮定震源（PLUM法） / 地名 ' + epicentre + ' / 仮定発生時刻 ' + _occurrence(first))
        note += '仮定震源の深さ・Mは固定値です。'
    else:
        extra.append('地震発生 ' + _occurrence(first) + ' / 震央 ' + epicentre
                     + ' / 深さ ' + _known(first.depth_of_hypocenter_raw, depth_of_hypocenter)
                     + ' / M' + _known(first.magnitude_raw, eew_magnitude))
    extra.extend(advice)
    notices = tuple(RegionNotice(code, eew_forecast_region[code], (), WARNING_COLOR) for code in sorted(regions))
    return EEWPlan('eew', '緊急地震速報', '通報全体の予測値 / 受信した警報対象区域', notices,
                   note, (), tuple(extra), headline)
