# dashboard/: Bluebikes ops dashboard (Looker Studio, free)

One page for a Boston operations or planning lead, with five parts:

1. **Stockout heatmap**: which stations are empty or full, at which hour of the day
2. **Top problem stations**: ranked by stockout hours and by estimated lost rides
3. **Forecast vs actual**: the net-flow model against the "same hour last week" baseline
4. **Siting map**: Census tracts, new-station candidates and equity gaps
5. **Experiment result**: do new stations add rides or move them from neighbours?

Built in **Looker Studio** (free with a Google account, runs in the browser, so it works on a
Mac). Power BI Desktop is Windows-only; the same measures are given in DAX at the end for
anyone on Windows.

## Why flat export tables?

Dashboards are good at filtering and summing, and bad at joins, window functions and
anything that needs the raw 5-minute snapshots. So `export_tables.py` does the work in
DuckDB/pandas and writes one CSV per chart, each with one clear grain. The dashboard only
adds things up and divides. Two rules keep the numbers right:

* **Ratios are computed from sums, never averaged.** Empty share = SUM(empty hours) /
  SUM(observed hours) for whatever stations you filter to. Averaging each station's share
  would weight a station seen for 1 hour the same as one seen for 500.
* **No number is typed in by hand.** Every tile reads an export; the export reads the
  repo's outputs.

## 1. Export the tables

```bash
python analysis/build_views.py          # refresh DuckDB views over the newest snapshots
python dashboard/export_tables.py       # writes dashboard/exports/*.csv
```

Each block skips itself with a message when its input isn't on the machine. On Khushi's Mac
(collector + trip history + models) every table is written. What is in the repo today:

| File | Grain | Status (2026-11-03) |
|---|---|---|
| `stockout_heatmap.csv` | station × weekday/weekend × local hour of day | **Run on the Mac.** Snapshots exist only there; collection runs to ~2026-11-23 |
| `problem_stations.csv` | station, whole snapshot period | Run on the Mac |
| `peak_problem_stations.csv` | city × peak window × rank (top 20) | Run on the Mac |
| `forecast_vs_actual_hourly.csv` | city × local hour, Sep 2026 test month | **Exported** (Boston, Chicago) |
| `forecast_station_accuracy.csv` | station, Sep 2026 test month | **Exported** (Boston 618, Chicago 1,236 stations) |
| `forecast_metrics.csv` | city × variant × forecast × metric | **Exported** (long form of `analysis/results/netflow_metrics.csv`) |
| `siting_tracts.csv` | Boston Census tract | **Exported** (207 tracts) |
| `experiment_status.csv` + `experiment_*.csv` | as written by the DiD study | **Exported** (all three cities + pooled) |
| `export_manifest.csv` | file | what was written, when, from what |

NYC forecast tables need the Citi Bike panel (8 GB of trip zips); run
`python dashboard/export_tables.py` on the Mac after `models/netflow_features.py` to add it.

**Check that the export is honest.** The forecast block re-scores the test month with the
saved models and prints the departures WAPE; it must equal `analysis/results/netflow_metrics.csv`.
On 2026-11-03 it printed 64.70% (Boston) and 76.28% (Chicago), matching the `with_weather`
LightGBM rows exactly.

`--station-hours` also writes `exports/large/forecast_vs_actual_station_hour.csv` (one row per
station-hour, ~450k rows for Boston). It's git-ignored; upload it only if you want a
per-station forecast line chart.

## 2. Load the data into Looker Studio

