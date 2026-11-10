"""Conservative semantic plans for unpublished DCX images.

IS-QZSS-DCX-004 sections 4.2.4 and 5 define the scope, not the place names
or the apparent location of a decoded centre. Geometry and administrative
areas are independent candidates: rendering must validate the *whole* chosen
candidate, then try the next tier. In particular, missing municipal assets
must never be replaced by a containing city/prefecture or silently omitted.

This module has no plotting or geography dependencies. All geometry is in
physical WGS84 coordinates; projection failures belong to the renderer.
"""

from dataclasses import dataclass
import math

from azarashi.definitions.camf.a4_hazard_category_and_type import a4_hazard_type
from azarashi.definitions.qzss.dcx.ex1_target_area_code import ex1_target_area_code_ja
from azarashi.definitions.qzss.dcx.ex9_target_area_code import ex9_target_area_code_ja
from azarashi.reports import dcx


@dataclass(frozen=True)
class Ellipse:
    lon: float
    lat: float
    semi_major_km: float
    semi_minor_km: float
    azimuth: float  # East = 0 degrees; positive towards north, NOT a compass bearing.
    role: str = 'target'


@dataclass(frozen=True)
class Point:
    lon: float
    lat: float
    role: str = 'hazard_center'


@dataclass(frozen=True)
class Area:
    layer: str  # 'prefectures' or 'municipalities'
    code: int  # Pinned asset code, not an inferred parent or a display-name match.
    name: str


@dataclass(frozen=True)
class DcxPlan:
    topic: str
    headline: str
    status: str
    scope_kind: str  # 'geometry', 'area', or 'topic' (the first recommended tier).
    geometries: tuple[Ellipse | Point, ...]
    areas: tuple[Area, ...]
    details: tuple[str, ...]
    note: str


# These are hazard names, not Japanese warning levels. Other defined hazards
# retain the pinned English name instead of being assigned a guessed category.
_TOPICS = {
    1: '空襲', 4: '生物学的危険', 5: '化学物質', 6: '爆発物', 8: 'ミサイル',
    9: '原子力災害', 10: '原発事故', 11: '放射線', 13: 'サイレン試験',
    15: '大気汚染', 17: 'ガス漏れ', 18: '海洋汚染', 21: '河川汚染',
    24: '大規模火災', 27: '森林火災', 30: '火災の危険',
    32: '降灰', 33: '雪崩の危険', 35: '土石流', 36: '地震',
    39: '地すべり', 40: '溶岩流', 41: '火砕流', 44: '津波',
    45: '火山泥流', 46: '噴火', 47: '風・波・高潮',
    51: 'パンデミック', 53: '感染の危険', 54: '建物倒壊', 58: '停電',
    62: '沿岸浸水', 63: '寒波', 65: '干ばつ', 68: '洪水', 69: '霧',
    70: 'ひょう', 71: '熱波', 72: '雷', 74: '降雨', 75: '吹雪', 76: '降雪',
    79: '竜巻', 80: 'ハリケーン', 82: '台風', 95: '安全に関する警告',
    103: 'テロ', 104: '航空機事故', 110: '道路交通事故', 111: '鉄道事故',
    113: '試験情報',
}
_STATUS = {0: '訓練', 1: '発表', 2: '更新', 3: '解除'}
_SOURCES = {dcx.LAlert: 'Lアラート', dcx.JAlert: 'Jアラート',
            dcx.MTInfo: '自治体情報', dcx.OutsideJapan: '海外通報'}
# B4 entries known to contain scalar detail only. Earthquake (36) contains an
# additional epicentre vector, so it is deliberately not in this set.
_SCALAR_B4 = frozenset((4, 5, 6, 9, 10, 11, 15, 16, 18, 19, 21, 23, 27, 30,
                         32, 33, 37, 43, 44, 47, 51, 53, 55, 56, 57, 58, 60,
                         63, 64, 65, 68, 69, 70, 71, 72, 74, 75, 76, 77, 79,
                         80, 81, 82, 103))
_MAIN_WIDTHS = (('a12', 16), ('a13', 17), ('a14', 5), ('a15', 5), ('a16', 6))
_EX_WIDTHS = (('ex2', 1), ('ex3', 17), ('ex4', 17), ('ex5', 5), ('ex6', 5), ('ex7', 7))


def _raw(camf, field, width):
    value = getattr(camf, field, None)
    if type(value) is not int or not 0 <= value < 1 << width:
        raise ValueError('Missing or invalid DCX raw field')
    return value


