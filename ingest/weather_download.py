"""Download hourly weather for Boston, NYC and Chicago from Open-Meteo (free, no API key).

Two modes, same columns, both in UTC:

  history   Open-Meteo Historical Weather API (archive-api.open-meteo.com), a reanalysis
            dataset (ERA5 and related) that runs about 5 days behind real time. Used to
            train and test the net-flow forecast against trip history.
            -> data/weather/history/city=<city>/weather.parquet

  forecast  Open-Meteo Forecast API (api.open-meteo.com), the next 2 days plus the last
            day. Used by models/rebalance.py for the next-3-hours projection, because at
            run time the future weather is a forecast, not an observation.
            -> data/weather/forecast/city=<city>/fetched=<UTC timestamp>.parquet

One point per city (the coordinates below, roughly downtown). A city is one weather
cell in this project: rain in Brooklyn and rain in the Bronx count as the same rain.
That is a simplification worth saying out loud in an interview.

Usage:
  python ingest/weather_download.py history                      # dates covered by trip Parquet
  python ingest/weather_download.py history --start 2025-10-01 --end 2026-09-30
  python ingest/weather_download.py forecast
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[1]

# city: (latitude, longitude, where)
CITIES = {
    "boston": (42.3601, -71.0589, "Boston City Hall"),
    "nyc": (40.7128, -74.0060, "Lower Manhattan (NYC City Hall)"),
    "chicago": (41.8781, -87.6298, "Chicago Loop"),
}

# Hourly variables (Open-Meteo names). Units: degC, %, mm, cm, %, km/h.
HOURLY_VARS = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "snowfall",
    "cloud_cover",
    "wind_speed_10m",
]

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def get_json(url: str, params: dict, tries: int = 4) -> dict:
    for attempt in range(tries):
        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == tries - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"  request failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def to_frame(doc: dict, city: str) -> pd.DataFrame:
    """Open-Meteo returns {"hourly": {"time": [...], var: [...]}}; times are UTC because we ask for GMT."""
    hourly = doc["hourly"]
    df = pd.DataFrame({v: hourly[v] for v in HOURLY_VARS})
    df.insert(0, "hour_utc", pd.to_datetime(hourly["time"]))  # naive, UTC
    df.insert(0, "city", city)
    return df


def trip_date_range() -> tuple[date, date] | None:
    """First and last local date in the trip Parquet, padded by a day for time zones."""
    files = list((REPO / "data" / "trips" / "parquet").glob("*/*/*.parquet"))
    if not files:
        return None
    import duckdb

    glob = (REPO / "data" / "trips" / "parquet").as_posix() + "/*/*/*.parquet"
    lo, hi = duckdb.sql(
        f"SELECT min(started_at)::DATE, max(started_at)::DATE "
        f"FROM read_parquet('{glob}', hive_partitioning=false)"
    ).fetchone()
    return lo - timedelta(days=1), hi + timedelta(days=1)


def history(start: date, end: date, out_dir: Path) -> None:
    for city, (lat, lon, where) in CITIES.items():
        print(f"{city}: {start}..{end} at {lat},{lon} ({where})")
        doc = get_json(ARCHIVE_URL, {
            "latitude": lat, "longitude": lon,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "hourly": ",".join(HOURLY_VARS), "timezone": "GMT",
        })
        df = to_frame(doc, city)
        missing = df[HOURLY_VARS].isna().any(axis=1).sum()
        dest = out_dir / f"city={city}" / "weather.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(dest, index=False)
        print(f"  {len(df):,} hours, {missing} with a missing value -> {dest.relative_to(REPO)}")


def forecast(out_dir: Path) -> None:
    fetched = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    for city, (lat, lon, _where) in CITIES.items():
        doc = get_json(FORECAST_URL, {
            "latitude": lat, "longitude": lon,
            "hourly": ",".join(HOURLY_VARS), "timezone": "GMT",
            "past_days": 1, "forecast_days": 2,
        })
        df = to_frame(doc, city)
        dest = out_dir / f"city={city}" / f"fetched={fetched}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(dest, index=False)
        print(f"{city}: {len(df)} forecast hours -> {dest.relative_to(REPO)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["history", "forecast"])
    ap.add_argument("--start", type=date.fromisoformat, help="history: first date (default: from trip data)")
    ap.add_argument("--end", type=date.fromisoformat, help="history: last date (default: from trip data)")
    ap.add_argument("--data-dir", type=Path, default=REPO / "data" / "weather")
    args = ap.parse_args(argv)

    if args.mode == "forecast":
        forecast(args.data_dir / "forecast")
        return 0

    start, end = args.start, args.end
    if start is None or end is None:
        rng = trip_date_range()
        if rng is None:
            print("no trip Parquet found; pass --start and --end, or run ingest/trips_download.py first")
            return 1
        start, end = start or rng[0], end or rng[1]
    # The archive lags real time by about 5 days; asking past that returns nulls.
    end = min(end, datetime.now(timezone.utc).date() - timedelta(days=6))
    history(start, end, args.data_dir / "history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
