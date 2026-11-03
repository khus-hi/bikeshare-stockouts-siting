"""Export the flat CSV tables the charts (dashboard/make_charts.py) and the pitch read.

Every number is computed here, in DuckDB / pandas, from the repo's tested SQL and model
outputs; charts only read these tables. Each table has one clear grain (one row = one
station-hour-of-day, one tract, ...), so any BI tool could also load them as they are.

Tables (dashboard/exports/):
  stockout_heatmap.csv          station x day type x local hour of day: empty/full hours, lost rides
  problem_stations.csv          station totals over the whole snapshot period, ranked
  peak_problem_stations.csv     top 20 per city and weekday peak (the DuckDB view, as is)
  forecast_vs_actual_hourly.csv city x local hour, test month: actual vs LightGBM vs seasonal naive
  forecast_station_accuracy.csv station totals and WAPE over the test month, model vs baseline
  forecast_metrics.csv          analysis/results/netflow_metrics.csv in long form (for scorecards)
  siting_tracts.csv             Boston tracts: score, rank, candidate flag, equity gap, lat/lon
  experiment_*.csv              copies of analysis/results/new_stations/*.csv, if that study has run
  experiment_status.csv         one row saying whether the experiment results are in
  export_manifest.csv           what was written, rows, source and timestamp

Large, optional (git-ignored, pass --station-hours):
  large/forecast_vs_actual_station_hour.csv   every station-hour of the test month

Each block skips itself, with a message, when its input is not on this machine yet:
  stockout tables   need data/bikeshare.duckdb with GBFS snapshots (python analysis/build_views.py)
  forecast tables   need data/features/netflow/system=*/panel.parquet and data/models/netflow/
  siting            needs analysis/results/siting/boston_siting_scores.csv (committed)

No number is typed in here: everything is read from those outputs.

Usage (repo root):
  python dashboard/export_tables.py
  python dashboard/export_tables.py --station-hours          # also the big station-hour file
  python dashboard/export_tables.py --cities boston          # stockout/forecast tables for Boston only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "models"))
from netflow_common import (  # noqa: E402
    FEATURES_DIR, MODELS_DIR, RESULTS_DIR, SYSTEM_CITY, SYSTEM_TZ, TARGETS, feature_columns,
)

OUT = REPO / "dashboard" / "exports"
DUCKDB_PATH = REPO / "data" / "bikeshare.duckdb"
SITING_CSV = RESULTS_DIR / "siting" / "boston_siting_scores.csv"
SITING_CONFIG = REPO / "analysis" / "siting_config.json"
EXPERIMENT_DIR = RESULTS_DIR / "new_stations"
TEST_DAYS = 30  # same default as models/train_netflow.py, so the test month matches the metrics

manifest: list[dict] = []


def write(df: pd.DataFrame, name: str, source: str) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, float_format="%.4f")
    manifest.append({"file": name, "rows": len(df), "source": source})
    print(f"  {name}: {len(df):,} rows")


def latlon(lat: pd.Series, lon: pd.Series) -> pd.Series:
    """One "lat,lon" text field, the form most mapping tools accept as a single location."""
    return lat.round(6).astype(str) + "," + lon.round(6).astype(str)


# --- 1. Stockouts (GBFS snapshots + trip-history demand) ----------------------------------

def export_stockouts(cities: list[str], con=None) -> None:
    """con: an open DuckDB connection (tests pass one); default opens data/bikeshare.duckdb."""
    if con is None:
        if not DUCKDB_PATH.exists():
            print(f"stockouts: skipped, {DUCKDB_PATH.relative_to(REPO)} not found (run analysis/build_views.py)")
            return
        import duckdb
        con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views()").fetchall()}
    if "lost_rides_station_hour" not in views:
        print("stockouts: skipped, no stockout views yet (the collector has no snapshots on this machine)")
        return
    city_list = ", ".join(f"'{c}'" for c in cities)

    # Grain: station x weekday/weekend x local hour of day. Shares are computed in the
    # chart from the summed hours (sum of ratios would be wrong), so the hours are kept.
    heat = con.execute(f"""
        SELECT city, station_code, station_name, capacity, lat, lon,
               CASE WHEN isodow(hour_local) <= 5 THEN 'weekday' ELSE 'weekend' END AS day_type,
               hour(hour_local)                         AS hour_of_day,
               count(DISTINCT CAST(hour_local AS DATE)) AS days_observed,
               sum(observed_minutes) / 60               AS observed_hours,
               sum(minutes_empty) / 60                  AS empty_hours,
               sum(minutes_full) / 60                   AS full_hours,
               sum(est_lost_rides)                      AS est_lost_rides,
               sum(est_lost_rides_low)                  AS est_lost_rides_low,
               sum(est_lost_rides_high)                 AS est_lost_rides_high,
               min(hour_local)                          AS first_hour_local,
               max(hour_local)                          AS last_hour_local
        FROM lost_rides_station_hour
        WHERE city IN ({city_list}) AND observed_minutes > 0
        GROUP BY ALL
        ORDER BY city, station_code, day_type, hour_of_day
    """).df()
    heat["empty_share"] = heat["empty_hours"] / heat["observed_hours"]
    heat["full_share"] = heat["full_hours"] / heat["observed_hours"]
    heat["lat_lon"] = latlon(heat["lat"], heat["lon"])
    write(heat, "stockout_heatmap.csv", "DuckDB view lost_rides_station_hour")

    summ = con.execute(f"""
        SELECT * FROM station_stockout_summary WHERE city IN ({city_list})
    """).df()
    summ["stockout_hours"] = summ["station_hours_empty"] + summ["station_hours_full"]
    summ["stockout_share"] = summ["stockout_hours"] / summ["observed_hours"]
    summ["rank_stockout_hours"] = summ.groupby("city")["stockout_hours"].rank(method="first", ascending=False).astype(int)
    summ["rank_lost_rides"] = summ.groupby("city")["est_lost_rides"].rank(method="first", ascending=False).astype("Int64")
    summ["lat_lon"] = latlon(summ["lat"], summ["lon"])
    period = con.execute(f"""
        SELECT min(hour_local), max(hour_local) FROM station_hour_stockouts WHERE city IN ({city_list})
    """).fetchone()
    summ["period_from_local"], summ["period_to_local"] = period
    write(summ.sort_values(["city", "rank_stockout_hours"]), "problem_stations.csv",
          "DuckDB view station_stockout_summary")

    peak = con.execute(f"SELECT * FROM peak_problem_stations WHERE city IN ({city_list})").df()
    write(peak, "peak_problem_stations.csv", "DuckDB view peak_problem_stations")


# --- 2. Forecast vs actual (held-out test month) -----------------------------------------

def station_names(system: str) -> pd.DataFrame | None:
    """Most recent published name per station code, from trip history (if it is on disk)."""
    files = REPO / "data" / "trips" / "parquet" / f"system={system}"
    if not files.exists():
        return None
    import duckdb
    return duckdb.sql(f"""
        SELECT start_station_id AS station_code, arg_max(start_station_name, started_at) AS station_name
        FROM read_parquet('{(files / '*' / '*.parquet').as_posix()}')
        WHERE start_station_id IS NOT NULL GROUP BY 1
    """).df()


def predict_test_month(system: str) -> pd.DataFrame | None:
    """Re-score the test month with the saved models, exactly as train_netflow.py evaluated it.

    The saved models were trained on data before the validation window, so the test month
    is out of sample. The script checks itself against netflow_metrics.csv (WAPE printed).
    """
    panel = FEATURES_DIR / f"system={system}" / "panel.parquet"
    models = {t: MODELS_DIR / f"{system}_{t}.txt" for t in TARGETS}
    if not panel.exists() or not all(p.exists() for p in models.values()):
        print(f"forecast {system}: skipped, needs {panel.relative_to(REPO)} and data/models/netflow/{system}_*.txt")
        return None
    import lightgbm as lgb
    buffers = json.loads((MODELS_DIR / "buffers.json").read_text()) if (MODELS_DIR / "buffers.json").exists() else {}
    with_weather = buffers.get(system, {}).get("with_weather", True)
    df = pd.read_parquet(panel)
    end = df["hour_utc"].max() + pd.Timedelta(hours=1)
    test_start = (end - pd.Timedelta(days=TEST_DAYS)).floor("D")
    te = df[df["hour_utc"] >= test_start].sort_values(["station_code", "hour_utc"]).reset_index(drop=True)
    feats = feature_columns(with_weather)
    out = te[["station_code", "hour_utc", "hour_local", "dow_local", "is_weekend"]].copy()
    for t in TARGETS:
        booster = lgb.Booster(model_file=str(models[t]))
        out[f"actual_{t}"] = te[t].astype(float)
        out[f"forecast_{t}"] = booster.predict(te[feats])
        out[f"naive_{t}"] = te[f"{t}_lag168"].astype(float)
    for k in ("actual", "forecast", "naive"):
        out[f"{k}_net"] = out[f"{k}_departures"] - out[f"{k}_arrivals"]
    out["city"] = SYSTEM_CITY[system]
    out["model_variant"] = "with_weather" if with_weather else "no_weather"
    out["hour_local_ts"] = (out["hour_utc"].dt.tz_localize("UTC") if out["hour_utc"].dt.tz is None
                            else out["hour_utc"]).dt.tz_convert(SYSTEM_TZ[system]).dt.tz_localize(None)
    a, f = out["actual_departures"], out["forecast_departures"]
    print(f"forecast {system}: {test_start:%Y-%m-%d} .. {end:%Y-%m-%d %H:%M} UTC, "
          f"{out['station_code'].nunique()} stations, departures WAPE {100 * (a - f).abs().sum() / a.sum():.2f}% "
          f"(compare analysis/results/netflow_metrics.csv)")
    return out


def export_forecast(systems: list[str], station_hours: bool) -> None:
    hourly, acc, big = [], [], []
    for system in systems:
        out = predict_test_month(system)
        if out is None:
            continue
        val = [c for c in out.columns if c.startswith(("actual_", "forecast_", "naive_"))]
        h = out.groupby(["city", "model_variant", "hour_local_ts"], as_index=False)[val].sum()
        h["n_stations"] = out.groupby("hour_local_ts")["station_code"].nunique().to_numpy()
        hourly.append(h)

        g = out.groupby(["city", "station_code"], as_index=False)
        s = g[val].sum()
        abs_err = out.assign(
            ae_model=(out["actual_departures"] - out["forecast_departures"]).abs(),
            ae_naive=(out["actual_departures"] - out["naive_departures"]).abs(),
            ae_net_model=(out["actual_net"] - out["forecast_net"]).abs(),
            ae_net_naive=(out["actual_net"] - out["naive_net"]).abs(),
        ).groupby(["city", "station_code"], as_index=False)[["ae_model", "ae_naive", "ae_net_model", "ae_net_naive"]].sum()
        s = s.merge(abs_err, on=["city", "station_code"])
        # Raw sums are kept so WAPE can be computed for any group of stations:
        # WAPE = SUM(abs error) / SUM(actual). Averaging per-station WAPEs would be wrong.
        s["departures_wape_model"] = s["ae_model"] / s["actual_departures"].replace(0, np.nan)
        s["departures_wape_naive"] = s["ae_naive"] / s["actual_departures"].replace(0, np.nan)
        names = station_names(system)
        if names is not None:
            s = s.merge(names, on="station_code", how="left")
        panel_ll = pd.read_parquet(FEATURES_DIR / f"system={system}" / "panel.parquet",
                                   columns=["station_code", "lat", "lng"]).drop_duplicates("station_code")
        s = s.merge(panel_ll, on="station_code", how="left")
        s["lat_lon"] = latlon(s["lat"], s["lng"])
        acc.append(s)
        if station_hours:
            big.append(out.drop(columns=["hour_utc"]))
    if hourly:
        write(pd.concat(hourly), "forecast_vs_actual_hourly.csv", "saved LightGBM models re-scored on the test month")
        write(pd.concat(acc), "forecast_station_accuracy.csv", "same, summed per station")
    if big:
        write(pd.concat(big), "large/forecast_vs_actual_station_hour.csv", "same, every station-hour")

    m = pd.read_csv(RESULTS_DIR / "netflow_metrics.csv")
    ids = ["city", "system", "variant", "forecast", "test_from_utc", "test_to_utc", "stations", "test_station_hours"]
    write(m.melt(id_vars=ids, var_name="metric", value_name="value"), "forecast_metrics.csv",
          "analysis/results/netflow_metrics.csv")


# --- 3. Siting (Boston Census tracts) ----------------------------------------------------

def export_siting() -> None:
    if not SITING_CSV.exists():
        print("siting: skipped, run analysis/siting_score.py first")
        return
    cfg = json.loads(SITING_CONFIG.read_text())
    radius = cfg.get("walk_radius_m", 400)
    s = pd.read_csv(SITING_CSV, dtype={"geoid": str})
    s["is_candidate"] = s["eligible"] & (s["stations_within_400m"] == 0)
    cand = s[s["is_candidate"]].sort_values("rank")
    s["candidate_rank"] = pd.Series(range(1, len(cand) + 1), index=cand.index).reindex(s.index).astype("Int64")
    s["map_group"] = np.select(
        [s["equity_gap"], s["is_candidate"], s["eligible"]],
        ["Equity gap (low income, no station)", "New-station candidate", "Covered"],
        default="Not scored (< 500 residents)")
    s["lat_lon"] = latlon(s["lat"], s["lon"])
    keep = ["geoid", "neighborhood", "lat", "lon", "lat_lon", "population", "pop_density_sq_mi",
            "share_commute_transit_bike_walk", "share_households_no_vehicle", "share_18_34",
            "median_household_income", "nearest_station_name", "dist_nearest_station_m",
            "stations_within_400m", "eligible", "score", "rank", "is_candidate", "candidate_rank",
            "low_income", "poor_coverage", "equity_gap", "map_group", "stations_snapshot_utc"]
    out = s[keep].rename(columns={"stations_within_400m": f"stations_within_{radius}m"})
    write(out, "siting_tracts.csv", "analysis/results/siting/boston_siting_scores.csv")


# --- 4. Experiment (new-station diff-in-diff) --------------------------------------------

def export_experiment() -> None:
    files = sorted(EXPERIMENT_DIR.glob("*.csv")) if EXPERIMENT_DIR.exists() else []
    for f in files:
        shutil.copyfile(f, OUT / f"experiment_{f.name}")
        manifest.append({"file": f"experiment_{f.name}", "rows": sum(1 for _ in f.open()) - 1,
                         "source": str(f.relative_to(REPO))})
        print(f"  experiment_{f.name}: copied")
    status = pd.DataFrame([{
        "experiment": "Do new stations add rides or move them? (stacked diff-in-diff)",
        "status": "results in" if files else "pending: analysis/new_station_did.py has not written results yet",
        "files": "; ".join(f"experiment_{f.name}" for f in files),
    }])
    write(status, "experiment_status.csv", str(EXPERIMENT_DIR.relative_to(REPO)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cities", nargs="+", choices=list(SYSTEM_CITY.values()), default=list(SYSTEM_CITY.values()))
    ap.add_argument("--station-hours", action="store_true", help="also write the large station-hour forecast file")
    args = ap.parse_args(argv)
    systems = [s for s, c in SYSTEM_CITY.items() if c in args.cities]
    os.chdir(REPO)  # the DuckDB views read data/... with relative paths
    OUT.mkdir(parents=True, exist_ok=True)
    export_stockouts(args.cities)
    export_forecast(systems, args.station_hours)
    export_siting()
    export_experiment()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    old = pd.read_csv(OUT / "export_manifest.csv") if (OUT / "export_manifest.csv").exists() else pd.DataFrame()
    new = pd.DataFrame(manifest).assign(exported_utc=now)
    # Keep earlier rows for files not rewritten this run (e.g. stockouts exported on the Mac,
    # forecast exported elsewhere), so the manifest describes every file in the folder.
    if not old.empty:
        new = pd.concat([old[~old["file"].isin(new["file"])], new])
    new.sort_values("file").to_csv(OUT / "export_manifest.csv", index=False)
    print(f"wrote {OUT.relative_to(REPO)}/export_manifest.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
