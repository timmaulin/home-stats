import unittest
from datetime import date

from usage import build_daily_data, select_meter, select_property


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
        self.assertIsNone(rows[0]["gas_m3"])
        self.assertIsNone(rows[1]["electricity_kwh"])


if __name__ == "__main__":
    unittest.main()
