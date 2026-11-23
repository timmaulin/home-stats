import unittest
from datetime import date

from usage import (
    build_daily_data,
    gas_m3_to_kwh,
    merge_history,
    select_meter,
    select_property,
)


class UsageTest(unittest.TestCase):
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
