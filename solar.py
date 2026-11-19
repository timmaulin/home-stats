#!/usr/bin/env python3
"""
Fetch Solcast rooftop forecasts for two panels, sum them, and write:
  docs/data.json   - raw hourly numbers
  docs/index.html  - the website (serve with GitHub Pages)

Also simulates a 3 kWh battery switched on at 08:00 and run until 13:00
(next Octopus Cosy slot) with a 300 W baseload or 900 W with heat pump.

Needs env var SOLCAST_API_KEY (one key, used for both resource IDs).
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------- settings ----------------
# name: Solcast resource_id
SITES = {
    "Panel 1": "7dd4-5eac-c6ad-ce91",
    "Panel 2": "a4fe-41af-a2cf-9447",
}
HOURS_AHEAD = 72            # how far ahead to fetch
# Multiply each panel's Solcast output by this (Panel 1's API figure is double what you get).
SCALE = {
    "Panel 1": 0.5,
    "Panel 2": 1.0,
}
TZ = ZoneInfo("Europe/London")

BATTERY_WH = 3000      # 3 kWh
EFFICIENCY = 0.90      # battery + inverter loss on discharge (approximation)
START_HOUR = 8         # battery switched on
END_HOUR = 13          # next Cosy slot
SCENARIOS = [
    ("Baseload only", 300),
    ("Heat pump running", 900),
]
OUT_DIR = Path("docs")
# ------------------------------------------


def fetch(resource_id, api_key):
    # Hobbyist (Home PV) accounts can only use the rooftop_sites endpoint.
    r = requests.get(
        f"https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts",
        params={"hours": HOURS_AHEAD, "format": "json"},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    if not r.ok:
        sys.exit(f"Solcast returned HTTP {r.status_code} for resource {resource_id}: {r.text[:300]}")
    return r.json()["forecasts"]


def parse_end(s):
    # '2026-11-19T10:00:00.0000000Z' -> aware datetime (trim to seconds)
    return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


def build_slots():
    """Return {slot_start_local: {panel: {'est': W, 'p10': W}}} for 30 min slots."""
    slots = defaultdict(dict)
    api_key = os.environ.get("SOLCAST_API_KEY")
    if not api_key:
        sys.exit("Missing secret/env var SOLCAST_API_KEY")
    for name, resource_id in SITES.items():
        for f in fetch(resource_id, api_key):
            minutes = int(f.get("period", "PT30M").strip("PTM") or 30)
            start = (parse_end(f["period_end"]) - timedelta(minutes=minutes)).astimezone(TZ)
            est = f["pv_estimate"]  # kW
            k = 1000 * SCALE.get(name, 1.0)  # kW -> W, then per-panel scaling
            slots[start][name] = {
                "est": est * k,
                "p10": f.get("pv_estimate10", est) * k,
            }
    return slots


def slot_total(slot, key):
    return sum(p[key] for p in slot.values())


def hourly(slots, day):
    rows = []
    for h in range(24):
        pts = [slots[t] for t in slots if t.date() == day and t.hour == h]
        if not pts:
            continue
        row = {"hour": f"{h:02d}:00"}
        for name in SITES:
            row[name] = round(sum(p.get(name, {}).get("est", 0) for p in pts) / len(pts))
        row["total"] = round(sum(slot_total(p, "est") for p in pts) / len(pts))
        if row["total"] > 0 or START_HOUR <= h < END_HOUR:
            rows.append(row)
    return rows


def simulate(solar_w, load_w):
    """solar_w: list of average W per 30 min slot. Battery starts full."""
    energy = BATTERY_WH
    for i, s in enumerate(solar_w):
        net = load_w - s  # +ve = drawing from battery
        if net > 0:
            draw = net / EFFICIENCY * 0.5
            if draw >= energy:
                hrs = (i + energy / draw) * 0.5
                t = datetime.min + timedelta(hours=START_HOUR + hrs)
                return {"empty": True, "hours": round(hrs, 2), "empty_at": t.strftime("%H:%M")}
            energy -= draw
        else:
            energy = min(BATTERY_WH, energy + (-net) * 0.5 * EFFICIENCY)
    return {"empty": False, "hours": END_HOUR - START_HOUR,
            "remaining_pct": round(100 * energy / BATTERY_WH)}


def battery(slots, day):
    window = []
    for i in range((END_HOUR - START_HOUR) * 2):
        t = datetime(day.year, day.month, day.day, START_HOUR, tzinfo=TZ) + timedelta(minutes=30 * i)
        if t not in slots:
            return None
        window.append(slots[t])
    out = []
    for name, load in SCENARIOS:
        out.append({
            "name": name,
            "load_w": load,
            "expected": simulate([slot_total(s, "est") for s in window], load),
            "cloudy": simulate([slot_total(s, "p10") for s in window], load),
        })
    return out


def main():
    slots = build_slots()
    days = sorted({t.date() for t in slots})
    result = []
    for d in days:
        rows = hourly(slots, d)
        if not rows:
            continue
        result.append({
            "date": d.isoformat(),
            "label": d.strftime("%a %d %b"),
            "hours": rows,
            "total_kwh": round(sum(r["total"] for r in rows) / 1000, 2),
            "battery": battery(slots, d),
        })

    data = {
        "updated": datetime.now(TZ).strftime("%a %d %b %Y %H:%M"),
        "battery_wh": BATTERY_WH,
        "window": f"{START_HOUR:02d}:00-{END_HOUR:02d}:00",
        "panels": list(SITES),
        "days": result,
    }
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "data.json").write_text(json.dumps(data, indent=2))
    (OUT_DIR / "index.html").write_text(HTML.replace("__DATA__", json.dumps(data)))
    print(f"Wrote {len(result)} days to {OUT_DIR}/")


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solar forecast</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 body{font-family:system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px;background:#fafafa;color:#222}
 select{font-size:1rem;padding:6px;margin-bottom:12px}
 .cards{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
 .card{flex:1 1 260px;background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px}
 .big{font-size:1.4rem;font-weight:600}
 table{border-collapse:collapse;width:100%;background:#fff}
 th,td{padding:6px 10px;border-bottom:1px solid #eee;text-align:right}
 th:first-child,td:first-child{text-align:left}
 small{color:#666}
</style></head><body>
<h1>Solar forecast</h1>
<small id="upd"></small><br>
<select id="day"></select>
<div class="big" id="tot"></div>
<canvas id="chart" height="110"></canvas>
<h2>Battery (<span id="bat"></span>)</h2>
<div class="cards" id="cards"></div>
<h2>Hourly watts</h2>
<table id="tbl"></table>
<script>
const D = __DATA__;
const sel = document.getElementById('day');
document.getElementById('upd').textContent = 'Updated ' + D.updated;
document.getElementById('bat').textContent =
  (D.battery_wh/1000) + ' kWh, on at ' + D.window.replace('-', ', off at ');
D.days.forEach((d,i)=>sel.add(new Option(d.label,i)));
let chart;
function fmt(r){
  if(r.empty){
    const h=Math.floor(r.hours), m=Math.round((r.hours-h)*60);
    return 'Empty at '+r.empty_at+' ('+h+'h '+m+'m)';
  }
  return 'Lasts to '+D.window.split('-')[1]+', '+r.remaining_pct+'% left';
}
function render(i){
  const d=D.days[i];
  document.getElementById('tot').textContent = d.total_kwh+' kWh expected';
  const labels=d.hours.map(h=>h.hour);
  if(chart) chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'bar',
    data:{labels,datasets:D.panels.map(p=>({label:p,data:d.hours.map(h=>h[p])}))},
    options:{scales:{x:{stacked:true},y:{stacked:true,title:{display:true,text:'Watts'}}}}});
  document.getElementById('cards').innerHTML = d.battery ? d.battery.map(b=>
    '<div class="card"><b>'+b.name+' ('+b.load_w+' W)</b><div class="big">'+fmt(b.expected)+
    '</div><small>Cloudy case (P10): '+fmt(b.cloudy)+'</small></div>').join('')
    : '<div class="card">No full 08:00-13:00 forecast for this day.</div>';
  document.getElementById('tbl').innerHTML =
    '<tr><th>Hour</th>'+D.panels.map(p=>'<th>'+p+' (W)</th>').join('')+'<th>Total (W)</th></tr>'+
    d.hours.map(h=>'<tr><td>'+h.hour+'</td>'+D.panels.map(p=>'<td>'+h[p]+'</td>').join('')+
    '<td><b>'+h.total+'</b></td></tr>').join('');
}
sel.onchange=()=>render(sel.value);
render(0);
</script></body></html>
"""

if __name__ == "__main__":
    main()