def _number(report, field):
    value = getattr(report, field, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Missing or invalid DCX geometry')
    return float(value)


def _position(lon, lat):
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise ValueError('DCX coordinate is outside the earth')


def _main_absent(camf):
    # Only the domestic specification assigns the all-zero sentinel to EX1.
    return all(_raw(camf, field, width) == 0 for field, width in _MAIN_WIDTHS)


def _geometries(report):
    camf = report.camf
    if report.ignore_a12_to_a16 is not False or type(report) is dcx.JAlert:
        return ()
    if camf.a2 == 111 and _main_absent(camf):
        return ()
    for field, width in _MAIN_WIDTHS:
        _raw(camf, field, width)
    if report.ignore_a17_to_a18 is not False:
        return ()
    setting = _raw(camf, 'a17', 2)
    if setting == 2 or (setting == 3 and camf.a4 not in _SCALAR_B4):
        # Never draw just the main ellipse of a multiple-geometry message.
        return ()
    if type(report) is dcx.MTInfo:
        # DCX-004 requires EX2 *and* EX3..EX7 to be zero for no additional
        # instruction. The decoder's ignore flag alone does not check EX2.
        if any(_raw(camf, field, width) for field, width in _EX_WIDTHS):
            return ()
        if report.ignore_ex2_to_ex7 is not True:
            return ()

    if setting == 0:
        # Raw axis zero is 216.2 metres, with finer C3/C4 adjustments; it does
        # not mean a missing range or a point at the ellipse centre.
        lat = _number(report, 'c1_refined_latitude_of_centre_of_main_ellipse')
        lon = _number(report, 'c2_refined_longitude_of_centre_of_main_ellipse')
        major = _number(report, 'c3_refined_length_of_semi_major_axis')
        minor = _number(report, 'c4_refined_length_of_semi_minor_axis')
    else:
        lat = _number(report, 'a12_ellipse_centre_latitude')
        lon = _number(report, 'a13_ellipse_centre_longitude')
        major = _number(report, 'a14_ellipse_semi_major_axis')
        minor = _number(report, 'a15_ellipse_semi_minor_axis')
    azimuth = _number(report, 'a16_ellipse_azimuth')
    _position(lon, lat)
    if not 0 < minor <= major <= 2500 or not -90 <= azimuth < 90:
        raise ValueError('Invalid DCX ellipse')
    result = (Ellipse(lon, lat, major, minor, azimuth),)
    if setting == 1:
        lat = _number(report, 'c5_latitude_of_centre_of_hazard')
        lon = _number(report, 'c6_longitude_of_centre_of_hazard')
        _position(lon, lat)
        result += (Point(lon, lat),)
    return result


def _area(code):
    if type(code) is not int or code == 0 or code not in ex1_target_area_code_ja:
        raise ValueError('Unknown DCX administrative code')
    name = ex1_target_area_code_ja[code]
    if code % 1000 == 0 and 1 <= code // 1000 <= 47:
        return Area('prefectures', code // 1000, name)
    return Area('municipalities', code * 100, name)


def _areas(report):
    camf = report.camf
    if type(report) is dcx.JAlert:
        if report.ignore_ex8_to_ex9 is not False:
            return ()
        mode, bits = _raw(camf, 'ex8', 1), _raw(camf, 'ex9', 64)
        if mode == 0:
            if bits & ((1 << 17) - 1):
                raise ValueError('Unknown EX9 reserved bits')
            return tuple(Area('prefectures', bit + 1, ex9_target_area_code_ja[1 << bit])
                         for bit in range(47) if (bits >> 17) & (1 << bit))
        codes = tuple((bits >> shift) & 0xffff for shift in (48, 32, 16, 0))
        if any(code and code % 1000 == 0 for code in codes):
            # EX8=1 explicitly selects municipalities. Prefectural coverage
            # uses EX8=0; table membership alone cannot authorize widening.
            raise ValueError('Prefecture code in a municipal EX9 slot')
        # Resolve every non-empty slot before deduplicating. One unknown code
        # invalidates the whole candidate, not only that part of the map.
        return tuple(dict.fromkeys(_area(code) for code in codes if code))
    if type(report) in (dcx.LAlert, dcx.MTInfo):
        if report.ignore_ex1 is not False or not _main_absent(camf):
            return ()
        code = _raw(camf, 'ex1', 16)
        return (_area(code),) if code else ()
    return ()


def dcx_plan(reports):
    """Select geometry → exact administrative areas → a short topic card.

    A plan retains every independently valid candidate. An Area's asset may be
    unavailable: the renderer must check all codes before painting any area.
    Outside-Japan Vn versions only the issuer's EX11, which is not interpreted.
    """
    reports = tuple(reports)
    if len(reports) != 1 or type(reports[0]) not in _SOURCES:
        raise ValueError('DCX preview expects one supported alert')
    report, = reports
    camf = report.camf
    hazard = getattr(camf, 'a4', None)
    known_hazard = type(hazard) is int and hazard != 0 and hazard in a4_hazard_type
    topic = _TOPICS.get(hazard, a4_hazard_type[hazard]) if known_hazard else '防災情報'
    headline = topic if topic.endswith(('情報', '警告')) or hazard not in _TOPICS else topic + '情報'
    status = _STATUS.get(getattr(camf, 'a1', None), '状態不明')
    details = (_SOURCES[type(report)],)
    geometries, areas = (), ()
    try:
        _raw(camf, 'a1', 2)
        country = _raw(camf, 'a2', 9)
        version = _raw(camf, 'vn', 6)
        if not known_hazard or (country == 111 and version != 1):
            raise ValueError('Unsupported domestic version or hazard')
        # Ignore flags do not make a mismatched report class authoritative.
        provider = _raw(camf, 'a3', 5)
        if ((type(report) is dcx.OutsideJapan) == (country == 111)
                or (type(report) is dcx.LAlert and provider != 1)
                or (type(report) is dcx.JAlert and provider not in (2, 3))
                or (type(report) is dcx.MTInfo and provider != 4)):
            raise ValueError('Inconsistent DCX provider')
    except ValueError:
        pass
    else:
        # These must remain separate: failure of one does not invalidate an
        # independently transmitted candidate at the next fallback tier.
        try:
            geometries = _geometries(report)
        except ValueError:
            pass
        try:
            areas = _areas(report)
        except ValueError:
            pass
    kind = 'geometry' if geometries else 'area' if areas else 'topic'
    return DcxPlan(topic, headline, status, kind, geometries, areas,
                   details, '詳しい情報は投稿本文へ')
