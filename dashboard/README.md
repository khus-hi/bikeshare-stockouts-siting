# dashboard/: exported tables, charts and the Boston pitch

There is no BI-tool dashboard in this project. The results are shown as static charts drawn
with matplotlib, plus a 6-slide pitch outline. Two scripts do the work:

```bash
python analysis/build_views.py      # DuckDB views over the latest GBFS snapshots
python dashboard/export_tables.py   # flat CSVs -> dashboard/exports/
python dashboard/make_charts.py     # PNG charts -> dashboard/charts/
```

Every number comes from the repo's tested SQL and model outputs; nothing is typed in by hand.

## Charts (`charts/`)

| Chart | Shows | Status (2026-11-03) |
|---|---|---|
| `forecast_week_boston.png` | One week of Boston hourly departures: actual vs LightGBM vs same hour last week | Done |
| `forecast_error_by_city.png` | Station-hour departures WAPE, model vs baseline, per city | Done |
| `siting_map_boston.png` | Boston Census tracts: covered, new-station candidate, equity gap; top 5 labelled | Done |
| `new_station_effect.png` | Change at existing stations by distance from a new one, Boston and all cities, 95% CI | Done |
| `stockout_heatmap_boston.png` | Top 25 stations by lost rides × hour of day, share of time empty or full | Run on the Mac after the collector ends (~2026-11-23) |
| `problem_stations_boston.png` | 15 stations empty or full the longest | Same |

The two stockout charts need the GBFS snapshots, which only exist on the Mac running the
collector. Run the three commands above there once collection ends, then commit the PNGs.
Charts made earlier only cover a few days; don't publish those.

Colours: one blue for the model or the "act here" category, grey for baselines and context,
orange for the second series. The categorical colours are a colourblind-checked set, and
every chart has a legend or direct labels, so nothing relies on colour alone.

## Export tables (`exports/`)

One CSV per chart, each with one clear grain. They also load straight into any BI tool if one
is wanted later.

| File | Grain | Built from |
|---|---|---|
| `stockout_heatmap.csv` | station × weekday/weekend × local hour | DuckDB `lost_rides_station_hour` (Mac only) |
| `problem_stations.csv` | station, whole snapshot period, ranked | DuckDB `station_stockout_summary` (Mac only) |
| `peak_problem_stations.csv` | city × weekday peak × rank (top 20) | DuckDB `peak_problem_stations` (Mac only) |
| `forecast_vs_actual_hourly.csv` | city × local hour, Sep 2026 test month | saved LightGBM models re-scored |
| `forecast_station_accuracy.csv` | station, Sep 2026 test month | same, summed per station |
| `forecast_metrics.csv` | city × variant × forecast × metric | `analysis/results/netflow_metrics.csv` |
| `siting_tracts.csv` | Boston Census tract | `analysis/results/siting/boston_siting_scores.csv` |
| `experiment_*.csv` | as written by the study | `analysis/results/new_stations/` |
| `export_manifest.csv` | file | what was written, when, from what |

`export_tables.py --station-hours` also writes `exports/large/forecast_vs_actual_station_hour.csv`
(one row per station-hour, git-ignored).

**Check that the export is honest.** The forecast block re-scores the test month with the
saved models and prints the departures WAPE, which must equal
`analysis/results/netflow_metrics.csv`. On 2026-11-03 it printed 64.70% (Boston) and 76.28%
(Chicago), matching the `with_weather` LightGBM rows.

Two rules keep the numbers right when anything is summed later:

* **Ratios are computed from sums, never averaged.** Stockout share = (empty hours + full
  hours) / observed hours over whatever stations are grouped. Averaging per-station shares
  would weight a station seen for 1 hour the same as one seen for 500.
* **Station-hour vs system-hour error.** Station-hour WAPE (64.7% in Boston) is what matters
  for rebalancing one dock; summed across the city, errors cancel and system-hour WAPE is
  14.1%. Label which one you quote.

## Pitch

`pitch_outline.md`: 6 slides for the City of Boston Analytics Team, each number cited to its
file, with `[PENDING]` where the stockout numbers will go after ~2026-11-23.

## Tests

`tests/test_dashboard_export.py` checks the stockout export against the hand-worked fixture
(including that empty + full share never exceeds 100%); `tests/test_make_charts.py` checks the
stockout charts render from it.

## Interview notes

* **Why export tables before charting?** The stockout logic (poll gaps, hour splitting,
  lost-ride estimates) lives in tested SQL. Charts only read finished tables, so every value
  on a slide traces back to code.
* **Why static charts and not a BI dashboard?** The audience is a pitch and a portfolio; a PNG
  per finding is easier to review, version and drop into slides, and needs no account or
  licence.
