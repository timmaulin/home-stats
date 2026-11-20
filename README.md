# home-stats

The solar forecast's daily kWh excludes the first four hours from the first
positive morning forecast. Grey chart bars overlap that assumed shade period.

The battery model starts full at 07:00 with a 300 W baseload. The longest
heating run is one continuous period at 900 W total, with start and end times
on half-hour boundaries between 08:00 and 13:00. Solar supplies the load first,
and any surplus can recharge the battery.