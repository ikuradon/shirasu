"""Static geography carried by a cancellation; never a match to an older issue.

The complete wire slots are checked before selecting a background. Decoder
lists stop at the first zero slot, so trusting only those lists can silently
discard later regions. Cancelled coordinates, forecast values and inferred
parent regions are never used as geographic authority here.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from azarashi.reports import dcr
from azarashi.definitions.qzss.dcr.eew_forecast_region import eew_forecast_region
from azarashi.definitions.qzss.dcr.flood_forecast_region import flood_forecast_region
from azarashi.definitions.qzss.dcr.local_government import local_government
from azarashi.definitions.qzss.dcr.prefecture import prefecture
from azarashi.definitions.qzss.dcr.tsunami_forecast_region import tsunami_forecast_region
from azarashi.definitions.qzss.dcr.tsunami_height import tsunami_height
from azarashi.definitions.qzss.dcr.volcanic_warning_code import volcanic_warning_code
from azarashi.definitions.qzss.dcr.volcano_name import volcano_name
from azarashi.definitions.qzss.dcr.weather_forecast_region import weather_forecast_region

from image_service.preview_diagnostics import PreviewFallback


@dataclass(frozen=True)
class Scope:
    layer: str
    codes: tuple[int, ...]


_CATEGORIES = {
    dcr.EarthquakeEarlyWarning: 1, dcr.Hypocenter: 2, dcr.SeismicIntensity: 3,
    dcr.NankaiTroughEarthquake: 4, dcr.Tsunami: 5, dcr.NorthwestPacificTsunami: 6,
    dcr.Volcano: 8, dcr.AshFall: 9, dcr.Weather: 10, dcr.Flood: 11,
    dcr.Typhoon: 12, dcr.Marine: 14,
}


def _fail(code, field):
    raise PreviewFallback(code, stage='area', detail=field)


def _equal(report, name, expected):
    actual = getattr(report, name, None)
    if isinstance(expected, tuple):
        valid = (isinstance(actual, (tuple, list)) and len(actual) == len(expected)
                 and all(type(v) is int for v in actual) and tuple(actual) == expected)
    else:
        valid = type(actual) is int and actual == expected
    if not valid:
        _fail('invalid_fields', name)


def _wire(report, *, information=(2,)):
    raw = getattr(report, 'message', None)
    if not isinstance(raw, bytes) or len(raw) != 32:
        _fail('invalid_fields', 'QZQSM payload')
    word = int.from_bytes(raw, 'big')

    def bits(start, width):
        return (word >> (256 - start - width)) & ((1 << width) - 1)

    if bits(8, 6) != 43:
        _fail('invalid_fields', 'MT')
    _equal(report, 'disaster_category_no', bits(17, 4))
    if bits(17, 4) != _CATEGORIES[type(report)]:
        _fail('invalid_fields', 'Dc / report class')
    for name, start, width in (('version', 214, 6), ('report_classification_no', 14, 3),
                              ('information_type_no', 41, 2)):
        _equal(report, name, bits(start, width))
    if bits(214, 6) != 1:
        _fail('unsupported_report', 'DCR Vn')
    if bits(14, 3) not in (1, 2, 3, 7) or bits(41, 2) not in information:
        _fail('invalid_fields', 'Rc / It')
    at = getattr(report, 'report_time', None)
    if not isinstance(at, datetime) or at.utcoffset() is None:
        _fail('invalid_fields', 'At')
    at = at.astimezone(timezone.utc)
    if ((at.month, at.day, at.hour, at.minute) !=
            (bits(21, 4), bits(25, 5), bits(30, 5), bits(35, 6)) or at.second or at.microsecond):
        _fail('invalid_fields', 'At')
    return bits


def _slots(bits, start, widths, count, label):
    """Return all active rows; an all-zero row terminates the whole sequence."""
    result, ended = [], False
    stride = sum(widths)
    for index in range(count):
        offset, row = start + index * stride, []
        for width in widths:
            row.append(bits(offset, width))
            offset += width
        if not any(row):
            ended = True
        elif ended:
            _fail('invalid_fields', f'{label}: 終端後に受信値あり')
        else:
            result.append(tuple(row))
    return tuple(result)


def _column(report, rows, index, field):
    values = tuple(row[index] for row in rows)
    _equal(report, field, values)
    return values


def _codes(values, table, field, *, excluded=()):
    if any(code not in table or code in excluded for code in values):
        _fail('unknown_area', field)
    return values


def _weather_fields(report, bits):
    rows = _slots(bits, 56, (5, 19), 6, 'Ww / Pl')
    hazards = _column(report, rows, 0, 'weather_related_disaster_sub_categories_raw')
    codes = _column(report, rows, 1, 'weather_forecast_regions_raw')
    _equal(report, 'weather_warning_state_raw', bits(53, 3))
    if bits(53, 3) not in (1, 2) or any(not h or not c for h, c in rows):
        _fail('invalid_fields', 'Ar / Ww / Pl')
    # Table 4.1.2-37 explicitly transmits InfoType Cancellation for
    # record-breaking short-duration rain, not the other weather products.
    if any(hazard != 21 for hazard in hazards):
        _fail('unsupported_report', '気象の取消: Ww=21のみ確認済み')
    return codes


def _scope(report, bits):
    kind = type(report)
    if kind is dcr.Weather:
        return 'weather', _codes(_weather_fields(report, bits), weather_forecast_region, '気象 Pl')
    if kind is dcr.EarthquakeEarlyWarning:
        codes = tuple(index + 1 for index in range(80) if bits(130 + index, 1))
        _equal(report, 'eew_forecast_regions_raw', codes)
        return 'eew', _codes(codes, eew_forecast_region, '緊急地震速報 Pl', excluded=range(71, 81))
    if kind is dcr.SeismicIntensity:
        rows = _slots(bits, 69, (3, 6), 16, 'Es / Pl')
        values = _column(report, rows, 0, 'seismic_intensities_raw')
        codes = _column(report, rows, 1, 'prefectures_raw')
        if any(not 1 <= value <= 7 for value in values) or any(not code for code in codes):
            _fail('invalid_fields', '震度 Es / Pl')
        return 'prefectures', _codes(codes, prefecture, '震度 Pl')
    if kind is dcr.Tsunami:
        rows = _slots(bits, 84, (12, 4, 10), 5, 'Ta / Th / Pl')
        values = _column(report, rows, 1, 'tsunami_heights_raw')
        codes = _column(report, rows, 2, 'tsunami_forecast_regions_raw')
        if any(value not in tsunami_height for value in values) or any(not code for code in codes):
            _fail('invalid_fields', '津波 Th / Pl')
        return 'tsunami', _codes(codes, tsunami_forecast_region, '津波 Pl', excluded=(1000,))
    if kind is dcr.Flood:
        rows = _slots(bits, 53, (4, 40), 3, 'Lv / Pl')
        values = _column(report, rows, 0, 'flood_warning_levels_raw')
        codes = _column(report, rows, 1, 'flood_forecast_regions_raw')
        if any(value not in (1, 2, 3, 4) for value in values) or any(not code for code in codes):
            _fail('invalid_fields', '洪水 Lv / Pl')
        if any(code % 10_000_000_000 == 9_999_999_999 for code in codes):
            _fail('unknown_area', '洪水 Pl: 個別の河川区間ではない指定')
        return 'flood', _codes(codes, flood_forecast_region, '洪水 Pl')
    if kind is dcr.Volcano:
        code = bits(76, 12)
        _equal(report, 'volcano_name_raw', code)
        _equal(report, 'volcanic_warning_code_raw', bits(69, 7))
        if bits(69, 7) not in volcanic_warning_code:
            _fail('invalid_fields', '火山 Dw')
        _codes((code,), volcano_name, '火山 Vo', excluded=(900, 901, 902, 4000))
        rows = _slots(bits, 88, (23,), 5, '火山 Pl')
        codes = _column(report, rows, 0, 'local_governments_raw')
        _codes(codes, local_government, '火山 Pl')
        # Only the stable catalogue position is selected. No cancelled
        # crater radius, warning extent or inferred municipal union is drawn.
        return 'volcano', (code,)
    if kind is dcr.AshFall:
        _equal(report, 'volcano_name_raw', bits(71, 12))
        _codes((bits(71, 12),), volcano_name, '降灰 Vo', excluded=(900, 901, 902, 4000))
        _equal(report, 'ash_fall_warning_type_raw', bits(69, 2))
        if bits(69, 2) not in (1, 2):
            _fail('invalid_fields', '降灰 Dw1')
        rows = _slots(bits, 83, (3, 3, 23), 4, 'Ho / Dw2 / Pl')
        hours = _column(report, rows, 0, 'expected_ash_fall_times_raw')
        values = _column(report, rows, 1, 'ash_fall_warning_codes_raw')
        codes = _column(report, rows, 2, 'local_governments_raw')
        if any(not 1 <= hour <= 6 for hour in hours) or any(value not in (1, 2, 3, 4) for value in values):
            _fail('invalid_fields', '降灰 Ho / Dw2')
        if any(code % 100000 == 99999 for code in codes):
            _fail('unknown_area', '降灰 Pl: 個別の市町村ではない指定')
        return 'municipalities', _codes(codes, local_government, '降灰 Pl')
    if kind in (dcr.NorthwestPacificTsunami, dcr.Marine):
        _fail('unsupported_report', 'このDCR種別はIt=2未定義')
    _fail('no_area', '取消通報に独立した固定区域の指定なし')


def validate_cancellation(reports):
    """Check message identity and the reviewed cancellation product types.

    The separately confirmed reference path must use this too: a supplied
    original cannot make an undefined cancellation type authoritative. This
    does not require the cancellation to carry its own usable geography.
    """
    reports = tuple(reports)
    if not 1 <= len(reports) <= 32 or type(reports[0]) not in _CATEGORIES:
        _fail('unsupported_report', 'DCR取消通報が必要')
    first = reports[0]
    if any(type(report) is not type(first) for report in reports):
        _fail('invalid_fields', '取消通報の種別が混在')
    key = lambda report: (getattr(report, 'report_time', None),
                           getattr(report, 'report_classification_no', None),
                           getattr(report, 'information_type_no', None))
    if any(key(report) != key(first) for report in reports):
        _fail('invalid_fields', '取消通報の発表時刻・区分が混在')
    for report in reports:
        bits = _wire(report)
        if type(report) in (dcr.NorthwestPacificTsunami, dcr.Marine):
            _fail('unsupported_report', 'このDCR種別はIt=2未定義')
        if type(report) is dcr.Weather:
            _weather_fields(report, bits)
    return reports


def plan_scope(reports):
    """Select complete static geography, or reject the entire cancellation.

    This does not identify a previous report. An externally confirmed original
    reference is handled separately by the offline cancellation renderer.
    """
    reports = validate_cancellation(reports)
    layers, result = set(), set()
    for report in reports:
        layer, codes = _scope(report, _wire(report))
        if not codes:
            _fail('no_area', '取消通報の一部に区域指定なし')
        layers.add(layer)
        result.update(codes)
    if len(layers) != 1:
        _fail('invalid_fields', '取消背景の区域種別が混在')
    if not result:
        _fail('no_area', '取消通報に区域指定なし')
    return Scope(layers.pop(), tuple(sorted(result)))


def validate_reference(reports):
    """Validate full source slots before reproducing an explicit original map.

    The normal drawers validate their forecast values and geometry. This
    additional check covers the decoder's zero-termination behaviour and
    product families: a Ww=21 cancellation cannot refer to another weather
    product. It does not establish the original/cancellation relationship.
    """
    reports = tuple(reports)
    if not 1 <= len(reports) <= 32 or type(reports[0]) not in _CATEGORIES:
        _fail('unsupported_report', '参照元DCR通報が必要')
    first = reports[0]
    key = lambda report: (type(report), getattr(report, 'report_time', None),
                           getattr(report, 'report_classification_no', None),
                           getattr(report, 'information_type_no', None))
    if any(key(report) != key(first) for report in reports):
        _fail('invalid_fields', '参照元の発表時刻・種別・区分が混在')
    for report in reports:
        bits = _wire(report, information=(0, 1))
        if type(report) in (dcr.Hypocenter, dcr.Typhoon):
            # These are the explicitly supplied *original* coordinates, whose
            # full geometry is checked by the normal source drawer.
            continue
        _, codes = _scope(report, bits)
        if not codes:
            _fail('no_area', '参照元通報の一部に区域指定なし')
    return reports
