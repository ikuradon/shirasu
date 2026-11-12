"""Nankai preview cards show headers only and never touch multipart assembly."""

from dataclasses import replace
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from shirasu.images.nankai import header
from tools.release.prepare_image_service import FILES, PREVIEW_FILES
from tests.images import test_image_renderer as helpers


ROOT = Path(__file__).resolve().parents[2]
HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from azarashi.reports import dcr
    from image_service import preview_nankai as card
    from image_service import preview_wide_layout as wide
    from image_service import render as base
    from image_service import render_preview as preview


def records(name):
    rows = json.loads((ROOT / 'image_service/examples' / (name + '.json')).read_text())
    return tuple((row['nmea'], row['received_ms']) for row in rows)


class NankaiPreviewPackageTests(unittest.TestCase):
    def test_raw_metadata_and_headline_renderer_are_preview_only_dependencies(self):
        for name in ('shirasu/images/nankai.py', 'image_service/preview_nankai.py'):
            self.assertIn(name, PREVIEW_FILES)
            self.assertNotIn(name, FILES)
            self.assertTrue((ROOT / name).is_file())


@unittest.skipUnless(HAS_RENDERER, 'Nankai headline layout uses the isolated image renderer')
class NankaiHeadlineTests(unittest.TestCase):
    def figure(self):
        fig = wide.figure()
        self.addCleanup(fig.clear)
        return fig

    def labels(self, fig):
        return {label.get_gid(): label for label in fig.texts}

    def test_advisory_ended_and_cancel_samples_show_only_known_headers_and_jst(self):
        for name, keyword in (('nankai-advisory', '巨大地震注意'), ('nankai-ended', '調査終了'),
                              ('nankai-cancel', '取消')):
            with self.subTest(sample=name):
                fig = self.figure()
                metadata = header(records(name)[0])
                card.draw_card(fig, metadata)
                card.validate(fig)
                labels = self.labels(fig)
                self.assertEqual(fig.axes, [])
                self.assertEqual(labels['nankai-topic'].get_text(), keyword)
                self.assertEqual(labels['nankai-title'].get_text(), metadata.title)
                self.assertEqual(labels['nankai-sidebar-date'].get_text(), '2026/11/10')
                self.assertEqual(labels['nankai-sidebar-time'].get_text(), '09:10')
                self.assertEqual(labels['nankai-sidebar-zone'].get_text(), 'JST')
                self.assertEqual(labels['nankai-training'].get_text(), '【訓練・試験】')
                self.assertEqual(labels['nankai-sidebar-type'].get_text(), '取消' if name.endswith('cancel') else '発表')
                self.assertGreaterEqual(labels['nankai-topic'].get_fontsize() * wide.DPI / 72 * 360 / wide.WIDTH, 30)
                self.assertGreaterEqual(labels['nankai-training'].get_fontsize() * wide.DPI / 72 * 360 / wide.WIDTH, 11)
                all_text = '\n'.join(label.get_text() for label in fig.texts)
                for excluded in ('NMEA', 'ページ', '北緯', '東経', 'Natural Earth'):
                    self.assertNotIn(excluded, all_text)
                if metadata.information == 2:
                    self.assertNotIn('巨大地震注意', all_text)
                    self.assertEqual(labels['nankai-topic'].get_color(), card.STATUS_COLORS['取消'])

    def test_all_known_keywords_have_distinct_state_colors_and_training_is_independent(self):
        initial = header(records('nankai-advisory')[0])
        for serial, keyword in ((1, '調査中'), (2, '調査中'), (3, '調査中'),
                                 (4, '巨大地震警戒'), (5, '巨大地震注意'),
                                 (6, '調査終了'), (15, 'その他の情報')):
            with self.subTest(serial=serial):
                fig = self.figure()
                card.draw_card(fig, replace(initial, classification=3, serial=serial, information=1))
                card.validate(fig)
                labels = self.labels(fig)
                self.assertEqual(labels['nankai-topic'].get_text(), keyword)
                self.assertEqual(labels['nankai-topic'].get_color(), card.STATUS_COLORS[keyword])
                self.assertEqual(labels['nankai-sidebar-type'].get_text(), '訂正')
                self.assertNotIn('nankai-training', labels)
        self.assertEqual(len({card.STATUS_COLORS[keyword] for keyword in
                              ('調査中', '巨大地震警戒', '巨大地震注意', '調査終了', '取消')}), 5)

    def test_page_position_never_appears_and_cannot_change_the_headline(self):
        metadata = header(records('nankai-advisory')[0])
        first, another = self.figure(), self.figure()
        card.draw_card(first, metadata)
        card.draw_card(another, replace(metadata, page=42, total=63))
        self.assertEqual([label.get_text() for label in first.texts],
                         [label.get_text() for label in another.texts])
        self.assertEqual([label.get_position() for label in first.texts],
                         [label.get_position() for label in another.texts])

    def test_invalid_metadata_and_an_existing_map_are_rejected_before_drawing(self):
        metadata = header(records('nankai-advisory')[0])
        for invalid in (object(), replace(metadata, issued_at=datetime(2026, 11, 10)),
                        replace(metadata, classification=0), replace(metadata, information=3),
                        replace(metadata, serial=0)):
            with self.subTest(metadata=invalid):
                fig = self.figure()
                with self.assertRaises(ValueError):
                    card.draw_card(fig, invalid)
                self.assertEqual(fig.texts, [])
        fig = self.figure()
        fig.add_subplot()
        with self.assertRaisesRegex(ValueError, 'map'):
            card.draw_card(fig, metadata)
        self.assertEqual(fig.texts, [])

    def test_own_layout_validation_catches_large_text_entering_the_panel(self):
        fig = self.figure()
        card.draw_card(fig, header(records('nankai-advisory')[0]))
        self.labels(fig)['nankai-topic'].set_position((.50, .51))
        with self.assertRaises(ValueError):
            card.validate(fig)


