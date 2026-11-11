"""Shared CAMF instruction semantics; no rendering, image URLs or receiver state."""

from azarashi.definitions.camf.a2_country_region_name import a2_country_region_name
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
