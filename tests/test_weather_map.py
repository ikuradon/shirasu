"""Weather meanings use decoded training frames, not guessed warning footprints."""

import unittest

from image_service.weather_map import weather_plan, EMERGENCY, INFORMATION, RELEASE
from test_regional_reports import report


class WeatherMapTests(unittest.TestCase):
    def test_all_received_hazards_survive_grouping(self):
        received = report('weather')
        plan = weather_plan([received, received])
        self.assertEqual(received.report_classification_no, 7)
        self.assertEqual({n.code for n in plan.notices}, {130010, 140000, 120000, 110000})
        tokyo = next(n for n in plan.notices if n.code == 130010)
        self.assertEqual(tokyo.lines, ('大雨特別警報', '土砂災害警戒情報'))
        self.assertEqual(tokyo.color, EMERGENCY)
        self.assertEqual(next(n.color for n in plan.notices if n.code == 110000), INFORMATION)
        self.assertIn('区域全体の警戒・解除', plan.note)

    def test_release_is_not_report_cancellation(self):
        plan = weather_plan([report('weather', [(53, 3, 2)])])
        self.assertTrue(all(n.color == RELEASE for n in plan.notices))
        self.assertTrue(all('解除' in line for n in plan.notices for line in n.lines))
        with self.assertRaises(ValueError):
            weather_plan([report('weather', [(41, 2, 2)])])

    def test_code_seven_means_all_emergency_types_released(self):
        with self.assertRaises(ValueError):
            weather_plan([report('weather', [(56, 5, 7)])])
        plan = weather_plan([report('weather', [(53, 3, 2), (56, 5, 7)])])
        self.assertIn('全ての気象特別警報（解除）', next(n.lines for n in plan.notices if n.code == 130010))

    def test_unknown_and_internal_regions_do_not_become_partial_maps(self):
        for fields in ([(61, 19, 500000)], [(61, 19, 130040)], [(61, 19, 499999)],
                       [(56, 5, 31)], [(56, 5, 8)], [(53, 3, 0)], [(14, 3, 1)]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                weather_plan([report('weather', fields)])

    def test_mixed_state_and_empty_arrays_are_rejected(self):
        with self.assertRaises(ValueError):
            weather_plan([report('weather'), report('weather', [(53, 3, 2)])])
        with self.assertRaises(ValueError):
            weather_plan([report('weather', [(56, 144, 0)])])
        received = report('weather')
        received.weather_forecast_regions_raw.pop()
        with self.assertRaises(ValueError):
            weather_plan([received])


if __name__ == '__main__':
    unittest.main()
