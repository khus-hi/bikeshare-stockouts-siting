"""Train and evaluate the hourly station net-flow forecast, and compare it with the
seasonal-naive baseline.

Model: for each system, two LightGBM models, one for departures and one for arrivals,
with a Poisson objective (they are counts: never negative, mostly 0, 1 or 2).
Net flow forecast = departures forecast - arrivals forecast.
Why two models instead of one on net flow directly: the rebalancing step needs to know
how many riders want bikes and docks, and a station with 20 out and 20 in is busy, not
quiet, even though its net flow is 0.

Baseline (seasonal naive): the same station, same hour, one week earlier
(departures_lag168, arrivals_lag168). docs/requirements.md asks the model to beat it.

Time-based split, never random (random would leak the future into training):
  test   the last --test-days days of the panel   (default 30: September 2026 today)
  valid  the --valid-days days before test        (early stopping only)
  train  everything before valid

Metrics, per city, on the test period:
  departures, arrivals: MAPE over station-hours with actual >= 1 (MAPE is undefined at 0,
                        as in docs/requirements.md), and WAPE over all station-hours
  net flow:             MAPE over station-hours with |actual| >= 1, WAPE, MAE
  net flow, 3 hours:    MAE of the 3-hour sum, the quantity rebalancing uses; its value
                        becomes the safety buffer in models/rebalance.py

Outputs:
  data/models/netflow/<system>_<target>.txt         LightGBM models (git-ignored, rebuildable)
  data/models/netflow/<system>_profile.parquet     hour-of-week demand profile (for rebalance.py)
  data/models/netflow/buffers.json                  3-hour MAE per system (for rebalance.py)
  analysis/results/netflow_metrics.csv / .md        metrics table (committed)
  analysis/results/netflow_feature_importance.csv   gain importance per feature

Usage (from the repo root):
  python models/train_netflow.py                  # with weather if data/weather/history exists
  python models/train_netflow.py --no-weather     # calendar + lags only
  python models/train_netflow.py --ablation       # both, to show what weather adds
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netflow_common import (  # noqa: E402
    FEATURES_DIR, MODELS_DIR, PROFILE_DAYS, REPO, RESULTS_DIR, SYSTEM_CITY, SYSTEM_TZ, TARGETS,
    feature_columns, mae, mape, net_mape, wape,
)

PARAMS = {
    "objective": "poisson",
    "learning_rate": 0.1,
    "num_leaves": 63,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "max_bin": 63,          # coarser histograms: much faster, counts and lags have few distinct values anyway
    "force_col_wise": True,
    "verbose": -1,
    "seed": 42,
}


def split(df: pd.DataFrame, test_days: int, valid_days: int):
    end = df["hour_utc"].max() + pd.Timedelta(hours=1)
    test_start = (end - pd.Timedelta(days=test_days)).floor("D")
    valid_start = test_start - pd.Timedelta(days=valid_days)
    tr = df["hour_utc"] < valid_start
    va = (df["hour_utc"] >= valid_start) & (df["hour_utc"] < test_start)
    te = df["hour_utc"] >= test_start
    return tr, va, te, (valid_start, test_start, end)


def save_profile(system: str, df: pd.DataFrame) -> None:
    """Average departures/arrivals per station x local hour-of-week over the last 4 weeks.
    models/rebalance.py falls back to it for hours the GBFS collector did not observe, so the
    machine running rebalance.py needs this small file, not the whole trip history."""
    recent = df[df["hour_utc"] > df["hour_utc"].max() - pd.Timedelta(days=PROFILE_DAYS)]
    prof = recent.groupby(["station_code", "dow_local", "hour_local"], as_index=False, observed=True)[TARGETS].mean()
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    prof.to_parquet(MODELS_DIR / f"{system}_profile.parquet", index=False)


def three_hour_sum(df: pd.DataFrame, col: str) -> pd.Series:
    """Sum of col over hours t, t+1, t+2 for the same station (NaN if any hour is missing)."""
    g = df.groupby("station_code", sort=False)[col]
    out = df[col] + g.shift(-1) + g.shift(-2)
    contiguous = (df.groupby("station_code", sort=False)["hour_utc"].shift(-2) - df["hour_utc"]) == pd.Timedelta(hours=2)
    return out.where(contiguous)


def evaluate(te: pd.DataFrame, pred: dict[str, np.ndarray], label: str) -> dict:
    row = {"forecast": label}
    for t in TARGETS:
        row[f"{t}_mape"] = mape(te[t], pred[t])
        row[f"{t}_wape"] = wape(te[t], pred[t])
    net_a = (te["departures"] - te["arrivals"]).to_numpy()
    net_p = pred["departures"] - pred["arrivals"]
    row["net_mape"] = net_mape(net_a, net_p)
    row["net_wape"] = wape(net_a, net_p)
    row["net_mae"] = mae(net_a, net_p)
    tmp = te[["station_code", "hour_utc"]].copy()
    tmp["a"], tmp["p"] = net_a, net_p
    a3, p3 = three_hour_sum(tmp, "a"), three_hour_sum(tmp, "p")
    ok = a3.notna() & p3.notna()
    row["net3h_mae"] = mae(a3[ok], p3[ok])
    row["net3h_wape"] = wape(a3[ok], p3[ok])
    return row


def run_system(system: str, with_weather: bool, args) -> tuple[list[dict], list[dict], float | None]:
    path = FEATURES_DIR / f"system={system}" / "panel.parquet"
    if not path.exists():
        print(f"{system}: no panel at {path.relative_to(REPO)}; run models/netflow_features.py")
        return [], [], None
    df = pd.read_parquet(path)
    df = df.sort_values(["station_code", "hour_utc"], kind="stable").reset_index(drop=True)
    if with_weather and df["temperature_2m"].isna().all():
        raise SystemExit(f"{system}: panel has no weather; run ingest/weather_download.py history, "
                         "rebuild the panel, or pass --no-weather")
    if not args.ablation or with_weather:
        save_profile(system, df)
    feats = feature_columns(with_weather)
    tr, va, te, (vs, ts, end) = split(df, args.test_days, args.valid_days)
    variant = "with_weather" if with_weather else "no_weather"
    print(f"{system} [{variant}]: train {tr.sum():,} rows (< {vs:%Y-%m-%d}), valid {va.sum():,}, "
          f"test {te.sum():,} ({ts:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M} UTC)")

    test = df.loc[te].reset_index(drop=True)
    model_pred, importance = {}, []
    for t in TARGETS:
        t0 = time.time()
        dtr = lgb.Dataset(df.loc[tr, feats], df.loc[tr, t], params=PARAMS, free_raw_data=True)
        dva = lgb.Dataset(df.loc[va, feats], df.loc[va, t], reference=dtr)
        booster = lgb.train(PARAMS, dtr, num_boost_round=args.max_rounds, valid_sets=[dva],
                            callbacks=[lgb.early_stopping(50, verbose=False)])
        model_pred[t] = booster.predict(test[feats], num_iteration=booster.best_iteration)
        print(f"  {t}: {booster.best_iteration} trees, {time.time() - t0:.0f}s")
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        if with_weather or not args.ablation:  # the saved model is the one rebalance.py uses
            booster.save_model(str(MODELS_DIR / f"{system}_{t}.txt"), num_iteration=booster.best_iteration)
        gain = booster.feature_importance("gain", iteration=booster.best_iteration)
        for f, g in zip(feats, gain):
            importance.append({"system": system, "variant": variant, "target": t, "feature": f,
                               "gain_share": g / gain.sum()})

    base_pred = {t: test[f"{t}_lag168"].to_numpy(float) for t in TARGETS}
    meta = {"city": SYSTEM_CITY[system], "system": system, "variant": variant,
            "test_from_utc": f"{ts:%Y-%m-%d %H:%M}", "test_to_utc": f"{end:%Y-%m-%d %H:%M}",
            "stations": int(test["station_code"].nunique()), "test_station_hours": int(len(test))}
    rows = [meta | evaluate(test, base_pred, "seasonal_naive"), meta | evaluate(test, model_pred, "lightgbm")]
    return rows, importance, rows[1]["net3h_mae"]


def to_markdown(m: pd.DataFrame) -> str:
    cols = ["city", "variant", "forecast", "departures_mape", "departures_wape", "arrivals_mape",
            "arrivals_wape", "net_mape", "net_wape", "net_mae", "net3h_mae"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in m[cols].iterrows():
        lines.append("| " + " | ".join(f"{v:.2f}" if isinstance(v, float) else str(v) for v in r) + " |")
    first = m.iloc[0]
    return (f"# Net-flow forecast metrics\n\nTest period (UTC): {first.test_from_utc} to {first.test_to_utc}. "
            "MAPE and WAPE in %, MAE in bikes per station-hour (net3h: per 3-hour window). "
            "Built by `python models/train_netflow.py`; see models/README.md for definitions.\n\n"
            + "\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", nargs="+", choices=list(SYSTEM_TZ), default=list(SYSTEM_TZ))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-weather", action="store_true")
    g.add_argument("--ablation", action="store_true", help="train with and without weather")
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--valid-days", type=int, default=14)
    ap.add_argument("--max-rounds", type=int, default=1000)
    args = ap.parse_args(argv)

    variants = [False, True] if args.ablation else [not args.no_weather]
    rows, imp, buffers = [], [], {}
    for system in args.systems:
        for w in variants:
            r, i, buf = run_system(system, w, args)
            rows += r
            imp += i
            if buf is not None and (w or not args.ablation):
                buffers[system] = {"net3h_mae": buf, "with_weather": w}

    if not rows:
        return 1
    m = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    m.to_csv(RESULTS_DIR / "netflow_metrics.csv", index=False, float_format="%.4f")
    (RESULTS_DIR / "netflow_metrics.md").write_text(to_markdown(m))
    pd.DataFrame(imp).to_csv(RESULTS_DIR / "netflow_feature_importance.csv", index=False, float_format="%.5f")
    (MODELS_DIR / "buffers.json").write_text(json.dumps(buffers, indent=2))
    print("\n" + to_markdown(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
