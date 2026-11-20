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
import math
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
    "Wall panel": "7dd4-5eac-c6ad-ce91",
    "Roof panels": "a4fe-41af-a2cf-9447",
}
HOURS_AHEAD = 72            # how far ahead to fetch
# Multiply each panel's Solcast output by this (the wall panel's API figure is double what you get).
SCALE = {
    "Wall panel": 0.5,
    "Roof panels": 1.0,
}
TZ = ZoneInfo("Europe/London")

BATTERY_WH = 3000      # 3 kWh
EFFICIENCY = 0.90      # battery + inverter loss on discharge (approximation)
START_HOUR = 8         # battery switched on
END_HOUR = 13          # next Cosy slot
BASELOAD_START_HOUR = 7
BASELOAD_W = 300
OBSTRUCTION_HOURS = 4
PANEL_LOCATION = (53.375852, -1.439248)  # (latitude, longitude)
PANEL_AZIMUTH_DEG = 210
PANEL_TILT_DEG = 30
OBSTRUCTION_RADIUS_M = 150
OBSTRUCTION_CACHE = OUT_DIR / "obstruction.json"
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


def parse_height(tags, default):
    if not tags:
        return default
    value = tags.get("height") or tags.get("building:height")
    if value:
        try:
            return float(str(value).lower().replace("m", "").strip())
        except (TypeError, ValueError):
            pass
    levels = tags.get("building:levels")
    if levels:
        try:
            return float(levels) * 3.2
        except (TypeError, ValueError):
            pass
    return default


def local_xy(lat, lon):
    panel_lat, panel_lon = PANEL_LOCATION
    metres_per_degree_lat = 111320
    metres_per_degree_lon = 111320 * math.cos(math.radians(panel_lat))
    return (
        (lon - panel_lon) * metres_per_degree_lon,
        (lat - panel_lat) * metres_per_degree_lat,
    )


def bearing_between(x, y):
    if abs(x) < 0.01 and abs(y) < 0.01:
        return 0
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def fetch_obstruction_profile():
    """Read cached OSM obstruction geometry, or query and cache OpenStreetMap data."""
    if OBSTRUCTION_CACHE.exists():
        try:
            profile = json.loads(OBSTRUCTION_CACHE.read_text())
            if profile.get("panel") == list(PANEL_LOCATION):
                return profile
        except (json.JSONDecodeError, OSError):
            pass

    lat, lon = PANEL_LOCATION
    metres_per_degree_lat = 1 / 111320
    metres_per_degree_lon = 1 / (111320 * math.cos(math.radians(lat)))
    radius = OBSTRUCTION_RADIUS_M
    south = lat - radius * metres_per_degree_lat
    north = lat + radius * metres_per_degree_lat
    west = lon - radius * metres_per_degree_lon
    east = lon + radius * metres_per_degree_lon
    query = (
        "[out:json][timeout:25];("
        f"way[\"building\"]({south},{west},{north},{east});"
        f"node[\"natural\"=\"tree\"]({south},{west},{north},{east});"
        f"way[\"natural\"=\"wood\"]({south},{west},{north},{east});"
        ");out tags geom;"
    )
    try:
        response = requests.post(
            "https://overpass-api.de/api/interpreter",
            data=query,
            timeout=30,
        )
        if not response.ok:
            return None
        data = response.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None

    horizon = [0.0] * 72
    sources = {"buildings": 0, "trees": 0}
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        points = []
        if element.get("type") == "node":
            points = [(element["lat"], element["lon"])]
            default_height = 10.0
            kind = "tree"
        else:
            points = [(p["lat"], p["lon"]) for p in element.get("geometry", [])]
            if not points:
                continue
            kind = "building" if tags.get("building") else "tree"
            default_height = 7.0 if kind == "building" else 12.0
        height = parse_height(tags, default_height)
        for lat_point, lon_point in points:
            x, y = local_xy(lat_point, lon_point)
            distance = math.hypot(x, y)
            if distance < 2 or distance > OBSTRUCTION_RADIUS_M:
                continue
            elevation = math.degrees(math.atan2(height, distance))
            bearing = bearing_between(x, y)
            bin_index = round(bearing / 5) % 72
            for offset in (-1, 0, 1):
                index = (bin_index + offset) % 72
                horizon[index] = max(horizon[index], elevation)
            sources["buildings" if kind == "building" else "trees"] += 1

    profile = {
        "panel": list(PANEL_LOCATION),
        "azimuth": PANEL_AZIMUTH_DEG,
        "tilt": PANEL_TILT_DEG,
        "bin_degrees": 5,
        "horizon_elevation": horizon,
        "sources": sources,
        "source": "OpenStreetMap Overpass",
    }
    OUT_DIR.mkdir(exist_ok=True)
    OBSTRUCTION_CACHE.write_text(json.dumps(profile, indent=2))
    return profile


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
    """Battery starts full at 07:00; load_w is a list or fixed W value."""
    energy = BATTERY_WH
    for i, s in enumerate(solar_w):
        load = load_w[i] if isinstance(load_w, list) else load_w
        net = load - s  # +ve = drawing from battery
        if net > 0:
            draw = net / EFFICIENCY * 0.5
            if draw >= energy:
                hrs = (i + energy / draw) * 0.5
                t = datetime.min + timedelta(hours=BASELOAD_START_HOUR + hrs)
                return {"empty": True, "hours": round(hrs, 2), "empty_at": t.strftime("%H:%M")}
            energy -= draw
        else:
            energy = min(BATTERY_WH, energy + (-net) * 0.5 * EFFICIENCY)
    return {"empty": False, "hours": END_HOUR - BASELOAD_START_HOUR,
            "remaining_pct": round(100 * energy / BATTERY_WH)}


