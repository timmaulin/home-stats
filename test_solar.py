import unittest
from datetime import datetime, timedelta

from solar import BASELOAD_W, TZ, daily_metrics, max_heating_run


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
        self.assertEqual(metrics["obstruction"], {"start": "10:00", "end": "13:00"})
        self.assertEqual(metrics["total_kwh"], 0.0)
        heating = metrics["battery"]["heating"]
        self.assertLessEqual(heating["expected"]["hours"], 5)
        self.assertEqual(heating["cloudy"]["hours"], 2.5)
        self.assertEqual(BASELOAD_W, 200)

    def test_partial_day_total_uses_exact_shade_boundary_even_without_battery_results(self):
        start = datetime(2026, 11, 20, 9, 30, tzinfo=TZ)
        slots = {
            start + timedelta(minutes=30 * i): {"Roof panels": {"est": 400}}
            for i in range(10)
        }
        metrics = daily_metrics(slots, start.date(), has_cloudy=False)
        self.assertEqual(metrics["obstruction"], {"start": "09:00", "end": "12:00"})
        self.assertEqual(metrics["total_kwh"], 1.0)
        self.assertIsNone(metrics["battery"])

    def test_full_sun_allows_the_entire_heating_window(self):
        self.assertEqual(
            max_heating_run([900] * 12, 800),
            {"hours": 5, "start": "08:00", "end": "13:00"},
        )

    def test_obstruction_moves_with_the_first_positive_solar_hour(self):
        from solar import obstruction_window

        start = datetime(2026, 12, 20, 8, tzinfo=TZ)
        slots = {
            start: {"Roof panels": {"est": 0}},
            start + timedelta(hours=1): {"Roof panels": {"est": 200}},
            start + timedelta(hours=2): {"Roof panels": {"est": 400}},
        }
        obstruction = obstruction_window(slots)
        self.assertEqual(
            (obstruction[0].strftime("%H:%M"), obstruction[1].strftime("%H:%M")),
            ("09:00", "12:00"),
        )
