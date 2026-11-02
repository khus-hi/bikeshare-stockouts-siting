"""Create (or refresh) every DuckDB view in one database file: data/bikeshare.duckdb.

Runs, in order:
  ingest/sql/duckdb_views.sql     GBFS snapshot views   (needs data/gbfs/ from the collector)
  ingest/sql/trips_views.sql      trip history views    (needs data/trips/ from trips_download.py)
  analysis/sql/stockout_views.sql stockout KPI views    (needs both)

A group is skipped, with a message, when its data isn't there yet, so you can build the
trip views today and re-run once snapshots exist. Views store relative paths, so query
the database from the repo root (or whatever --root you built it with):

  python analysis/build_views.py
  duckdb data/bikeshare.duckdb -c "SELECT * FROM peak_problem_stations"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
GBFS_SQL = REPO / "ingest" / "sql" / "duckdb_views.sql"
TRIPS_SQL = REPO / "ingest" / "sql" / "trips_views.sql"
STOCKOUT_SQL = REPO / "analysis" / "sql" / "stockout_views.sql"


def has_files(root: Path, pattern: str) -> bool:
    return any(root.glob(pattern))


def build(root: Path, db_path: Path) -> list[str]:
    os.chdir(root)  # the SQL uses paths relative to the folder that contains data/
    have_gbfs = (has_files(root, "data/gbfs/station_status/*/*/*.parquet")
                 and has_files(root, "data/gbfs/polls/*/*.parquet")
                 and has_files(root, "data/gbfs/station_information/*/*/*.parquet"))
    have_trips = has_files(root, "data/trips/parquet/*/*/*.parquet")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    built = []
    for name, sql, ready, why in [
        ("GBFS snapshot views", GBFS_SQL, have_gbfs, "no GBFS Parquet under data/gbfs/ yet"),
        ("trip views", TRIPS_SQL, have_trips, "no trip Parquet; run ingest/trips_download.py"),
        ("stockout KPI views", STOCKOUT_SQL, have_gbfs and have_trips, "needs both GBFS and trip data"),
    ]:
        if not ready:
            print(f"skipped {name}: {why}")
            continue
        con.execute(sql.read_text())
        built.append(name)
        print(f"built {name}")
    con.close()
    return built


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=REPO, help="folder that contains data/ (default: repo root)")
    ap.add_argument("--db", type=Path, default=None, help="database file (default: <root>/data/bikeshare.duckdb)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    build(root, (args.db or root / "data" / "bikeshare.duckdb").resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
