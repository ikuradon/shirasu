"""Classify reviewed fallback literals without exposing exception text.

These are exact ValueError messages from the preview drawers and their shared
semantic/geographic helpers. This adapter adds no exception handling or new
fallback conditions. Unreviewed messages, dynamic values, and asset failures
remain unclassified; their text must never become a gallery diagnostic.
"""


# Keep the literals explicit: substring matching would let paths or report
# contents masquerade as a known constraint. Ambiguous source messages belong
# to invalid_fields rather than claiming one particular field was missing.
_MESSAGES_BY_CODE = {
    'layout_unavailable': (
        # preview_wide_layout.py and shared label placement.
        'Unbounded presentation text',
        'Wide title does not fit',
        'Wide scope does not fit',
        'Too many sidebar entries',
        'Sidebar heading does not fit',
        'Metric does not fit',
        'Complete sidebar does not fit',
        'Presentation text leaves the image',
        'Presentation text overlaps',
        'Label is too close to its marker',
        'Labels do not fit the image',
        'Regional labels do not fit',
        'Too many regional labels for a readable image',
        'Weather labels do not fit',
        'Too many forecast labels for a readable map',
        'Forecast labels do not fit',
        # preview_wide_points.py and preview_wide_regions.py.
        'Too many dated centres for a readable map',
        'Dated centres do not fit the map',
        'Dated centres have no clear label layout',
        'Dated centres do not fit outside the locator',
        'No readable reference point inside the received region',
        'Too many regional references',
        'Regional reference markers overlap',
        'Complete seismic badges do not fit inside their prefectures',
        'Seismic intensity label does not fit its badge',
        # preview_wide_special.py, preview_wide_flood.py, tsunami drawer.
        'Forecast numbers do not fit outside the locator',
        'Complete ash time matrix does not fit',
        'Complete flood warning does not fit',
        'Complete flood sidebar does not fit',
        'Complete river name does not fit outside the forecast reach',
        'Tsunami value label is too large',
        'Complete tsunami values do not fit outside the locator',
        'Cannot place a flood label on the flow line',
    ),
    'missing_boundary': (
        # region_map.py and eew_map.py.
        'Missing regional geometry',
        'No geometry for this EEW warning area',
    ),
    'missing_position': (
        # ash_map.py / preview_wide_special.py: the catalogue lookup failed.
        'No official position for this volcano',
    ),
    'unknown_area': (
        # These literals concern only region codes, not combined field checks.
        'Invalid regional code',
        'Unknown geographic region',
        'Unknown forecast region',
        'Unknown volcano municipality',
    ),
    'no_area': (
        'No EEW warning areas',
        'No received seismic intensity regions',
        'No received tsunami regions',
        'No marine forecast areas',
        'No received weather regions',
    ),
    'unsupported_geometry': (
        'Tsunami regions require coastline geometry',
        'Regional area requires polygon geometry',
    ),
    'projection_unavailable': (
        # preview_dcx_geometry.py: the complete WGS84 boundary is unusable.
        'DCX ellipse has no unique near WGS84 surface',
        'Invalid DCX WGS84 boundary',
        'DCX ellipse crosses the Mercator latitude limit',
        'DCX ellipse surrounds a pole or cannot be unwrapped locally',
        'DCX ellipse has an invalid projected boundary',
    ),
    'unsupported_report': (
        'Unsupported report',
        'Unsupported regional report bundle',
        'Unsupported wide regional map',
        'Unsupported regional preview map',
        'Unsupported forecast map',
        'Unsupported ash forecast map',
        'Unsupported flood forecast map',
        'Unsupported NWPTAC map',
        'Unsupported weather report classification',
        'Unsupported EEW report classification or information type',
        'Unsupported flood report classification',
        'Unsupported ash report classification',
        'Unsupported Northwest Pacific classification or information type',
        'This potential requires the complete bulletin text',
    ),
    'invalid_fields': (
        # Shared adapters and received-value consistency checks.
        'Mixed or cancelled regional announcement',
        'Missing regional timestamp',
        'Mixed earthquake occurrence times',
        'Unknown prefecture or seismic intensity',
        'Invalid tsunami arrival fields',
        'Invalid tsunami arrival day',
        'Unknown tsunami arrival code',
        'Missing tsunami arrival date',
        'Unknown or mixed tsunami warning types',
        'Unknown tsunami forecast region or height',
        'Unknown marine area or warning',
        'Unknown or mixed weather announcement states',
        'Unknown weather region or hazard',
        'All-weather release code in an announcement',
        'Unknown EEW value',
        'Reversed EEW prediction bounds',
        'EEW intensity none conflicts with another bound',
        'EEW open upper bound needs a defined lower bound',
        'Invalid EEW occurrence fields',
        'Invalid or future EEW occurrence time',
        'Inconsistent assumptive EEW hypocentre',
        'Different EEW earthquakes or predictions must not be combined',
        'Unknown volcano activity-time precision',
        'Unknown volcano position or warning',
        'Mixed volcano warning or activity times',
        'Inconsistent volcano municipality fields',
        'Missing flood issue time',
        'No flood forecast entries or too many entries',
        'Unknown flood forecast region or warning level',
        'Conflicting warning levels for one flood forecast reach',
        'Overlapping flood reaches have different warning levels',
        'Unknown or invalid ash forecast base time',
        'Missing ash issue time',
        'Ash activity time is inconsistent or in the future',
        'Unknown ash volcano or forecast type',
        'Different volcanoes, forecast types or activity times',
        'No ash forecast entries or too many entries',
        'Unknown municipality, ash forecast time or phenomenon',
        'Conflicting ash amounts for one municipality and time',
        'Invalid Northwest Pacific arrival fields',
        'Ambiguous arrival must not have a resolved time',
        'Undefined Northwest Pacific arrival time',
        'Arrival time disagrees with its UTC issue date and fields',
        'Missing Northwest Pacific issue time',
        'Different tsunami potentials must not be combined',
        'No received forecast points or too many entries',
        'Unknown Northwest Pacific point or tsunami height',
        'Conflicting forecasts for the same point',
        # Combined missing/duplicate checks do not prove an asset is missing.
        'Missing or duplicated municipality geometry',
        'Missing or duplicated flood forecast geometry',
        'Unknown or repeated NWPTAC forecast point',
        'Overlapping regions have conflicting colours',
        'Overlapping regions have conflicting colors',
        'Overlapping forecast regions have conflicting colours',
        'Unknown or mixed typhoon position, time or identity',
        'Conflicting typhoon centres at one reference time',
        'Hypocenter maps accept one report',
        'Unknown hypocenter position',
        'Inconsistent hypocenter advisory fields',
        'Unsupported number of tsunami forecast regions',
        'Unsupported number of NWPTAC forecast points',
        'Unsupported number of flood forecast reaches',
        'Unsupported number of ash forecast municipalities',
        # Pure DCX geometry validation; the mixed limit message does not tell
        # us whether an axis is invalid or the latitude is unprojectable.
        'Invalid DCX ellipse parameters',
        'Non-finite DCX ellipse parameters',
        'DCX ellipse is outside the supported projection or axis range',
        'Invalid DCX ellipse azimuth',
    ),
}
_CODES = {message: code for code, messages in _MESSAGES_BY_CODE.items() for message in messages}


def known_error_code(error):
    """Return a stable code for an exact reviewed ValueError, otherwise None."""
    if not isinstance(error, ValueError) or len(error.args) != 1 or type(error.args[0]) is not str:
        return None
    return _CODES.get(error.args[0])
