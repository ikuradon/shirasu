"""Offline cancellation context: confirmed reference, received scope, then none.

No original-report lookup is inferred from a time, region, or latest post. An
optional reference is an explicit assertion by the offline caller that its
relationship was independently confirmed. It never enters a published URL.
"""

from azarashi.reports import dcr
import numpy as np

from image_service import ash_map, flood_map, region_map, volcano_map
from image_service import preview_regions
from image_service import preview_geometry_cache as cached_geometry
from image_service import preview_cancel_map as background
from image_service import preview_cancel_card as card
from image_service import preview_wide_layout as wide
from image_service import render as base
from image_service.preview_cancel_scope import plan_scope, validate_cancellation, validate_reference
from image_service.preview_diagnostics import Decision, PreviewFallback, error_reasons, reason, unique_reasons
from image_service.regional_reports import RegionMap, RegionNotice


def _scope_sequences(scope):
    """Resolve every received code; do not salvage a partial geographic scope."""
    if scope.layer in ('weather', 'eew', 'prefectures', 'tsunami'):
        plan = RegionMap(scope.layer, '', '', tuple(
            RegionNotice(code, '', (), wide.MUTED) for code in scope.codes), '')
        try:
            _, _, _, full, _ = preview_regions._regions(plan)
        except (KeyError, ValueError) as error:
            raise PreviewFallback('missing_boundary', stage='area',
                                  detail='取消報に記載された区域の地図', technical=str(error)) from error
        return full
    if scope.layer == 'flood':
        catalogue = flood_map._rivers()
        if any(code not in catalogue for code in scope.codes):
            raise PreviewFallback('missing_boundary', stage='area', detail='取消報の河川予報区域')
        return tuple(part for code in scope.codes for part in flood_map._parts(catalogue[code]))
    if scope.layer == 'municipalities':
        catalogue = ash_map._municipalities()
        if any(code not in catalogue for code in scope.codes):
            raise PreviewFallback('missing_boundary', stage='area', detail='取消報の市町村')
        return tuple(ring for code in scope.codes
                     for polygon in cached_geometry.geometry(catalogue[code], 'prefectures', 139.)
                     for ring in polygon)
    if scope.layer == 'volcano':
        catalogue = volcano_map._positions()
        points = []
        for code in scope.codes:
            if str(code) not in catalogue:
                raise PreviewFallback('missing_position', stage='area', detail='取消報の火山')
            lon, lat = region_map._position(catalogue[str(code)]['coordinates'])
            points.append((lon, float(base._my(lat))))
        return (np.asarray(points),)
    raise PreviewFallback('unsupported_geometry', stage='area', detail='取消報の区域種別')


def _reference_reports(reports, records, drawers):
    """Check an explicitly confirmed input; these checks do not establish identity."""
    validate_cancellation(reports)
    first = reports[0]
    original = base._decode(records)
    reference = original[0]
    if (not isinstance(first, dcr.Base) or first.information_type_no != 2
            or type(reference) is not type(first)
            or reference.information_type_no not in (0, 1)
            or reference.report_classification_no != first.report_classification_no
            or reference.version != first.version
            or reference.report_time > first.report_time
            or drawers.get(type(reference)) is None):
        raise ValueError('Incompatible confirmed cancellation reference')
    validate_reference(original)
    # Check every member of both bundles, not only the first frame. Separate
    # volcanoes or preliminary/detailed ash products cannot share a reference
    # merely because their issue time and generic DCR category match.
    for name in ('typhoon_number_raw', 'volcano_name_raw', 'ash_fall_warning_type_raw'):
        absent = {None} if name == 'ash_fall_warning_type_raw' else {None, 0}
        received = {getattr(report, name, None) for report in reports} - absent
        previous = {getattr(report, name, None) for report in original} - absent
        if received and (len(received) != 1 or received != previous):
            raise ValueError('Conflicting cancellation reference identity')
    return original


def draw(fig, reports, drawers, *, cancel_reference=None):
    reasons = [reason('cancelled')]
    mode = 'none'
    if cancel_reference is not None:
        try:
            original = _reference_reports(reports, tuple(cancel_reference), drawers)
            frame = background.capture_frame(drawers[type(original[0])], original)
            background.draw_frame(fig, frame, obstacles=card.CANCEL_OBSTACLES)
            mode = 'reference'
        except ValueError as error:
            fig.clear()
            reasons.append(reason('cancel_reference_unavailable', detail='明示された元情報を地図参照に使用できません'))
            reasons.extend(error_reasons(error, stage='geometry'))
    if mode == 'none':
        try:
            scope = plan_scope(reports)
            sequences = _scope_sequences(scope)
            background.draw_scope(fig, sequences, obstacles=card.CANCEL_OBSTACLES)
            mode = 'area'
        except ValueError as error:
            fig.clear()
            reasons.extend(error_reasons(error, stage='area'))
    try:
        if mode != 'none':
            wide.context_names(fig)
        card.draw_card(fig, reports, background=mode)
    except ValueError as error:
        if mode == 'none':
            raise
        fig.clear()
        reasons.extend(error_reasons(error, stage='layout'))
        mode = 'none'
        card.draw_card(fig, reports, background=mode)
    if mode != 'none':
        reasons.append(reason('cancel_reference_map' if mode == 'reference' else 'cancel_area_map',
                              stage='geometry' if mode == 'reference' else 'area'))
    return Decision('map' if mode != 'none' else 'summary', unique_reasons(reasons))
