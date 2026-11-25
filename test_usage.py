import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from usage import (
    build_daily_data,
    fetch_consumption,
    fetch_temperatures,
    gas_m3_to_kwh,
    history_bounds,
    merge_history,
    select_meter,
    select_property,
)


class UsageTest(unittest.TestCase):
    def test_history_window_is_two_years(self):
        start, end = history_bounds(date(2026, 11, 24))
        self.assertEqual(start, date(2024, 11, 25))
        self.assertEqual(end, date(2026, 11, 24))

    @patch("usage.octopus_get")
    def test_consumption_fetches_all_pages(self, get):
        get.side_effect = [
            {"results": [{"interval_start": "2026-11-24T00:00:00Z", "consumption": 3}], "next": "https://api.octopus.energy/next"},
            {"results": [{"interval_start": "2024-11-25T00:00:00Z", "consumption": 2}], "next": None},
        ]
        start = datetime(2024, 11, 25, tzinfo=timezone.utc)
        end = datetime(2026, 11, 25, tzinfo=timezone.utc)
        result = fetch_consumption("/meter", "key", start, end)
        self.assertEqual(result, {"2026-11-24": 3, "2024-11-25": 2})
        self.assertEqual(get.call_args_list[0].kwargs["params"]["period_from"], start.isoformat())
        self.assertIsNone(get.call_args_list[1].kwargs["params"])

    @patch("usage.requests.get")
    def test_weather_archive_requests_full_date_range(self, get):
        get.return_value.json.return_value = {"daily": {
            "time": ["2024-11-25", "2026-11-24"],
            "temperature_2m_mean": [5.0, 7.0],
        }}
        result = fetch_temperatures(51.5, -0.1, date(2024, 11, 25), date(2026, 11, 24))
        self.assertEqual(result, {"2024-11-25": 5.0, "2026-11-24": 7.0})
        self.assertEqual(get.call_args.args[0], "https://archive-api.open-meteo.com/v1/archive")
        self.assertEqual(get.call_args.kwargs["params"]["start_date"], "2024-11-25")
        self.assertEqual(get.call_args.kwargs["params"]["end_date"], "2026-11-24")

    def test_selects_first_property_with_meters(self):
        account = {
            "properties": [
                {"postcode": "AA1 1AA"},
                {
                    "postcode": "BB2 2BB",
                    "moved_out_at": "2025-01-01",
                    "gas_meter_points": [{"mprn": "old"}],
                },
                {"postcode": "CC3 3CC", "gas_meter_points": [{"mprn": "current"}]},
            ]
        }
        self.assertEqual(select_property(account)["postcode"], "CC3 3CC")

    def test_prefers_active_meter(self):
        point = {
            "meters": [
                {"serial_number": "old", "is_active": False},
                {"serial_number": "current", "is_active": True},
            ]
        }
        self.assertEqual(select_meter(point)["serial_number"], "current")

    def test_keeps_missing_readings_distinct_from_zero(self):
        rows = build_daily_data(
            date(2026, 1, 1),
            date(2026, 1, 2),
            {"2026-01-01": 0},
            {},
            {"2026-01-01": 8.5},
        )
        self.assertEqual(rows[0]["electricity_kwh"], 0)
        self.assertIsNone(rows[0]["gas_kwh"])
        self.assertIsNone(rows[1]["electricity_kwh"])

    def test_converts_gas_volume_to_estimated_kwh(self):
        self.assertAlmostEqual(gas_m3_to_kwh(1), 11.079, places=3)

    def test_new_downloads_extend_and_refresh_history(self):
        rows = merge_history(
            [{"date": "2026-01-01", "electricity_kwh": 5}],
            [
                {"date": "2026-01-01", "electricity_kwh": 6},
                {"date": "2026-01-02", "electricity_kwh": 7},
            ],
        )
        self.assertEqual([row["electricity_kwh"] for row in rows], [6, 7])

    def test_missing_refresh_does_not_erase_saved_reading(self):
        rows = merge_history(
            [{"date": "2026-01-01", "electricity_kwh": 5, "gas_kwh": 11}],
            [{"date": "2026-01-01", "electricity_kwh": None, "gas_kwh": 12}],
        )
        self.assertEqual(rows[0]["electricity_kwh"], 5)
        self.assertEqual(rows[0]["gas_kwh"], 12)

    def test_migrates_saved_gas_volume_to_kwh(self):
        rows = merge_history(
            [{"date": "2026-01-01", "gas_m3": 1}],
            [],
        )
        self.assertEqual(rows[0]["gas_kwh"], 11.079)
        self.assertNotIn("gas_m3", rows[0])


if __name__ == "__main__":
    unittest.main()
