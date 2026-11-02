"""Download Census ACS 2024 5-year tract data, tract boundaries and neighborhood boundaries
for Boston, New York City and Chicago, and write one tract table.

Sources (all official, all free):
  ACS 5-year 2020-2024, Census Data API     https://api.census.gov/data/2024/acs/acs5
      (a free key in .env as CENSUS_API_KEY raises the daily limit; it also works without one)
  TIGER/Line 2024 tracts and places          https://www2.census.gov/geo/tiger/TIGER2024/
  Boston neighborhoods (Analyze Boston, "Boston Neighborhood Boundaries Approximated by
      2020 Census Tracts")                    data.boston.gov
  NYC 2020 Neighborhood Tabulation Areas (NYC Open Data, dataset 9nt8-h7nd)
  Chicago community areas (Chicago Data Portal, dataset igwz-8jzy)

What it does:
  1. Pulls the ACS variables below for every tract in the counties that contain each city.
     Before using them it checks each variable's official label, so a wrong code fails loudly
     instead of silently pulling the wrong number.
  2. Downloads the tract shapefile (for land area and an interior point) and the place
     shapefile (the legal city boundary). A tract belongs to a city when its interior point
     (INTPTLAT/INTPTLON, a point the Census Bureau guarantees is inside the tract) falls inside
     the city boundary. NYC is exactly its five counties, so every tract there counts.
  3. Assigns each tract a neighborhood by the same interior-point test against the city's
     official neighborhood polygons.
  4. Writes analysis/results/siting/tracts_acs2024.csv (one row per tract, three cities).

Distance to the nearest station is a separate step (analysis/tract_station_distance.py), so
the Excel workbook can bring it in with XLOOKUP.

Usage (from the repo root):
  python ingest/census_download.py
  python ingest/census_download.py --cities boston
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "data" / "census"
OUT = REPO / "analysis" / "results" / "siting" / "tracts_acs2024.csv"

ACS_YEAR = 2024
ACS_URL = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"
TIGER = "https://www2.census.gov/geo/tiger/TIGER2024"

# city: state FIPS, counties that contain the city, Census place GEOID (legal city boundary)
CITIES = {
    "boston": {"state": "25", "counties": ["025"], "place": "2507000"},  # Suffolk County
    # Bronx, Kings (Brooklyn), New York (Manhattan), Queens, Richmond (Staten Island)
    "nyc": {"state": "36", "counties": ["005", "047", "061", "081", "085"], "place": "3651000"},
    # Cook, plus DuPage for the O'Hare area of Chicago
    "chicago": {"state": "17", "counties": ["031", "043"], "place": "1714000"},
}

NEIGHBORHOODS = {
    "boston": {
        "url": ("https://data.boston.gov/dataset/5997399b-c665-4600-848f-a2a32834f009/resource/"
                "42a271c9-486d-4f9e-adc2-63e4bf47fe3e/download/"
                "boston_neighborhood_boundaries_approximated_by_2020_census_tracts.geojson"),
        "name_field": "neighborhood",
    },
    "nyc": {
        "url": "https://data.cityofnewyork.us/resource/9nt8-h7nd.geojson?$limit=5000",
        "name_field": "ntaname",
    },
    "chicago": {
        "url": "https://data.cityofchicago.org/resource/igwz-8jzy.geojson?$limit=5000",
        "name_field": "community",
    },
}

NYC_BOROUGHS = {"005": "Bronx", "047": "Brooklyn", "061": "Manhattan", "081": "Queens", "085": "Staten Island"}

# ACS variable code -> the end of its official label (checked against the API before use).
ACS_VARS = {
    "B01003_001E": "Estimate!!Total",                               # total population
    "B01001_001E": "Estimate!!Total:",                              # sex by age, total
    "B01001_007E": "Male:!!18 and 19 years",
    "B01001_008E": "Male:!!20 years",
    "B01001_009E": "Male:!!21 years",
    "B01001_010E": "Male:!!22 to 24 years",
    "B01001_011E": "Male:!!25 to 29 years",
    "B01001_012E": "Male:!!30 to 34 years",
    "B01001_031E": "Female:!!18 and 19 years",
    "B01001_032E": "Female:!!20 years",
    "B01001_033E": "Female:!!21 years",
    "B01001_034E": "Female:!!22 to 24 years",
    "B01001_035E": "Female:!!25 to 29 years",
    "B01001_036E": "Female:!!30 to 34 years",
    "B08301_001E": "Estimate!!Total:",                              # workers 16+, means of transport
    "B08301_010E": "Public transportation (excluding taxicab):",
    "B08301_018E": "Bicycle",
    "B08301_019E": "Walked",
    "B08301_021E": "Worked from home",
    "B08201_001E": "Estimate!!Total:",                              # households
    "B08201_002E": "No vehicle available",
    "B19013_001E": "Median household income in the past 12 months (in 2024 inflation-adjusted dollars)",
}

SQ_M_PER_SQ_MI = 2_589_988.110336


def get(url: str, params: dict | None = None, tries: int = 4) -> requests.Response:
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=120)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == tries - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"  retrying in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def download(url: str, dest: Path) -> Path:
    """Download once and keep the file under data/census (git-ignored)."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(get(url).content)
    tmp.replace(dest)
    return dest


