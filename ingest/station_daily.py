"""Build a small station x day ride-count table from the official trip files.

Why a separate table: the new-station study (analysis/new_station_did.py) needs about
30 months of history to see 18 months of openings plus a 12-month burn-in. Keeping 30
months of Citi Bike as full trip Parquet would be ~3 GB; the study only needs how many
rides started and ended at each station each day, which is a few MB.

For each system-month:
  * If data/trips/parquet/system=<s>/month=<m>/trips.parquet exists (made by
    trips_download.py), aggregate from it. No download.
  * Otherwise download the operator's file (same buckets and checks as trips_download.py),
    aggregate straight from the CSV, and delete the raw file.

Output, one file per system-month:
  data/station_daily/system=<s>/month=YYYY-MM/station_daily.parquet
    system, station_id, day (local date), departures, arrivals
  data/station_daily/system=<s>/month=YYYY-MM/station_coords.parquet
    system, station_id, station_name (most common), lat, lng (median of trip coordinates),
    n_trips (rides with a coordinate used for the median)

Days are local wall-clock dates, as published. Departures are counted on the start day,
arrivals on the end day. A ride that ends in the next month is counted in the month file
it was published in, so a few arrivals land on the 1st of the following month; the study
sums by day across all months, which puts them back on the right day.

Usage:
  python ingest/station_daily.py --start 2024-04 --end 2026-09
  python ingest/station_daily.py --start 2024-04 --end 2026-09 --systems bluebikes divvy
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trips_download as td  # noqa: E402  (reuses bucket listing, download, unzip, column check)


def month_range(start: str, end: str) -> list[str]:
    y, m = map(int, start.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= end:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def source_sql(files: list[str], from_parquet: bool) -> str:
    """A SELECT with typed columns, from either our Parquet or the raw CSV text."""
    if from_parquet:
        return f"SELECT start_station_id, start_station_name, end_station_id, end_station_name, " \
               f"started_at, ended_at, start_lat, start_lng, end_lat, end_lng FROM read_parquet({files!r})"
    return f"""
        SELECT NULLIF(trim(start_station_id), '')   AS start_station_id,
               NULLIF(trim(start_station_name), '') AS start_station_name,
               NULLIF(trim(end_station_id), '')     AS end_station_id,
               NULLIF(trim(end_station_name), '')   AS end_station_name,
               TRY_CAST(started_at AS TIMESTAMP)    AS started_at,
               TRY_CAST(ended_at AS TIMESTAMP)      AS ended_at,
               TRY_CAST(start_lat AS DOUBLE) AS start_lat, TRY_CAST(start_lng AS DOUBLE) AS start_lng,
               TRY_CAST(end_lat AS DOUBLE)   AS end_lat,   TRY_CAST(end_lng AS DOUBLE)   AS end_lng
        FROM read_csv({files!r}, all_varchar = true, header = true)"""


def aggregate(con: duckdb.DuckDBPyConnection, system: str, src: str, out_dir: Path) -> tuple[int, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"CREATE OR REPLACE TEMP TABLE t AS {src}")
    con.execute(f"""
        COPY (
            WITH dep AS (
                SELECT start_station_id AS station_id, started_at::DATE AS day, count(*) AS departures
                FROM t WHERE start_station_id IS NOT NULL AND started_at IS NOT NULL GROUP BY ALL),
            arr AS (
                SELECT end_station_id AS station_id, ended_at::DATE AS day, count(*) AS arrivals
                FROM t WHERE end_station_id IS NOT NULL AND ended_at IS NOT NULL GROUP BY ALL)
            SELECT '{system}' AS system, station_id, day,
                   coalesce(departures, 0)::INTEGER AS departures, coalesce(arrivals, 0)::INTEGER AS arrivals
            FROM dep FULL OUTER JOIN arr USING (station_id, day)
            ORDER BY station_id, day
        ) TO '{out_dir / 'station_daily.parquet'}' (FORMAT parquet)
    """)
    con.execute(f"""
        COPY (
            WITH ends AS (
                SELECT start_station_id AS station_id, start_station_name AS station_name,
                       start_lat AS lat, start_lng AS lng FROM t WHERE start_station_id IS NOT NULL
                UNION ALL
                SELECT end_station_id, end_station_name, end_lat, end_lng FROM t WHERE end_station_id IS NOT NULL)
            SELECT '{system}' AS system, station_id,
                   mode(station_name) AS station_name,
                   median(lat) AS lat, median(lng) AS lng,
                   count(lat) AS n_trips
            FROM ends
            WHERE lat BETWEEN -90 AND 90 AND lat <> 0 AND lng <> 0
            GROUP BY ALL ORDER BY station_id
        ) TO '{out_dir / 'station_coords.parquet'}' (FORMAT parquet)
    """)
    n_trips, n_stations = con.execute(
        "SELECT count(*), count(DISTINCT start_station_id) FROM t").fetchone()
    con.execute("DROP TABLE t")
    return n_trips, n_stations


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", nargs="+", choices=list(td.SYSTEMS), default=list(td.SYSTEMS))
    ap.add_argument("--start", required=True, help="first month YYYY-MM")
    ap.add_argument("--end", required=True, help="last month YYYY-MM")
    ap.add_argument("--data-dir", type=Path, default=td.repo_root() / "data")
    ap.add_argument("--memory-limit", default="6GB")
    args = ap.parse_args(argv)

    data_dir = args.data_dir.resolve()
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(f"SET temp_directory = '{data_dir / '_duckdb_tmp'}'")
    failures = 0
    for system in args.systems:
        available = None
        for month in month_range(args.start, args.end):
            out_dir = data_dir / "station_daily" / f"system={system}" / f"month={month}"
            if (out_dir / "station_coords.parquet").exists():
                print(f"{system} {month}: done already, skipping")
                continue
            t0 = time.time()
            trips_pq = data_dir / "trips" / "parquet" / f"system={system}" / f"month={month}" / "trips.parquet"
            work = data_dir / "trips" / "_raw_daily" / system / month
            try:
                if trips_pq.exists():
                    n, s = aggregate(con, system, source_sql([str(trips_pq)], True), out_dir)
                    how = "from trips.parquet"
                else:
                    if available is None:
                        available = td.monthly_files(system)
                    obj = available.get(month)
                    if obj is None:
                        print(f"{system} {month}: not published")
                        failures += 1
                        continue
                    raw = work / obj["key"]
                    td.download(f"{td.SYSTEMS[system][0]}/{obj['key']}", raw, obj["size"])
                    csvs = [str(p) for p in td.extract_csvs(raw, work / "extracted")]
                    for f in csvs:
                        cols = [r[0] for r in con.execute(
                            "DESCRIBE SELECT * FROM read_csv(?, all_varchar = true, header = true)", [f]).fetchall()]
                        if cols != td.EXPECTED_COLUMNS:
                            raise ValueError(f"{f}: unexpected columns {cols}")
                    n, s = aggregate(con, system, source_sql(csvs, False), out_dir)
                    how = f"from {obj['key']} ({obj['size'] / 1e6:.0f} MB)"
                print(f"{system} {month}: {n:,} trips, {s:,} start stations, {how}, {time.time() - t0:.0f}s",
                      flush=True)
            except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                failures += 1
                print(f"{system} {month}: FAILED {type(exc).__name__}: {exc}", flush=True)
            finally:
                shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(data_dir / "_duckdb_tmp", ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
