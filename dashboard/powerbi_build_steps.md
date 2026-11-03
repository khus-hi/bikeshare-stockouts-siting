# Power BI build steps: Bluebikes ops dashboard

A click-by-click guide to the same one-page dashboard as `dashboard/README.md` (Looker Studio),
built in **Power BI Desktop** (free). Every visual reads a CSV from `dashboard/exports/`, and
every number is computed by `dashboard/export_tables.py`. Power BI only sums and divides.

Time: about 2 hours the first time.

## 0. Getting Power BI Desktop when you're on a Mac

Power BI Desktop is a **Windows-only** app; there is no Mac version. Free ways to use it:

| Option | Cost | Notes |
|---|---|---|
| A Windows PC in a university computer lab or library | Free | Easiest. Check whether Power BI Desktop is installed; if not, it's a free install from the Microsoft Store. |
| A friend's or family Windows PC | Free | Install from the Microsoft Store, no account needed to build and save a `.pbix`. |
| Power BI in the browser (app.powerbi.com) with your **school** Microsoft account | Free if your school allows it | Personal Gmail/Outlook addresses can't sign up; a school or work address is required. Web editing is more limited than Desktop (fewer Power Query and modelling options), so this guide assumes Desktop. |

Don't pay for Parallels or a Windows licence for this; the project rule is free tools only. If
none of these work, the Looker Studio version in `dashboard/README.md` gives the same page on
the Mac.

Copy the whole `dashboard/exports/` folder to the Windows machine (USB, OneDrive, or
`git clone` the repo there).

## 1. Refresh the exports first (on the Mac)

```bash
cd ~/bikeshare-stockouts-siting
git pull
python analysis/build_views.py         # DuckDB views over the snapshots collected so far
python dashboard/export_tables.py      # writes dashboard/exports/*.csv
```

The stockout tables (`stockout_heatmap.csv`, `problem_stations.csv`,
`peak_problem_stations.csv`) can only be made on the Mac, because the snapshots live there.
Exporting now gives you a few days of data to **build** the visuals with; after the collector
finishes (~2026-11-23) you re-run this and press Refresh in Power BI. Don't quote the early
numbers anywhere: they cover only a few days.

## 2. Load the CSVs

**Home → Get data → Text/CSV**, one file at a time. In the preview, click **Transform Data**
(not Load) so you can fix types in Power Query.

Load these (rename each query to the file name without `.csv`):

| Table | Used for |
|---|---|
| `stockout_heatmap` | heatmap |
| `problem_stations`, `peak_problem_stations` | problem stations |
| `forecast_vs_actual_hourly`, `forecast_station_accuracy`, `forecast_metrics` | forecast |
| `siting_tracts` | map |
| `experiment_did_summary`, `experiment_net_effect`, `experiment_did_by_distance`, `experiment_event_study` | experiment |
| `export_manifest` | "data as of" note |

Skip `experiment_group_did` and `experiment_new_stations` unless you want detail pages.

### Fix types in Power Query

Select the column, then **Transform → Data Type**:

| Table | Column | Type | Why |
|---|---|---|---|
| all | `station_code`, `geoid`, `system`, `city` | Text | ids, not numbers (`geoid` would lose meaning as a number) |
| all | `lat`, `lon`, `lng` | Decimal Number | then set Data category in step 3 |
| `forecast_vs_actual_hourly` | `hour_local_ts` | Date/Time | for the line chart |
| `stockout_heatmap` | `hour_of_day` | Whole Number | column order 0–23 |
| `siting_tracts` | `eligible`, `is_candidate`, `low_income`, `equity_gap`, `poor_coverage` | True/False | for filters |
| `forecast_station_accuracy` | `departures_wape_model`, `departures_wape_naive` | Decimal Number | blanks are stations with 0 departures |

If Power Query guesses the locale wrong (decimals read as text), use **Data Type → Using
Locale… → English (United States)**.

Then **Home → Close & Apply**.

### Add one helper column

The experiment files use `system`, the others use `city`. In **Data view**, select
`experiment_did_summary` → **New column**, and repeat for the other experiment tables:

```DAX
city = SWITCH ( [system], "bluebikes", "boston", "citibike", "nyc", "divvy", "chicago", "all", "all cities" )
```

And in `forecast_station_accuracy`:

```DAX
Model vs baseline = IF ( [ae_model] < [ae_naive], "Model better", "Baseline as good or better" )
```

