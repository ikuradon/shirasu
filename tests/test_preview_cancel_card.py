"""Cancellation state stays prominent without reusing received hazard values."""

from datetime import datetime, timezone
import importlib.util
import unittest


HAS_RENDERER = all(importlib.util.find_spec(name) for name in ('azarashi', 'matplotlib', 'numpy'))
if HAS_RENDERER:
    from azarashi.reports import dcr, dcx
    from image_service import preview_cancel_card as card
    from image_service import preview_wide_layout as wide


@unittest.skipUnless(HAS_RENDERER, 'Cancellation cards use the isolated image renderer')
class CancellationCardTests(unittest.TestCase):
    def figure(self):
        fig = wide.figure()
        self.addCleanup(fig.clear)
        return fig

    def report(self, report_type=None, *, training=False, state=2):
        # Deliberately omit all body fields: a card must never read forecast
        # coordinates, levels, typhoon number or decoded warning descriptions.
        report = object.__new__(report_type or dcr.Weather)
        report.report_time = datetime(2026, 11, 11, 3, 24, tzinfo=timezone.utc)
        report.report_classification_no = 7 if training else 3
        report.information_type_no = state
        return report

    def labels(self, fig):
        return {text.get_gid(): text for text in fig.texts}

    def text(self, fig):
        return '\n'.join(text.get_text() for text in fig.texts)

    def test_all_dcr_categories_show_cancellation_using_only_header_fields(self):
        types = (
            (dcr.EarthquakeEarlyWarning, '緊急地震速報'),
            (dcr.Hypocenter, '震源情報'),
            (dcr.SeismicIntensity, '震度情報'),
            (dcr.NankaiTroughEarthquake, '南海トラフ地震関連情報'),
            (dcr.Tsunami, '津波情報'),
            (dcr.NorthwestPacificTsunami, '北西太平洋津波情報'),
            (dcr.Volcano, '火山情報'),
            (dcr.AshFall, '降灰予報'),
            (dcr.Weather, '気象情報'),
            (dcr.Flood, '指定河川洪水予報'),
            (dcr.Typhoon, '台風情報'),
            (dcr.Marine, '海上警報'),
        )
        for report_type, expected in types:
            with self.subTest(category=report_type.__name__):
                fig = self.figure()
                card.draw_card(fig, (self.report(report_type),))
                labels = self.labels(fig)
                self.assertEqual(labels['wide-cancel-status'].get_text(), '取消')
                self.assertEqual(labels['wide-cancel-category'].get_text(), expected)
                self.assertEqual(labels['wide-cancel-time'].get_text(),
                                 '取消報 発表日時：2026/11/11 12:24 JST')
                self.assertEqual(fig.axes, [])
                self.assertNotIn('wide-cancel-training', labels)
                self.assertFalse(any((text.get_gid() or '').startswith('wide-sidebar') for text in fig.texts))
                self.assertNotIn('解除', self.text(fig))
                self.assertNotIn('安全', self.text(fig))
                self.assertNotIn('地形', self.text(fig))
                self.assertNotIn('区域', self.text(fig))

    def test_training_and_sns_size_are_distinct_from_the_cancellation_state(self):
        fig = self.figure()
        report = self.report(training=True)
        before = dict(report.__dict__)
        card.draw_card(fig, (report,))
        labels = self.labels(fig)
        self.assertEqual(labels['wide-cancel-training'].get_text(), '【訓練・試験】')
        self.assertEqual(labels['wide-cancel-status'].get_color(), wide.WHITE)
        status_pixels_at_360 = labels['wide-cancel-status'].get_fontsize() * wide.DPI / 72 * 360 / wide.WIDTH
        self.assertGreaterEqual(status_pixels_at_360, 30)
        self.assertEqual(report.__dict__, before)
        self.assertEqual(labels['wide-cancel-footer'].get_text(), '詳細は投稿本文を参照')

    def test_background_provenance_is_explicit_and_map_is_dimmed_without_a_right_panel(self):
        for kind, expected in (('area', '背景：取消報に記載された地域'),
                               ('reference', '背景：元情報と同じ表示範囲')):
            with self.subTest(background=kind):
                fig = self.figure()
                ax = fig.add_axes((0, 0, 1, 1))
                card.draw_card(fig, (self.report(),), background=kind)
                labels = self.labels(fig)
                self.assertEqual(labels['wide-cancel-context'].get_text(), expected)
                self.assertIn('区域：気象庁（加工）', labels['wide-cancel-credit'].get_text())
                self.assertIn('地形：Natural Earth', labels['wide-cancel-credit'].get_text())
                veil, = [artist for artist in fig.artists if artist.get_gid() == 'cancel-background-veil']
                self.assertEqual((veil.get_x(), veil.get_y(), veil.get_width(), veil.get_height()), (0, 0, 1, 1))
                self.assertGreater(veil.get_alpha(), 0)
                self.assertLess(veil.get_alpha(), 1)
                self.assertEqual(fig.axes, [ax])
                self.assertFalse(ax.lines or ax.collections or ax.patches)

    def test_card_labels_avoid_the_plain_locator_and_declared_geographic_label_area(self):
        fig = self.figure()
        card.draw_card(fig, (self.report(dcr.NankaiTroughEarthquake, training=True),))
        renderer = fig.canvas.get_renderer()
        lx, ly, lw, lh = wide.LOCATOR_POSITION
        for text in fig.texts:
            box = text.get_window_extent(renderer).transformed(fig.transFigure.inverted())
            self.assertFalse(box.x0 < lx + lw and box.x1 > lx and box.y0 < ly + lh and box.y1 > ly,
                             text.get_text())
            self.assertTrue(any(left <= box.x0 and bottom <= box.y0 and right >= box.x1 and top >= box.y1
                                for left, bottom, right, top in card.CANCEL_OBSTACLES), text.get_text())

    def test_issue_correction_and_dcx_all_clear_cannot_be_drawn_as_cancellation(self):
        for state in (0, 1, 3):
            with self.subTest(state=state):
                fig = self.figure()
                with self.assertRaises(ValueError):
                    card.draw_card(fig, (self.report(state=state),))
                self.assertEqual(fig.texts, [])
        fig = self.figure()
        with self.assertRaises(ValueError):
            card.draw_card(fig, (object.__new__(dcx.OutsideJapan),))
        self.assertEqual(fig.texts, [])

    def test_missing_context_invalid_time_and_mixed_announcements_fail_before_drawing(self):
        cases = []
        naive = self.report()
        naive.report_time = naive.report_time.replace(tzinfo=None)
        unknown = self.report()
        unknown.report_classification_no = 0
        later = self.report()
        later.report_time = datetime(2026, 11, 12, 3, 24, tzinfo=timezone.utc)
        cases.extend(((), (naive,), (unknown,), (self.report(), later),
                      (self.report(), self.report(dcr.Typhoon)),
                      (self.report(), self.report(training=True))))
        for reports in cases:
            with self.subTest(reports=reports):
                fig = self.figure()
                with self.assertRaises(ValueError):
                    card.draw_card(fig, reports)
                self.assertEqual(fig.texts, [])
        for kind in ('reference', 'area', 'guess'):
            with self.subTest(background=kind):
                fig = self.figure()
                with self.assertRaises(ValueError):
                    card.draw_card(fig, (self.report(),), background=kind)
                self.assertEqual(fig.texts, [])
        fig = self.figure()
        fig.add_axes((0, 0, 1, 1))
        with self.assertRaises(ValueError):
            card.draw_card(fig, (self.report(),))
        self.assertEqual(fig.texts, [])


if __name__ == '__main__':
    unittest.main()