@unittest.skipUnless(HAS_RENDERER, 'Nankai dispatch tests import the isolated image renderer')
class NankaiDispatchTests(unittest.TestCase):
    def render_with_mock_canvas(self, entries, **kwargs):
        fig = Mock()
        fig.findobj.return_value = []
        fig.savefig.side_effect = lambda output, **options: output.write(b'preview-png')
        with patch.object(preview, 'verify_assets'), patch.object(preview.layout, 'figure', return_value=fig), \
                patch.object(preview.nankai, 'draw_card') as draw, patch.object(preview.nankai, 'validate'), \
                patch.object(base, '_decode', side_effect=AssertionError('Must not assemble Nankai text')), \
                patch.object(base, '_reset_assembly', side_effect=AssertionError('Must not reset shared assembly')), \
                patch.object(preview.layout, 'summary', side_effect=AssertionError('Must not show text fragments')), \
                patch.object(preview.cancellation, 'draw', side_effect=AssertionError('Nankai has its own cancel topic')):
            image = preview.render(entries, **kwargs)
        fig.clear.assert_called_once_with()
        return image, draw

    def test_issue_and_cancel_use_the_topic_without_reading_or_resetting_global_assembly(self):
        pending = {8: object()}
        announcement = object()
        with patch.object(dcr.NankaiTroughEarthquake, 'reports', pending), \
                patch.object(dcr.NankaiTroughEarthquake, 'announcement', announcement), \
                patch.object(dcr.NankaiTroughEarthquake, 'completed', True):
            for name in ('nankai-advisory', 'nankai-ended', 'nankai-cancel'):
                with self.subTest(sample=name):
                    image, draw = self.render_with_mock_canvas(records(name)[:1])
                    self.assertEqual(image.png, b'preview-png')
                    self.assertEqual(image.category, 'NankaiTroughEarthquake')
                    self.assertEqual(image.decision.mode, 'topic')
                    self.assertEqual([why.code for why in image.decision.reasons],
                                     ['cancelled'] if name.endswith('cancel') else [])
                    self.assertEqual(draw.call_args.args[1], header(records(name)[0]))
                    self.assertIs(dcr.NankaiTroughEarthquake.reports, pending)
                    self.assertIs(dcr.NankaiTroughEarthquake.announcement, announcement)
                    self.assertTrue(dcr.NankaiTroughEarthquake.completed)

    def test_multi_page_mixed_and_unknown_nankai_inputs_never_reach_the_legacy_summary(self):
        entry = records('nankai-advisory')[0]
        unknown = ((helpers.change_fields(entry[0], [(53, 4, 0)]), entry[1]),)
        for entries in (records('nankai-advisory'), (entry, records('weather')[0]),
                        (records('weather')[0], entry), unknown):
            with self.subTest(entries=entries), patch.object(preview, 'verify_assets'), \
                    patch.object(base, '_decode', side_effect=AssertionError('No shared decoding')), \
                    patch.object(base, '_reset_assembly', side_effect=AssertionError('No shared reset')), \
                    patch.object(preview.layout, 'summary', side_effect=AssertionError('No fragment fallback')):
                with self.assertRaises(ValueError):
                    preview.render(entries)

    def test_a_topic_failure_is_explicit_instead_of_falling_back_to_bulletin_fragments(self):
        fig = Mock()
        with patch.object(preview, 'verify_assets'), patch.object(preview.layout, 'figure', return_value=fig), \
                patch.object(preview.nankai, 'draw_card', side_effect=ValueError('Headline cannot be drawn')), \
                patch.object(base, '_decode', side_effect=AssertionError('No shared decoding')), \
                patch.object(base, '_reset_assembly', side_effect=AssertionError('No shared reset')), \
                patch.object(preview.layout, 'summary', side_effect=AssertionError('No fragment fallback')):
            with self.assertRaisesRegex(ValueError, 'Headline cannot be drawn'):
                preview.render(records('nankai-advisory')[:1])
        fig.clear.assert_called_once_with()

    def test_non_nankai_rendering_keeps_the_existing_decode_and_cleanup_path(self):
        fig = Mock()
        fig.findobj.return_value = []
        fig.savefig.side_effect = lambda output, **options: output.write(b'weather-png')
        report = object.__new__(dcr.Weather)
        draw = Mock()
        with patch.object(preview, 'verify_assets'), patch.object(preview.layout, 'figure', return_value=fig), \
                patch.object(base, '_decode', return_value=(report,)) as decode, \
                patch.object(base, '_reset_assembly') as reset, \
                patch.object(base, '_metadata', return_value=('', '', False, False)), \
                patch.dict(preview.DRAWERS, {dcr.Weather: draw}), \
                patch.object(preview.layout, 'context_names'), patch.object(preview.layout, 'validate'), \
                patch.object(preview.nankai, 'draw_card', side_effect=AssertionError('Not a Nankai input')):
            entries = records('weather')
            image = preview.render(entries)
        decode.assert_called_once_with(entries)
        self.assertEqual(reset.call_count, 2)
        draw.assert_called_once_with(fig, (report,))
        self.assertEqual(image.png, b'weather-png')
        self.assertEqual(image.category, 'Weather')
        self.assertEqual(image.decision.mode, 'map')


if __name__ == '__main__':
    unittest.main()