## 3. Model view: one city slicer for every table

1. **Modeling → New table**:

   ```DAX
   Cities = DATATABLE ( "city", STRING, { { "boston" }, { "nyc" }, { "chicago" }, { "all cities" } } )
   ```

2. In **Model view**, drag `Cities[city]` onto the `city` column of each table that has one.
   Each is **one-to-many**, cross-filter direction **Single** (Cities filters the table).
   `siting_tracts` is Boston-only and has no `city` column; leave it unrelated.
3. Select `lat` / `lon` columns → **Column tools → Data category → Latitude / Longitude**.
   Select `hour_of_day` → **Sort by column** isn't needed (it's a number already).
4. Set **Summarization: Don't summarize** on `rank`, `candidate_rank`, `hour_of_day`,
   `ring_m`, `rel_week`, `lat`, `lon`. Otherwise visuals add them up.

Why a separate Cities table: a slicer on one table's `city` only filters that table. A small
dimension table related to all of them filters the whole page at once (a star schema).

## 4. Measures

Create a measure table to keep things tidy: **Home → Enter data**, name it `_Measures`, one
empty column, Load. Then select it and **New measure** for each line below.

```DAX
-- Stockouts (ratio of sums: correct at any filter)
Observed Hours  = SUM ( stockout_heatmap[observed_hours] )
Empty Hours     = SUM ( stockout_heatmap[empty_hours] )
Full Hours      = SUM ( stockout_heatmap[full_hours] )
Empty Share     = DIVIDE ( [Empty Hours], [Observed Hours] )
Full Share      = DIVIDE ( [Full Hours], [Observed Hours] )
Stockout Share  = DIVIDE ( [Empty Hours] + [Full Hours], [Observed Hours] )
Lost Rides      = SUM ( stockout_heatmap[est_lost_rides] )
Lost Rides Range =
    FORMAT ( SUM ( stockout_heatmap[est_lost_rides_low] ), "#,0" ) & " to "
        & FORMAT ( SUM ( stockout_heatmap[est_lost_rides_high] ), "#,0" )
Stockout Period =
    "Snapshots " & FORMAT ( MIN ( problem_stations[period_from_local] ), "mmm d" ) & " to "
        & FORMAT ( MAX ( problem_stations[period_to_local] ), "mmm d, yyyy" )

-- Forecast (station-hour WAPE, reproduces analysis/results/netflow_metrics.csv)
WAPE Model       = DIVIDE ( SUM ( forecast_station_accuracy[ae_model] ), SUM ( forecast_station_accuracy[actual_departures] ) )
WAPE Baseline    = DIVIDE ( SUM ( forecast_station_accuracy[ae_naive] ), SUM ( forecast_station_accuracy[actual_departures] ) )
WAPE Improvement = [WAPE Baseline] - [WAPE Model]
Stations Model Better =
    CALCULATE ( COUNTROWS ( forecast_station_accuracy ), forecast_station_accuracy[Model vs baseline] = "Model better" )
Share Stations Model Better = DIVIDE ( [Stations Model Better], COUNTROWS ( forecast_station_accuracy ) )
Net 3h MAE Model =
    CALCULATE ( SUM ( forecast_metrics[value] ), forecast_metrics[metric] = "net3h_mae",
                forecast_metrics[forecast] = "lightgbm", forecast_metrics[variant] = "with_weather" )
Net 3h MAE Baseline =
    CALCULATE ( SUM ( forecast_metrics[value] ), forecast_metrics[metric] = "net3h_mae",
                forecast_metrics[forecast] = "seasonal_naive", forecast_metrics[variant] = "with_weather" )

-- Siting
Candidate Tracts      = CALCULATE ( COUNTROWS ( siting_tracts ), siting_tracts[is_candidate] = TRUE () )
Equity Gap Tracts     = CALCULATE ( COUNTROWS ( siting_tracts ), siting_tracts[equity_gap] = TRUE () )
People in Equity Gaps = CALCULATE ( SUM ( siting_tracts[population] ), siting_tracts[equity_gap] = TRUE () )

-- Experiment (main test, departures)
DiD % Nearby =
    CALCULATE ( MAX ( experiment_did_summary[did_pct_of_pre] ),
                experiment_did_summary[outcome] = "departures", experiment_did_summary[test] = "main" ) / 100
DiD % CI =
    VAR lo = CALCULATE ( MAX ( experiment_did_summary[pct_ci95_low] ),
                         experiment_did_summary[outcome] = "departures", experiment_did_summary[test] = "main" )
    VAR hi = CALCULATE ( MAX ( experiment_did_summary[pct_ci95_high] ),
                         experiment_did_summary[outcome] = "departures", experiment_did_summary[test] = "main" )
    RETURN "95% CI " & FORMAT ( lo, "0.0" ) & "% to " & FORMAT ( hi, "0.0" ) & "%"
Placebo % =
    CALCULATE ( MAX ( experiment_did_summary[did_pct_of_pre] ),
                experiment_did_summary[outcome] = "departures", experiment_did_summary[test] = "placebo_26w_earlier" ) / 100
Own Rides per Week   = MAX ( experiment_net_effect[own_departures_per_week] )
Net Rides per Week   = MAX ( experiment_net_effect[net_change_per_week] )
Net Rides CI =
    "95% CI " & FORMAT ( MAX ( experiment_net_effect[net_change_per_week_ci95_low] ), "+0;-0" ) & " to "
        & FORMAT ( MAX ( experiment_net_effect[net_change_per_week_ci95_high] ), "+0;-0" )
```

