"""Build the hourly station panel used to train the net-flow forecast.

One row per (station, UTC hour), including hours with no trips (0 departures, 0
arrivals). That "dense" panel matters: a model trained only on hours that had trips
would never learn that most station-hours are quiet.

Steps, per system:
  1. Count departures (by trip start hour) and arrivals (by trip end hour) per station
     per UTC hour from trips_clean (ingest/sql/trips_views.sql). UTC avoids the missing
     and doubled hours at the daylight-saving switches.
  2. Keep stations with at least --min-trips trips in the whole period (drops test and
     temporary station codes) and fill every hour between a station's first and last
     trip with zeros.
  3. Lag features with window functions: the same hour 1 day, 1, 2 and 3 weeks earlier,
     the 3-week same-hour average, and yesterday's total. Rows from a station's first 3
     weeks are dropped because their lags are not complete yet.
  4. Join hourly weather for the city (data/weather/history, from
     ingest/weather_download.py) on the UTC hour, if it has been downloaded.
  5. Calendar features in local time: hour, weekday, weekend, US federal holiday.

Output: data/features/netflow/system=<system>/panel.parquet

Usage (from the repo root):
  python models/netflow_features.py
  python models/netflow_features.py --systems bluebikes
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netflow_common import (  # noqa: E402
    FEATURES_DIR, REPO, SYSTEM_CITY, SYSTEM_TZ, TARGETS, WEATHER_FEATURES, lag_features,
)

TRIPS_SQL = REPO / "ingest" / "sql" / "trips_views.sql"
WEATHER_GLOB = "data/weather/history/city={city}/weather.parquet"

PANEL_SQL = """
CREATE OR REPLACE TEMP TABLE hourly AS
WITH ev AS (
    SELECT start_station_id AS station_code,
           date_trunc('hour', timezone('UTC', started_at_utc)) AS hour_utc,
           1 AS dep, 0 AS arr, start_lat AS lat, start_lng AS lng
    FROM trips_clean WHERE system = $system AND start_station_id IS NOT NULL
    UNION ALL
    SELECT end_station_id, date_trunc('hour', timezone('UTC', ended_at_utc)),
           0, 1, end_lat, end_lng
    FROM trips_clean WHERE system = $system AND end_station_id IS NOT NULL
)
SELECT station_code, hour_utc,
       sum(dep)::INT AS departures, sum(arr)::INT AS arrivals,
       median(lat) FILTER (WHERE dep = 1) AS lat, median(lng) FILTER (WHERE dep = 1) AS lng
FROM ev GROUP BY ALL;

-- The panel covers the hours in which trips in the files *start*. Arrivals after the last
-- start hour belong to trips from the previous month's file and would be half-counted.
CREATE OR REPLACE TEMP TABLE bounds AS
SELECT min(hour_utc) AS lo, max(hour_utc) AS hi FROM hourly WHERE departures > 0;

CREATE OR REPLACE TEMP TABLE stations AS
SELECT station_code,
       greatest(min(hour_utc), (SELECT lo FROM bounds)) AS first_h,
       least(max(hour_utc),    (SELECT hi FROM bounds)) AS last_h,
       sum(departures + arrivals) AS n_trips,
       median(lat) AS lat, median(lng) AS lng
FROM hourly GROUP BY station_code
HAVING sum(departures + arrivals) >= $min_trips;
"""

LAGS_SQL = """
WITH grid AS (
    SELECT station_code, unnest(generate_series(first_h, last_h, INTERVAL 1 HOUR)) AS hour_utc
    FROM stations
), dense AS (
    SELECT g.station_code, g.hour_utc,
           coalesce(h.departures, 0) AS departures,
           coalesce(h.arrivals, 0)   AS arrivals
    FROM grid g LEFT JOIN hourly h USING (station_code, hour_utc)
), lagged AS (
    SELECT *,
        {lag_cols}
    FROM dense
    WINDOW w AS (PARTITION BY station_code ORDER BY hour_utc)
)
SELECT l.station_code, l.hour_utc, l.departures::SMALLINT AS departures, l.arrivals::SMALLINT AS arrivals,
       {lag_casts},
       s.lat::FLOAT AS lat, s.lng::FLOAT AS lng{weather_cols},
       -- Calendar in the station's local time (same rules as netflow_common.add_calendar)
       hour(timezone($tz, timezone('UTC', l.hour_utc)))::TINYINT               AS hour_local,
       (isodow(timezone($tz, timezone('UTC', l.hour_utc))) - 1)::TINYINT       AS dow_local,
       (isodow(timezone($tz, timezone('UTC', l.hour_utc))) >= 6)::TINYINT      AS is_weekend,
       (CAST(timezone($tz, timezone('UTC', l.hour_utc)) AS DATE) IN (SELECT d FROM holidays))::TINYINT AS is_holiday
