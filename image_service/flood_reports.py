"""Received flood warnings attached to official forecast reaches, for preview.

IS-QZSS-DCR-017 tables 4.1.2-43..45 define three region/level pairs. Its
four-bit warning codes are NOT the public five-level evacuation scale.
Code 1 also covers a downgrade to flood advisory; it is not an all-clear.
https://qzss.go.jp/en/technical/download/pdf/ps-is-qzss/is-qzss-dcr-017.pdf

Colours follow the named designated-river products in JMA's table 1-1,
not the numeric DCR code: warning red, danger purple, occurrence black.
https://www.jma.go.jp/jma/kishou/info/colorguide/HPColorGuide_202007.pdf
"""

from dataclasses import dataclass
from datetime import datetime

from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.flood_forecast_region import flood_forecast_region
from azarashi.definitions.qzss.dcr.flood_warning_level import flood_warning_level

from image_service.regional_reports import RegionMap, RegionNotice, _reports
from image_service.weather_map_v4 import LabelStyle

# DCR-017's 47 prefectural and 10 regional "other rivers" entries identify no
# specific forecast reach. Do not infer a river from a prefecture or its name.
NON_SPECIFIC_CODES = frozenset(code for code in flood_forecast_region
                               if code % 10_000_000_000 == 9_999_999_999)
REGION_CODES = frozenset(flood_forecast_region) - NON_SPECIFIC_CODES
STYLES = {
    1: LabelStyle('警報解除', 'none', '#c8c8cb', '#c8c8cb'),
    2: LabelStyle('氾濫警戒情報', '#ff2800', '#ffffff', '#ff2800'),
    3: LabelStyle('氾濫危険情報', '#aa00aa', '#ffffff', '#aa00aa'),
    4: LabelStyle('氾濫発生情報', '#0c000c', '#ffffff', '#ffffff'),
}
COLORS = {1: '#c8c8cb', 2: '#ff2800', 3: '#aa00aa', 4: '#0c000c'}
RELEASE_NOTE = '警報解除には氾濫注意情報への切替も含みます。安全を示すものではありません。'


@dataclass(frozen=True)
class FloodNotice(RegionNotice):
    styles: tuple[LabelStyle, ...]
    warning_level: int


def flood_plan(reports):
    reports = _reports(reports, dcr.Flood)
    first = reports[0]
    if (type(first.report_classification_no) is not int
            or first.report_classification_no not in (2, 3, 7)):
        raise ValueError('Unsupported flood report classification')
    if not isinstance(first.report_time, datetime) or first.report_time.utcoffset() is None:
        raise ValueError('Missing flood issue time')
    regions = {}
    for report in reports:
        if (not isinstance(report.flood_forecast_regions_raw, (list, tuple))
                or not isinstance(report.flood_warning_levels_raw, (list, tuple))
                or not 1 <= len(report.flood_forecast_regions_raw) <= 3):
            raise ValueError('No flood forecast entries or too many entries')
        for code, level in zip(report.flood_forecast_regions_raw,
                               report.flood_warning_levels_raw, strict=True):
            if (type(code) is not int or code not in REGION_CODES
                    or type(level) is not int or level not in STYLES):
                raise ValueError('Unknown flood forecast region or warning level')
            if code in regions and regions[code] != level:
                raise ValueError('Conflicting warning levels for one flood forecast reach')
            regions[code] = level
    notices = tuple(FloodNotice(code, flood_forecast_region[code],
                    (flood_warning_level[level],), COLORS[level], (STYLES[level],), level)
                    for code, level in sorted(regions.items()))
    return RegionMap('flood', '指定河川洪水予報', '予報区間ごとの受信情報', notices,
        '線は受信した洪水予報の対象区間。浸水範囲や未掲載地域の安全を示すものではありません。',
        (), (RELEASE_NOTE,) if 1 in regions.values() else ())
