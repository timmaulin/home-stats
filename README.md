# home-stats

The solar forecast's daily kWh excludes the first three full hours starting at
the first positive Solcast reading in the morning. Grey chart bars overlap that
assumed shade period, so sunrise and seasonal changes move it automatically.

The battery model starts full at 07:00 with a 200 W baseload. The longest
heating run is one continuous period at 800 W total, with start and end times
on half-hour boundaries between 08:00 and 13:00. Solar supplies the load first,
and any surplus can recharge the battery.

For each 30-minute slot:

`draw = max(0, load - solar) / 0.90`
`battery_change = -draw * 0.5` for battery use, or
`battery_change = (solar - load) * 0.90 * 0.5` for charging

Thus 200 W for 1 hour draws 0.11 kWh from the battery. 800 W for 1 hour draws
0.44 kWh if there is no solar. The search tries every continuous 800 W window
and chooses the longest window that never empties the battery before 13:00.