Format: select each share/WAPE/DiD % measure → **Measure tools → Format: Percentage**, 1 decimal.

The experiment measures use `MAX` because there is exactly one row per city once the city
slicer is set. With no city selected they'd show the largest city's value, so set the slicer
to single-select (step 5).

Why measures and not columns: a measure is computed after filters apply, so "Stockout Share"
for 10 selected stations is SUM(empty)/SUM(observed) over those 10. Averaging a per-station
share column would weight a station seen for 1 hour the same as one seen for 500.

## 5. Build the page

**View → Page view: Fit to page.** Canvas: **Format page → Canvas settings → Custom 1600 × 1400**.

### Header (top strip)
1. **Slicer** with `Cities[city]`. Format → Slicer settings → Style **Dropdown**, Selection
   **Single select** on, default **boston**.
2. **Slicer** with `stockout_heatmap[day_type]` (Tile style).
3. **Card (new)** visual with four measures: `Stockout Share`, `Lost Rides`, `WAPE Model`,
   `Net Rides per Week`. Add a small text **Card** under it with `Lost Rides Range` and
   `Stockout Period`.

### Visual 1: Stockout heatmap
1. Insert **Matrix**. Rows: `stockout_heatmap[station_name]`. Columns: `stockout_heatmap[hour_of_day]`.
   Values: `Stockout Share`.
2. Format → **Cell elements → Background color: On** → fx → Format style **Gradient**,
   based on `Stockout Share`, minimum white, maximum dark red.
3. Turn off row/column subtotals (Format → Row subtotals / Column subtotals: Off).
4. **Filters pane → station_name → Filter type: Top N → Top 25 by `Lost Rides`**.
5. Title: "Share of the hour stations were empty or full (top 25 by lost rides)".

### Visual 2: Top problem stations
1. Insert **Table**: `problem_stations[rank_stockout_hours]`, `station_name`, `capacity`,
   `station_hours_empty`, `station_hours_full`, `stockout_share`, `est_lost_rides`.
2. Format → **Cell elements → Data bars: On** for `station_hours_empty` (blue) and
   `station_hours_full` (orange).
3. Filter `rank_stockout_hours` is less than or equal to 15; sort ascending.
4. Second table from `peak_problem_stations`: `peak_window`, `rank`, `station_name`,
   `stockout_share`, `est_lost_rides`, plus a slicer on `peak_window`.

### Visual 3: Forecast vs actual
1. Insert **Line chart**. X-axis: `forecast_vs_actual_hourly[hour_local_ts]`; click the drop-down
   on it in the field well and choose **hour_local_ts** (not Date Hierarchy) so you get hours.
   Y-axis: Sum of `actual_departures`, `forecast_departures`, `naive_departures`.
2. Format → Lines: actual dark grey solid, forecast blue, naive light grey **dashed**.
3. Add a **Slicer** on `hour_local_ts` (style **Between**) to zoom to one week.
4. Next to it, a **Clustered bar chart**: Y-axis `forecast_station_accuracy[Model vs baseline]`,
   X-axis Count of `station_code`. For Boston it shows 550 vs 68.
5. Two **Cards**: `WAPE Model` vs `WAPE Baseline`, and `Net 3h MAE Model` vs `Net 3h MAE Baseline`.
   Title them "station-hour error", so nobody confuses it with the much smaller city-wide error.

