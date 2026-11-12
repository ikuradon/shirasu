"""Shared DCX instruction semantics; no rendering, image URLs or receiver state."""

from dataclasses import dataclass
import math

from azarashi.decoders.camf.geometry import semi_axis
from azarashi.definitions.camf.a2_country_region_name import a2_country_region_name
from azarashi.definitions.qzss.dcx.ex2_evacuate_direction_type import ex2_evacuate_direction_type
from azarashi.reports import dcx


def _uint(camf, name, width):
    value = getattr(camf, name, None)
    return value if type(value) is int and 0 <= value < 1 << width else None


def is_evacuation_destination(report):
    """Whether the reviewed CAMF 1.2 C10=0 default instruction applies.

    Section 3.7.3.4 pairs the secondary evacuation destination with IC-A-03
    for the main ellipse. It does not mean that the main population has no
    instruction. A9/A10 must select the reviewed international library #1.
    Projection, image availability and foreign-specific Vn are independent.
    """
    if type(report) is not dcx.OutsideJapan:
        return False
    if (getattr(report, 'ignore_a12_to_a16', None) is not False
            or getattr(report, 'ignore_a17_to_a18', None) is not False):
        return False
    camf = getattr(report, 'camf', None)
    widths = (('a1', 2), ('a2', 9), ('a9', 1), ('a10', 3),
              ('a12', 16), ('a13', 17), ('a14', 5), ('a15', 5), ('a16', 6),
              ('a17', 2), ('a18', 15), ('c7', 2), ('c8', 3), ('c9', 5), ('c10', 5))
    fields = {name: _uint(camf, name, width) for name, width in widths}
    if any(value is None for value in fields.values()):
        return False
    if (fields['a2'] == 111 or fields['a2'] not in a2_country_region_name
            or fields['a9'] != 0 or fields['a10'] != 0
            or fields['a17'] != 2 or fields['c10'] != 0
            or fields['a15'] > fields['a14']):
        return False
    # C7-C10 are views into the transmitted A18 bits, not separate settings.
    packed = (fields['c7'] << 13) | (fields['c8'] << 10) | (fields['c9'] << 5) | fields['c10']
    return fields['a18'] == packed


@dataclass(frozen=True)
class MtInfoEvacuation:
    direction: int  # 0: leave the additional range, 1: head to that range.
    lon: float
    lat: float
    semi_major_km: float
    semi_minor_km: float
    azimuth: float  # East = 0; positive towards north.


class MtInfoEvacuationError(ValueError):
    def __init__(self, field, *, unsupported=False):
        self.field = field
        self.unsupported = unsupported
        super().__init__(f'MTInfo: {field}')


def mtinfo_evacuation(report):
    """Read the independently centred EX2-EX7 ellipse in DCX-004 section 5.10.

    The no-instruction sentinel requires *all six* fields to be zero. The pinned decoder
    tests only EX3-EX7 when setting its ignore flag, so EX2=1 on its own must
    be recovered from the transmitted fields. A pole or an unavailable map
    does not remove an otherwise valid received instruction from the post.
    """
    if type(report) is not dcx.MTInfo:
        return None
    camf = getattr(report, 'camf', None)

    def uint(name, width):
        value = _uint(camf, name, width)
        if value is None:
            raise MtInfoEvacuationError(name)
        return value

    values = tuple(uint(field, width) for field, width in (
        ('ex2', 1), ('ex3', 17), ('ex4', 17), ('ex5', 5), ('ex6', 5), ('ex7', 7)))
    ignored = getattr(report, 'ignore_ex2_to_ex7', None)
    if not any(values):
        if ignored is not True:
            raise MtInfoEvacuationError('ignore_ex2_to_ex7')
        return None

    fields = {name: uint(name, width) for name, width in (
        ('a1', 2), ('a2', 9), ('a3', 5), ('a9', 1), ('a10', 3), ('a17', 2), ('vn', 6))}
    for name, expected in (('a2', 111), ('a3', 4)):
        if fields[name] != expected:
            raise MtInfoEvacuationError(name)
    for name, expected in (('vn', 1), ('a9', 1), ('a10', 0), ('a17', 0)):
        if fields[name] != expected:
            raise MtInfoEvacuationError(name, unsupported=True)

    direction, latitude, longitude, major, minor, azimuth = values
    if minor > major:
        raise MtInfoEvacuationError('ex5/ex6')
    lat = round(-90 + 180 / 0x1FFFF * latitude, 6)
    lon = round(45 + 180 / 0x1FFFF * longitude, 6)
    a, b = round(semi_axis(major), 3), round(semi_axis(minor), 3)
    angle = round(-90 + 180 / 0x80 * azimuth, 5)
    expected = (
        ('ex2_evacuate_direction_type', ex2_evacuate_direction_type[direction]),
        ('ex3_additional_ellipse_centre_latitude', lat),
        ('ex4_additional_ellipse_centre_longitude', lon),
        ('ex5_additional_ellipse_semi_major_axis', a),
        ('ex6_additional_ellipse_semi_minor_axis', b),
        ('ex7_additional_ellipse_azimuth', angle),
    )
    decoder_zero_exception = direction == 1 and not any(values[1:]) and ignored is True
    if decoder_zero_exception:
        # Decode only the known sentinel mismatch, not arbitrary ignored data.
        for field, _ in expected:
            if getattr(report, field, None) is not None:
                raise MtInfoEvacuationError(field)
    else:
        if ignored is not False:
            raise MtInfoEvacuationError('ignore_ex2_to_ex7')
        for field, value in expected:
            actual = getattr(report, field, None)
            if isinstance(value, str):
                valid = actual == value
            else:
                valid = (not isinstance(actual, bool) and isinstance(actual, (int, float))
                         and math.isfinite(actual) and actual == value)
            if not valid:
                raise MtInfoEvacuationError(field)
    return MtInfoEvacuation(direction, lon - 360 if lon > 180 else lon, lat, a, b, angle)
