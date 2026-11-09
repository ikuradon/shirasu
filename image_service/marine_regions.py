"""Preserve every marine warning attached to each received forecast area."""

from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.marine_forecast_region import marine_forecast_region
from azarashi.definitions.qzss.dcr.marine_warning_code import marine_warning_code

from image_service.regional_reports import RegionMap, RegionNotice, _reports

WARNING_COLOR, CLEAR_COLOR = '#ffce6c', '#6ce2ec'
KNOWN_WARNINGS = {0, 10, 11, 12, 20, 21, 22, 23}


def marine_map(reports):
    reports = _reports(reports, dcr.Marine)
    regions = {}
    for report in reports:
        for code, warning in zip(report.marine_forecast_regions_raw,
                                  report.marine_warning_codes_raw, strict=True):
            if (type(code) is not int or code not in marine_forecast_region or code == 10000
                    or warning not in KNOWN_WARNINGS):
                raise ValueError('Unknown marine area or warning')
            values = regions.setdefault(code, [])
            if warning not in values:
                values.append(warning)
    if not regions:
        raise ValueError('No marine forecast areas')
    notices, mixed = [], False
    for code, warnings in sorted(regions.items()):
        labels = ['警報解除' if warning == 0 else marine_warning_code[warning].removeprefix('海上')
                  for warning in warnings]
        # Fog, icing and wind are different hazards, not competing severity
        # levels. Use one warning colour and preserve each warning in words.
        color = CLEAR_COLOR if warnings == [0] else WARNING_COLOR
        notices.append(RegionNotice(code, marine_forecast_region[code], tuple(labels), color))
        mixed = mixed or (0 in warnings and len(warnings) > 1)
    legend = []
    if any(notice.color == WARNING_COLOR for notice in notices):
        legend.append(('警報対象', WARNING_COLOR))
    if any(notice.color == CLEAR_COLOR for notice in notices):
        legend.append(('警報解除', CLEAR_COLOR))
    return RegionMap('marine', '海上警報', '地方海上予報区ごとの受信情報', tuple(notices),
        '着色は本通報の対象海域。無着色は警報なしを意味しません。', tuple(legend),
        ('同一海域の警報と解除を併記（受信内容のまま）',) if mixed else ())