def check_acs_labels() -> None:
    """Fail if any variable code does not mean what we think it means."""
    groups = sorted({v.split("_")[0] for v in ACS_VARS})
    bad = []
    for g in groups:
        labels = get(f"{ACS_URL}/groups/{g}.json").json()["variables"]
        for var, expected_end in ACS_VARS.items():
            if var.startswith(g + "_"):
                label = labels.get(var, {}).get("label", "<missing>")
                if not label.endswith(expected_end):
                    bad.append(f"{var}: API label '{label}' does not end with '{expected_end}'")
    if bad:
        sys.exit("ACS variable check failed:\n  " + "\n  ".join(bad))
    print(f"ACS variable labels checked ({len(ACS_VARS)} variables).")


def fetch_acs(city: str, key: str | None) -> pd.DataFrame:
    c = CITIES[city]
    params = {
        "get": "NAME," + ",".join(ACS_VARS),
        "for": "tract:*",
        "in": f"state:{c['state']} county:{','.join(c['counties'])}",
    }
    if key:
        params["key"] = key
    rows = get(ACS_URL, params).json()
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df["geoid"] = df["state"] + df["county"] + df["tract"]
    for v in ACS_VARS:
        df[v] = pd.to_numeric(df[v], errors="coerce")
        # The API marks "no estimate" with large negative codes such as -666666666.
        df.loc[df[v] < 0, v] = np.nan
    return df


def tracts_in_city(city: str) -> gpd.GeoDataFrame:
    c = CITIES[city]
    st = c["state"]
    tz = download(f"{TIGER}/TRACT/tl_{ACS_YEAR}_{st}_tract.zip", RAW / "tiger" / f"tl_{ACS_YEAR}_{st}_tract.zip")
    tr = gpd.read_file(f"zip://{tz}")
    tr = tr[tr["COUNTYFP"].isin(c["counties"])].copy()

    pz = download(f"{TIGER}/PLACE/tl_{ACS_YEAR}_{st}_place.zip", RAW / "tiger" / f"tl_{ACS_YEAR}_{st}_place.zip")
    place = gpd.read_file(f"zip://{pz}")
    place = place[place["GEOID"] == c["place"]].to_crs("EPSG:4326")
    if len(place) != 1:
        sys.exit(f"Place {c['place']} not found in the {st} place file")

    pts = gpd.GeoDataFrame(
        tr[["GEOID", "ALAND", "AWATER", "COUNTYFP"]].copy(),
        geometry=gpd.points_from_xy(tr["INTPTLON"].astype(float), tr["INTPTLAT"].astype(float)),
        crs="EPSG:4326",
    )
    pts["lat"] = tr["INTPTLAT"].astype(float).values
    pts["lon"] = tr["INTPTLON"].astype(float).values
    if city != "nyc":
        pts = pts[pts.within(place.geometry.iloc[0])].copy()
    return pts