1. Go to [lookerstudio.google.com](https://lookerstudio.google.com) → **Create → Report**.
2. Add data → **File Upload** connector → drop in the CSV. (Free: up to 100 MB per file.)
   One data source per CSV. Rename each to the file name without `.csv`.
3. Fix field types (data source → **Edit connection** is not needed; click the data source,
   then the type next to each field):

| Data source | Field | Type |
|---|---|---|
| all with `lat_lon` | `lat_lon` | Geo → **Latitude, Longitude** |
| `forecast_vs_actual_hourly` | `hour_local_ts` | Date & Time → **Date Hour** |
| `siting_tracts` | `geoid` | **Text** (it's an id, not a quantity) |
| `stockout_heatmap` | `hour_of_day` | **Number**; also set Default aggregation = None |
| any id/code field | `station_code`, `candidate_rank`, `rank` | Text / Number, aggregation **None** |

> Looker Studio sums numeric fields by default. Ranks, lat/lon and hour of day must have
> aggregation set to **None** or a chart will add them up.

4. Add the **calculated fields** below (data source → **Add a field**). They're written as
   ratios of sums so they stay correct at any filter level.

### Calculated fields

`stockout_heatmap` and `problem_stations`:

```text
Empty share      = SUM(empty_hours) / SUM(observed_hours)
Full share       = SUM(full_hours) / SUM(observed_hours)
Stockout share   = (SUM(empty_hours) + SUM(full_hours)) / SUM(observed_hours)
Lost rides       = SUM(est_lost_rides)
Lost rides range = CONCAT(CAST(ROUND(SUM(est_lost_rides_low), 0) AS TEXT), " to ",
                          CAST(ROUND(SUM(est_lost_rides_high), 0) AS TEXT))
```

(`problem_stations` names the columns `station_hours_empty` / `station_hours_full`; use those
in its copy of the formulas. Its `stockout_share` column is per-station and fine in a table,
but use the calculated field for any total.)

`forecast_station_accuracy` (station-hour errors summed per station, so these reproduce the
published metrics for any group of stations):

```text
WAPE model          = SUM(ae_model) / SUM(actual_departures)
WAPE baseline       = SUM(ae_naive) / SUM(actual_departures)
WAPE improvement pt = (SUM(ae_naive) - SUM(ae_model)) / SUM(actual_departures)
Model beats baseline = CASE WHEN ae_model < ae_naive THEN "Model better" ELSE "Baseline as good or better" END
```

`forecast_vs_actual_hourly` (system total per hour, for the line chart):

```text
Abs error model    = ABS(forecast_departures - actual_departures)
Abs error baseline = ABS(naive_departures - actual_departures)
System-hour WAPE model    = SUM(Abs error model) / SUM(actual_departures)
System-hour WAPE baseline = SUM(Abs error baseline) / SUM(actual_departures)
```

Keep the two WAPEs apart in labels: **station-hour** WAPE (64.7% for Boston) is the
rebalancing-relevant one; **system-hour** WAPE is far lower because station errors cancel
when summed across the city.

`siting_tracts`:

```text
Candidate label = CASE WHEN is_candidate THEN CONCAT("#", CAST(candidate_rank AS TEXT), " ", neighborhood)
                       ELSE neighborhood END
Income band     = CASE WHEN low_income THEN "Low income (bottom 25%)" ELSE "Other" END
```

## 3. Build the page

Canvas: **Theme and layout → Canvas size: Custom 1600 × 1400**. Grid of 3 rows.

### Header row: filters and scorecards
* **Drop-down list** control on `city` (default: boston). Controls filter every chart on the
  page that uses a data source with a `city` field.
* **Drop-down list** on `day_type` (weekday/weekend) for the heatmap.
* Scorecards (Scorecard chart, one metric each):
  * Stockout share (`problem_stations`) and Lost rides, with "Lost rides range" in a small
    table underneath (scorecards can't show text ranges).
  * WAPE model and WAPE baseline (`forecast_station_accuracy`), formatted as Percent.
  * Count of tracts where `map_group = "Equity gap (low income, no station)"` (`siting_tracts`,
    Record Count with a chart-level filter).

### 1. Stockout heatmap (data: `stockout_heatmap`)
* Chart: **Pivot table with heatmap**.
* Row dimension `station_name`, column dimension `hour_of_day` (sort ascending), metric
  **Stockout share** (or Empty share). Style → heatmap colour on the metric.
* Sort rows by **Lost rides** descending and limit to 25 rows, so the worst stations sit on top.
* Why a pivot and not a map: the question is "when", and hour columns show the 8am/5pm
  pattern at a glance.

### 2. Top problem stations (data: `problem_stations`)
* Chart: **Table** with bars. Dimensions `rank_stockout_hours`, `station_name`, `capacity`;
  metrics `station_hours_empty`, `station_hours_full`, Stockout share, Lost rides.
  Style → show `station_hours_empty` and `station_hours_full` as **bars**.
* Second small table from `peak_problem_stations`, filtered by a `peak_window` control:
  the top 20 at weekday 7–9am and 4–7pm.
* Add the period as a text box from `period_from_local` / `period_to_local` (a Scorecard
  with MIN/MAX of those fields works).

### 3. Forecast vs actual (data: `forecast_vs_actual_hourly`)
* Chart: **Time series**. Dimension `hour_local_ts`, metrics `actual_departures`,
  `forecast_departures`, `naive_departures`. Make actual solid dark, model a colour,
  baseline grey dashed.
* Add a **date range control** so a reader can zoom to one week; the full month is too dense.
* Next to it, a **Bar chart** from `forecast_station_accuracy`: dimension "Model beats
  baseline", metric Record Count. In Boston 550 of 618 stations (89%) are better with the model.
* Optional: a **Table** of `forecast_metrics` filtered to `variant = with_weather` and
  `metric in (departures_wape, net3h_mae)` as a pivot of forecast × metric.

### 4. Siting map (data: `siting_tracts`)
* Chart: **Google Maps → Bubble map**. Location `lat_lon`, tooltip `Candidate label`,
  colour dimension `map_group`, size metric `population`.
* Colour by `map_group`: Covered = light grey, New-station candidate = blue, Equity gap = orange,
  Not scored = no fill.
* A **Table** beside it, filtered to `is_candidate = true`, sorted by `candidate_rank`, limit 15:
  `candidate_rank`, `neighborhood`, `dist_nearest_station_m`, `score`, `median_household_income`.
* Points are tract interior points, not station sites. Say so in a text box: the map points
  to areas of about 4,000 people, not street corners.

### 5. Experiment result (data: `experiment_did_summary`, `experiment_net_effect`, `experiment_did_by_distance`)
The study writes `analysis/results/new_stations/*.csv`; the export copies them as
`experiment_*.csv`. `system` is bluebikes / citibike / divvy /
all (all = the three cities pooled), so the city control needs `boston → bluebikes`; simplest is a
chart-level filter `system = bluebikes`.
* **Scorecards** from `experiment_net_effect`: `own_departures_per_week`,
  `neighbour_change_per_week`, `net_change_per_week` (aggregation None or Max; one row per system).
  Put the `_ci95_low` / `_ci95_high` columns in a small table beneath.
* **Headline row** from `experiment_did_summary`, filter `outcome = departures` and
  `test = main`: `did_pct_of_pre` with `pct_ci95_low` / `pct_ci95_high`. Add the
  `test = placebo_26w_earlier` row next to it so readers see the check.
* **Bar chart** from `experiment_did_by_distance`: dimension `ring_m`, metric
  `did_pct_of_pre`. Looker Studio has no error bars, so show the CI columns in the tooltip or
  a table; in Power BI use error bars on the bar visual.

### Finishing
* **Share → Manage access → Anyone with the link can view** to put the link on a CV/LinkedIn.
* **File → Download → PDF** for a static copy in `dashboard/` (e.g. `ops_dashboard.pdf`).
* After the collector finishes (~2026-11-23): re-run both scripts on the Mac, then in Looker
  Studio **Edit data source → Replace file** for the stockout CSVs. Charts keep their setup.

## Optional: Power BI Desktop (Windows) DAX measures

Full click-by-click Power BI steps, including how to get Power BI from a Mac: [`powerbi_build_steps.md`](powerbi_build_steps.md). The short list of measures:

Load each CSV with **Get data → Text/CSV**. Relationships are not needed: each visual uses
one table, and a `Cities` table (`DISTINCT` of `city`) can drive a slicer if you relate it to
each table's `city`.

```DAX
-- stockout_heatmap
Observed Hours  = SUM ( stockout_heatmap[observed_hours] )
Empty Share     = DIVIDE ( SUM ( stockout_heatmap[empty_hours] ), [Observed Hours] )
Full Share      = DIVIDE ( SUM ( stockout_heatmap[full_hours] ), [Observed Hours] )
Stockout Share  = DIVIDE ( SUM ( stockout_heatmap[empty_hours] ) + SUM ( stockout_heatmap[full_hours] ), [Observed Hours] )
Lost Rides      = SUM ( stockout_heatmap[est_lost_rides] )
Lost Rides Range =
    FORMAT ( SUM ( stockout_heatmap[est_lost_rides_low] ), "#,0" ) & " to "
        & FORMAT ( SUM ( stockout_heatmap[est_lost_rides_high] ), "#,0" )

-- problem_stations
Problem Station Rank =
    RANKX ( ALLSELECTED ( problem_stations[station_code] ),
            CALCULATE ( SUM ( problem_stations[stockout_hours] ) ), , DESC, Dense )

-- forecast_station_accuracy
WAPE Model        = DIVIDE ( SUM ( forecast_station_accuracy[ae_model] ), SUM ( forecast_station_accuracy[actual_departures] ) )
WAPE Baseline     = DIVIDE ( SUM ( forecast_station_accuracy[ae_naive] ), SUM ( forecast_station_accuracy[actual_departures] ) )
WAPE Improvement  = [WAPE Baseline] - [WAPE Model]
Stations Model Better =
    COUNTROWS ( FILTER ( forecast_station_accuracy, forecast_station_accuracy[ae_model] < forecast_station_accuracy[ae_naive] ) )

-- forecast_vs_actual_hourly
System-hour WAPE Model =
    DIVIDE ( SUMX ( forecast_vs_actual_hourly,
                    ABS ( forecast_vs_actual_hourly[forecast_departures] - forecast_vs_actual_hourly[actual_departures] ) ),
             SUM ( forecast_vs_actual_hourly[actual_departures] ) )

-- siting_tracts
Candidates   = CALCULATE ( COUNTROWS ( siting_tracts ), siting_tracts[is_candidate] = TRUE () )
Equity Gaps  = CALCULATE ( COUNTROWS ( siting_tracts ), siting_tracts[equity_gap] = TRUE () )
People in Equity Gaps = CALCULATE ( SUM ( siting_tracts[population] ), siting_tracts[equity_gap] = TRUE () )
```

Visuals map one-to-one: Matrix with conditional-format background (heatmap), Table with
data bars (problem stations), Line chart (forecast vs actual), Map or Azure Map with
`lat`/`lon` (siting), Card (experiment).

## Interview notes

* **Why export tables instead of connecting the dashboard to DuckDB?** Free BI tools can't
  read a local DuckDB file, and putting the logic in SQL/Python keeps it tested
  (`tests/test_dashboard_export.py`) and version-controlled instead of hidden in a dashboard.
* **Why ratio-of-sums calculated fields?** So every total is correct at any filter: the
  station-level WAPE fields reproduce the published 64.7% exactly when no filter is applied.
* **Why two WAPEs?** Station-hour error matters for rebalancing a single dock; system-hour
  error matters for staffing. Quoting the system-hour number (14.1% in Boston) as if it were the station-hour one (64.7%) would overstate the model more than fourfold.
