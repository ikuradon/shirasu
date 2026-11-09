"""v3 volcano positions with received warning text, never an inferred hazard area."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path

from azarashi.definitions.qzss.dcr.local_government import local_government
from azarashi.definitions.qzss.dcr.volcanic_warning_code import volcanic_warning_code
from azarashi.definitions.qzss.dcr.volcano_name import volcano_name
from azarashi.reports import dcr
import numpy as np

from image_service import render as base
from image_service import region_map
from image_service import map_base_v3 as terrain
from image_service.regional_reports import RegionMap, RegionNotice, _reports

ASSETS = Path(__file__).parent / 'assets' / 'v3'
COLORS = {
    11: '#8cb9dc', 12: '#f2df86', 13: '#ffb65b', 14: '#ec5561', 15: '#bd84e4',
    21: '#8cb9dc', 22: '#f2df86', 23: '#ffb65b', 24: '#ec5561', 25: '#bd84e4',
    35: '#8cb9dc', 36: '#ffb65b', 52: '#ec5561', 62: '#ec5561',
}


@lru_cache(maxsize=1)
def verify_assets():
    base.verify_assets()
    manifest = json.loads((ASSETS / 'volcano_manifest.json').read_text())
    for name in ('volcano.json', 'VOLCANO_NOTICE.txt'):
        if hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() != manifest[name]['sha256']:
            raise RuntimeError('Volcano assets do not match their frozen version')
    return True


@lru_cache(maxsize=1)
def _positions():
    verify_assets()
    result = json.loads((ASSETS / 'volcano.json').read_text())
    if len(result) != 117:
        raise RuntimeError('Incomplete official volcano catalogue')
    return result


def _activity(report):
    ambiguity = report.ambiguity_of_activity_time_no
    if type(ambiguity) is not int or not 0 <= ambiguity <= 7:
        raise ValueError('Unknown volcano activity-time precision')
    if ambiguity >= 6:
        unit = '月' if ambiguity == 6 else '年'
        return f'活動時刻：{unit}単位の概数（日時は特定不可）'
    if report.activity_time is None:
        return '活動時刻：不明'
    # Preserve the decoder's precision: approximate day stays in UTC and must
    # not acquire an invented hour or a JST label. Months/years have no date.
    text = report.convert_dt_to_ambiguous_time_str(report.activity_time, ambiguity)
    zone = 'UTC' if ambiguity == 5 else 'JST'
    year = (report.activity_time.year if ambiguity == 5
            else report.activity_time.astimezone(base.JST).year)
    return f'活動時刻：{year}年{text} {zone}'


def volcano_plan(reports):
    reports = _reports(reports, dcr.Volcano)
    first = reports[0]
    code, warning = first.volcano_name_raw, first.volcanic_warning_code_raw
    if (type(code) is not int or code not in volcano_name or str(code) not in _positions()
            or warning not in COLORS):
        raise ValueError('Unknown volcano position or warning')
    key = lambda report: (report.volcano_name_raw, report.volcanic_warning_code_raw,
        report.ambiguity_of_activity_time_no, report.activity_time,
        tuple(sorted(report.activity_time_raw.items())))
    if any(key(report) != key(first) for report in reports):
        raise ValueError('Mixed volcano warning or activity times')
    municipalities = []
    for report in reports:
        # A name-only warning still preserves every received city. No city is
        # given an invented polygon or coordinate to make a map more detailed.
        if len(report.local_governments) != len(report.local_governments_raw):
            raise ValueError('Inconsistent volcano municipality fields')
        for municipality in report.local_governments_raw:
            if type(municipality) is not int or municipality not in local_government:
                raise ValueError('Unknown volcano municipality')
            if municipality not in municipalities:
                municipalities.append(municipality)
    lines = [volcanic_warning_code[warning], _activity(first)]
    if municipalities:
        lines.append('対象市町村（受信分）')
        lines.extend(local_government[municipality] for municipality in municipalities)
    else:
        lines.append('対象市町村：本通報に記載なし')
    notice = RegionNotice(code, volcano_name[code], tuple(lines), COLORS[warning])
    return RegionMap('volcano', '火山情報', '火山の位置と受信情報', (notice,),
        '印は火山の代表位置。火口・警戒範囲・避難対象区域を示すものではありません。',
        (('火山の位置', COLORS[warning]),))


def draw_volcano(fig, reports):
    """Draw one volcano with complete received text, or reject the entire bundle."""
    plan = volcano_plan(reports)
    notice = plan.notices[0]
    lon, lat = region_map._position(_positions()[str(notice.code)]['coordinates'])
    point = (lon, float(base._my(lat)))
    ax = terrain.axes(fig, region_map._limits([np.asarray([point])]))
    ax.scatter(*point, marker='^', s=130, facecolor=notice.color,
               edgecolor=base.WHITE, linewidth=1.1, zorder=8,
               gid=f'volcano-point-{notice.code}')
    region_map._annotations(fig, ax, [point], plan.notices)
    return plan