FROM lagged l
JOIN stations s USING (station_code)
{weather_join}
WHERE l.departures_lag504 IS NOT NULL
"""


def lag_sql() -> str:
    parts = []
    for t in TARGETS:
        parts += [
            f"lag({t}, 24)  OVER w AS {t}_lag24",
            f"lag({t}, 168) OVER w AS {t}_lag168",
            f"lag({t}, 336) OVER w AS {t}_lag336",
            f"lag({t}, 504) OVER w AS {t}_lag504",
            f"(lag({t}, 168) OVER w + lag({t}, 336) OVER w + lag({t}, 504) OVER w) / 3.0 AS {t}_how3w",
            f"sum({t}) OVER (PARTITION BY station_code ORDER BY hour_utc "
            f"ROWS BETWEEN 47 PRECEDING AND 24 PRECEDING) AS {t}_prev24",
        ]
    return ",\n        ".join(parts)


def build(system: str, min_trips: int, out_dir: Path) -> Path:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("SET memory_limit = '6GB'")
    con.execute("SET preserve_insertion_order = false")
    # Same views as analysis/build_views.py, but reading only this system's files (faster,
    # and safe while another system is still downloading).
    con.execute(TRIPS_SQL.read_text().replace(
        "data/trips/parquet/*/*/*.parquet", f"data/trips/parquet/system={system}/*/*.parquet"))
    for stmt in PANEL_SQL.split(";"):
        if stmt.strip():
            params = {k: v for k, v in {"system": system, "min_trips": min_trips}.items() if f"${k}" in stmt}
            con.execute(stmt, params)

    weather_file = REPO / WEATHER_GLOB.format(city=SYSTEM_CITY[system])
    if weather_file.exists():
        weather_cols = "".join(f", w.{c}::FLOAT AS {c}" for c in WEATHER_FEATURES)
        weather_join = f"LEFT JOIN read_parquet('{weather_file.as_posix()}') w ON w.hour_utc = l.hour_utc"
    else:
        print(f"  {system}: no weather file at {weather_file.relative_to(REPO)}; weather columns left empty")
        weather_cols = "".join(f", NULL::FLOAT AS {c}" for c in WEATHER_FEATURES)
        weather_join = ""

    hol = USFederalHolidayCalendar().holidays("2000-01-01", "2040-12-31")
    con.register("holidays_df", pd.DataFrame({"d": hol.date}))
    con.execute("CREATE TEMP TABLE holidays AS SELECT d::DATE AS d FROM holidays_df")
    lag_casts = ", ".join(f"l.{c}::FLOAT AS {c}" for t in TARGETS for c in lag_features(t))
    sql = LAGS_SQL.format(lag_cols=lag_sql(), lag_casts=lag_casts, weather_cols=weather_cols,
                          weather_join=weather_join)
    dest = out_dir / f"system={system}" / "panel.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Written straight from DuckDB: Citi Bike is ~20M rows, too big to round-trip through pandas
    # on a 16 GB laptop.
    sql = sql.replace("$system", "'" + system + "'").replace("$tz", "'" + SYSTEM_TZ[system] + "'")
    con.execute(f"COPY ({sql}) TO '{dest.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
    n_st = con.execute("SELECT count(*) FROM stations").fetchone()[0]
    n, lo, hi, wmiss = con.execute(
        f"SELECT count(*), min(hour_utc), max(hour_utc), avg((temperature_2m IS NULL)::INT) "
        f"FROM read_parquet('{dest.as_posix()}')").fetchone()
    con.close()
    print(f"  {system}: {n_st:,} stations, {n:,} station-hours ({lo} .. {hi} UTC), "
          f"weather missing on {wmiss:.1%} of rows, {time.time() - t0:.0f}s -> {dest.relative_to(REPO)}")
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", nargs="+", choices=list(SYSTEM_TZ), default=list(SYSTEM_TZ))
    ap.add_argument("--min-trips", type=int, default=100,
                    help="drop station codes with fewer trips (starts + ends) in the whole period (default 100)")
    ap.add_argument("--out-dir", type=Path, default=FEATURES_DIR)
    args = ap.parse_args(argv)
    import os
    os.chdir(REPO)  # trips_views.sql uses paths relative to the repo root
    for system in args.systems:
        build(system, args.min_trips, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
