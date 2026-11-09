"""Weather label colours preserve the meaning of real decoded DCR frames."""

import copy
import unittest

from image_service.weather_map import weather_plan as legacy_plan
from image_service.weather_map_v4 import weather_plan
from test_regional_reports import report


def received_hazards(*hazards, state=1, region=130010):
    """Replace the transmitted entries, then decode a CRC-valid training frame."""
    fields = [(53, 3, state), (56, 144, 0)]
    for index, hazard in enumerate(hazards):
        fields.extend(((56 + index * 24, 5, hazard),
                       (61 + index * 24, 19, region)))
    return report('weather', fields)


class WeatherMapV4Tests(unittest.TestCase):
    def test_every_emergency_type_uses_black_without_inventing_an_evacuation_level(self):
        names = ('暴風雪特別警報', '大雨特別警報', '暴風特別警報',
                 '大雪特別警報', '波浪特別警報', '高潮特別警報')
        for hazard, name in enumerate(names, 1):
            with self.subTest(hazard=hazard):
                received = received_hazards(hazard)
                self.assertEqual(received.weather_related_disaster_sub_categories_raw, [hazard])
                notice, = weather_plan([received]).notices
                self.assertEqual(notice.lines, (name,))
                style, = notice.styles
                self.assertEqual(style.background, '#0c000c')
                self.assertEqual((style.foreground, style.edge), ('#ffffff', '#ffffff'))
                self.assertNotIn('レベル', notice.lines[0])

    def test_landslide_alert_keeps_its_name_and_official_purple(self):
        notice, = weather_plan([received_hazards(23)]).notices
        self.assertEqual(notice.lines, ('土砂災害警戒情報',))
        self.assertEqual(notice.styles[0].background, '#aa00aa')
        self.assertEqual(notice.styles[0].foreground, '#ffffff')
        self.assertNotIn('危険警報', notice.lines[0])

    def test_rain_record_and_tornado_information_are_not_classified_as_normal_warnings(self):
        notice, = weather_plan([received_hazards(21, 22)]).notices
        self.assertEqual(notice.lines, ('記録的短時間大雨情報', '竜巻注意情報'))
        self.assertEqual(len(notice.styles), 2)
        for style in notice.styles:
            self.assertEqual(style.background, 'none')
            self.assertEqual(style.foreground, '#ffffff')
            self.assertNotIn(style.edge, ('#ff2800', '#f2e700', '#aa00aa', '#0c000c'))
            self.assertNotIn(style.label, ('警報', '注意報', '危険警報', '特別警報'))

    def test_grouping_keeps_each_information_colour_instead_of_applying_the_highest_to_all(self):
        first = received_hazards(22, 2, 22)
        second = received_hazards(23, 21, 2)
        notice, = weather_plan([first, second, first]).notices
        self.assertEqual(notice.lines, ('竜巻注意情報', '大雨特別警報',
                                      '土砂災害警戒情報', '記録的短時間大雨情報'))
        self.assertEqual(tuple(style.background for style in notice.styles),
                         ('none', '#0c000c', '#aa00aa', 'none'))
        self.assertEqual(notice.color, '#ffffff', 'The region itself has no inferred severity')

    def test_release_drops_active_severity_but_preserves_every_released_information_name(self):
        for hazard in (1, 2, 3, 4, 5, 6, 21, 22, 23):
            with self.subTest(hazard=hazard):
                active, = weather_plan([received_hazards(hazard)]).notices
                released, = weather_plan([received_hazards(hazard, state=2)]).notices
                self.assertEqual(released.lines, (active.lines[0] + '（解除）',))
                self.assertEqual(released.styles[0].background, 'none')
                self.assertEqual(released.styles[0].foreground, '#c8c8cb')
                self.assertEqual(released.styles[0].edge, '#c8c8cb')
                self.assertEqual(released.styles[0].label, '解除')

    def test_code_seven_only_releases_emergency_warnings_and_never_announces_them(self):
        notice, = weather_plan([received_hazards(7, state=2)]).notices
        self.assertEqual(notice.lines, ('全ての気象特別警報（解除）',))
        self.assertEqual(notice.styles[0].background, 'none')
        with self.assertRaises(ValueError):
            weather_plan([received_hazards(7)])

    def test_new_styles_do_not_mutate_received_reports_or_the_published_v3_plan(self):
        received = report('weather')
        original_fields = copy.deepcopy(vars(received))
        original_plan = legacy_plan([received])
        current = weather_plan(iter([received]))
        self.assertEqual(vars(received), original_fields)
        self.assertEqual(legacy_plan([received]), original_plan)
        legacy_tokyo = next(n for n in original_plan.notices if n.code == 130010)
        self.assertEqual(legacy_tokyo.color, '#ca91e8')
        self.assertEqual(next(n.color for n in original_plan.notices if n.code == 110000),
                         '#ffce6c')
        self.assertTrue(all(n.color == '#ffffff' for n in current.notices))
        self.assertEqual(current.note, original_plan.note)
        self.assertIn('区域全体の警戒・解除', current.note)
        self.assertIn('未掲載地域の安全', current.note)
        self.assertEqual(current.legend, ())

    def test_one_unknown_value_cannot_be_dropped_to_produce_a_plausible_partial_map(self):
        cases = ([(56, 5, 31)], [(56, 5, 8)], [(61, 19, 500000)],
                 [(61, 19, 130040)], [(61, 19, 499999)], [(53, 3, 0)],
                 [(14, 3, 1)])
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                weather_plan([report('weather'), report('weather', fields)])

    def test_cancellation_and_mixed_bulletins_cannot_look_like_active_or_released_weather(self):
        cases = ([report('weather', [(41, 2, 2)])],
                 [report('weather', [(41, 2, 2), (53, 3, 2)])],
                 [received_hazards(2), received_hazards(2, state=2)],
                 [report('weather'), report('weather', [(41, 2, 1)])],
                 [report('weather'), report('marine')], [received_hazards()], [])
        for reports in cases:
            with self.subTest(reports=reports), self.assertRaises(ValueError):
                weather_plan(reports)

    def test_mismatched_decoded_arrays_are_not_silently_zipped_into_partial_labels(self):
        received = report('weather')
        received.weather_forecast_regions_raw.pop()
        with self.assertRaises(ValueError):
            weather_plan([received])


if __name__ == '__main__':
    unittest.main()
