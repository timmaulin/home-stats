#!/usr/bin/env python3
"""
Fetch Solcast rooftop forecasts for two panels, sum them, and write:
  docs/data.json   - raw hourly numbers
  docs/index.html  - the website (serve with GitHub Pages)

Also simulates a 3 kWh battery from 07:00 until 13:00 (next Octopus Cosy
slot), with an editable 200 W baseload and up to 800 W with heating from 08:00.

Needs env var SOLCAST_API_KEY (one key, used for both resource IDs).
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
START_HOUR = 8         # earliest heating start
END_HOUR = 13          # next Cosy slot
BASELOAD_START_HOUR = 7
BASELOAD_W = 200
OBSTRUCTION_HOURS = 3
SCENARIOS = [
    ("Baseload only", BASELOAD_W),
    ("Heat pump running", 800),
]
OUT_DIR = Path("docs")
# ------------------------------------------


def fetch(resource_id, api_key):
    import requests

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


def obstruction_window(slots):
    if not slots:
        return None
    first_positive = min(
        (t for t, slot in slots.items() if slot_total(slot, "est") > 0),
        default=None,
    )
    if first_positive is None:
        return None
    obstruction_start = datetime(
        first_positive.year,
        first_positive.month,
        first_positive.day,
        first_positive.hour,
        tzinfo=TZ,
    )
    obstruction_end = obstruction_start + timedelta(hours=OBSTRUCTION_HOURS)
    return obstruction_start, obstruction_end


def is_obstructed(t, obstruction):
    return obstruction is not None and obstruction[0] <= t < obstruction[1]


def expected_kwh(slots, obstruction):
    return round(sum(slot_total(s, "est") * 0.5 for t, s in slots.items()
                     if not is_obstructed(t, obstruction)) / 1000, 2)


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
            if draw > energy + 1e-9 or (i < len(solar_w) - 1 and draw >= energy - 1e-9):
                hrs = (i + min(1, energy / draw)) * 0.5
                t = datetime.min + timedelta(hours=BASELOAD_START_HOUR + hrs)
                return {"empty": True, "hours": round(hrs, 2), "empty_at": t.strftime("%H:%M")}
            energy = max(0, energy - draw)
        else:
            energy = min(BATTERY_WH, energy + (-net) * 0.5 * EFFICIENCY)
    return {"empty": False, "hours": END_HOUR - BASELOAD_START_HOUR,
            "remaining_pct": round(100 * energy / BATTERY_WH)}


def max_heating_run(solar, load, baseload_w=BASELOAD_W):
    """Find the longest continuous run, preferring the earliest start on a tie."""
    first = (START_HOUR - BASELOAD_START_HOUR) * 2
    for duration in range(len(solar) - first, 0, -1):
        for start in range(first, len(solar) - duration + 1):
            end = start + duration
            loads = [baseload_w] * start + [load] * duration + [baseload_w] * (len(solar) - end)
            if not simulate(solar, loads)["empty"]:
                return {"hours": duration / 2,
                        "start": battery_time(start), "end": battery_time(end)}
    return {"hours": 0}


def battery_time(slot_index):
    return (datetime.min + timedelta(hours=BASELOAD_START_HOUR,
                                     minutes=30 * slot_index)).strftime("%H:%M")


def battery(slots, day, obstruction, *, has_cloudy=True,
            baseload_w=BASELOAD_W, heat_pump_w=800):
    slot_count = (END_HOUR - BASELOAD_START_HOUR) * 2
    start = datetime(day.year, day.month, day.day, BASELOAD_START_HOUR, tzinfo=TZ)
    window = [start + timedelta(minutes=30 * i) for i in range(slot_count)]
    if any(t not in slots for t in window):
        return None

    def solar_for(key):
        return [0 if is_obstructed(t, obstruction) else slot_total(slots[t], key) for t in window]

    expected_solar = solar_for("est")
    cloudy_solar = solar_for("p10") if has_cloudy else None
    first_increased = (START_HOUR - BASELOAD_START_HOUR) * 2

    out = []
    heating = {}
    for name, load in SCENARIOS:
        scenario_load = baseload_w if load == BASELOAD_W else heat_pump_w
        loads = [baseload_w] * first_increased + [scenario_load] * (slot_count - first_increased)
        out.append({
            "name": name,
            "load_w": scenario_load,
            "expected": simulate(expected_solar, loads),
            "cloudy": simulate(cloudy_solar, loads) if has_cloudy else None,
        })
        if scenario_load > baseload_w:
            heating = {
                "expected": max_heating_run(expected_solar, scenario_load, baseload_w),
                "cloudy": max_heating_run(cloudy_solar, scenario_load, baseload_w) if has_cloudy else None,
            }
    return {"scenarios": out, "heating": heating}


def daily_metrics(slots, day, *, has_cloudy=True):
    obstruction = obstruction_window(slots)
    return {
        "total_kwh": expected_kwh(slots, obstruction),
        "obstruction": (
            {"start": obstruction[0].strftime("%H:%M"), "end": obstruction[1].strftime("%H:%M")}
            if obstruction else None
        ),
        "battery": battery(slots, day, obstruction, has_cloudy=has_cloudy),
    }


def main():
    days = defaultdict(dict)
    for t, slot in build_slots().items():
        days[t.date()][t] = slot
    result = []
    for d, slots in sorted(days.items()):
        rows = hourly(slots, d)
        if not rows:
            continue
        result.append({
            "date": d.isoformat(),
            "label": d.strftime("%a %d %b"),
            "hours": rows,
            **daily_metrics(slots, d),
        })

    data = {
        "updated": datetime.now(TZ).strftime("%a %d %b %Y %H:%M"),
        "battery_wh": BATTERY_WH,
        "baseload_w": BASELOAD_W,
        "heat_pump_w": 800,
        "baseload_start": f"{BASELOAD_START_HOUR:02d}:00",
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
 #load-settings{display:flex;gap:16px;flex-wrap:wrap;padding:12px 0}
 #load-settings label{display:flex;align-items:center;gap:6px}
 #load-settings input{width:110px;min-height:44px;font:inherit;padding:8px}
 @media(max-width:480px){body{padding:12px}.card{flex-basis:100%}.chart-wrap{height:300px}}
 @media(min-width:700px){.chart-wrap{height:360px}}
 .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
</style></head><body>
<nav><a href="usage.html">Past energy usage →</a></nav>
<h1>Solar forecast</h1>
<small id="upd"></small><br>
<label class="sr-only" for="day">Forecast day</label>
<select id="day"></select>
<div class="big" id="tot"></div>
<small id="shade"></small>
<div class="chart-wrap"><canvas id="chart"></canvas></div>
<h2>Battery (<span id="bat"></span>)</h2>
<div class="cards" id="cards"></div>
<hr>
<h2>Battery load settings</h2>
<form id="load-settings">
  <label>Baseload
    <input id="baseload-input" type="number" min="0" step="10" value="200" inputmode="numeric"> W
  </label>
  <label>Heat pump
    <input id="heat-pump-input" type="number" min="0" step="10" value="800" inputmode="numeric"> W
  </label>
</form>
<script>
const D = __DATA__;
const sel = document.getElementById('day');
document.getElementById('baseload-input').value=D.baseload_w||200;
document.getElementById('heat-pump-input').value=D.heat_pump_w||800;
document.getElementById('upd').textContent = 'Updated ' + D.updated +
  (D.hourly_snapshot ? ' (estimates recalculated from saved hourly data)' : '');
const [heatStart,end] = D.window.split('-');
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
function fmtRun(run){
  return run.hours ? fmtHours(run.hours)+' ('+run.start+'–'+run.end+')' : '0h';
}
function batteryTime(slot){
  const minutes=7*60+slot*30;
  return String(Math.floor(minutes/60)).padStart(2,'0')+':'+String(minutes%60).padStart(2,'0');
}
function simulateLoads(solar, loads){
  let energy=D.battery_wh;
  for(let i=0;i<solar.length;i++){
    const net=loads[i]-solar[i];
    if(net>0){
      const draw=net/0.9*0.5;
      if(draw>energy+1e-9 || (i<solar.length-1 && draw>=energy-1e-9)){
        const hrs=i*0.5+Math.min(1,energy/draw)*0.5;
        const h=Math.floor(7+hrs), m=Math.round((7+hrs-h)*60);
        return {empty:true,hours:Math.round(hrs*100)/100,empty_at:String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')};
      }
      energy=Math.max(0,energy-draw);
    }else{
      energy=Math.min(D.battery_wh,energy+(-net)*0.5*0.9);
    }
  }
  return {empty:false,hours:6,remaining_pct:Math.round(100*energy/D.battery_wh)};
}
function longestHeatingRun(solar, baseload, heatPump){
  const first=2;
  for(let duration=solar.length-first;duration>0;duration--){
    for(let start=first;start<=solar.length-duration;start++){
      const end=start+duration;
      const loads=[];
      for(let i=0;i<solar.length;i++){
        loads.push(i>=start&&i<end?heatPump:baseload);
      }
      if(!simulateLoads(solar,loads).empty){
        return {hours:duration/2,start:batteryTime(start),end:batteryTime(end)};
      }
    }
  }
  return {hours:0,start:'',end:''};
}
function liveBatteryCards(d,baseload,heatPump){
  const obstruction=d.obstruction;
  const solar=[];
  for(let hour=7;hour<13;hour++){
    const row=d.hours.find(h=>h.hour===String(hour).padStart(2,'0')+':00');
    const watts=row?row.total:0;
    for(let half=0;half<2;half++){
      const shaded=obstruction&&String(hour).padStart(2,'0')>=obstruction.start.slice(0,2)&&
                   String(hour).padStart(2,'0')<obstruction.end.slice(0,2);
      solar.push(shaded?0:watts);
    }
  }
  const baseLoads=Array(12).fill(baseload);
  const heatLoads=baseLoads.map((load,i)=>i>=2?heatPump:load);
  const base=simulateLoads(solar,baseLoads);
  const heat=simulateLoads(solar,heatLoads);
  const heatRun=longestHeatingRun(solar,baseload,heatPump);
  const battery=d.battery;
  const scenarios=[
    {name:'Baseload only',load_w:baseload,expected:base,cloudy:battery&&battery.scenarios&&battery.scenarios[0]?battery.scenarios[0].cloudy:null},
    {name:'Heat pump running',load_w:heatPump,expected:heat,cloudy:battery&&battery.scenarios&&battery.scenarios[1]?battery.scenarios[1].cloudy:null}
  ];
  document.getElementById('cards').innerHTML=scenarios.map(b=>
    '<div class="card"><b class="battery-name">'+b.name+' ('+b.load_w+' W)</b>'+
    (b.load_w>baseload?'<small>With heating from '+heatStart+'</small>':'')+
    '<span class="battery-result big">'+fmt(b.expected)+'</span>'+
    (b.cloudy?'<small class="battery-cloudy">Saved cloudy case (P10): '+fmt(b.cloudy)+'</small>':'')+
    (b.load_w>baseload?
      '<small class="battery-cloudy"><b>Longest heating run:</b> '+fmtRun(heatRun)+'</small>':'')+
    '</div>').join('');
}
function render(i){
  const d=D.days[i];
  const obstruction=d.obstruction;
  document.getElementById('tot').textContent = d.total_kwh.toFixed(1)+' kWh expected';
  document.getElementById('shade').textContent = obstruction ?
    'Assumed shade '+obstruction.start+'–'+obstruction.end+' is excluded from the total; grey bars overlap that period.' : '';
  const labels=d.hours.map(h=>h.hour);
  if(chart) chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'bar',
  data:{labels,datasets:D.panels.map((p,i)=>{
    return {
      label:p,
      data:d.hours.map(h=>h[p]),
      backgroundColor:d.hours.map(h=>obstruction&&h.hour.slice(0,2)>=obstruction.start.slice(0,2)&&h.hour<obstruction.end
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
  document.getElementById('bat').textContent =
    (D.battery_wh/1000)+' kWh, '+D.baseload_start+'–'+end;
  const baseloadInput=document.getElementById('baseload-input');
  const heatPumpInput=document.getElementById('heat-pump-input');
  liveBatteryCards(d,Number(baseloadInput.value)||0,Number(heatPumpInput.value)||0);
}
sel.onchange=()=>render(sel.value);
const baseloadInput=document.getElementById('baseload-input');
const heatPumpInput=document.getElementById('heat-pump-input');
try{
  const saved=JSON.parse(localStorage.getItem('solar-load-settings'));
  if(saved && Number.isFinite(saved.baseload)) baseloadInput.value=saved.baseload;
  if(saved && Number.isFinite(saved.heatPump)) heatPumpInput.value=saved.heatPump;
}catch(e){}
baseloadInput.addEventListener('input',()=>{
  localStorage.setItem('solar-load-settings',JSON.stringify({baseload:Number(baseloadInput.value)||0,heatPump:Number(heatPumpInput.value)||0}));
  render(sel.value);
});
heatPumpInput.addEventListener('input',()=>{
  localStorage.setItem('solar-load-settings',JSON.stringify({baseload:Number(baseloadInput.value)||0,heatPump:Number(heatPumpInput.value)||0}));
  render(sel.value);
});
if(D.days.length) render(0);
</script></body></html>
"""


if __name__ == "__main__":
    main()
