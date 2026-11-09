"""Posting text shared by live delivery and offline previews."""

from datetime import timedelta, timezone

from azarashi.reports import dcr


JST = timezone(timedelta(hours=9), 'JST')


def _marine_header(report):
    training = report.report_classification_no == 7
    cancelled = report.information_type_no == 2
    markers = ('【訓練・試験】' if training else '') + ('【取消】' if cancelled else '')
    if not markers:
        return f'【海上警報】{report.information_type}（{report.report_classification}）'
    details = []
    if not cancelled:
        details.append(report.information_type)
    if not training:
        details.append(report.report_classification)
    suffix = f'（{"・".join(details)}）' if details else ''
    return f'{markers}海上警報{suffix}'


def _marine_text(report):
    # Never let a malformed pair of arrays silently discard a warning or region.
    lengths = {len(values) for values in (
        report.marine_warning_codes, report.marine_warning_codes_raw,
        report.marine_forecast_regions, report.marine_forecast_regions_raw,
    )}
    if len(lengths) != 1:
        raise ValueError('Marine warning codes and regions must have equal lengths')
    if report.report_time is None or report.report_time.utcoffset() is None:
        raise ValueError('Marine report time must be timezone-aware')
    at = report.report_time.astimezone(JST)
    lines = [_marine_header(report), f'発表時刻：{at.month}月{at.day}日 {at:%H:%M} JST']
    groups = {}
    for code, label, region in zip(report.marine_warning_codes_raw,
                                   report.marine_warning_codes,
                                   report.marine_forecast_regions, strict=True):
        # Retain both code and label: unknown or distinct codes must not merge
        # merely because their display labels happen to be identical.
        groups.setdefault((code, label), []).append(region)
    for (_, label), regions in groups.items():
        lines.extend(('', f'■ {label}'))
        lines.extend(f'・{region}' for region in regions)
    return '\n'.join(lines)


def report_message(report):
    text = _marine_text(report) if isinstance(report, dcr.Marine) else str(report)
    return f'{text}\n---\nNMEA: {report.nmea}'
