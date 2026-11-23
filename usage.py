#!/usr/bin/env python3
"""Build a static daily energy-usage and weather page from Octopus data."""

import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


BASE_URL = "https://api.octopus.energy"
OUT_DIR = Path("docs")
TZ = ZoneInfo("Europe/London")
DAYS = 90


def octopus_get(path_or_url, api_key, **kwargs):
    url = path_or_url if path_or_url.startswith("http") else f"{BASE_URL}{path_or_url}"
    response = requests.get(
        url,
        auth=(api_key, ""),
        timeout=30,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def select_property(account):
    properties = [
        prop
        for prop in account.get("properties", [])
        if prop.get("electricity_meter_points") or prop.get("gas_meter_points")
    ]
    if not account.get("properties"):
        raise RuntimeError("The Octopus account has no properties")
    for prop in properties:
        if not prop.get("moved_out_at"):
            return prop
    if properties:
        return properties[0]
    raise RuntimeError("No electricity or gas meter points found on the Octopus account")


def select_meter(point):
    meters = point.get("meters", [])
    if not meters:
        return None
    return next((meter for meter in meters if meter.get("is_active")), meters[-1])


def fetch_consumption(path, api_key, period_from, period_to):
    daily = defaultdict(float)
    url = f"{BASE_URL}{path}/consumption/"
    params = {
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "group_by": "day",
        "page_size": 100,
    }
    while url:
        payload = octopus_get(url, api_key, params=params)
        for result in payload.get("results", []):
            start = datetime.fromisoformat(result["interval_start"].replace("Z", "+00:00"))
            local_day = start.astimezone(TZ).date().isoformat()
            daily[local_day] += float(result["consumption"])
        url = payload.get("next")
        params = None
    return dict(daily)


def fetch_meter_usage(prop, api_key, period_from, period_to):
    electricity = defaultdict(float)
    gas = defaultdict(float)

    for point in prop.get("electricity_meter_points", []):
        if point.get("is_export"):
            continue
        meter = select_meter(point)
        if not meter:
            continue
        values = fetch_consumption(
            f"/v1/electricity-meter-points/{point['mpan']}/meters/{meter['serial_number']}",
            api_key,
            period_from,
            period_to,
        )
        for day, value in values.items():
            electricity[day] += value

    for point in prop.get("gas_meter_points", []):
        meter = select_meter(point)
        if not meter:
            continue
        values = fetch_consumption(
            f"/v1/gas-meter-points/{point['mprn']}/meters/{meter['serial_number']}",
            api_key,
            period_from,
            period_to,
        )
        for day, value in values.items():
            gas[day] += value

    return dict(electricity), dict(gas)


def fetch_coordinates(postcode):
    response = requests.get(
        f"https://api.postcodes.io/postcodes/{postcode}",
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json().get("result", {})
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if latitude is None or longitude is None:
        raise RuntimeError("Could not look up the postcode for weather data")
    return latitude, longitude


def fetch_temperatures(latitude, longitude, start_day, end_day):
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "daily": "temperature_2m_mean",
            "timezone": "Europe/London",
            "past_days": DAYS,
            "forecast_days": 1,
        },
        timeout=30,
    )
    response.raise_for_status()
    daily = response.json().get("daily", {})
    values = {
        day: temperature
        for day, temperature in zip(
            daily.get("time", []), daily.get("temperature_2m_mean", [])
        )
    }
    return {
        day: temperature
        for day, temperature in values.items()
        if start_day.isoformat() <= day <= end_day.isoformat()
    }


def build_daily_data(start_day, end_day, electricity, gas, temperatures):
    rows = []
    day = start_day
    while day <= end_day:
        key = day.isoformat()
        rows.append(
            {
                "date": key,
                "electricity_kwh": electricity.get(key),
                "gas_m3": gas.get(key),
                "temperature_c": temperatures.get(key),
            }
        )
        day += timedelta(days=1)
    return rows


def main():
    api_key = os.environ.get("OCTOPUS_API_KEY")
    account_number = os.environ.get("OCTOPUS_ACCOUNT_NUMBER")
    if not api_key or not account_number:
        raise RuntimeError(
            "Set OCTOPUS_API_KEY and OCTOPUS_ACCOUNT_NUMBER in GitHub Actions"
        )

    account = octopus_get(f"/v1/accounts/{account_number}/", api_key)
    prop = select_property(account)
    end_day = datetime.now(TZ).date() - timedelta(days=1)
    start_day = end_day - timedelta(days=DAYS - 1)
    period_from = datetime.combine(
        start_day, datetime.min.time(), tzinfo=TZ
    ).astimezone(timezone.utc)
    period_to = datetime.combine(
        end_day + timedelta(days=1), datetime.min.time(), tzinfo=TZ
    ).astimezone(timezone.utc)

    electricity, gas = fetch_meter_usage(prop, api_key, period_from, period_to)
    latitude, longitude = fetch_coordinates(prop["postcode"])
    temperatures = fetch_temperatures(latitude, longitude, start_day, end_day)
    rows = build_daily_data(start_day, end_day, electricity, gas, temperatures)

    data = {
        "updated": datetime.now(TZ).strftime("%a %d %b %Y %H:%M"),
        "period_days": DAYS,
        "days": rows,
    }
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "usage-data.json").write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {len(rows)} usage days to {OUT_DIR}/usage-data.json")


if __name__ == "__main__":
    main()
