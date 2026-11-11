"""Recorded QZQSM messages survive the signed-image and offline-preview paths.

The corpus is pinned independently of these renderers. Official training
scenario values are checked separately from our existing synthetic edge cases.
"""

from collections import Counter
from datetime import datetime, timezone
import importlib.util
import struct
import unittest
from unittest.mock import patch

from image_protocol import make_path, parse_path
from image_publication import image_entry
from qzqsm_samples import decode_reports, official_scenarios

HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
KEY = bytes(range(32))  # Public test key, never a deployment credential.
RECEIVER = 'dcr_receiver_20260924'

if HAS_RENDERER:
    from matplotlib.colors import to_rgba
    from matplotlib.text import Text
    from image_service import render as base
    from image_service import render_v4 as published
    from image_service import render_preview as preview
    from image_service.ash_reports import ash_plan
    from image_service.regional_reports import tsunami_map


@unittest.skipUnless(HAS_RENDERER, 'Install image_service/requirements.txt for recorded-image tests')
class PublicQzqsmImageTests(unittest.TestCase):
    def setUp(self):
        self.reports = decode_reports(RECEIVER)

    def reports_of(self, category):
        return [report for report in self.reports if type(report).__name__ == category]

    def signed_records(self, reports):
        entries = tuple(image_entry(report) for report in reports)
        path = make_path(entries, 'fixture', KEY, version='v4')
        parsed = parse_path(path, {'fixture': KEY})
        self.assertCountEqual(parsed, entries)
        return parsed

    def capture(self, renderer, records):
        snapshots = []
        original = base.Figure.savefig

        def save(figure, *args, **kwargs):
            result = original(figure, *args, **kwargs)
            # Observe what was actually saved. In particular, leave clear()
            # intact because renderers use it when replacing a failed map.
            snapshots.append({
                'texts': '\n'.join(text.get_text() for text in figure.findobj(Text)),
                'axes_count': len(figure.axes),
                'collection_gids': frozenset(artist.get_gid()
                    for axes in figure.axes for artist in axes.collections
                    if artist.get_gid()),
                'ash_cells': [(artist.get_gid(), artist.get_facecolor())
                    for artist in figure.artists
                    if (artist.get_gid() or '').startswith('ash-cell-')],
                'ash_cell_texts': [(text.get_gid(), text.get_text())
                    for text in figure.texts
                    if (text.get_gid() or '').startswith('ash-cell-')],
            })
            return result

        with patch.object(base.Figure, 'savefig', save):
            image = renderer.render(records)
        self.assertEqual(image.png[:8], b'\x89PNG\r\n\x1a\n')
        expected = (1920, 1080) if renderer is preview else (1440, 1200)
        self.assertEqual(struct.unpack('>II', image.png[16:24]), expected)
        return image, snapshots[-1]

    def test_recorded_categories_reach_published_png_with_received_content(self):
        # Constants below are facts from the pinned recording, not text generated
        # by the current formatter and then saved as an expected snapshot.
        cases = {
            'Weather': ('竜巻注意情報', '上川・留萌地方', '石狩・空知・後志地方'),
            'Marine': ('海上警報', '網走沖', '北海道西方海上', '三陸沖西部', '風警報'),
            'EarthquakeEarlyWarning': ('訓練', '緊急地震速報', '日向灘'),
            'Hypocenter': ('訓練', '日向灘', '6.4'),
            'SeismicIntensity': ('訓練', '大分県', '宮崎県', '5強'),
            'Volcano': ('訓練', '阿蘇山', '噴火'),
        }
        for category, expected in cases.items():
            with self.subTest(category=category):
                report = self.reports_of(category)[0]
                image, snapshot = self.capture(published, self.signed_records([report]))
                self.assertEqual(image.category, category)
                text = snapshot['texts']
                for phrase in expected:
                    self.assertIn(phrase, text)
                self.assertEqual('訓練' in text, report.report_classification_no == 7)

    def test_long_tsunami_training_notice_retains_forecasts_and_marks_image_excerpt(self):
        report = self.reports_of('Tsunami')[0]
        records = self.signed_records([report])
        plan = tsunami_map(base._decode(records))
        self.assertEqual({notice.code for notice in plan.notices}, {600, 610, 760})
        self.assertEqual({notice.title for notice in plan.notices},
                         {'愛媛県宇和海沿岸', '高知県', '宮崎県'})
        for notice in plan.notices:
            self.assertEqual(notice.lines, ('予想高 3m', '到達予想 09/24 10:05 JST'))
        image, snapshot = self.capture(published, records)
        self.assertEqual(image.category, 'Tsunami')
        # This real scenario has too much advisory text for the frozen v4 map.
        # Its summary is deliberately an excerpt, never a partial geographic map.
        self.assertEqual(snapshot['axes_count'], 0)
        text = snapshot['texts']
        for phrase in ('訓練', '津波警報', '抜粋 / 全文は投稿本文を参照'):
            self.assertIn(phrase, text)

    def test_typhoon_keeps_all_seven_received_points_and_the_signed_date_context(self):
        reports = [report for report in self.reports_of('Typhoon')
                   if report.report_time == datetime(2026, 9, 24, 0, 50, tzinfo=timezone.utc)]
        self.assertEqual(len(reports), 7)
        self.assertEqual({report.typhoon_number_raw for report in reports}, {26})
        self.assertEqual([report.elapsed_time_from_reference_time_raw for report in reports],
                         [0, 12, 24, 48, 72, 96, 120])
        self.assertEqual([report.central_pressure_raw for report in reports],
                         [1002, 998, 994, 985, 975, 965, 965])
        records = self.signed_records(reports)
        decoded = base._decode(records)
        self.assertCountEqual([report.reference_time for report in decoded],
                              [report.reference_time for report in reports])
        image, snapshot = self.capture(published, records)
        self.assertEqual(image.category, 'Typhoon')
        text = snapshot['texts']
        self.assertIn('26号', text)
        self.assertNotIn('訓練', text)
        for day in (24, 25, 26, 27, 28, 29):
            self.assertIn(f'9/{day}', text)
        for pressure in (1002, 998, 994, 985, 975, 965):
            self.assertIn(str(pressure), text)

    def detailed_ash(self):
        return [report for report in self.reports_of('AshFall')
                if report.ash_fall_warning_type_raw == 2]

    def test_five_ash_frames_match_official_eighteen_forecast_entries(self):
        reports = self.detailed_ash()
        expected = official_scenarios()['ash_detail']
        self.assertEqual(len(reports), expected['frame_count'])
        self.assertEqual([len(report.local_governments_raw) for report in reports],
                         expected['entries_per_frame'])
        actual = []
        for report in reports:
            self.assertEqual(report.report_classification_no, expected['classification'])
            self.assertEqual(report.information_type_no, expected['information_type'])
            self.assertEqual(report.ash_fall_warning_type_raw, expected['warning_type'])
            self.assertEqual(report.volcano_name_raw, expected['volcano_code'])
            actual.extend(zip(report.expected_ash_fall_times_raw,
                              report.ash_fall_warning_codes_raw,
                              report.local_governments_raw, strict=True))
        self.assertCountEqual(actual,
            [(entry['hour'], entry['warning_code'], entry['municipality_code'])
             for entry in expected['entries']])
        # The eighteen entries are three municipalities at six forecast hours,
        # not eighteen different municipalities.
        self.assertEqual(Counter(city for _, _, city in actual),
                         {4321400: 6, 4342800: 6, 4343300: 6})
        plan = ash_plan(reports)
        self.assertEqual(plan.volcano_name, '阿蘇山')
        self.assertEqual({notice.code for notice in plan.notices},
                         {4321400, 4342800, 4343300})
        for notice in plan.notices:
            self.assertEqual(len(notice.lines), 6)
            for hour, line in enumerate(notice.lines, start=1):
                amount = 'やや多量の降灰' if hour == 1 and notice.code != 4343300 else '少量の降灰'
                self.assertEqual(line, f'基点＋{hour}時間：{amount}')
        self.assertEqual(ash_plan(list(reversed(reports)) + reports), plan)

    def test_ash_preview_preserves_all_six_hours_for_each_received_municipality(self):
        reports = self.detailed_ash()
        image, snapshot = self.capture(preview, tuple(image_entry(report) for report in reports))
        self.assertEqual(image.category, 'AshFall')
        text = snapshot['texts']
        for phrase in ('訓練', '詳細', '阿蘇山', '阿蘇市', '高森町', '南阿蘇村'):
            self.assertIn(phrase, text)
        # The wide preview uses a municipality-by-hour matrix. Match every
        # actual saved cell to the independently pinned official scenario,
        # including its wording and colour, rather than expecting the old
        # grouped prose labels or deriving expectations from the renderer.
        expected_cells, expected_texts = [], []
        styles = {1: ('少量', '#d9d9d9'), 2: ('やや\n多量', '#ffff00')}
        for entry in official_scenarios()['ash_detail']['entries']:
            identity = f'ash-cell-{entry["municipality_code"]}-{entry["hour"]}-ash'
            value, color = styles[entry['warning_code']]
            expected_cells.append((identity, to_rgba(color)))
            expected_texts.append((f'{identity}-{entry["warning_code"]}', value))
        self.assertEqual(len(expected_cells), 18)
        self.assertCountEqual(snapshot['ash_cells'], expected_cells)
        self.assertCountEqual(snapshot['ash_cell_texts'], expected_texts)
        for hour in range(1, 7):
            self.assertEqual(text.splitlines().count(f'＋{hour}'), 1)
        self.assertIn('基点からの時間', text)
        self.assertIn('単位：時間', text)
        self.assertNotIn('1〜6時間', text)
        anchors = {gid for gid in snapshot['collection_gids']
                   if gid.startswith('municipality-anchor-')}
        self.assertEqual(anchors, {'municipality-anchor-4321400',
                                  'municipality-anchor-4342800',
                                  'municipality-anchor-4343300'})

    def test_recorded_flood_issue_and_release_stay_distinct_in_preview(self):
        reports = self.reports_of('Flood')
        self.assertEqual([report.flood_warning_levels_raw for report in reports], [[2], [1]])
        for report, expected in zip(reports, ('氾濫警戒情報', '警報解除'), strict=True):
            with self.subTest(level=expected):
                image, snapshot = self.capture(preview, [image_entry(report)])
                self.assertEqual(image.category, 'Flood')
                text = snapshot['texts']
                for phrase in ('訓練', '鬼怒川', expected):
                    self.assertIn(phrase, text)


if __name__ == '__main__':
    unittest.main()
