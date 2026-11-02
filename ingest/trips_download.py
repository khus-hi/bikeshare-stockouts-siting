"""Download official monthly trip history for Bluebikes, Citi Bike and Divvy and store it
as one normalized trips table in Parquet (queried with DuckDB, no server needed).

Sources (public S3 buckets published by the operators, no key needed):
  Bluebikes  https://s3.amazonaws.com/hubway-data/
  Citi Bike  https://s3.amazonaws.com/tripdata/       (NYC files only; "JC-" Jersey City files skipped)
  Divvy      https://divvy-tripdata.s3.amazonaws.com/

What it does, one system-month at a time (so disk use stays small):
  1. Lists the bucket and picks the latest N published months (default 12) per system.
  2. Downloads the file (zip or plain csv) to data/trips/_raw/.
  3. Extracts every CSV inside (Citi Bike zips hold several CSVs, sometimes nested zips).
  4. Reads all CSVs with DuckDB as text, checks the columns are the expected ones,
     casts types, adds city/system columns, and writes
       data/trips/parquet/system=<system>/month=YYYY-MM/trips.parquet   (zstd)
  5. Deletes the raw download (use --keep-raw to keep it) and records row counts and
     the source file in data/trips/manifest.csv.

Months already converted are skipped, so re-running only fetches what is new.

Usage:
  python ingest/trips_download.py                         # latest 12 months, all systems
  python ingest/trips_download.py --systems bluebikes divvy
  python ingest/trips_download.py --months 2025-11 2025-12
  python ingest/trips_download.py --latest 3 --keep-raw
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests

SYSTEMS = {
    # system: (bucket URL, file-name pattern, city label, local time zone)
    "bluebikes": ("https://s3.amazonaws.com/hubway-data", r"^(\d{6})-bluebikes-tripdata\.(?:csv\.zip|zip|csv)$",
                  "boston", "America/New_York"),
    "citibike": ("https://s3.amazonaws.com/tripdata", r"^(\d{6})-citibike-tripdata(?:_csv)?\.(?:csv\.zip|zip|csv)$",
                 "nyc", "America/New_York"),
    "divvy": ("https://divvy-tripdata.s3.amazonaws.com", r"^(\d{6})-divvy-tripdata\.(?:csv\.zip|zip|csv)$",
              "chicago", "America/Chicago"),
}

# All three operators (Lyft-run) have used this schema since 2023.
EXPECTED_COLUMNS = [
    "ride_id", "rideable_type", "started_at", "ended_at",
    "start_station_name", "start_station_id", "end_station_name", "end_station_id",
    "start_lat", "start_lng", "end_lat", "end_lng", "member_casual",
]

S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def list_bucket(bucket_url: str) -> list[dict]:
    """List every object in a public S3 bucket (handles pagination)."""
    objects, token = [], None
    while True:
        params = {"list-type": "2"}
        if token:
            params["continuation-token"] = token
        resp = requests.get(bucket_url + "/", params=params, timeout=60)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for c in root.findall("s3:Contents", S3_NS):
            objects.append({
                "key": c.find("s3:Key", S3_NS).text,
                "size": int(c.find("s3:Size", S3_NS).text),
                "last_modified": c.find("s3:LastModified", S3_NS).text,
            })
        if root.findtext("s3:IsTruncated", namespaces=S3_NS) == "true":
            token = root.findtext("s3:NextContinuationToken", namespaces=S3_NS)
        else:
            return objects


def monthly_files(system: str) -> dict[str, dict]:
    """{'YYYY-MM': object} for one system. If a month has more than one file (e.g. a
    re-upload under a new name), the most recently modified one wins."""
    bucket, pattern, _, _ = SYSTEMS[system]
    out: dict[str, dict] = {}
    for obj in list_bucket(bucket):
        m = re.match(pattern, obj["key"])
        if not m:
            continue
        month = f"{m.group(1)[:4]}-{m.group(1)[4:]}"
        if month not in out or obj["last_modified"] > out[month]["last_modified"]:
            out[month] = obj
    return out


def download(url: str, dest: Path, expected_size: int) -> None:
    """Stream to disk with retries; resume is not needed because files are re-fetched whole."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            with requests.get(url, stream=True, timeout=(10, 120)) as resp:
                resp.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8 << 20):
                        fh.write(chunk)
            if tmp.stat().st_size != expected_size:
                raise IOError(f"size mismatch: got {tmp.stat().st_size}, bucket says {expected_size}")
            tmp.replace(dest)
            return
        except (requests.RequestException, IOError) as exc:
            wait = 2 ** (attempt + 1)
            print(f"    download failed ({exc}); retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"could not download {url}")


def extract_csvs(path: Path, out_dir: Path) -> list[Path]:
    """Return all CSV files contained in `path`, unzipping nested zips. Skips macOS
    resource-fork entries (__MACOSX/, ._file)."""
    if path.suffix.lower() == ".csv":
        return [path]
    out_dir.mkdir(parents=True, exist_ok=True)
    found: list[Path] = []
    with zipfile.ZipFile(path) as zf:
        for i, info in enumerate(zf.infolist()):
            name = info.filename
            base = Path(name).name
            if info.is_dir() or name.startswith("__MACOSX") or base.startswith("._"):
                continue
            if base.lower().endswith((".csv", ".zip")):
                # Flatten paths and prefix an index so two entries can't collide.
                target = out_dir / f"{i:03d}_{base}"
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 8 << 20)
                if base.lower().endswith(".zip"):
                    found += extract_csvs(target, out_dir / target.stem)
                    target.unlink()
                else:
                    found.append(target)
    return found


