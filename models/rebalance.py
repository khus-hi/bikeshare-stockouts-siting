"""Rebalancing priority list: which stations will run out of bikes (empty) or docks
(full) in the next 3 hours, and how many bikes to move.

Runs on the machine that collects GBFS snapshots (Khushi's Mac), because it needs the
current bike and dock counts. It needs:
  data/gbfs/...                   from ingest/gbfs_collector.py (current state + recent history)
  data/models/netflow/*.txt       from models/train_netflow.py
  data/models/netflow/*_profile.parquet   from models/train_netflow.py (fallback demand profile)
  internet access to Open-Meteo   for the weather forecast (no key)

The logic, in plain English:
  1. Start from what each station has right now: bikes available and empty docks.
  2. Forecast departures and arrivals for each of the next 3 hours with the trained
     LightGBM models. Net outflow = departures - arrivals.
  3. Walk forward hour by hour: bikes(t) = bikes now - cumulative net outflow. The
     current hour only counts for the part that is left (at 10:40 that is 20 minutes),
     and the hour 3 hours ahead only for the part up to now + 3 h.
  4. If the projected bikes drop to 0 or below, the station is projected to go EMPTY.
     If they rise to the number of slots (bikes now + empty docks), it is projected FULL.
  5. Bikes to move = how far past the limit the projection goes, plus a safety buffer.
     The buffer is the model's measured mean absolute error on 3-hour net flow in the
     test period (data/models/netflow/buffers.json), so the move covers a typical miss.
     A move can never exceed the free docks (when adding) or the bikes there (when removing).
     Stations that stay inside the limits but come within the buffer are "near empty" /
     "near full" and are listed after the projected ones.
  6. Rank: projected stockouts first, by shortfall (how many riders the forecast says
     would find no bike or no dock), then by how soon it happens.

Where the lag features come from at run time. Trip history is published once a month,
so last week's trips are not available yet. Recent demand is estimated from the GBFS
snapshots instead: a drop in bikes between two polls counts as departures, a rise as
arrivals. This undercounts at busy stations (a departure and an arrival within the same
5 minutes cancel out) and counts truck rebalancing as rides. Hours the collector did
not observe (or from before collection started) fall back to the station's average for
that hour of the week over the last 4 weeks of published trip history. The output says
which share of lag values came from each source.

Usage (from the repo root):
  python models/rebalance.py                        # all systems, now
  python models/rebalance.py --systems bluebikes --top 20
  python models/rebalance.py --as-of "2026-11-10 13:00"   # replay a past moment (UTC)

Output: data/rebalancing/priority_<UTC time>.csv, and the top stations per system printed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingest"))
from netflow_common import (  # noqa: E402
    LAG_HOURS, MODELS_DIR, REPO, SYSTEM_CITY, SYSTEM_TZ, TARGETS,
    WEATHER_FEATURES, add_calendar,
)

GBFS_SQL = REPO / "ingest" / "sql" / "duckdb_views.sql"
OUT_DIR = REPO / "data" / "rebalancing"
HORIZON_H = 3
MIN_HOUR_COVERAGE = 0.9   # a GBFS hour is used as demand history only if polls covered 90% of it
MAX_POLL_GAP_MIN = 15     # same gap rule as the stockout KPIs (docs/requirements.md)


# 1. Projection logic (pure function, unit-tested in tests/test_rebalance.py) ------------

def plan_station(bikes_now: float, docks_now: float, net_out: list[float], weights: list[float],
                 buffer: float) -> dict:
    """Project bikes over the horizon and decide what to move.

    net_out[i]  forecast departures - arrivals for hour i (full-hour amounts)
    weights[i]  share of hour i inside the horizon (first and last hours are partial)
    buffer      safety margin in bikes
    """
    slots = bikes_now + docks_now
    path, cum = [], 0.0
    for n, w in zip(net_out, weights):
        cum += n * w
        path.append(bikes_now - cum)
    lo, hi = min(path + [bikes_now]), max(path + [bikes_now])

    empty_short = max(0.0, -lo)           # riders who would find no bike
    full_short = max(0.0, hi - slots)     # riders who would find no dock
    hits_empty = lo <= 0
    hits_full = hi >= slots
    first_empty = (0 if bikes_now <= 0 else next(i for i, b in enumerate(path) if b <= 0)) if hits_empty else None
    first_full = (0 if docks_now <= 0 else next(i for i, b in enumerate(path) if b >= slots)) if hits_full else None

    # The side that goes further past its limit decides the action; if both sides are hit
    # (a big swing out then in), moving bikes cannot fix both, so the larger shortfall wins.
    need_add = -lo + buffer               # bikes to add so the low point stays >= buffer
    need_remove = hi - slots + buffer     # bikes to take so the high point stays <= slots - buffer
    if hits_empty and (not hits_full or empty_short >= full_short):
        status, action, move, first = "projected_empty", "add", need_add, first_empty
    elif hits_full:
        status, action, move, first = "projected_full", "remove", need_remove, first_full
    elif need_add > 0 and need_add >= need_remove:
        status, action, move, first = "near_empty", "add", need_add, None
    elif need_remove > 0:
        status, action, move, first = "near_full", "remove", need_remove, None
    else:
        status, action, move, first = "ok", "none", 0.0, None

    cap = docks_now if action == "add" else bikes_now if action == "remove" else 0
    move = int(min(math.ceil(max(move, 0.0) - 1e-9), cap))
    return {
        "status": status, "action": action, "bikes_to_move": move,
        "shortfall": round(empty_short if action == "add" else full_short if action == "remove" else 0.0, 2),
        "projected_min_bikes": round(lo, 2), "projected_max_bikes": round(hi, 2),
        "first_hit_hour_index": first,
    }


def horizon(now_utc: pd.Timestamp) -> tuple[list[pd.Timestamp], list[float]]:
    """The hours touched by [now, now + 3 h) and the share of each one inside it."""
    h0 = now_utc.floor("h")
    w0 = 1 - (now_utc - h0) / pd.Timedelta(hours=1)
    hours = [h0 + pd.Timedelta(hours=i) for i in range(HORIZON_H + 1)]
    weights = [w0] + [1.0] * (HORIZON_H - 1) + [1 - w0]
    if weights[-1] <= 1e-9:  # exactly on the hour: three whole hours
        hours, weights = hours[:-1], weights[:-1]
    return hours, weights


# 2. Data at run time --------------------------------------------------------------------

def gbfs_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(GBFS_SQL.read_text())
    return con


def current_state(con, system: str, as_of: pd.Timestamp | None) -> tuple[pd.Timestamp, pd.DataFrame]:
    as_of_sql = "TIMESTAMP '9999-01-01'" if as_of is None else f"TIMESTAMP '{as_of:%Y-%m-%d %H:%M:%S}'"
    now = con.execute(f"""
        SELECT max(poll_ts AT TIME ZONE 'UTC') FROM polls
        WHERE system = ? AND ok AND poll_ts AT TIME ZONE 'UTC' <= {as_of_sql}""", [system]).fetchone()[0]
    if now is None:
        raise SystemExit(f"{system}: no successful GBFS poll found under data/gbfs/")
    now = pd.Timestamp(now)
    # Latest saved row per station on the same UTC day (each day starts with a keyframe of
    # every station, so a station missing today has left the feed).
    df = con.execute("""
        WITH s AS (
            SELECT *, poll_ts AT TIME ZONE 'UTC' AS t FROM station_status_changes
            WHERE system = $system
              AND poll_ts AT TIME ZONE 'UTC' <= $now
              AND poll_ts AT TIME ZONE 'UTC' >= date_trunc('day', $now::TIMESTAMP)
            QUALIFY row_number() OVER (PARTITION BY station_id ORDER BY poll_ts DESC) = 1
        ), info AS (
            SELECT * FROM station_information_snapshots
            WHERE system = $system AND poll_ts AT TIME ZONE 'UTC' <= $now
            QUALIFY row_number() OVER (PARTITION BY station_id ORDER BY poll_ts DESC) = 1
        )
        SELECT s.station_id, i.short_name AS station_code, i.name, i.lat, i.lon, i.capacity,
               s.num_bikes_available AS bikes_now, s.num_docks_available AS docks_now,
               s.t AS status_utc
        FROM s JOIN info i USING (station_id)
        WHERE s.is_installed = 1 AND s.is_renting = 1 AND s.is_returning = 1
    """, {"system": system, "now": now.to_pydatetime()}).df()
    return now, df


def gbfs_demand_proxy(con, system: str, now: pd.Timestamp) -> pd.DataFrame:
    """Hourly departures/arrivals estimated from bike-count changes between polls."""
    start = now.floor("h") - pd.Timedelta(hours=max(LAG_HOURS.values()) + 48)
    return con.execute(f"""
        WITH sys_polls AS (
            SELECT poll_ts AT TIME ZONE 'UTC' AS t,
                   lag(poll_ts AT TIME ZONE 'UTC') OVER (ORDER BY poll_ts) AS t_prev
            FROM polls WHERE system = $system AND ok
        ), seg AS (   -- observed time per hour (polls at most {MAX_POLL_GAP_MIN} min apart)
            SELECT date_trunc('hour', t) AS hour_utc,
                   sum(date_diff('second', t_prev, t)) / 3600.0 AS coverage
            FROM sys_polls
            WHERE t_prev IS NOT NULL AND t - t_prev <= INTERVAL {MAX_POLL_GAP_MIN} MINUTE
              AND t >= $start AND t <= $now
            GROUP BY 1
        ), rows AS (
            SELECT c.station_id, c.poll_ts AT TIME ZONE 'UTC' AS t, c.num_bikes_available AS b,
                   lag(c.num_bikes_available) OVER (PARTITION BY c.station_id ORDER BY c.poll_ts) AS b_prev
            FROM station_status_changes c
            WHERE c.system = $system AND c.poll_ts AT TIME ZONE 'UTC' BETWEEN $start AND $now
        ), deltas AS (   -- a change seen right after a long gap could have happened in the gap: drop it
            SELECT r.station_id, date_trunc('hour', r.t) AS hour_utc, r.b - r.b_prev AS d
            FROM rows r JOIN sys_polls p ON p.t = r.t
            WHERE r.b_prev IS NOT NULL AND p.t - p.t_prev <= INTERVAL {MAX_POLL_GAP_MIN} MINUTE
        )
        SELECT i.short_name AS station_code, seg.hour_utc,
               coalesce(sum(greatest(-d, 0)), 0) AS departures,
               coalesce(sum(greatest(d, 0)), 0)  AS arrivals
        FROM seg
        CROSS JOIN (SELECT DISTINCT station_id FROM rows) st
        LEFT JOIN deltas USING (station_id, hour_utc)
        JOIN (SELECT station_id, short_name FROM stations WHERE system = $system) i USING (station_id)
        WHERE seg.coverage >= {MIN_HOUR_COVERAGE}
        GROUP BY ALL
    """, {"system": system, "start": start.to_pydatetime(), "now": now.to_pydatetime()}).df()


def trip_profile(system: str) -> pd.DataFrame:
    """Average departures/arrivals per station x local hour-of-week, last 4 weeks of trip
    history (written by models/train_netflow.py next to the models)."""
    return pd.read_parquet(MODELS_DIR / f"{system}_profile.parquet")


def weather_forecast(city: str, hours: list[pd.Timestamp]) -> pd.DataFrame:
    import weather_download

    folder = weather_download.REPO / "data" / "weather" / "forecast" / f"city={city}"
    files = sorted(folder.glob("fetched=*.parquet"))
    fresh = files and (pd.Timestamp.now("UTC").tz_localize(None)
                       - pd.Timestamp(files[-1].stem.split("=")[1].rstrip("Z"))) < pd.Timedelta(hours=1)
    if not fresh:
        weather_download.forecast(folder.parent)
        files = sorted(folder.glob("fetched=*.parquet"))
    w = pd.read_parquet(files[-1])
    w = w[w["hour_utc"].isin(hours)]
    if len(w) < len(hours):
        raise SystemExit(f"{city}: weather forecast {files[-1].name} does not cover {hours[0]}..{hours[-1]}")
    return w[["hour_utc"] + WEATHER_FEATURES]


def live_features(stations: pd.DataFrame, hours: list[pd.Timestamp], proxy: pd.DataFrame,
                  profile: pd.DataFrame, tz: str) -> tuple[pd.DataFrame, float]:
    """Feature rows (station x forecast hour), with lags taken from GBFS where observed,
    else from the trip-history profile. Returns the rows and the share of lag values from GBFS."""
    codes = stations["station_code"].dropna().unique()
    first = hours[0] - pd.Timedelta(hours=max(LAG_HOURS.values()))
    last = hours[-1] - pd.Timedelta(hours=24)
    hist_hours = pd.date_range(first, last, freq="h")
    grid = pd.MultiIndex.from_product([codes, hist_hours], names=["station_code", "hour_utc"]).to_frame(index=False)
    grid = add_calendar(grid, tz)
    grid = grid.merge(profile, on=["station_code", "dow_local", "hour_local"], how="left")
    grid = grid.merge(proxy.rename(columns={t: f"{t}_gbfs" for t in TARGETS}),
                      on=["station_code", "hour_utc"], how="left")
    from_gbfs = grid["departures_gbfs"].notna()
    for t in TARGETS:
        grid[t] = grid[f"{t}_gbfs"].where(from_gbfs, grid[t])
    gbfs_share = float(from_gbfs.mean()) if len(grid) else 0.0

    # Wide arrays [station, hour] so each lag is a column offset.
    idx = {h: i for i, h in enumerate(hist_hours)}
    wide = {t: grid.pivot(index="station_code", columns="hour_utc", values=t).reindex(index=codes, columns=hist_hours)
            .to_numpy(float) for t in TARGETS}
    rows = []
    for h in hours:
        r = pd.DataFrame({"station_code": codes, "hour_utc": h})
        for t in TARGETS:
            a = wide[t]
            for name, k in LAG_HOURS.items():
                r[f"{t}_{name}"] = a[:, idx[h - pd.Timedelta(hours=k)]]
            r[f"{t}_how3w"] = (r[f"{t}_lag168"] + r[f"{t}_lag336"] + r[f"{t}_lag504"]) / 3
            j = idx[h - pd.Timedelta(hours=24)]
            r[f"{t}_prev24"] = a[:, j - 23: j + 1].sum(axis=1)  # hours H-47 .. H-24, as in training
        rows.append(r)
    feats = pd.concat(rows, ignore_index=True)
    feats = add_calendar(feats, tz)
    return feats, gbfs_share


# 3. Put it together ---------------------------------------------------------------------

def run_system(con, system: str, as_of, buffers: dict) -> pd.DataFrame:
    tz, city = SYSTEM_TZ[system], SYSTEM_CITY[system]
    now, st = current_state(con, system, as_of)
    st = st[st["station_code"].notna()].drop_duplicates("station_code")
    hours, weights = horizon(now)
    models = {t: lgb.Booster(model_file=str(MODELS_DIR / f"{system}_{t}.txt")) for t in TARGETS}
    feat_names = models["departures"].feature_name()

    feats, gbfs_share = live_features(st, hours, gbfs_demand_proxy(con, system, now), trip_profile(system), tz)
    feats = feats.merge(st[["station_code", "lat", "lon"]].drop_duplicates("station_code")
                        .rename(columns={"lon": "lng"}), on="station_code")
    if any(f in feat_names for f in WEATHER_FEATURES):
        feats = feats.merge(weather_forecast(city, hours), on="hour_utc", how="left")
    # Stations with no history at all (new codes, not yet in GBFS lags or trip profile) are skipped.
    has_hist = feats[[f"{t}_lag168" for t in TARGETS]].notna().all(axis=1)
    skipped = feats.loc[~has_hist, "station_code"].nunique()
    feats = feats[has_hist].copy()
    for t in TARGETS:
        feats[t] = np.clip(models[t].predict(feats[feat_names]), 0, None)
    feats["net_out"] = feats["departures"] - feats["arrivals"]
    wide = feats.pivot(index="station_code", columns="hour_utc", values="net_out").reindex(columns=hours)
    dep3 = feats.assign(w=feats["hour_utc"].map(dict(zip(hours, weights))))
    sums = dep3.assign(d=dep3.departures * dep3.w, a=dep3.arrivals * dep3.w).groupby("station_code")[["d", "a"]].sum()

    buf = buffers.get(system, {}).get("net3h_mae", 0.0)
    out = []
    for _, s in st[st["station_code"].isin(wide.index)].iterrows():
        p = plan_station(float(s.bikes_now), float(s.docks_now), wide.loc[s.station_code].tolist(), weights, buf)
        first = p.pop("first_hit_hour_index")
        hit_utc = hit_local = None
        if first is not None:  # end of the hour in which the limit is crossed
            hit_utc = min(hours[first] + pd.Timedelta(hours=1), now + pd.Timedelta(hours=HORIZON_H))
            if (p["action"] == "add" and s.bikes_now <= 0) or (p["action"] == "remove" and s.docks_now <= 0):
                hit_utc = now  # already empty / full right now
            hit_local = hit_utc.tz_localize("UTC").tz_convert(tz).strftime("%H:%M")
        out.append({
            "system": system, "city": city, "station_id": s.station_id, "station_code": s.station_code,
            "name": s["name"], "lat": s.lat, "lon": s.lon, "capacity": s.capacity,
            "bikes_now": int(s.bikes_now), "docks_now": int(s.docks_now),
            "forecast_departures_3h": round(sums.loc[s.station_code, "d"], 2),
            "forecast_arrivals_3h": round(sums.loc[s.station_code, "a"], 2),
            **p, "hits_limit_by_utc": hit_utc, "hits_limit_by_local": hit_local,
        })
    df = pd.DataFrame(out)
    order = {"projected_empty": 0, "projected_full": 0, "near_empty": 1, "near_full": 1, "ok": 2}
    df["_tier"] = df["status"].map(order)
    df["_margin"] = np.where(df["action"] == "add", df["projected_min_bikes"],
                             df["docks_now"] + df["bikes_now"] - df["projected_max_bikes"])
    df = df.sort_values(["_tier", "shortfall", "hits_limit_by_utc", "_margin"],
                        ascending=[True, False, True, True], na_position="last")
    df = df.drop(columns=["_tier", "_margin"])
    df.insert(2, "priority", pd.array(range(1, len(df) + 1), dtype="Int64"))
    df.loc[df["status"] == "ok", "priority"] = pd.NA
    local_now = now.tz_localize("UTC").tz_convert(tz)
    df["as_of_utc"] = now
    print(f"\n{system} ({city}): as of {local_now:%Y-%m-%d %H:%M %Z}, {len(st):,} stations in service; "
          f"{(df.status == 'projected_empty').sum()} projected empty, {(df.status == 'projected_full').sum()} "
          f"projected full, {df.status.str.startswith('near').sum()} near a limit; buffer {buf:.2f} bikes; "
          f"lags {gbfs_share:.0%} from GBFS, {1 - gbfs_share:.0%} from trip profile; "
          f"{skipped} stations skipped (no history)")
    if (pd.Timestamp.now("UTC").tz_localize(None) - now) > pd.Timedelta(minutes=30) and as_of is None:
        print(f"  warning: latest successful poll is {now} UTC, more than 30 minutes ago")
    return df


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", nargs="+", choices=list(SYSTEM_TZ), default=list(SYSTEM_TZ))
    ap.add_argument("--as-of", type=pd.Timestamp, help="UTC time to replay (default: latest poll)")
    ap.add_argument("--top", type=int, default=15, help="rows to print per system")
    args = ap.parse_args(argv)
    os.chdir(REPO)  # the GBFS views use paths relative to the repo root

    buffers_path = MODELS_DIR / "buffers.json"
    buffers = json.loads(buffers_path.read_text()) if buffers_path.exists() else {}
    con = gbfs_connection()
    frames = [run_system(con, s, args.as_of, buffers) for s in args.systems]
    df = pd.concat(frames, ignore_index=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / f"priority_{df['as_of_utc'].max():%Y%m%dT%H%MZ}.csv"
    df.to_csv(dest, index=False)
    show = ["priority", "name", "bikes_now", "docks_now", "forecast_departures_3h", "forecast_arrivals_3h",
            "status", "hits_limit_by_local", "action", "bikes_to_move"]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        for s, g in df.groupby("system", sort=False):
            print(f"\n{s}: top {args.top}")
            print(g[g["status"] != "ok"].head(args.top)[show].to_string(index=False))
    print(f"\nfull list -> {dest.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
