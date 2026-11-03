"""Logic test for the stockout SQL views.

The rows below are a tiny hand-written fixture that exists only to check the SQL
arithmetic (gap exclusion, hour splitting, empty/full rules, lambda and lost rides)
against answers worked out by hand. They are written to a temporary folder, never to
data/, and no number from this file is ever reported as a result.

Run:  python -m pytest tests/
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ingest"))
sys.path.insert(0, str(REPO / "analysis"))
from gbfs_collector import INFO_SCHEMA, POLL_SCHEMA, STATUS_SCHEMA  # noqa: E402
import build_views  # noqa: E402

UTC = timezone.utc


def t(hh: int, mm: int) -> datetime:
    """A time on Tue 2026-11-03 (UTC). 12:00 UTC = 07:00 in Boston (EST, UTC-5)."""
    return datetime(2026, 11, 3, hh, mm, tzinfo=UTC)


def write(table_rows, schema, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(table_rows, schema=schema), path)


def status(ts, sid, bikes, docks, keyframe=False, returning=1):
    return {"system": "bluebikes", "poll_ts": ts, "feed_last_updated": ts, "station_id": sid,
            "num_bikes_available": bikes, "num_ebikes_available": 0, "num_bikes_disabled": 0,
            "num_docks_available": docks, "num_docks_disabled": 0, "is_installed": 1,
            "is_renting": 1, "is_returning": returning, "last_reported": ts, "is_keyframe": keyframe}


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    root = tmp_path_factory.mktemp("fixture")
    g = root / "data" / "gbfs"

    # Successful polls every 5 min 11:50-13:10, except a 25-minute hole 12:20 -> 12:45
    # (one failed poll logged at 12:25), then one pair 13:58 -> 14:03 that crosses an hour.
    poll_times = [t(11, 50) + timedelta(minutes=5 * i) for i in range(17)]
    poll_times = [p for p in poll_times if not (t(12, 20) < p < t(12, 45))] + [t(13, 58), t(14, 3)]
    polls = [{"system": "bluebikes", "poll_ts": p, "ok": True, "error": None, "feed_last_updated": p,
              "n_stations": 2, "n_status_rows_written": 0, "is_keyframe": False, "info_changed": False,
              "fetch_seconds": 1.0} for p in poll_times]
    polls.append({**polls[0], "poll_ts": t(12, 25), "ok": False, "error": "ConnectionError"})
    write(polls, POLL_SCHEMA, g / "polls" / "date=2026-11-03" / "data.parquet")

    rows = [
        status(t(11, 50), "uuid-1", 3, 7, keyframe=True),
        status(t(11, 50), "uuid-2", 12, 0, keyframe=True),   # full all day
        status(t(12, 10), "uuid-1", 0, 10),                  # empty from 12:10
        status(t(13, 0), "uuid-1", 2, 8),                    # refilled at 13:00
        status(t(11, 50), "uuid-3", 0, 0, keyframe=True),   # no usable dock: out of service
    ]
    write(rows, STATUS_SCHEMA, g / "station_status" / "system=bluebikes" / "date=2026-11-03" / "data.parquet")
    info = [{"system": "bluebikes", "poll_ts": t(11, 50), "station_id": sid, "short_name": code,
             "name": name, "lat": 42.36, "lon": -71.06, "capacity": 12, "region_id": None}
            for sid, code, name in [("uuid-1", "A1", "Station One"), ("uuid-2", "B2", "Station Two"),
                                    ("uuid-3", "C3", "Station Three")]]
    write(info, INFO_SCHEMA, g / "station_information" / "system=bluebikes" / "date=2026-11-03" / "data.parquet")

    # Reference trips at A1 in Nov 2025 (same month, one year earlier): Tuesdays Nov 4, 11,
    # 18, 25 are in the station's span (first trip Nov 4, last trip Nov 25), so the
    # 07:00 Tuesday hour occurred 4 times; 4 departures in it -> lambda = 1.0.
    trips_dir = root / "data" / "trips" / "parquet" / "system=bluebikes" / "month=2025-11"
    trips_dir.mkdir(parents=True)
    starts = ["2025-11-04 07:10", "2025-11-04 07:20", "2025-11-11 07:05", "2025-11-11 07:40",
              "2025-11-25 18:00"]
    duckdb.execute(f"""
        COPY (
          SELECT 'bluebikes' AS system, 'boston' AS city, 'r' || i AS ride_id, 'classic_bike' AS rideable_type,
                 s::TIMESTAMP AS started_at, s::TIMESTAMP + INTERVAL 10 MINUTE AS ended_at,
                 'A1' AS start_station_id, 'Station One' AS start_station_name,
                 'B2' AS end_station_id, 'Station Two' AS end_station_name,
                 42.36 AS start_lat, -71.06 AS start_lng, 42.36 AS end_lat, -71.06 AS end_lng,
                 'member' AS member_casual, 'fixture' AS source_file
          FROM (SELECT unnest({starts!r}) AS s, unnest(range({len(starts)})) AS i)
        ) TO '{trips_dir / "trips.parquet"}' (FORMAT parquet)
    """)

    db = root / "data" / "test.duckdb"
    built = build_views.build(root, db)
    assert built == ["GBFS snapshot views", "trip views", "stockout KPI views"]
    c = duckdb.connect(str(db))
    yield c
    c.close()


def hour_row(con, station_code, hh):
    return con.execute("""
        SELECT observed_minutes, minutes_empty, minutes_full, hour_local, est_lost_rides
        FROM lost_rides_station_hour
        WHERE station_code = ? AND hour_utc = ?
    """, [station_code, datetime(2026, 11, 3, hh)]).fetchone()


def test_gap_excluded_and_empty_minutes(con):
    obs, empty, full, local, _ = hour_row(con, "A1", 12)
    # 12:00-12:20 observed (empty from 12:10), 12:20-12:45 hole, 12:45-13:00 observed & empty
    assert obs == 35 and empty == 25 and full == 0
    assert local == datetime(2026, 11, 3, 7)


def test_segment_split_across_hour(con):
    assert hour_row(con, "A1", 13)[:3] == (12, 0, 0)  # 13:00-13:10 plus 13:58-14:00
    assert hour_row(con, "A1", 14)[:3] == (3, 0, 0)   # 14:00-14:03
    assert hour_row(con, "A1", 11)[:3] == (10, 0, 0)  # 11:50-12:00


def test_full_station(con):
    obs, empty, full, _, lost = hour_row(con, "B2", 12)
    assert obs == full == 35 and empty == 0
    assert lost is None  # B2 has no reference-month departures, so no estimate


def test_typical_demand_and_lost_rides(con):
    lam, n, low, high = con.execute("""
        SELECT demand_per_hour, n_hours, demand_low, demand_high FROM typical_demand
        WHERE station_code = 'A1' AND dow = 2 AND hr = 7""").fetchone()
    assert (lam, n) == (1.0, 4)
    assert low == pytest.approx(max(4 - 1.96 * 2, 0) / 4)
    assert high == pytest.approx((4 + 1.96 * 2) / 4)
    assert hour_row(con, "A1", 12)[4] == pytest.approx(1.0 * 25 / 60)
    # B2 had no departures in the reference month, so no estimate is possible
    assert con.execute("SELECT bool_or(has_demand_estimate) FROM lost_rides_station_hour "
                       "WHERE station_code = 'B2'").fetchone()[0] is False


def test_peak_ranking(con):
    rows = con.execute("SELECT peak_window, rank, station_code, stockout_share FROM peak_problem_stations "
                       "ORDER BY peak_window, rank").fetchall()
    # 12:00 and 13:00 UTC are 7am and 8am local on a Tuesday (AM window: 35 + 12 = 47
    # observed minutes); 14:00 UTC is 9am local, outside the window.
    assert rows == [("weekday 7-9am", 1, "B2", 1.0), ("weekday 7-9am", 2, "A1", round(25 / 47, 4)),
                    ("weekday 7-9am", 3, "C3", 0.0)]


def test_zero_bikes_zero_docks_is_out_of_service(con):
    # 0 bikes and 0 docks: neither empty nor full (it used to count as both -> 200%)
    obs, empty, full, _, _ = hour_row(con, "C3", 12)
    assert obs == 35 and empty == 0 and full == 0
    oos = con.execute("SELECT minutes_out_of_service FROM station_hour_stockouts "
                      "WHERE station_code = 'C3' AND hour_utc = ?", [datetime(2026, 11, 3, 12)]).fetchone()[0]
    assert oos == 35


def test_trip_hourly_counts(con):
    dep, arr = con.execute("""
        SELECT sum(departures) FILTER (WHERE station_code = 'A1'),
               sum(arrivals)   FILTER (WHERE station_code = 'B2')
        FROM station_hour_trips""").fetchone()
    assert (dep, arr) == (5, 5)
