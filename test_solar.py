import unittest
from datetime import datetime, timedelta

from solar import TZ, daily_metrics, max_heating_run


class ForecastTest(unittest.TestCase):
    def test_solar_after_three_hour_shade_enables_a_longer_run_with_a_later_start(self):
        start = datetime(2026, 11, 20, 7, tzinfo=TZ)
        slots = {
            start + timedelta(minutes=30 * i): {
                "Roof panels": {"est": 0 if i < 6 else 900, "p10": 0}
            }
            for i in range(12)
        }
        metrics = daily_metrics(slots, start.date())
        self.assertEqual(metrics["obstruction"], {"start": "07:00", "end": "10:00"})
        self.assertEqual(metrics["total_kwh"], 2.7)
        heating = metrics["battery"]["heating"]
        self.assertEqual(heating["expected"], {"hours": 5, "start": "08:00", "end": "13:00"})
        self.assertEqual(heating["cloudy"], {"hours": 1.5, "start": "08:00", "end": "09:30"})
        self.assertFalse(metrics["battery"]["scenarios"][1]["expected"]["empty"])

    def test_partial_day_total_uses_exact_shade_boundary_even_without_battery_results(self):
        start = datetime(2026, 11, 20, 9, 30, tzinfo=TZ)
        slots = {
            start + timedelta(minutes=30 * i): {"Roof panels": {"est": 400}}
            for i in range(10)
        }
        metrics = daily_metrics(slots, start.date(), has_cloudy=False)
        self.assertEqual(metrics["obstruction"], {"start": "07:00", "end": "10:00"})
        self.assertEqual(metrics["total_kwh"], 1.8)
        self.assertIsNone(metrics["battery"])

    def test_full_sun_allows_the_entire_heating_window(self):
        self.assertEqual(
            max_heating_run([900] * 12, 900),
            {"hours": 5, "start": "08:00", "end": "13:00"},
        )