### Visual 4: Siting map
1. **File → Options and settings → Options → Security → Map and Filled Map visuals: tick
   "Use Map and Filled Map visuals"**, then restart Power BI. (Or use the **Azure Maps** visual.)
2. Insert **Map**. Latitude: `siting_tracts[lat]`, Longitude: `siting_tracts[lon]`,
   Legend: `map_group`, Bubble size: `population`, Tooltips: `neighborhood`, `candidate_rank`,
   `dist_nearest_station_m`, `median_household_income`.
3. Format → Bubbles → Colors: Covered light grey, New-station candidate blue,
   Equity gap orange; filter out "Not scored (< 500 residents)".
4. Beside it, a **Table**: `candidate_rank`, `neighborhood`, `dist_nearest_station_m`, `score`,
   `median_household_income`, filtered to `is_candidate = True` and `candidate_rank` ≤ 15,
   sorted by `candidate_rank`.
5. Text box under the map: "Points are Census tract centres (~4,000 people each), not exact
   station sites. Distances are straight-line."

### Visual 5: Experiment result
1. **Cards**: `Own Rides per Week`, `DiD % Nearby` (with `DiD % CI` as a second card below),
   `Net Rides per Week` (with `Net Rides CI`), `Placebo %`.
2. **Clustered column chart**: X-axis `experiment_did_by_distance[ring_m]`, Y-axis
   `did_pct_of_pre` (Summarization: Don't summarize, or Max).
   **Analytics pane → Error bars → Enabled**, Upper bound `pct_ci95_high`, Lower bound
   `pct_ci95_low`. Sort the axis 0-250, 250-500, 500-1000 (they sort as text correctly).
3. Optional **Line chart** of `experiment_event_study`: X `rel_week`, Y `estimate`, with the same
   error bars using `ci95_low` / `ci95_high`. A flat line before week 0 is the visual check
   that treated and control stations were trending alike before the opening.
4. Text box: "Do new stations add rides or take them from neighbours? Stacked
   difference-in-differences on new-station openings in the public trip history; see
   analysis/results/new_stations/new_station_experiment.md."

### Footer
Text box with: "Sources: Bluebikes/Citi Bike/Divvy GBFS (self-collected) and trip history,
US Census ACS 2020–2024. Built with free tools. Code: github.com/khus-hi/bikeshare-stockouts-siting".

## 6. Check it before you share it

With the city slicer on **boston** and nothing else filtered, these must match the repo:

| Visual | Should show | Source |
|---|---|---|
| WAPE Model / Baseline | 64.7% / 85.2% | `analysis/results/netflow_metrics.csv` (boston, with_weather, departures_wape) |
| Stations model better | 550 of 618 | `forecast_station_accuracy.csv` |
| Net 3h MAE | 1.60 / 2.20 | `netflow_metrics.csv` (net3h_mae) |
| Candidate tracts / equity gaps | 35 / 6 | `analysis/results/siting/boston_siting_summary.md` |
| DiD % nearby, net rides | -3.4%, +82/week | `analysis/results/new_stations/did_summary.csv`, `net_effect.csv` |

If one doesn't match, the usual cause is a column left on Sum or Average that should be
"Don't summarize", or a slicer still set to a different city.

## 7. Save and share (free)

* **File → Save as** `dashboard/ops_dashboard.pbix`. Commit it; it's a few MB.
* **File → Export → Export to PDF** → `dashboard/ops_dashboard.pdf`, and take a PNG
  screenshot for the README. Recruiters can see these without Power BI.
* "Publish to web" needs a Power BI Pro or school/work account, so a PDF and screenshot are
  the free way to show it. Link the Looker Studio version for a clickable copy.
* After ~2026-11-23: re-run step 1 on the Mac, copy the new CSVs over the old ones, then
  **Home → Refresh** in Power BI. All visuals update; nothing needs rebuilding.

## Interview notes

* **Star schema in miniature:** one `Cities` dimension filtering several fact tables, so one
  slicer drives the whole page.
* **Measures vs calculated columns:** columns are computed once at load, row by row; measures
  are computed at query time under the current filters. Ratios must be measures.
* **Why the work happens before Power BI:** the stockout logic (poll gaps, hour splitting,
  lost-ride estimates) is tested SQL in the repo. The dashboard only aggregates, so the numbers
  on screen can be traced back to code.
