"""Logic tests for the siting score and tract-to-station distance.

Every number here is a hand-written fixture used only to check arithmetic against answers
worked out by hand. Nothing from this file is reported as a result.

Run:  python -m pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "analysis"))
import siting_score as ss  # noqa: E402
import tract_station_distance as tsd  # noqa: E402


def test_haversine_one_degree_latitude():
    # One degree of latitude is about 111.2 km on a sphere of radius 6,371 km.
    d = tsd.haversine_matrix([42.0], [-71.0], [43.0], [-71.0])
    assert d[0, 0] == pytest.approx(111_195, rel=1e-3)


def test_nearest_station_and_count_within_radius():
    tracts = pd.DataFrame({"geoid": ["A", "B"], "lat": [42.0, 42.1], "lon": [-71.0, -71.0]})
    # Station s1 is ~111 m north of A, s2 ~333 m north of A, s3 sits on B.
    stations = pd.DataFrame({"short_name": ["s1", "s2", "s3"], "name": ["one", "two", "three"],
                             "lat": [42.001, 42.003, 42.1], "lon": [-71.0, -71.0, -71.0]})
    r = tsd.nearest_stations(tracts, stations, radius_m=400).set_index("geoid")
    assert r.loc["A", "nearest_station_code"] == "s1"
    assert r.loc["A", "dist_nearest_station_m"] == pytest.approx(111, abs=1)
    assert r.loc["A", "stations_within_400m"] == 2
    assert r.loc["B", "dist_nearest_station_m"] == 0
    assert r.loc["B", "stations_within_400m"] == 1


def fixture():
    return pd.DataFrame({
        "geoid": ["t1", "t2", "t3", "t4"],
        "population": [1000, 2000, 3000, 100],          # t4 is below min_population
        "pop_density_sq_mi": [10_000, 20_000, 30_000, 99_999],
        "share_commute_transit_bike_walk": [0.2, 0.4, 0.6, 1.0],
        "share_households_no_vehicle": [0.1, 0.1, 0.4, 1.0],
        "share_18_34": [0.3, 0.2, 0.1, 1.0],
        "dist_nearest_station_m": [100, 500, 3000, 50],
        "median_household_income": [50_000, 100_000, 150_000, 1],
    })


def test_score_by_hand():
    w = {"pop_density": 0.2, "commute_transit_bike_walk": 0.2, "no_vehicle": 0.2,
         "age_18_34": 0.2, "station_gap": 0.2, "low_income": 0.0}
    s = ss.score_tracts(fixture(), w, min_population=500, distance_cap_m=1600).set_index("geoid")
    assert not s.loc["t4", "eligible"] and np.isnan(s.loc["t4", "score"])
    # Capped distances 100, 500, 1600 -> gap z = 0, 0.2667, 1.
    assert s.loc["t2", "z_station_gap"] == pytest.approx(400 / 1500)
    # t1: density 0, mode 0, no-car 0, age 1, gap 0 -> 0.2
    assert s.loc["t1", "score"] == pytest.approx(0.2)
    # t2: 0.5, 0.5, 0, 0.5, 0.26667 -> 0.2 * 1.76667
    assert s.loc["t2", "score"] == pytest.approx(0.2 * (1.5 + 400 / 1500))
    # t3: 1, 1, 1, 0, 1 -> 0.8
    assert s.loc["t3", "score"] == pytest.approx(0.8)
    assert list(s.loc[["t3", "t2", "t1"], "rank"]) == [1, 2, 3]
    # Low income is reversed: lowest income -> 1.
    assert s.loc["t1", "z_low_income"] == pytest.approx(1.0)
    assert s.loc["t3", "z_low_income"] == pytest.approx(0.0)


def test_config_weights_sum_to_one():
    cfg = ss.load_config()
    assert sum(cfg["weights"].values()) == pytest.approx(1.0)
