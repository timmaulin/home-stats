# home-stats

The site has two GitHub Pages views:

- `docs/index.html`: solar forecast and battery estimates.
- `docs/usage.html`: Octopus electricity and gas use, plotted in kWh with local
  average daily temperature.

To enable the usage page, add these repository Actions secrets:

- `OCTOPUS_API_KEY`: an Octopus API key from the account developer settings.
- `OCTOPUS_ACCOUNT_NUMBER`: the account number shown in the Octopus account.

The scheduled workflow reads the account's first property with meter points.
It uses that property's postcode for Open-Meteo temperature data. Octopus and
weather calls run in GitHub Actions; credentials are not included in the
published page. The workflow runs nightly, downloads the latest 730 complete
days (two years) from Octopus and Open-Meteo, and merges them with previously
saved days. It therefore builds an increasing history rather than replacing
older data.

On the usage page, select **Last 14 months** to see a full year and the
surrounding months, or use **Compare with same dates last year** to compare
the summary cards for a shorter selected period. Year-on-year comparisons
are hidden for a metric if either period has missing readings, or if the
selected period includes 29 February (which has no matching date).

Electricity is displayed in kWh. Gas is returned by Octopus in m³ and converted
to estimated kWh using the standard UK formula: `m³ × 1.02264 × 39.0 ÷ 3.6`.
Set the optional `GAS_CALORIFIC_VALUE_MJ_M3` repository variable if you want
to use a different calorific value. Because this repository is public, the
generated daily usage totals are public too; do not enable the usage page if
those values are private.

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