def battery(slots, day):
    window = []
    slot_count = (END_HOUR - BASELOAD_START_HOUR) * 2
    for i in range(slot_count):
        t = datetime(day.year, day.month, day.day, BASELOAD_START_HOUR, tzinfo=TZ) + timedelta(minutes=30 * i)
        if t not in slots:
            return None
        window.append(slots[t])

    first_solar = next(
        (i for i, s in enumerate(window) if slot_total(s, "est") > 0),
        slot_count,
    )
    obstruction_end_i = min(slot_count, first_solar + OBSTRUCTION_HOURS * 2)
    obstruction_start = datetime(day.year, day.month, day.day, BASELOAD_START_HOUR, tzinfo=TZ) + timedelta(minutes=30 * first_solar)
    obstruction_end = datetime(day.year, day.month, day.day, BASELOAD_START_HOUR, tzinfo=TZ) + timedelta(minutes=30 * obstruction_end_i)

    expected_solar = [
        0 if i < obstruction_end_i else slot_total(s, "est")
        for i, s in enumerate(window)
    ]
    cloudy_solar = [
        0 if i < obstruction_end_i else slot_total(s, "p10")
        for i, s in enumerate(window)
    ]

    out = []
    for name, load in SCENARIOS:
        loads = [BASELOAD_W] * slot_count
        if load != BASELOAD_W:
            first_increased = (START_HOUR - BASELOAD_START_HOUR) * 2
            loads[first_increased:] = [load] * (slot_count - first_increased)
        out.append({
            "name": name,
            "load_w": load,
            "expected": simulate(expected_solar, loads),
            "cloudy": simulate(cloudy_solar, loads),
        })
    heat_index = next((i for i, b in enumerate(out) if b["load_w"] != BASELOAD_W), None)
    heating = None
    if heat_index is not None:
        def max_heating_hours(solar):
            first_increased = (START_HOUR - BASELOAD_START_HOUR) * 2
            for half_hours in range(slot_count - first_increased, -1, -1):
                loads = [BASELOAD_W] * slot_count
                loads[first_increased:first_increased + half_hours] = [900] * half_hours
                if not simulate(solar, loads)["empty"]:
                    return round(half_hours * 0.5, 1)
            return 0
        heating = {
            "expected": max_heating_hours(expected_solar),
            "cloudy": max_heating_hours(cloudy_solar),
        }
    return {
        "scenarios": out,
        "heating": heating,
        "obstruction": {
            "start": obstruction_start.strftime("%H:%M"),
            "end": obstruction_end.strftime("%H:%M"),
        },
    }


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
    (OUT_DIR / "obstruction.html").write_text(OBSTRUCTION_HTML.replace("__DATA__", json.dumps(data)))
    print(f"Wrote {len(result)} days to {OUT_DIR}/")


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solar forecast</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,sans-serif;width:min(100%,860px);margin:0 auto;padding:clamp(12px,4vw,24px);background:#fafafa;color:#222}
 h1{font-size:clamp(1.7rem,6vw,2.2rem);line-height:1.15}
 select{display:block;width:100%;max-width:360px;min-height:44px;font:inherit;padding:8px 10px;margin:12px 0}
 .cards{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
 .card{flex:1 1 260px;min-width:0;background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px}
 .big{font-size:clamp(1.2rem,4vw,1.4rem);font-weight:600}
 .chart-wrap{position:relative;width:100%;height:280px;margin:16px 0}
 small{color:#666}
 .battery-name{display:block;font-size:.95rem;margin-bottom:8px}
 .battery-result{display:block;line-height:1.25}
 .battery-cloudy{display:block;margin-top:8px;font-size:.9rem}
 @media(max-width:480px){body{padding:12px}.card{flex-basis:100%}.chart-wrap{height:300px}}
 @media(min-width:700px){.chart-wrap{height:360px}}
 .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
</style></head><body>
<h1>Solar forecast</h1>
<p><a href="obstruction.html">Compare obstruction models</a></p>
<small id="upd"></small><br>
<label class="sr-only" for="day">Forecast day</label>
<select id="day"></select>
<div class="big" id="tot"></div>
<div class="chart-wrap"><canvas id="chart"></canvas></div>
<h2>Battery (<span id="bat"></span>)</h2>
<div class="cards" id="cards"></div>
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
function fmtHours(hours){
  const h=Math.floor(hours), m=Math.round((hours-h)*60);
  return h+(m?'h '+m+'m':'h');
}
function render(i){
  const d=D.days[i];
  document.getElementById('tot').textContent = d.total_kwh+' kWh expected';
  const labels=d.hours.map(h=>h.hour);
  if(chart) chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'bar',
  data:{labels,datasets:D.panels.map((p,i)=>{
    const obstruction=d.battery&&d.battery.obstruction;
    return {
      label:p,
      data:d.hours.map(h=>h[p]),
      backgroundColor:d.hours.map(h=>obstruction&&h.hour>=obstruction.start&&h.hour<obstruction.end
        ? (i===0?'#ded4c3':'#cbd3d8')
        : (i===0?'#c98a45':'#6f91a8')),
      borderRadius:4,
      borderSkipped:false,
      maxBarThickness:42
    };
  })},
    options:{
      responsive:true,
      maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{position:'bottom',labels:{boxWidth:10,boxHeight:10,padding:14,font:{size:12}}},
        tooltip:{callbacks:{label:c=>c.dataset.label+': '+c.parsed.y.toLocaleString()+' W'}}
      },
      scales:{
        x:{stacked:true,grid:{display:false},ticks:{font:{size:11},maxRotation:0}},
        y:{stacked:true,beginAtZero:true,grid:{color:'#e5e7eb'},ticks:{font:{size:11},callback:v=>v>=1000?(v/1000)+' kW':v+' W'}}
      }
    }});
  const battery=d.battery&&d.battery.scenarios?d.battery:d.battery?{scenarios:d.battery}:null;
  document.getElementById('bat').textContent =
    (D.battery_wh/1000)+' kWh, on at '+D.window.replace('-', ', off at ');
  if(battery&&battery.obstruction){
    document.getElementById('bat').textContent +=
      '; no solar '+battery.obstruction.start+'-'+battery.obstruction.end;
  }
  document.getElementById('cards').innerHTML = battery ? battery.scenarios.map(b=>
      '<div class="card"><b class="battery-name">'+b.name+' ('+b.load_w+' W)</b>'+
      '<span class="battery-result big">'+fmt(b.expected)+'</span>'+
      '<small class="battery-cloudy">Cloudy case (P10): '+fmt(b.cloudy)+'</small>'+
      (b.load_w>300&&battery.heating?
        '<small class="battery-cloudy"><b>Heating possible:</b> '+fmtHours(battery.heating.expected)+'<br>'+
        (battery.heating.cloudy?('Cloudy: '+fmtHours(battery.heating.cloudy)+' (before 13:00, not depleted)<br>'):
        'This is the expected forecast; cloudy case is shown above.')+'</small>':'')+
      '</div>').join('')
    : '<div class="card">No full 08:00-13:00 forecast for this day.</div>';
}
sel.onchange=()=>render(sel.value);
render(0);
</script></body></html>
"""

OBSTRUCTION_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Obstruction model comparison</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,sans-serif;max-width:860px;margin:0 auto;padding:clamp(12px,4vw,24px);background:#fafafa;color:#222}
 h1{font-size:clamp(1.7rem,6vw,2.2rem)}
 select{display:block;width:100%;max-width:360px;min-height:44px;margin:12px 0;font:inherit;padding:8px 10px}
 .chart-wrap{height:320px}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:16px 0}
 .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px}
 .big{display:block;font-size:1.35rem;font-weight:700;margin:6px 0}
 small{color:#666}
 @media(max-width:480px){body{padding:12px}.chart-wrap{height:300px}}
</style></head><body>
<p><a href="index.html">Solar forecast</a></p>
<h1>Obstruction model comparison</h1>
<label for="day">Forecast day</label>
<select id="day"></select>
<div class="chart-wrap"><canvas id="chart"></canvas></div>
<div class="cards" id="cards"></div>
<script>
const D=__DATA__;
const sel=document.getElementById('day');
D.days.forEach((d,i)=>sel.add(new Option(d.label,i)));
let chart;
function render(i){
  const d=D.days[i];
  const obstruction=d.battery&&d.battery.obstruction;
  const start=obstruction?obstruction.start:'07:00';
  const end=obstruction?obstruction.end:'11:00';
  const raw=d.hours.map(h=>h.total);
  const fixed=d.hours.map(h=>h.hour>=start&&h.hour<end?0:h.total);
  if(chart)chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'line',
    data:{labels:d.hours.map(h=>h.hour),datasets:[
      {label:'Solcast no obstruction',data:raw,borderColor:'#6f91a8',backgroundColor:'#6f91a822',fill:false,tension:.2},
      {label:'Fixed obstruction '+start+'-'+end,data:fixed,borderColor:'#c98a45',backgroundColor:'#c98a4522',fill:false,tension:.2}
    ]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:c=>c.dataset.label+': '+c.parsed.y+' W'}}},
      scales:{x:{grid:{display:false}},y:{beginAtZero:true,title:{display:true,text:'Watts'}}}}
  });
  const rawKwh=raw.reduce((sum,v)=>sum+v,0)/1000;
  const fixedKwh=fixed.reduce((sum,v)=>sum+v,0)/1000;
  document.getElementById('cards').innerHTML=
    '<div class="card"><b>No obstruction</b><span class="big">'+rawKwh.toFixed(2)+' kWh</span><small>Solcast forecast</small></div>'+
    '<div class="card"><b>Fixed 4-hour model</b><span class="big">'+fixedKwh.toFixed(2)+' kWh</span><small>'+start+'-'+end+' excluded</small></div>'+
    '<div class="card"><b>Battery heating</b><span class="big">'+(d.battery.heating?.expected??0)+' hours</span><small>Expected no-depletion result</small></div>';
}
sel.onchange=()=>render(sel.value);
render(0);
</script></body></html>
"""

if __name__ == "__main__":
    main()
