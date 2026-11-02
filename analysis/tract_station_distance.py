"""Distance from each Census tract to the nearest bike-share station.

Inputs
  analysis/results/siting/tracts_acs2024.csv      from ingest/census_download.py
  Station locations from GBFS station_information. By default the latest snapshot the
  collector saved under data/gbfs/station_information/ (so the station list matches the
  stockout data). If there is none, it fetches station_information live (--live forces this).

Output
  analysis/results/siting/tract_station_distance.csv
    geoid, city, nearest_station_code, nearest_station_name, dist_nearest_station_m,
    stations_within_400m, stations_snapshot_utc

How distance is measured
  From the tract's interior point (a Census point inside the tract, usually near its middle)
  to each station, as a straight line on the globe (haversine formula). Walking distance
  along streets is longer; straight-line distance is the usual first pass and is stated as
  such. 400 m is about a 5-minute walk, the common planning rule for station spacing.
  Every station of the city's system counts, including stations just outside the city line
  (a Boston tract next to Cambridge is served by Cambridge stations).

Usage (from the repo root):
  python analysis/tract_station_distance.py
  python analysis/tract_station_distance.py --live
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ingest"))
from gbfs_collector import SYSTEMS as GBFS_ROOTS, default_data_dir, info_rows, parse_feed_list  # noqa: E402

TRACTS = REPO / "analysis" / "results" / "siting" / "tracts_acs2024.csv"
OUT = REPO / "analysis" / "results" / "siting" / "tract_station_distance.csv"
CITY_SYSTEM = {"boston": "bluebikes", "nyc": "citibike", "chicago": "divvy"}
WALK_RADIUS_M = 400
EARTH_RADIUS_M = 6_371_008.8


def haversine_matrix(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distance in metres between every point in set 1 and every point in set 2."""
    la1, lo1 = np.radians(np.asarray(lat1))[:, None], np.radians(np.asarray(lon1))[:, None]
    la2, lo2 = np.radians(np.asarray(lat2))[None, :], np.radians(np.asarray(lon2))[None, :]
    h = np.sin((la2 - la1) / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def nearest_stations(tracts: pd.DataFrame, stations: pd.DataFrame, radius_m: float = WALK_RADIUS_M) -> pd.DataFrame:
    d = haversine_matrix(tracts["lat"], tracts["lon"], stations["lat"], stations["lon"])
    idx = d.argmin(axis=1)
    return pd.DataFrame({
        "geoid": tracts["geoid"].values,
        "nearest_station_code": stations["short_name"].values[idx],
        "nearest_station_name": stations["name"].values[idx],
        "dist_nearest_station_m": d[np.arange(len(idx)), idx].round(0).astype(int),
        f"stations_within_{int(radius_m)}m": (d <= radius_m).sum(axis=1),
    })


def stations_from_collector(system: str) -> pd.DataFrame | None:
    base = default_data_dir() / "station_information" / f"system={system}"
    files = sorted(base.glob("date=*/*.parquet"))
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    latest = df["poll_ts"].max()
    return df[df["poll_ts"] == latest][["short_name", "name", "lat", "lon"]].assign(snapshot_utc=latest)


def stations_live(system: str) -> pd.DataFrame:
    """Fetch station_information now, using the collector's own feed discovery and parsing."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    feeds = parse_feed_list(requests.get(GBFS_ROOTS[system], timeout=30).json())
    doc = requests.get(feeds["station_information"], timeout=30).json()
    df = pd.DataFrame(info_rows(system, now, doc))
    return df[["short_name", "name", "lat", "lon"]].assign(snapshot_utc=now)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--live", action="store_true", help="fetch station_information live instead of the collector's copy")
    args = p.parse_args()

    tracts = pd.read_csv(TRACTS, dtype={"geoid": str})
    out = []
    for city, system in CITY_SYSTEM.items():
        t = tracts[tracts["city"] == city]
        if t.empty:
            continue
        st = None if args.live else stations_from_collector(system)
        source = "collector snapshot"
        if st is None:
            st, source = stations_live(system), "live feed"
        st = st.dropna(subset=["lat", "lon"])
        st = st[(st["lat"] != 0) & (st["lon"] != 0)]
        res = nearest_stations(t, st)
        res.insert(1, "city", city)
        res["stations_snapshot_utc"] = str(st["snapshot_utc"].iloc[0])
        out.append(res)
        print(f"{city}: {len(st)} {system} stations ({source}, {st['snapshot_utc'].iloc[0]}), "
              f"median tract distance {res['dist_nearest_station_m'].median():.0f} m")
    pd.concat(out, ignore_index=True).to_csv(OUT, index=False)
    print(f"Wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