def convert(con: duckdb.DuckDBPyConnection, system: str, month: str, csvs: list[Path],
            source_key: str, out_path: Path) -> dict:
    """Read the month's CSVs as text, validate columns, normalize, write one Parquet file."""
    _, _, city, _ = SYSTEMS[system]
    files = [str(p) for p in csvs]
    for f in files:  # every file must have exactly the expected header
        cols = [r[0] for r in con.execute(
            "DESCRIBE SELECT * FROM read_csv(?, all_varchar = true, header = true)", [f]).fetchall()]
        if cols != EXPECTED_COLUMNS:
            raise ValueError(f"{f}: unexpected columns {cols}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.tmp")
    # Times in the files are local wall-clock time without an offset, with milliseconds.
    # We keep local time (that's what riders experienced: "8am") and round down to whole
    # seconds: the milliseconds are noise for hourly analysis and nearly double the size
    # of the time columns. UTC versions are computed in the trips view, not stored.
    con.execute(f"""
        COPY (
            SELECT
                '{system}'                                    AS system,
                '{city}'                                      AS city,
                ride_id,
                rideable_type,
                date_trunc('second', TRY_CAST(started_at AS TIMESTAMP)) AS started_at,
                date_trunc('second', TRY_CAST(ended_at   AS TIMESTAMP)) AS ended_at,
                NULLIF(trim(start_station_id), '')            AS start_station_id,
                NULLIF(trim(start_station_name), '')          AS start_station_name,
                NULLIF(trim(end_station_id), '')              AS end_station_id,
                NULLIF(trim(end_station_name), '')            AS end_station_name,
                TRY_CAST(start_lat AS DOUBLE)                 AS start_lat,
                TRY_CAST(start_lng AS DOUBLE)                 AS start_lng,
                TRY_CAST(end_lat   AS DOUBLE)                 AS end_lat,
                TRY_CAST(end_lng   AS DOUBLE)                 AS end_lng,
                member_casual,
                '{source_key}'                                AS source_file
            FROM read_csv({files!r}, all_varchar = true, header = true)
            ORDER BY started_at
        ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 1000000)
    """)
    stats = con.execute(f"""
        SELECT count(*) AS n_rows,
               count(*) FILTER (WHERE started_at IS NULL OR ended_at IS NULL) AS n_bad_time,
               count(*) FILTER (WHERE start_station_id IS NULL) AS n_no_start_station,
               count(*) FILTER (WHERE end_station_id IS NULL) AS n_no_end_station,
               min(started_at)::VARCHAR AS min_started, max(started_at)::VARCHAR AS max_started
        FROM read_parquet('{tmp}')
    """).fetchone()
    tmp.replace(out_path)
    return dict(zip(["n_rows", "n_bad_time", "n_no_start_station", "n_no_end_station",
                     "min_started", "max_started"], stats))


