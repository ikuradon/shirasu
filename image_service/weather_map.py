"""Weather announcements in their transmitted forecast regions (DCR-017).

The received prefectural forecast region is not a municipal warning polygon:
https://www.jma.go.jp/jma/kishou/know/saibun/
"""

from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.weather_forecast_region import weather_forecast_region
from azarashi.definitions.qzss.dcr.weather_related_disaster_sub_category import weather_related_disaster_sub_category

from image_service.regional_reports import RegionMap, RegionNotice, _reports

EMERGENCY, INFORMATION, RELEASE = '#ca91e8', '#ffce6c', '#6ce2ec'
KNOWN_HAZARDS = frozenset(range(1, 8)) | {21, 22, 23}


def weather_plan(reports):
    reports = _reports(reports, dcr.Weather)
    if reports[0].report_classification_no not in (2, 3, 7):
        raise ValueError('Unsupported weather report classification')
    state = reports[0].weather_warning_state_raw
    if state not in (1, 2) or any(report.weather_warning_state_raw != state for report in reports):
        raise ValueError('Unknown or mixed weather announcement states')
    regions = {}
    for report in reports:
        for region, hazard in zip(report.weather_forecast_regions_raw,
                                   report.weather_related_disaster_sub_categories_raw, strict=True):
            if (type(region) is not int or region not in weather_forecast_region or region == 500000
                    or type(hazard) is not int or hazard not in KNOWN_HAZARDS):
                raise ValueError('Unknown weather region or hazard')
            # DCR-017 reserves 7 for the release of all six emergency-warning
            # types; it is not an announcement of six simultaneous warnings.
            if hazard == 7 and state != 2:
                raise ValueError('All-weather release code in an announcement')
            values = regions.setdefault(region, [])
            if hazard not in values:
                values.append(hazard)
    if not regions:
        raise ValueError('No received weather regions')
    notices = []
    for region, hazards in sorted(regions.items()):
        color = RELEASE if state == 2 else EMERGENCY if any(h <= 7 for h in hazards) else INFORMATION
        lines = tuple(weather_related_disaster_sub_category[h] + ('（解除）' if state == 2 else '')
                      for h in hazards)
        notices.append(RegionNotice(region, weather_forecast_region[region], lines, color))
    legend = []
    for label, color in (('特別警報', EMERGENCY), ('気象情報', INFORMATION), ('解除', RELEASE)):
        if any(notice.color == color for notice in notices):
            legend.append((label, color))
    return RegionMap('weather', '気象情報', '府県予報区ごとの受信情報', tuple(notices),
        '輪郭は受信した予報区。区域全体の警戒・解除や、未掲載地域の安全を示すものではありません。',
        tuple(legend))
