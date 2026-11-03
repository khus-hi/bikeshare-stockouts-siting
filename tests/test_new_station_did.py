"""Logic tests for the new-station difference-in-differences study.

Every station and ride count here is a hand-made fixture used only to check the code
against answers worked out by hand. Nothing from this file is reported as a result.

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
import new_station_did as nd  # noqa: E402

WEEKS = pd.date_range("2024-04-01", periods=80, freq="W-MON")  # Mondays
M_PER_DEG_LAT = 111_195


def station(sid, north_m, east_m=0.0, first="2024-04-01", last="2025-11-30"):
    return {"station_id": sid, "station_name": sid, "lat": 42.0 + north_m / M_PER_DEG_LAT,
            "lng": -71.0 + east_m / (M_PER_DEG_LAT * np.cos(np.radians(42))),
            "first_day": pd.Timestamp(first), "last_day": pd.Timestamp(last)}


@pytest.fixture
def world():
    """One new station N opens 2025-06-02 (week 61). Near (300 m) loses 20 rides/week
    after the opening; controls 3 km away are flat. A seasonal +50 bump hits everyone."""
    cfg = nd.Config(pre_weeks=4, post_weeks=4, n_match=2, n_boot=200)
    open_week = pd.Timestamp("2025-06-02")
    st = pd.DataFrame([
        station("N", 0, first="2025-06-03"),
        station("Near", 300),
        station("C1", 3000), station("C2", -3000), station("C3", 0, 3000),
    ])
    season = np.where(WEEKS >= open_week, 50, 0)
    dep = pd.DataFrame(index=st["station_id"], columns=WEEKS, dtype=float)
    dep.loc["N"] = np.where(WEEKS > open_week, 70, 0)
    dep.loc["Near"] = 100 + season - np.where(WEEKS > open_week, 20, 0)
    dep.loc["C1"] = 100 + season
    dep.loc["C2"] = 102 + season
    dep.loc["C3"] = 60 + season
    return cfg, st, dep, open_week


def test_classify_flags_new_replacement_and_temporary(world):
    cfg, st, dep, _ = world
    st = pd.concat([st, pd.DataFrame([
        # R appears 50 m from Old the day after Old's last ride: an ID change, not new.
        station("Old", 5000, last="2025-07-01"), station("R", 5050, first="2025-07-02"),
        # T has rides in only one week after opening.
        station("T", 9000, first="2025-07-02"),
    ])], ignore_index=True)
    for s in ("Old", "R", "T"):
        dep.loc[s] = 0.0
    dep.loc["R", WEEKS > pd.Timestamp("2025-06-30")] = 30
    dep.loc["T", pd.Timestamp("2025-07-07")] = 5
    new = nd.classify_new_stations(st, dep, cfg).set_index("station_id")
    assert set(new.index) == {"N", "R", "T"}  # everything first seen after the 12-month burn-in
    assert new.loc["N", "status"] == "analysed"
    assert new.loc["R", "status"] == "replacement" and new.loc["R", "replaces_station_id"] == "Old"
    assert new.loc["T", "status"] == "temporary"
    assert new.loc["N", "existing_within_treat_m"] == 1  # only "Near" is within 500 m


def test_did_recovers_the_built_in_effect(world):
    cfg, st, dep, _ = world
    new = nd.classify_new_stations(st, dep, cfg)
    groups = nd.build_groups(st, new, dep, cfg)
    treated = groups[groups["role"] == "treated"]
    assert list(treated["station_id"]) == ["Near"]
    # Matching picks the two controls closest in pre-period level (100 and 102), not C3 (60).
    assert set(groups[groups["role"] == "control"]["station_id"]) == {"C1", "C2"}
    did = nd.group_did(nd.group_series(groups, dep, cfg))
    # Treated: pre 100, post 130 -> +30. Controls: pre 101, post 151 -> +50. DiD = -20.
    assert did["did"].iloc[0] == pytest.approx(-20)
    net, _ = nd.net_effect(did, new, dep, cfg)
    assert net["own_departures_per_week"] == pytest.approx(70)
    assert net["net_change_per_week"] == pytest.approx(50)
    assert net["cannibalised_share"] == pytest.approx(20 / 70, abs=1e-3)


def test_event_study_is_zero_before_and_effect_after(world):
    cfg, st, dep, _ = world
    new = nd.classify_new_stations(st, dep, cfg)
    es = nd.event_study(nd.group_series(nd.build_groups(st, new, dep, cfg), dep, cfg), cfg).set_index("rel_week")
    assert (es.loc[es.index < 0, "estimate"] == 0).all()
    assert (es.loc[es.index > 0, "estimate"] == -20).all()


def test_placebo_shift_finds_nothing(world):
    cfg, st, dep, _ = world
    new = nd.classify_new_stations(st, dep, cfg)
    g = nd.build_groups(st, new, dep, cfg, shift_weeks=20)
    did = nd.group_did(nd.group_series(g, dep, cfg))
    assert did["did"].abs().max() == pytest.approx(0)


def test_seasonally_closed_station_is_not_used(world):
    cfg, st, dep, open_week = world
    dep.loc["Near", WEEKS < open_week] = 0  # closed for the whole pre-period
    new = nd.classify_new_stations(st, dep, cfg)
    assert nd.build_groups(st, new, dep, cfg).empty


def test_cluster_se_matches_statsmodels():
    sm = pytest.importorskip("statsmodels.api")
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"event_id": np.repeat(list("abcdefgh"), 3), "did": rng.normal(size=24)})
    fit = sm.OLS(df["did"], np.ones(len(df))).fit(cov_type="cluster", cov_kwds={"groups": df["event_id"]})
    # statsmodels' CR1 uses G/(G-1) * (N-1)/(N-K); with K=1 the second factor is 1.
    assert nd.cluster_se_mean(df, "did") == pytest.approx(float(fit.bse.iloc[0]), rel=1e-9)


def test_stitch_joins_renumbered_station_into_one_history():
    weeks = WEEKS[:4]
    p = pd.DataFrame({"system": "x", "station_id": ["Old", "Old", "New", "New"], "week": weeks,
                      "departures": [5, 6, 7, 8], "arrivals": [1, 1, 1, 1]})
    st = pd.DataFrame([station("Old", 0, last="2024-04-14"), station("New", 20, first="2024-04-15")])
    new = pd.DataFrame({"station_id": ["New"], "status": ["replacement"], "replaces_station_id": ["Old"]})
    p2, st2 = nd.stitch_replacements(p, st, new)
    assert list(p2["station_id"].unique()) == ["Old"] and list(p2["departures"]) == [5, 6, 7, 8]
    assert list(st2["station_id"]) == ["Old"] and st2["last_day"].iloc[0] == pd.Timestamp("2025-11-30")


def test_public_racks_and_demo_points_are_excluded():
    cfg = nd.Config()
    st = pd.DataFrame({"station_name": ["Public Rack - Webster Ave & Halsted St", "LA Metro Demo 3",
                                        "MTL-LAB-BKN", "Racine/Lake Corral", "chargingstx2",
                                        "Clark St & Lake St", "Testa Ave & 1 St"]})
    kept, excluded = nd.exclude_non_stations(st, cfg)
    assert list(kept["station_name"]) == ["Clark St & Lake St", "Testa Ave & 1 St"]
    assert len(excluded) == 5
