"""Cancellation geography is complete received context, not an inferred match."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import unittest

HAS_DECODER = importlib.util.find_spec('azarashi') is not None
if HAS_DECODER:
    import azarashi
    from image_service.preview_cancel_scope import Scope, plan_scope, validate_cancellation, validate_reference
    from image_service.preview_diagnostics import PreviewFallback
    from tools.preview.gallery_catalog import _field_variant, load_cases


@unittest.skipUnless(HAS_DECODER, 'Cancellation scope needs the pinned azarashi dependency')
class CancelScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def report(self, slug='weather', fields=()):
        sentence, at = self.cases[slug].records[0]
        if slug == 'weather':
            # The reviewed weather Cancellation product is short-duration
            # record rain, Ww=21. The old all-weather cancel case is not that.
            original = azarashi.decode(sentence, timestamp=datetime.fromtimestamp(at / 1000, timezone.utc))
            fields = tuple((56 + i * 24, 5, 21)
                           for i in range(len(original.weather_forecast_regions_raw))) + tuple(fields)
        sentence = _field_variant(sentence, ((41, 2, 2), *fields))
        return azarashi.decode(sentence, timestamp=datetime.fromtimestamp(at / 1000, timezone.utc))

    def assert_fallback(self, reports, code):
        with self.assertRaises(PreviewFallback) as raised:
            plan_scope(reports)
        self.assertEqual(raised.exception.reason.code, code)
        self.assertEqual(raised.exception.reason.stage, 'area')

    def test_static_scope_for_every_supported_family_preserves_received_codes(self):
        for slug, layer, attribute in (
                ('weather', 'weather', 'weather_forecast_regions_raw'),
                ('eew', 'eew', 'eew_forecast_regions_raw'),
                ('seismic-intensity', 'prefectures', 'prefectures_raw'),
                ('tsunami-warning', 'tsunami', 'tsunami_forecast_regions_raw'),
                ('flood-issue', 'flood', 'flood_forecast_regions_raw'),
                ('volcano', 'volcano', 'volcano_name_raw'),
                ('ash-detail', 'municipalities', 'local_governments_raw')):
            with self.subTest(slug=slug):
                report = self.report(slug)
                before = deepcopy(report.get_params())
                codes = getattr(report, attribute)
                if type(codes) is int:
                    codes = [codes]
                self.assertEqual(plan_scope((report,)), Scope(layer, tuple(sorted(set(codes)))))
                self.assertEqual(report.get_params(), before)

    def test_cancelled_forecast_coordinates_do_not_supply_background_locations(self):
        for slug in ('hypocenter', 'typhoon-track'):
            with self.subTest(slug=slug):
                self.assert_fallback((self.report(slug),), 'no_area')

    def test_information_types_not_defined_by_the_family_do_not_supply_maps(self):
        for slug in ('marine', 'nwp'):
            with self.subTest(slug=slug):
                self.assert_fallback((self.report(slug),), 'unsupported_report')
        # This historical synthetic fixture retained emergency warnings,
        # landslide warnings and tornado advisories while only changing It.
        self.assert_fallback((self.report('weather-cancel'),), 'unsupported_report')

    def test_reference_validation_keeps_product_limits_without_requiring_new_geography(self):
        for slug in ('typhoon-track', 'hypocenter'):
            report = self.report(slug)
            self.assertEqual(validate_cancellation((report,)), (report,))
        for slug in ('marine', 'nwp', 'weather-cancel'):
            with self.subTest(slug=slug), self.assertRaises(PreviewFallback) as raised:
                validate_cancellation((self.report(slug),))
            self.assertEqual(raised.exception.reason.code, 'unsupported_report')

    def test_original_reference_scope_checks_source_product_and_termination(self):
        for slug in ('weather-cancel-reference', 'typhoon-cancel-reference'):
            records = self.cases[slug].cancel_reference
            reports = tuple(azarashi.decode(n, timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
                            for n, ms in records)
            self.assertEqual(validate_reference(reports), reports)
        records = self.cases['weather-cancel-reference'].cancel_reference
        for fields, code in ((((56, 5, 2),), 'unsupported_report'),
                             (((56, 24, 0), (80, 24, (21 << 19) | 140000)), 'invalid_fields')):
            reports = tuple(azarashi.decode(_field_variant(n, fields),
                                           timestamp=datetime.fromtimestamp(ms / 1000, timezone.utc))
                            for n, ms in records)
            with self.subTest(fields=fields), self.assertRaises(PreviewFallback) as raised:
                validate_reference(reports)
            self.assertEqual(raised.exception.reason.code, code)

    def test_original_reports_and_release_reports_are_not_cancellations(self):
        for slug in ('weather', 'tsunami-release', 'flood-release'):
            sentence, at = self.cases[slug].records[0]
            report = azarashi.decode(sentence, timestamp=datetime.fromtimestamp(at / 1000, timezone.utc))
            with self.subTest(slug=slug):
                self.assert_fallback((report,), 'invalid_fields')

    def test_undefined_codes_never_drop_just_one_received_area(self):
        for slug, fields in (
                ('weather', ((61, 19, 524287),)),
                ('eew', ((200, 1, 1),)),  # Pl71 is reserved, not another prefecture.
                ('seismic-intensity', ((72, 6, 63),)),
                ('tsunami-warning', ((100, 10, 1000),)),
                ('flood-issue', ((57, 40, 899999999999),)),
                ('volcano', ((76, 12, 4000),)),
                ('ash-detail', ((89, 23, 1399999),))):
            with self.subTest(slug=slug):
                self.assert_fallback((self.report(slug, fields),), 'unknown_area')

    def test_zero_terminators_cannot_hide_later_nonzero_wire_slots(self):
        for slug, fields in (
                ('weather', ((56, 24, 0),)),
                ('seismic-intensity', ((69, 9, 0),)),
                ('tsunami-warning', ((84, 26, 0),)),
                ('flood-issue', ((53, 44, 0), (97, 44, (2 << 40) | 830303020300))),
                ('volcano', ((88, 23, 0), (111, 23, 120200))),
                ('ash-detail', ((83, 29, 0),))):
            with self.subTest(slug=slug):
                self.assert_fallback((self.report(slug, fields),), 'invalid_fields')

    def test_no_area_and_half_empty_pairs_do_not_become_a_partial_map(self):
        self.assert_fallback((self.report('weather', ((56, 144, 0),)),), 'no_area')
        self.assert_fallback((self.report('eew', ((130, 80, 0),)),), 'no_area')
        for slug, fields in (
                ('weather', ((61, 19, 0),)),
                ('seismic-intensity', ((69, 3, 0),)),
                ('tsunami-warning', ((100, 10, 0),)),
                ('flood-issue', ((53, 4, 0),)),
                ('ash-detail', ((83, 3, 0),))):
            with self.subTest(slug=slug):
                self.assert_fallback((self.report(slug, fields),), 'invalid_fields')

    def test_mutated_decoded_arrays_must_match_all_wire_slots(self):
        report = self.report()
        for attribute in ('weather_forecast_regions_raw', 'weather_related_disaster_sub_categories_raw'):
            for mutation in (lambda values: values[:-1], lambda values: [True, *values[1:]],
                             lambda values: [values[0] + 1, *values[1:]]):
                with self.subTest(attribute=attribute, mutation=mutation):
                    changed = deepcopy(report)
                    setattr(changed, attribute, mutation(getattr(changed, attribute)))
                    self.assert_fallback((changed,), 'invalid_fields')

    def test_core_fields_and_time_are_compared_to_the_actual_payload(self):
        for field, value in (('version', 0), ('disaster_category_no', 1),
                             ('report_classification_no', True), ('information_type_no', 0),
                             ('message', b''), ('report_time', datetime(2026, 11, 9))):
            with self.subTest(field=field):
                report = self.report()
                setattr(report, field, value)
                self.assert_fallback((report,), 'invalid_fields')
        report = self.report()
        report.report_time += timedelta(minutes=1)
        self.assert_fallback((report,), 'invalid_fields')

    def test_bundles_require_consistent_cancellation_context_and_keep_every_area(self):
        report = self.report()
        self.assertEqual(plan_scope((report, deepcopy(report))), plan_scope((report,)))
        self.assert_fallback((report, self.report('flood-issue')), 'invalid_fields')
        other = deepcopy(report)
        other.report_time += timedelta(minutes=1)
        self.assert_fallback((report, other), 'invalid_fields')
        self.assert_fallback((report, self.report('weather', ((56, 144, 0),))), 'no_area')
        self.assert_fallback((), 'unsupported_report')
        self.assert_fallback((report,) * 33, 'unsupported_report')
        self.assert_fallback((object(),), 'unsupported_report')


if __name__ == '__main__':
    unittest.main()
