# analysis/: stockout KPIs in DuckDB

Two data sets meet here:

* **Trip history** (official monthly files from the operators) says how many rides
  started and ended at each station, each hour. It cannot show when a station was empty:
  a rider who found no bike leaves no record.
* **GBFS snapshots** (self-collected every 5 minutes by `ingest/gbfs_collector.py`) say
  how many bikes and docks each station had. That is where empty and full time comes from.

Lost rides need both: how long a station was empty (snapshots) times how many rides it
normally gets at that hour (trips).

## Build it

```bash
python ingest/trips_download.py      # latest 12 published months, ~1.5 GB Parquet, ~9 min (see below)
python analysis/build_views.py       # creates data/bikeshare.duckdb with every view
duckdb data/bikeshare.duckdb -c "SELECT * FROM peak_problem_stations WHERE city = 'boston'"
```

Run DuckDB from the repo root: the views point at `data/...` with relative paths.
`build_views.py` skips any group of views whose data isn't there yet, so the trip views
work today and the stockout views appear once the collector has saved some snapshots
(re-run it whenever you want the views to see new files; the views themselves always read
whatever Parquet is on disk at query time).

## Trip history

Files: Bluebikes `s3.amazonaws.com/hubway-data`, Citi Bike `s3.amazonaws.com/tripdata`
(NYC files only; the `JC-` Jersey City files are skipped), Divvy `divvy-tripdata.s3.amazonaws.com`.
All three use the same 13-column layout; the script refuses any file whose header differs.

Stored as one Parquet file per system per month:
`data/trips/parquet/system=<system>/month=YYYY-MM/trips.parquet`, plus
`data/trips/manifest.csv` with the source file, row counts and sizes. Raw zips are deleted
after conversion, so peak disk use is one month of Citi Bike (about 1 GB zip + 4 GB of CSV).

Measured on 2026-11-02 (latest 12 published months: October 2025 to September 2026;
October 2026 was not yet published):

| System | Trips | Downloaded (zip/csv) | Parquet on disk |
|---|---:|---:|---:|
| Bluebikes | 4,573,597 | 0.30 GB | 128 MB |
| Citi Bike | 45,384,105 | 8.27 GB | 1,186 MB |
| Divvy | 6,171,785 | 0.24 GB | 167 MB |
| **Total** | **56,129,487** | **8.81 GB** | **1,481 MB** |

The full run took about 9 minutes on a 4-core cloud machine; on home Wi-Fi the 8.8 GB
download will dominate. Two choices keep the Parquet small: timestamps are rounded down to
whole seconds (the published milliseconds are noise for hourly work and nearly doubled the
time columns), and UTC times are computed in the view instead of stored. That took Citi
Bike from 1.6 GB to 1.2 GB. The largest remaining column is `ride_id` (random hex, does not
compress); it is kept as the trip's unique key. Divvy trips without a station (about 21%,
dockless e-bike rides) are kept in `trips` but can't count toward station demand.

### Columns of `trips`

| Column | Meaning |
|---|---|
| `system`, `city` | `bluebikes`/`boston`, `citibike`/`nyc`, `divvy`/`chicago` |
| `started_at`, `ended_at` | local clock time, as published |
| `started_at_utc`, `ended_at_utc` | the same instants in UTC (GBFS snapshots are UTC) |
| `start_station_id`, `end_station_id` | the operator's public station code; equals GBFS `short_name` |
| `rideable_type`, `member_casual`, lat/lng, names | as published |
| `source_file` | which S3 object the row came from |

Station codes were checked against the live GBFS `station_information` on 2026-11-02:
trip files use codes like Bluebikes `M32006`, Citi Bike `6140.05`, Divvy `CHI00252`, and
those are the GBFS `short_name`s. The GBFS `station_id` is a different internal id
(mostly UUIDs), so every join between the two goes through `short_name`.

## Views

| View | Grain | What it is |
|---|---|---|
| `trips`, `trips_clean` | trip | all trips; `trips_clean` drops non-positive or 24 h+ durations |
| `trips_quality` | system × month | trips, missing stations, bad durations, median minutes |
| `station_hour_trips` | station × local hour | departures, arrivals, net inflow (hours with no trips have no row) |
| `poll_segments` | system × poll | time between consecutive successful polls, if ≤ 15 min |
| `poll_coverage_daily` | system × UTC day | share of the day observed |
| `station_hour_stockouts` | station × hour | observed, empty, full and out-of-service minutes |
| `typical_demand` | station × hour of week | average departures per hour, with a 95% range |
| `lost_rides_station_hour` | station × hour | the two above joined: estimated lost rides |
| `station_stockout_summary` | station | totals over the snapshot period |
| `peak_problem_stations` | city × peak × rank | top 20 per city, weekday 7–9am and 4–7pm |
| `station_match_rate` | system | share of GBFS stations that have a demand estimate |

