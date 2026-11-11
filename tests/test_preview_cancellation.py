"""Cancellation rendering uses complete neutral geography, never an active forecast."""

import importlib.util
import unittest
from unittest.mock import patch

from scripts.gallery_catalog import load_cases, _field_variant
from test_image_renderer_v3 import changed, sample

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from image_service import render as base
    from image_service import render_preview as preview
    from image_service import preview_cancellation as cancellation
    from image_service import preview_cancel_map as backgrounds
    from image_service import preview_wide_layout as wide


@unittest.skipUnless(HAS_RENDERER, 'Cancellation images use the isolated renderer')
class CancellationRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.slug: case for case in load_cases()}

    def capture(self, records, **kwargs):
        saved = []
        original = base.Figure.savefig

        def save(fig, *args, **options):
            result = original(fig, *args, **options)
            saved.append(fig)
            fig.clear = lambda *args, **kwargs: None
            self.addCleanup(lambda: base.Figure.clear(fig))
            return result

        with patch.object(base.Figure, 'savefig', save):
            image = preview.render(records, **kwargs)
        fig, = saved
        return image, fig

    def assert_plain_background(self, fig):
        self.assertTrue(fig.axes)
        main = fig.axes[0]
        self.assertEqual({p.get_gid() for p in main.patches}, {'v3-land'})
        self.assertEqual({c.get_gid() for c in main.collections}, {'preview-prefecture-boundaries'})
        self.assertTrue(all(line.get_gid() is None for line in main.lines))
        self.assertFalse(main.artists)
        self.assertFalse(any(getattr(text, '_wide_sidebar', False) for text in fig.texts))
        self.assertTrue(all(not (text.get_gid() or '').startswith(('wide-hazard', 'wide-summary'))
                            for text in fig.texts))
        for inset in fig.axes[1:]:
            self.assertEqual(inset.get_gid(), 'preview-locator')
            self.assertFalse(inset.texts or inset.collections or inset.lines or inset.artists)
            self.assertEqual(len(inset.patches), 3)
        status, = [text for text in fig.texts if text.get_gid() == 'wide-cancel-status']
        self.assertEqual(status.get_text(), '取消')
        self.assertGreaterEqual(status.get_fontsize(), 90)

    def test_complete_static_scopes_have_only_a_neutral_map_and_cancellation(self):
        cases = [(name, changed(name, (41, 2, 2))) for name in
                 ('eew', 'seismic_intensity', 'tsunami', 'volcano', 'ash_fall', 'flood')]
        cases.append(('weather', self.cases['weather-cancel-area'].records))
        for name, records in cases:
            with self.subTest(category=name):
                before = tuple(str(report) for report in base._decode(records))
                image, fig = self.capture(records)
                self.assertEqual(image.decision.mode, 'map', image.decision)
                self.assertIn('cancel_area_map', [r.code for r in image.decision.reasons])
                self.assert_plain_background(fig)
                self.assertEqual(tuple(str(report) for report in base._decode(records)), before)

    def test_missing_or_unsupported_scope_has_a_dedicated_card_with_no_map_credit(self):
        cases = [('unknown', self.cases['weather-cancel-unknown'].records),
                 ('unreviewed-weather', self.cases['weather-cancel'].records)]
        cases.extend((name, changed(name, (41, 2, 2))) for name in
                     ('typhoon', 'marine', 'northwest_pacific_tsunami'))
        for name, records in cases:
            with self.subTest(category=name):
                image, fig = self.capture(records)
                self.assertEqual(image.decision.mode, 'summary')
                self.assertFalse(fig.axes)
                labels = '\n'.join(t.get_text() for t in fig.texts)
                self.assertIn('取消報 発表日時', labels)
                self.assertNotIn('Natural Earth', labels)
                self.assertNotIn('解除', labels)

    def test_explicit_reference_recreates_the_current_preview_camera_without_forecasts(self):
        for slug in ('weather-cancel-reference', 'typhoon-cancel-reference'):
            case = self.cases[slug]
            original = base._decode(case.cancel_reference)
            drawer = preview.DRAWERS[type(original[0])]
            frame = backgrounds.capture_frame(drawer, original)
            with self.subTest(slug=slug):
                image, fig = self.capture(case.records, cancel_reference=case.cancel_reference)
                self.assertIn('cancel_reference_map', [r.code for r in image.decision.reasons])
                self.assertNotIn('cancel_area_map', [r.code for r in image.decision.reasons])
                self.assertEqual(tuple(fig.axes[0].get_xlim()) + tuple(fig.axes[0].get_ylim()), frame.limits)
                self.assert_plain_background(fig)
                self.assertIn('背景：元情報と同じ表示範囲', '\n'.join(t.get_text() for t in fig.texts))

    def test_incompatible_or_unrenderable_reference_falls_back_to_complete_received_scope(self):
        case = self.cases['weather-cancel-reference']
        invalid = [sample('typhoon'), (), case.records]
        # A source later than the cancellation cannot be the previous issue.
        invalid.append(tuple((_field_variant(nmea, ((35, 6, 59),)), ms)
                             for nmea, ms in case.cancel_reference))
        for reference in invalid:
            with self.subTest(reference=reference):
                image, fig = self.capture(case.records, cancel_reference=reference)
                self.assertEqual(image.decision.mode, 'map')
                self.assertIn('cancel_reference_unavailable', [r.code for r in image.decision.reasons])
                self.assertIn('cancel_area_map', [r.code for r in image.decision.reasons])
                self.assert_plain_background(fig)
        with patch.object(backgrounds, 'capture_frame', side_effect=ValueError('private reference error')):
            image, fig = self.capture(case.records, cancel_reference=case.cancel_reference)
        self.assertIn('cancel_area_map', [r.code for r in image.decision.reasons])
        self.assertNotIn('private reference error', str(image.decision.as_dict()))

    def test_missing_one_boundary_discards_the_whole_map_and_reference_never_applies_to_issue(self):
        case = self.cases['weather-cancel-area']
        with patch.object(cancellation, '_scope_sequences', side_effect=ValueError('missing boundary')):
            image, fig = self.capture(case.records)
        self.assertEqual(image.decision.mode, 'summary')
        self.assertFalse(fig.axes)
        with self.assertRaisesRegex(ValueError, 'requires a cancellation'):
            preview.render(sample('weather'), cancel_reference=sample('weather'))

    def test_reference_validation_rejects_different_weather_product_and_hidden_source_slots(self):
        case = self.cases['weather-cancel-reference']
        reports = base._decode(case.records)
        self.assertTrue(cancellation._reference_reports(reports, case.cancel_reference, preview.DRAWERS))
        for fields in (((56, 5, 2),), ((56, 24, 0), (80, 24, (21 << 19) | 140000))):
            reference = tuple((_field_variant(nmea, fields), ms) for nmea, ms in case.cancel_reference)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                cancellation._reference_reports(reports, reference, preview.DRAWERS)

    def test_reference_identity_checks_every_cancelled_volcano_and_ash_product(self):
        for name, start, width in (('volcano', 76, 12), ('ash_fall', 71, 12)):
            original = sample(name)
            first = base._decode(original)[0]
            other = 101 if first.volcano_name_raw != 101 else 102
            sentence, at = original[0]
            cancelled = _field_variant(sentence, ((41, 2, 2),))
            conflict = _field_variant(sentence, ((41, 2, 2), (start, width, other)))
            reports = base._decode(((cancelled, at), (conflict, at)))
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'reference identity'):
                cancellation._reference_reports(reports, original, preview.DRAWERS)
        original = sample('ash_fall')
        first = base._decode(original)[0]
        sentence, at = original[0]
        other = 1 if first.ash_fall_warning_type_raw == 2 else 2
        for value in (0, other):
            cancelled = _field_variant(sentence, ((41, 2, 2), (69, 2, value)))
            reports = base._decode(((cancelled, at),))
            with self.subTest(ash_kind=value), self.assertRaisesRegex(ValueError, 'reference identity'):
                cancellation._reference_reports(reports, original, preview.DRAWERS)


if __name__ == '__main__':
    unittest.main()