MANIFEST_FIELDS = ["system", "month", "source_file", "source_bytes", "source_last_modified",
                   "n_rows", "n_bad_time", "n_no_start_station", "n_no_end_station",
                   "min_started", "max_started", "parquet_bytes", "converted_at_utc"]


def append_manifest(path: Path, row: dict) -> None:
    new = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", nargs="+", choices=list(SYSTEMS), default=list(SYSTEMS))
    ap.add_argument("--latest", type=int, default=12, help="latest N published months per system (default 12)")
    ap.add_argument("--months", nargs="+", help="explicit months YYYY-MM (overrides --latest)")
    ap.add_argument("--data-dir", type=Path, default=repo_root() / "data" / "trips")
    ap.add_argument("--keep-raw", action="store_true", help="keep downloaded zips/csvs")
    ap.add_argument("--threads", type=int, default=None, help="DuckDB threads (default: all cores)")
    ap.add_argument("--memory-limit", default="4GB", help="DuckDB memory limit (default 4GB)")
    args = ap.parse_args(argv)

    data_dir: Path = args.data_dir.resolve()
    raw_dir, pq_dir = data_dir / "_raw", data_dir / "parquet"
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(f"SET temp_directory = '{data_dir / '_duckdb_tmp'}'")
    if args.threads:
        con.execute(f"SET threads = {args.threads}")

    failures = 0
    for system in args.systems:
        bucket = SYSTEMS[system][0]
        available = monthly_files(system)
        months = args.months or sorted(available)[-args.latest:]
        print(f"{system}: {len(available)} monthly files published; taking {months[0]} .. {months[-1]}", flush=True)
        for month in months:
            out_path = pq_dir / f"system={system}" / f"month={month}" / "trips.parquet"
            if out_path.exists():
                print(f"  {month}: already converted, skipping")
                continue
            obj = available.get(month)
            if obj is None:
                print(f"  {month}: not published in {bucket}")
                failures += 1
                continue
            work = raw_dir / system / month
            try:
                t0 = time.time()
                src = work / obj["key"]
                print(f"  {month}: downloading {obj['key']} ({obj['size'] / 1e6:.0f} MB)", flush=True)
                download(f"{bucket}/{obj['key']}", src, obj["size"])
                csvs = extract_csvs(src, work / "extracted")
                stats = convert(con, system, month, csvs, obj["key"], out_path)
                size = out_path.stat().st_size
                append_manifest(data_dir / "manifest.csv", {
                    "system": system, "month": month, "source_file": obj["key"],
                    "source_bytes": obj["size"], "source_last_modified": obj["last_modified"],
                    **stats, "parquet_bytes": size,
                    "converted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                print(f"  {month}: {stats['n_rows']:,} trips from {len(csvs)} csv file(s) -> "
                      f"{size / 1e6:.0f} MB parquet in {time.time() - t0:.0f}s", flush=True)
            except Exception as exc:  # noqa: BLE001 - one bad month must not stop the rest
                failures += 1
                print(f"  {month}: FAILED: {type(exc).__name__}: {exc}", flush=True)
            finally:
                if not args.keep_raw:
                    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(data_dir / "_duckdb_tmp", ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
