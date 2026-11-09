"""Weather labels with JMA category colours, independent of the published v3.

JMA's colour guide assigns black to weather emergency warnings (including
those without an evacuation level) and purple to landslide alert information:
https://www.jma.go.jp/jma/kishou/info/colorguide/HPColorGuide_202007.pdf

DCR-017 codes 21/22 are information products, not ordinary warning/advisory
classes. Their unfilled labels deliberately convey no inferred risk level.
"""

from dataclasses import dataclass, replace

from image_service.regional_reports import RegionNotice
from image_service.weather_map import weather_plan as legacy_plan


@dataclass(frozen=True)
class LabelStyle:
    label: str
    background: str
    foreground: str
    edge: str


EMERGENCY = LabelStyle('特別警報', '#0c000c', '#ffffff', '#ffffff')
LANDSLIDE = LabelStyle('土砂災害警戒情報', '#aa00aa', '#ffffff', '#aa00aa')
INFORMATION = LabelStyle('その他の情報', 'none', '#ffffff', '#ffffff')
RELEASE = LabelStyle('解除', 'none', '#c8c8cb', '#c8c8cb')


@dataclass(frozen=True)
class WeatherNotice(RegionNotice):
    styles: tuple[LabelStyle, ...]


def weather_plan(reports):
    reports = tuple(reports)
    plan = legacy_plan(reports)  # Keep the frozen DCR validation and wording.
    released = reports[0].weather_warning_state_raw == 2
    hazards_by_region = {}
    for report in reports:
        for region, hazard in zip(report.weather_forecast_regions_raw,
                                  report.weather_related_disaster_sub_categories_raw, strict=True):
            hazards = hazards_by_region.setdefault(region, [])
            if hazard not in hazards:
                hazards.append(hazard)
    notices = []
    for notice in plan.notices:
        styles = tuple(RELEASE if released else EMERGENCY if hazard <= 6 else
                       LANDSLIDE if hazard == 23 else INFORMATION
                       for hazard in hazards_by_region[notice.code])
        notices.append(WeatherNotice(notice.code, notice.title, notice.lines, '#ffffff', styles))
    return replace(plan, notices=tuple(notices), legend=())
