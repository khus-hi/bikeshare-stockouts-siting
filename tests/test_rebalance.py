"""Logic tests for the net-flow forecast helpers and the rebalancing projection.

Every number here is a hand-written fixture used only to check arithmetic against answers
worked out by hand. Fixtures go to a temporary folder, never to data/, and nothing from
this file is reported as a result.

Run:  python -m pytest tests/
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ingest"))
sys.path.insert(0, str(REPO / "models"))
from gbfs_collector import INFO_SCHEMA, POLL_SCHEMA, STATUS_SCHEMA  # noqa: E402
import netflow_common as nc  # noqa: E402
import rebalance as rb  # noqa: E402

UTC = timezone.utc


# plan_station -------------------------------------------------------------------------

def test_projected_empty_adds_shortfall_plus_buffer():
    # 4 bikes, 6 docks; losing 3, 3, 1 bikes over three whole hours -> low point 4-7 = -3.
    p = rb.plan_station(4, 6, [3, 3, 1], [1, 1, 1], buffer=1.2)
    assert p["status"] == "projected_empty" and p["action"] == "add"
    assert p["shortfall"] == 3 and p["projected_min_bikes"] == -3
    assert p["bikes_to_move"] == 5          # ceil(3 + 1.2)
    assert p["first_hit_hour_index"] == 1   # 4 -> 1 -> -2


def test_projected_full_removes_and_is_capped_by_bikes_there():
    # 2 bikes, 1 dock (3 slots); gaining 4 bikes -> high point 6 = 3 over.
    p = rb.plan_station(2, 1, [-4], [1], buffer=0.5)
    assert p["status"] == "projected_full" and p["action"] == "remove"
    assert p["shortfall"] == 3
    assert p["bikes_to_move"] == 2          # wants ceil(3.5) = 4, only 2 bikes to take


def test_partial_hours_are_weighted():
    # At 10:40 the horizon has 1/3 of the current hour, two whole hours, 2/3 of the last.
    hours, weights = rb.horizon(pd.Timestamp("2026-11-10 10:40"))
    assert hours[0] == pd.Timestamp("2026-11-10 10:00") and len(hours) == 4
    assert weights == pytest.approx([1 / 3, 1, 1, 2 / 3])
    # 1 bike; losing 3 per hour in hour 0 only counts 1 -> reaches 0 exactly: empty.
    p = rb.plan_station(1, 9, [3, 0, 0, 0], weights, buffer=0)
    assert p["status"] == "projected_empty" and p["shortfall"] == 0 and p["bikes_to_move"] == 0


def test_on_the_hour_uses_three_whole_hours():
    hours, weights = rb.horizon(pd.Timestamp("2026-11-10 10:00"))
    assert len(hours) == 3 and weights == [1, 1, 1]


def test_near_empty_within_buffer_and_ok():
    near = rb.plan_station(5, 5, [2, 2], [1, 1], buffer=2)   # low point 1, buffer 2 -> add 1
    assert near["status"] == "near_empty" and near["bikes_to_move"] == 1
    ok = rb.plan_station(5, 5, [1, -1], [1, 1], buffer=1)
    assert ok["status"] == "ok" and ok["bikes_to_move"] == 0


def test_already_empty_station():
    p = rb.plan_station(0, 10, [0.5, 0.5, 0.5], [1, 1, 1], buffer=1)
    assert p["status"] == "projected_empty" and p["first_hit_hour_index"] == 0
    assert p["bikes_to_move"] == 3          # ceil(1.5 + 1)


# metrics ------------------------------------------------------------------------------

def test_mape_skips_zero_hours_and_wape_does_not():
    actual, pred = np.array([0, 1, 4]), np.array([1, 2, 2])
    assert nc.mape(actual, pred) == pytest.approx(100 * (1 / 1 + 2 / 4) / 2)   # 75
    assert nc.wape(actual, pred) == pytest.approx(100 * 4 / 5)                  # 80


def test_net_mape_uses_absolute_actual():
    actual, pred = np.array([-2, 0, 4]), np.array([-1, 3, 2])
    assert nc.net_mape(actual, pred) == pytest.approx(100 * (1 / 2 + 2 / 4) / 2)
    assert nc.wape(actual, pred) == pytest.approx(100 * 6 / 6)


# calendar: Python (live) and SQL (training) must agree ---------------------------------

def test_calendar_python_matches_sql():
    from pandas.tseries.holiday import USFederalHolidayCalendar

    hours = pd.DataFrame({"hour_utc": pd.to_datetime([
        "2026-11-01 05:00", "2026-11-01 06:00", "2026-11-01 07:00",   # DST ends 06:00 UTC in NYC
        "2026-11-11 15:00",                                           # Veterans Day
        "2026-11-15 04:30",                                           # Sat 23:30 local
    ])})
    for tz in ["America/New_York", "America/Chicago"]:
        py = nc.add_calendar(hours.copy(), tz)
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        con.register("h", hours)
        hol = USFederalHolidayCalendar().holidays("2026-01-01", "2026-12-31")
        con.register("holidays", pd.DataFrame({"d": hol.date}))
        sql = con.execute(f"""
            SELECT hour(timezone('{tz}', timezone('UTC', hour_utc)))::INT AS hour_local,
                   (isodow(timezone('{tz}', timezone('UTC', hour_utc))) - 1)::INT AS dow_local,
                   (isodow(timezone('{tz}', timezone('UTC', hour_utc))) >= 6)::INT AS is_weekend,
                   (CAST(timezone('{tz}', timezone('UTC', hour_utc)) AS DATE) IN (SELECT d FROM holidays))::INT AS is_holiday
            FROM h""").df()
        for c in nc.CALENDAR_FEATURES:
            assert py[c].astype(int).tolist() == sql[c].tolist(), (tz, c)
    ny = nc.add_calendar(hours.copy(), "America/New_York")
    assert ny["hour_local"].tolist() == [1, 1, 2, 10, 23]
    assert ny["is_holiday"].tolist() == [0, 0, 0, 1, 0]
    assert ny["is_weekend"].tolist() == [1, 1, 1, 0, 1]


# GBFS demand proxy ----------------------------------------------------------------------

def t(hh: int, mm: int) -> datetime:
    return datetime(2026, 11, 3, hh, mm, tzinfo=UTC)


def write(rows, schema, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def status(ts, bikes):
    return {"system": "bluebikes", "poll_ts": ts, "feed_last_updated": ts, "station_id": "uuid-1",
            "num_bikes_available": bikes, "num_ebikes_available": 0, "num_bikes_disabled": 0,
            "num_docks_available": 10 - bikes, "num_docks_disabled": 0, "is_installed": 1,
            "is_renting": 1, "is_returning": 1, "last_reported": ts, "is_keyframe": False}


@pytest.fixture()
def gbfs_con(tmp_path):
    g = tmp_path / "data" / "gbfs"
    # Polls every 5 min 10:00-12:55, except a 30-minute hole 11:10 -> 11:40.
    polls = [t(10, 0) + timedelta(minutes=5 * i) for i in range(36)]
    polls = [p for p in polls if not (t(11, 10) < p < t(11, 40))]
    write([{"system": "bluebikes", "poll_ts": p, "ok": True, "error": None, "feed_last_updated": p,
            "n_stations": 1, "n_status_rows_written": 0, "is_keyframe": False, "info_changed": False,
            "fetch_seconds": 1.0} for p in polls], POLL_SCHEMA, g / "polls" / "date=2026-11-03" / "p.parquet")
    rows = [status(t(10, 0), 5),
            status(t(10, 15), 3),    # 2 departures in hour 10
            status(t(10, 40), 4),    # 1 arrival in hour 10
            status(t(11, 40), 9),    # change seen right after the hole: dropped
            status(t(12, 5), 6),     # 3 departures in hour 12
            status(t(12, 30), 8)]    # 2 arrivals in hour 12
    write(rows, STATUS_SCHEMA, g / "station_status" / "system=bluebikes" / "date=2026-11-03" / "s.parquet")
    write([{"system": "bluebikes", "poll_ts": t(10, 0), "station_id": "uuid-1", "short_name": "A1",
            "name": "Station One", "lat": 42.36, "lon": -71.06, "capacity": 10, "region_id": None}],
          INFO_SCHEMA, g / "station_information" / "system=bluebikes" / "date=2026-11-03" / "i.parquet")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield rb.gbfs_connection()
    finally:
        os.chdir(cwd)


def test_gbfs_proxy_counts_and_coverage(gbfs_con):
    df = rb.gbfs_demand_proxy(gbfs_con, "bluebikes", pd.Timestamp("2026-11-03 12:55"))
    got = {r.hour_utc.hour: (r.departures, r.arrivals) for r in df.itertuples()}
    # Hour 10: fully observed (polls 10:00-10:55 plus 10:55->11:00 counted in hour 11).
    # Hour 11: only 10:55-11:10 observed -> below 90% coverage -> not used.
    # Hour 12: 12:00-12:55 observed = 55 of 60 minutes (92%) -> used.
    assert got == {10: (2, 1), 12: (3, 2)}


def test_current_state_and_as_of(gbfs_con):
    now, st = rb.current_state(gbfs_con, "bluebikes", pd.Timestamp("2026-11-03 12:10"))
    assert now == pd.Timestamp("2026-11-03 12:10")
    assert st.loc[0, "station_code"] == "A1" and st.loc[0, "bikes_now"] == 6 and st.loc[0, "docks_now"] == 4


# live lag features -----------------------------------------------------------------------

def test_live_lags_prefer_gbfs_then_profile():
    hours = [pd.Timestamp("2026-11-24 10:00")]
    stations = pd.DataFrame({"station_code": ["A1"]})
    # Profile: 1 departure / 2 arrivals in every hour-of-week.
    prof = pd.MultiIndex.from_product([["A1"], range(7), range(24)],
                                      names=["station_code", "dow_local", "hour_local"]).to_frame(index=False)
    prof["departures"], prof["arrivals"] = 1.0, 2.0
    # GBFS observed only the hour exactly one week earlier: 7 departures, 0 arrivals.
    proxy = pd.DataFrame({"station_code": ["A1"], "hour_utc": [pd.Timestamp("2026-11-17 10:00")],
                          "departures": [7.0], "arrivals": [0.0]})
    f, share = rb.live_features(stations, hours, proxy, prof, "America/New_York")
    r = f.iloc[0]
    assert r.departures_lag168 == 7 and r.arrivals_lag168 == 0      # from GBFS
    assert r.departures_lag24 == 1 and r.departures_lag336 == 1      # from profile
    assert r.departures_how3w == pytest.approx((7 + 1 + 1) / 3)
    assert r.departures_prev24 == 24 and r.arrivals_prev24 == 48
    assert 0 < share < 0.01
    assert set(nc.feature_columns(False)) - {"lat", "lng"} <= set(f.columns)