### How empty/full minutes are counted

1. **Observed time.** The collector saves a station only when it changes, so a missing
   row is ambiguous. The poll log settles it: the time between two successful polls of a
   system counts as observed if the gap is 15 minutes or less (one missed poll is fine,
   longer outages are excluded, as `docs/requirements.md` specifies).
2. **State in each segment.** Each station takes its last saved state at or before the
   segment start (an `ASOF JOIN`), looking back only within the same UTC day. The daily
   keyframe guarantees every station has a row each day, and a station that drops out of
   the feed stops counting at the end of its last day.
3. **Empty** = `num_bikes_available = 0` while installed and renting.
   **Full** = `num_docks_available = 0` while installed and returning.
4. **Split by hour.** Segments are at most 15 minutes, so each is split at most once at an
   hour boundary. Hours are reported in UTC and in the station's local time.

A station that goes empty between two polls is counted from the next poll, so each spell
can be off by up to 5 minutes at each end; over many spells this evens out.

### How lost rides are estimated

`lost rides = typical departures in that hour × (minutes empty ÷ 60)`

* **Typical departures** (`typical_demand.demand_per_hour`, λ in the requirements doc):
  the average number of departures at that station in that hour of the week (e.g.
  Tuesdays 7–8am), from trips in the **same calendar month a year earlier** (snapshots in
  November 2026 use trips from November 2025). Hours with zero departures count as zeros.
* **Range:** a 95% Poisson interval on the departure count. With one reference month each
  hour of the week occurs about 4 times, so the range is wide; that is reported, not hidden.
* **Why it is conservative:** trip history only records rides that happened. If the station
  was empty during the reference month, those hours pull the average down. The requirements
  ask for demand from hours when the station was *never* empty, which needs snapshots and
  trips for the same period. Our snapshots start 2026-11-02 and the November 2026 trip file
  is published in early December, so that refinement can be added then.
* Stations with no trips in the reference month (new or renumbered) get no estimate
  (`has_demand_estimate = false`) rather than a guess. `station_match_rate` shows how many.

### Peak problem stations

Weekdays Mon–Fri, local time; AM = 7:00–9:00, PM = 16:00–19:00. Ranked by the share of
observed peak time the station was empty or full; ties go to more estimated lost rides.
Stations observed for less than half as long as the best-covered station in that window
are left out, so one that only appeared for a day can't top the list.

## Tests

`python -m pytest tests/` checks the arithmetic on a tiny hand-worked example (a gap, an
hour-crossing segment, an empty and a full station, one demand estimate). The example rows
live only in the test and a temp folder; no number from them is ever reported.

## Interview notes

* **Why Parquet + DuckDB?** Columnar and compressed, so a year of Citi Bike fits in a few
  hundred MB and a full scan takes seconds on a laptop, with plain SQL and no server.
* **Why join on `short_name`?** The trip files and the live feed use different station
  ids; checking that before writing any join avoids a silent zero-match result.
* **Why poll segments instead of row-to-row intervals?** With change-only storage a
  station can legitimately go hours without a row. Only the poll log can tell "unchanged"
  from "not looking".
* **Why a range for lost rides?** It is an estimate built on an average of a few hours,
  and the method understates demand at chronically empty stations; the range and the
  caveat go with the number.

## Siting score (Census tracts, Boston)

Three scripts, run from the repo root on a machine that can reach api.census.gov and
www2.census.gov (the cloud dev environment cannot):

```bash
python ingest/census_download.py           # ACS 2020-2024 5-year tracts for Boston, NYC, Chicago
python analysis/tract_station_distance.py  # tract interior point -> nearest GBFS station
python analysis/siting_score.py            # Boston score, top 15, sensitivity, equity gaps
```

Outputs go to `analysis/results/siting/`. Settings (weights, 500-resident floor, 1,600 m
distance cap, 400 m walk radius) are in `analysis/siting_config.json`. The score is built by
hand in Excel following `docs/siting_excel_guide.md`; `siting_score.py` is the same math in
Python, used to check the workbook and to run the leave-one-out weight sensitivity.

* **Factors:** population density, share of commuters using transit/bike/walk, share of
  households without a car, share aged 18-34, distance to the nearest station (capped),
  and optionally low income (weight 0 by default). Each is min-max scaled across eligible
  Boston tracts, then `score = SUMPRODUCT(z, weights)`.
* **Not yet included:** lost rides at nearby stations (needs the finished 21-day stockout
  collection) and distance to MBTA stops; both are in the requirements and planned.
* **Equity gap:** a tract in the bottom 25% of eligible Boston tracts by median household
  income with no station within 400 m.
* `tests/test_siting.py` checks the distance math and a hand-scored three-tract example.