def add_neighborhoods(city: str, pts: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    cfg = NEIGHBORHOODS[city]
    path = download(cfg["url"], RAW / "neighborhoods" / f"{city}.geojson")
    nb = gpd.read_file(path).to_crs("EPSG:4326")
    field = cfg["name_field"]
    if field not in nb.columns:
        sys.exit(f"{city} neighborhood file has no '{field}' column; columns are {list(nb.columns)}")
    joined = gpd.sjoin(pts, nb[[field, "geometry"]], how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    pts["neighborhood"] = joined[field].astype("string").str.strip()
    if city == "chicago":
        pts["neighborhood"] = pts["neighborhood"].str.title()
    return pts


def build_city(city: str, key: str | None) -> pd.DataFrame:
    print(f"\n== {city}")
    acs = fetch_acs(city, key)
    pts = add_neighborhoods(city, tracts_in_city(city))
    df = pts.drop(columns="geometry").merge(acs, left_on="GEOID", right_on="geoid", how="left")

    a = df  # short alias for the formulas below
    young = a[[f"B01001_{n:03d}E" for n in (7, 8, 9, 10, 11, 12, 31, 32, 33, 34, 35, 36)]].sum(axis=1, min_count=1)
    commuters = a["B08301_001E"] - a["B08301_021E"]  # workers who travel to work (excludes work from home)
    out = pd.DataFrame({
        "geoid": a["GEOID"],
        "city": city,
        "county_fips": a["COUNTYFP"],
        "borough": a["COUNTYFP"].map(NYC_BOROUGHS) if city == "nyc" else "",
        "neighborhood": a["neighborhood"].fillna("(none)"),
        "tract_name": a["NAME"],
        "lat": a["lat"].round(6),
        "lon": a["lon"].round(6),
        "land_sq_mi": (a["ALAND"] / SQ_M_PER_SQ_MI).round(4),
        "population": a["B01003_001E"],
        "pop_density_sq_mi": (a["B01003_001E"] / (a["ALAND"] / SQ_M_PER_SQ_MI)).round(1),
        "pop_18_34": young,
        "share_18_34": (young / a["B01001_001E"]).round(4),
        "workers_16plus": a["B08301_001E"],
        "commuters": commuters,
        "commute_transit": a["B08301_010E"],
        "commute_bike": a["B08301_018E"],
        "commute_walk": a["B08301_019E"],
        "share_commute_transit_bike_walk": (
            (a["B08301_010E"] + a["B08301_018E"] + a["B08301_019E"]) / commuters).round(4),
        "households": a["B08201_001E"],
        "households_no_vehicle": a["B08201_002E"],
        "share_households_no_vehicle": (a["B08201_002E"] / a["B08201_001E"]).round(4),
        "median_household_income": a["B19013_001E"],
    })
    # Divisions by zero (tracts with no people, workers or households) become blank, not inf.
    out = out.replace([np.inf, -np.inf], np.nan)
    missing_nb = (out["neighborhood"] == "(none)").sum()
    print(f"  {len(out)} tracts, population {int(out['population'].sum()):,}, "
          f"{missing_nb} tracts without a neighborhood match")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cities", nargs="+", choices=list(CITIES), default=list(CITIES))
    args = p.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except ImportError:
        pass
    key = os.environ.get("CENSUS_API_KEY") or None
    print("Census API key:", "found in .env" if key else "not set (fine for a few calls a day)")

    check_acs_labels()
    df = pd.concat([build_city(c, key) for c in args.cities], ignore_index=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists() and set(args.cities) != set(CITIES):
        old = pd.read_csv(OUT, dtype={"geoid": str, "county_fips": str})
        df = pd.concat([old[~old["city"].isin(args.cities)], df], ignore_index=True)
    df.sort_values(["city", "geoid"]).to_csv(OUT, index=False)
    print(f"\nWrote {OUT.relative_to(REPO)} ({len(df)} tracts)")


if __name__ == "__main__":
    main()
