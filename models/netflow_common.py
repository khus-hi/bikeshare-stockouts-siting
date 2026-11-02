"""Shared definitions for the station net-flow forecast: feature list, metrics, paths.

Used by models/netflow_features.py (build training table), models/train_netflow.py
(train and evaluate) and models/rebalance.py (live 3-hour projection), so the model
always sees the same columns in training and at run time.

Words used throughout:
  departures   trips that start at the station in the hour
  arrivals     trips that end at the station in the hour
  net flow     departures - arrivals  (positive = the station is losing bikes)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
FEATURES_DIR = REPO / "data" / "features" / "netflow"
MODELS_DIR = REPO / "data" / "models" / "netflow"
RESULTS_DIR = REPO / "analysis" / "results"

SYSTEM_TZ = {"bluebikes": "America/New_York", "citibike": "America/New_York", "divvy": "America/Chicago"}
SYSTEM_CITY = {"bluebikes": "boston", "citibike": "nyc", "divvy": "chicago"}

TARGETS = ["departures", "arrivals"]
PROFILE_DAYS = 28  # window for the hour-of-week fallback profile used by rebalance.py

# Lag features, in hours. The shortest lag is 24 h on purpose: a feature the model uses
# must be known when the forecast is made, and 24 h lets one model forecast any of the
# next 24 hours without feeding its own predictions back in.
LAG_HOURS = {"lag24": 24, "lag168": 168, "lag336": 336, "lag504": 504}

CALENDAR_FEATURES = ["hour_local", "dow_local", "is_weekend", "is_holiday"]
STATION_FEATURES = ["lat", "lng"]
WEATHER_FEATURES = [
    "temperature_2m", "apparent_temperature", "relative_humidity_2m", "precipitation",
    "rain", "snowfall", "cloud_cover", "wind_speed_10m",
]


def lag_features(target: str) -> list[str]:
    """Lags of one target series, e.g. departures_lag168 = same hour one week earlier.

    <target>_how3w  mean of the same hour-of-week over the last 3 weeks (lags 168/336/504)
    <target>_prev24 total over the 24 hours that end 24 hours before the forecast hour
    """
    return [f"{target}_{k}" for k in LAG_HOURS] + [f"{target}_how3w", f"{target}_prev24"]


def feature_columns(with_weather: bool) -> list[str]:
    cols = CALENDAR_FEATURES + STATION_FEATURES
    for t in TARGETS:  # both series help predict each one (e.g. arrivals hint at departures)
        cols += lag_features(t)
    if with_weather:
        cols += WEATHER_FEATURES
    return cols


def add_calendar(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Local hour, weekday, weekend and US federal holiday flags from a naive UTC hour_utc column."""
    from pandas.tseries.holiday import USFederalHolidayCalendar

    local = pd.to_datetime(df["hour_utc"]).dt.tz_localize("UTC").dt.tz_convert(tz)
    df["hour_local"] = local.dt.hour.astype("int8")
    df["dow_local"] = local.dt.dayofweek.astype("int8")  # Monday = 0
    df["is_weekend"] = (df["dow_local"] >= 5).astype("int8")
    day = local.dt.tz_localize(None).dt.normalize()
    hol = USFederalHolidayCalendar().holidays(day.min(), day.max())
    df["is_holiday"] = day.isin(hol).astype("int8")
    return df


# Metrics -------------------------------------------------------------------------------

def mape(actual: np.ndarray, pred: np.ndarray) -> float:
    """MAPE in %, only over rows where actual >= 1 (undefined at 0), per docs/requirements.md."""
    actual, pred = np.asarray(actual, float), np.asarray(pred, float)
    m = actual >= 1
    if not m.any():
        return float("nan")
    return float(100 * np.mean(np.abs(actual[m] - pred[m]) / actual[m]))


def wape(actual: np.ndarray, pred: np.ndarray) -> float:
    """WAPE in %: sum |error| / sum |actual|. Works for net flow, which can be negative."""
    actual, pred = np.asarray(actual, float), np.asarray(pred, float)
    denom = np.abs(actual).sum()
    return float(100 * np.abs(actual - pred).sum() / denom) if denom else float("nan")


def net_mape(actual: np.ndarray, pred: np.ndarray) -> float:
    """MAPE for net flow, over rows where |actual net| >= 1 (net 0 is undefined)."""
    actual, pred = np.asarray(actual, float), np.asarray(pred, float)
    m = np.abs(actual) >= 1
    if not m.any():
        return float("nan")
    return float(100 * np.mean(np.abs(actual[m] - pred[m]) / np.abs(actual[m])))


def mae(actual: np.ndarray, pred: np.ndarray) -> float:
    return float(np.mean(np.abs(np.asarray(actual, float) - np.asarray(pred, float))))
