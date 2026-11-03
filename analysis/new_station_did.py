"""Do new stations add rides, or take them from nearby stations?

A stacked difference-in-differences (DiD) study on official trip history.

  1. Station panel. Weekly departures and arrivals per station, from the station x day
     table built by ingest/station_daily.py (30 months: a 12-month burn-in + 18 months).
  2. New stations. A station is "new" if its first ride is after the burn-in (so a
     station that only closes for winter is not counted as new). We drop:
       * replacements: an old station within REPLACE_M that stopped within
         REPLACE_DAYS of the new one starting (a move or an ID change, not new capacity);
       * temporary stations: fewer than MIN_ACTIVE_SHARE of post-weeks with a departure.
  3. Event = one new station opening (week 0 = its first-ride week, left out because it
     is partly before and partly after). Window: PRE_WEEKS before and POST_WEEKS after.
  4. Treated units: stations that existed before the window, stayed open through it and
     had rides in at least UNIT_ACTIVE_SHARE of pre and of post weeks, within TREAT_M of the new station. A treated station is dropped from an event if a
     different new station opened within TREAT_M of it during the same window (we could
     not tell the two openings apart).
  5. Controls: stations CONTROL_MIN_M..CONTROL_MAX_M from the new station (far enough not
     to share riders, near enough to share the neighbourhood's weather, season and
     system-wide changes), open the whole window, and not within CLEAN_M of any station
     that opened during the window. Each treated station gets the N_MATCH controls closest
     to it on pre-period ridership (log mean weekly departures and pre-period trend), with
     replacement.
  6. Estimate. Each treated station + its controls is one "group". Group DiD =
     (treated post mean - treated pre mean) - (controls post mean - controls pre mean).
     The overall effect is the mean over groups. With a balanced panel this equals the
     two-way fixed-effects estimate on the stacked data (unit x group and group x week
     fixed effects). Confidence intervals: 95% from a cluster bootstrap that resamples
     whole events (new stations), because neighbours of the same new station share shocks.

Run from the repo root after ingest/station_daily.py:
  python analysis/new_station_did.py
Outputs go to analysis/results/new_stations/.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "analysis"))
from tract_station_distance import haversine_matrix  # noqa: E402


@dataclass
class Config:
    data_start: str = "2024-04-01"      # first day of downloaded history
    burn_in_months: int = 12            # a station first seen inside this is "existing"
    pre_weeks: int = 12
    post_weeks: int = 12
    treat_m: float = 500.0
    control_min_m: float = 2000.0
    control_max_m: float = 5000.0
    clean_m: float = 1000.0             # controls must be this far from every window opening
    n_match: int = 3
    replace_m: float = 150.0
    replace_days: int = 60
    min_active_share: float = 0.5       # new station: share of post-weeks with >=1 departure
    unit_active_share: float = 0.8      # treated/control: share of pre AND of post weeks with >=1 departure
    # Not permanent docked stations: Divvy "Public Rack" e-bike parking, event "Corral"
    # valet sites, charging points, operator test/demo points.
    exclude_name_regex: str = r"^Public Rack|Corral\b|(?i:charging)|\bDemo\b|-LAB-|\bTest\b|\bTEST\b"
    placebo_shift_weeks: int = 26       # fake opening date for the placebo-in-time test
    n_boot: int = 2000
    seed: int = 20261103


# ----------------------------------------------------------------------------- data

def load_panel(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Weekly station panel (only full weeks) and one coordinate row per station."""
    con = duckdb.connect()
    base = data_dir / "station_daily"
    daily = con.execute(f"""
        SELECT system, station_id, date_trunc('week', day)::DATE AS week,
               sum(departures)::BIGINT AS departures, sum(arrivals)::BIGINT AS arrivals,
               min(day) AS first_day_in_week, max(day) AS last_day_in_week
        FROM read_parquet('{base}/system=*/month=*/station_daily.parquet', hive_partitioning = false)
        GROUP BY ALL
    """).df()
    span = con.execute(f"""
        SELECT system, min(day) AS d0, max(day) AS d1
        FROM read_parquet('{base}/system=*/month=*/station_daily.parquet', hive_partitioning = false)
        GROUP BY ALL""").df()
    coords = con.execute(f"""
        SELECT system, station_id,
               arg_max(station_name, (n_trips, station_name)) AS station_name,
               -- exact decimal sums, so that float summation order can't change a distance tie
               round(sum(lat::DECIMAL(18, 9) * n_trips) / sum(n_trips), 6)::DOUBLE AS lat,
               round(sum(lng::DECIMAL(18, 9) * n_trips) / sum(n_trips), 6)::DOUBLE AS lng
        FROM read_parquet('{base}/system=*/month=*/station_coords.parquet', hive_partitioning = false)
        WHERE n_trips > 0
        GROUP BY ALL ORDER BY ALL
    """).df()
    first = con.execute(f"""
        SELECT system, station_id,
               min(day) FILTER (WHERE departures > 0) AS first_day,
               max(day) FILTER (WHERE departures > 0) AS last_day
        FROM read_parquet('{base}/system=*/month=*/station_daily.parquet', hive_partitioning = false)
        GROUP BY ALL ORDER BY ALL""").df()
    coords = coords.merge(first, on=["system", "station_id"], how="inner").sort_values(["system", "station_id"])
    # Keep full Monday-Sunday weeks inside each system's data span.
    daily = daily.merge(span, on="system")
    w0 = pd.to_datetime(daily["d0"]) + pd.to_timedelta((7 - pd.to_datetime(daily["d0"]).dt.weekday) % 7, "D")
    w1 = pd.to_datetime(daily["d1"]) - pd.to_timedelta((pd.to_datetime(daily["d1"]).dt.weekday + 1) % 7, "D")
    daily["week"] = pd.to_datetime(daily["week"])
    panel = daily[(daily["week"] >= w0) & (daily["week"] + pd.Timedelta(days=6) <= w1)]
    panel = panel[["system", "station_id", "week", "departures", "arrivals"]].sort_values(["system", "station_id", "week"])
    for c in ("first_day", "last_day"):
        coords[c] = pd.to_datetime(coords[c])
    return panel.reset_index(drop=True), coords.dropna(subset=["lat", "lng", "first_day"]).reset_index(drop=True)


def wide(panel: pd.DataFrame, col: str) -> pd.DataFrame:
    """station x week matrix with zeros for weeks with no rides."""
    return panel.pivot_table(index="station_id", columns="week", values=col, aggfunc="sum", fill_value=0)


# ----------------------------------------------------------------------------- design

def week_of(ts: pd.Series | pd.Timestamp):
    return ts - pd.to_timedelta(ts.weekday() if isinstance(ts, pd.Timestamp) else ts.dt.weekday, "D")


def classify_new_stations(st: pd.DataFrame, dep: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """One row per station first seen after the burn-in, with a status column."""
    burn_end = pd.Timestamp(cfg.data_start) + pd.DateOffset(months=cfg.burn_in_months)
    weeks = dep.columns
    last_week = weeks.max()
    st = st.copy()
    new = st[st["first_day"] >= burn_end].copy()
    old = st[st["first_day"] < burn_end]
    new["open_week"] = week_of(new["first_day"])
    d = haversine_matrix(new["lat"], new["lng"], old["lat"], old["lng"]) if len(old) else np.zeros((len(new), 0))
    status, replaced, n_existing_500 = [], [], []
    for i, (_, r) in enumerate(new.iterrows()):
        near = d[i] <= cfg.replace_m
        gap = (old["last_day"].values - np.datetime64(r["first_day"])) / np.timedelta64(1, "D")
        rep = near & (np.abs(gap) <= cfg.replace_days)
        n_existing_500.append(int(((d[i] <= cfg.treat_m) & (old["first_day"].values < np.datetime64(r["first_day"]))).sum()))
        post_end = r["open_week"] + pd.Timedelta(weeks=cfg.post_weeks)
        post = [w for w in weeks if r["open_week"] < w <= post_end]
        if rep.any():
            status.append("replacement")
            replaced.append(old["station_id"].values[np.argmin(np.where(rep, d[i], np.inf))])  # nearest
            continue
        replaced.append(None)
        if post_end > last_week:
            status.append("too_recent")
            continue
        active = (dep.loc[r["station_id"], post] > 0).mean() if r["station_id"] in dep.index else 0.0
        status.append("analysed" if active >= cfg.min_active_share else "temporary")
    new["status"] = status
    new["replaces_station_id"] = replaced
    new["existing_within_treat_m"] = n_existing_500
    return new.sort_values("first_day").reset_index(drop=True)


def build_groups(st: pd.DataFrame, new: pd.DataFrame, dep: pd.DataFrame, cfg: Config,
                 shift_weeks: int = 0, ring: tuple[float, float] | None = None) -> pd.DataFrame:
    """Treated-control groups for every analysed opening of one system.

    shift_weeks moves every event date earlier (placebo-in-time).
    ring=(a, b) uses stations a..b metres away as "treated" instead of 0..treat_m.
    Returns long rows: event_id, group_id, station_id, role (treated/control), weight.
    """
    lo, hi = ring if ring else (0.0, cfg.treat_m)
    events = new[new["status"] == "analysed"].copy()
    events["event_week"] = events["open_week"] - pd.Timedelta(weeks=shift_weeks)
    all_new = new[new["status"].isin(["analysed", "too_recent", "temporary"])]
    weeks = dep.columns
    rows = []
    rng = np.random.default_rng(cfg.seed)  # only used to break exact ties
    for _, e in events.iterrows():
        w_start = e["event_week"] - pd.Timedelta(weeks=cfg.pre_weeks)
        w_end = e["event_week"] + pd.Timedelta(weeks=cfg.post_weeks)
        if w_start < weeks.min() or w_end > weeks.max():
            continue
        # Stations open for the whole window.
        alive = st[(st["first_day"] < w_start) & (st["last_day"] > w_end)]
        alive = alive[alive["station_id"].isin(dep.index) & (alive["station_id"] != e["station_id"])]
        # Drop stations that were closed for much of either half of the window (e.g. winter
        # removals): their before/after change would be the season, not the new station.
        pre_w = [w for w in weeks if w_start <= w < e["event_week"]]
        post_w = [w for w in weeks if e["event_week"] < w <= w_end]
        act = dep.loc[alive["station_id"]]
        ok = ((act[pre_w] > 0).mean(axis=1) >= cfg.unit_active_share) & \
             ((act[post_w] > 0).mean(axis=1) >= cfg.unit_active_share)
        alive = alive[ok.values]
        if alive.empty:
            continue
        d_e = haversine_matrix([e["lat"]], [e["lng"]], alive["lat"], alive["lng"])[0]
        # Other openings during this window (contamination).
        others = all_new[(all_new["station_id"] != e["station_id"])
                         & (all_new["open_week"] >= w_start) & (all_new["open_week"] <= w_end)]
        window_new = all_new[(all_new["open_week"] >= w_start) & (all_new["open_week"] <= w_end)]
        d_other = (haversine_matrix(alive["lat"], alive["lng"], others["lat"], others["lng"]).min(axis=1)
                   if len(others) else np.full(len(alive), np.inf))
        d_any = (haversine_matrix(alive["lat"], alive["lng"], window_new["lat"], window_new["lng"]).min(axis=1)
                 if len(window_new) else np.full(len(alive), np.inf))
        treated = alive[(d_e > lo) & (d_e <= hi) & (d_other > cfg.treat_m)] if lo > 0 else \
            alive[(d_e <= hi) & (d_other > cfg.treat_m)]
        pool = alive[(d_e >= cfg.control_min_m) & (d_e <= cfg.control_max_m) & (d_any > cfg.clean_m)]
        if treated.empty or len(pool) < cfg.n_match:
            continue
        f_pre = _match_features(dep.loc[:, pre_w])
        ft, fp = f_pre.loc[treated["station_id"]], f_pre.loc[pool["station_id"]]
        cov = np.cov(f_pre.loc[list(treated["station_id"]) + list(pool["station_id"])].T.values) + np.eye(2) * 1e-9
        inv = np.linalg.inv(cov)
        for sid, x in ft.iterrows():
            diff = fp.values - x.values
            md = np.einsum("ij,jk,ik->i", diff, inv, diff) + rng.uniform(0, 1e-12, len(fp))
            pick = fp.index[np.argsort(md)[:cfg.n_match]]
            gid = f"{e['station_id']}|{sid}"
            rows.append((e["station_id"], gid, sid, "treated", 1.0, e["event_week"],
                         float(d_e[alive["station_id"].values == sid][0])))
            for c in pick:
                rows.append((e["station_id"], gid, c, "control", 1.0 / cfg.n_match, e["event_week"],
                             float(d_e[alive["station_id"].values == c][0])))
    return pd.DataFrame(rows, columns=["event_id", "group_id", "station_id", "role", "weight",
                                       "event_week", "dist_to_new_m"])


def _match_features(pre: pd.DataFrame) -> pd.DataFrame:
    """Log mean weekly departures and log growth from first to second half of the pre-period."""
    half = pre.shape[1] // 2
    a = pre.iloc[:, :half].mean(axis=1)
    b = pre.iloc[:, half:].mean(axis=1)
    return pd.DataFrame({"log_level": np.log1p(pre.mean(axis=1)),
                         "log_trend": np.log1p(b) - np.log1p(a)})


# ----------------------------------------------------------------------------- estimation

def group_series(groups: pd.DataFrame, mat: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Rows: group x relative week, with treated value and weighted control mean."""
    out = []
    rel = [k for k in range(-cfg.pre_weeks, cfg.post_weeks + 1) if k != 0]
    for (gid, eid, ew), g in groups.groupby(["group_id", "event_id", "event_week"]):
        cols = [ew + pd.Timedelta(weeks=k) for k in rel]
        vals = mat.loc[g["station_id"], cols].values
        w = g["weight"].values
        t = vals[g["role"].values == "treated"][0]
        c = (vals[g["role"].values == "control"] * w[g["role"].values == "control"][:, None]).sum(axis=0)
        for k, tv, cv in zip(rel, t, c):
            out.append((eid, gid, k, tv, cv))
    return pd.DataFrame(out, columns=["event_id", "group_id", "rel_week", "treated", "control"])


def group_did(series: pd.DataFrame) -> pd.DataFrame:
    s = series.assign(post=series["rel_week"] > 0)
    m = s.groupby(["event_id", "group_id", "post"])[["treated", "control"]].mean().unstack("post")
    out = pd.DataFrame({
        "treated_pre": m[("treated", False)], "treated_post": m[("treated", True)],
        "control_pre": m[("control", False)], "control_post": m[("control", True)],
    })
    out["did"] = (out["treated_post"] - out["treated_pre"]) - (out["control_post"] - out["control_pre"])
    # Log version: change in log(1 + weekly rides), ~ percent effect.
    lg = s.assign(lt=np.log1p(s["treated"]), lc=np.log1p(s["control"]))
    ml = lg.groupby(["event_id", "group_id", "post"])[["lt", "lc"]].mean().unstack("post")
    out["did_log"] = (ml[("lt", True)] - ml[("lt", False)]) - (ml[("lc", True)] - ml[("lc", False)])
    return out.reset_index()


def cluster_bootstrap(df: pd.DataFrame, stat, n_boot: int, seed: int, cluster: str = "event_id"):
    """Point estimate and 95% percentile CI, resampling whole clusters."""
    rng = np.random.default_rng(seed)
    ids = df[cluster].unique()
    by = {k: v for k, v in df.groupby(cluster)}
    est = stat(df)
    draws = []
    for _ in range(n_boot):
        pick = rng.choice(ids, size=len(ids), replace=True)
        draws.append(stat(pd.concat([by[k] for k in pick], ignore_index=True)))
    draws = np.array(draws)
    return est, np.nanpercentile(draws, 2.5, axis=0), np.nanpercentile(draws, 97.5, axis=0)


def cluster_se_mean(df: pd.DataFrame, col: str, cluster: str = "event_id") -> float:
    """Analytic cluster-robust SE of a mean (CR1, same as OLS on a constant with clustered SEs)."""
    x = df[col] - df[col].mean()
    n, g = len(df), df[cluster].nunique()
    s = x.groupby(df[cluster]).sum()
    return float(np.sqrt((g / (g - 1)) * (s ** 2).sum()) / n)


def summarise(did: pd.DataFrame, cfg: Config, label: dict) -> dict:
    if did.empty:
        return {**label, "n_events": 0, "n_treated": 0}
    est, lo, hi = cluster_bootstrap(
        did, lambda d: np.array([d["did"].mean(), d["did_log"].mean(),
                                 d["did"].mean() / d["treated_pre"].mean()]), cfg.n_boot, cfg.seed)
    return {**label,
            "n_events": int(did["event_id"].nunique()), "n_treated": int(len(did)),
            "pre_mean_weekly_departures": round(float(did["treated_pre"].mean()), 2),
            "did_rides_per_week": round(float(est[0]), 2),
            "ci95_low": round(float(lo[0]), 2), "ci95_high": round(float(hi[0]), 2),
            "cluster_se": round(cluster_se_mean(did, "did"), 2),
            "did_pct_of_pre": round(100 * float(est[2]), 1),
            "pct_ci95_low": round(100 * float(lo[2]), 1), "pct_ci95_high": round(100 * float(hi[2]), 1),
            "did_log": round(float(est[1]), 4),
            "log_ci95_low": round(float(lo[1]), 4), "log_ci95_high": round(float(hi[1]), 4)}


def event_study(series: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Treated - control gap per relative week, minus the same gap at week -1, with 95% CI."""
    s = series.assign(gap=series["treated"] - series["control"])
    base = s[s["rel_week"] == -1].set_index("group_id")["gap"]
    s["gap_vs_base"] = s["gap"] - s["group_id"].map(base)
    rel = sorted(s["rel_week"].unique())

    def stat(d):
        return d.groupby("rel_week")["gap_vs_base"].mean().reindex(rel).values
    est, lo, hi = cluster_bootstrap(s[["event_id", "rel_week", "gap_vs_base"]], stat, cfg.n_boot, cfg.seed)
    return pd.DataFrame({"rel_week": rel, "estimate": est.round(2), "ci95_low": lo.round(2), "ci95_high": hi.round(2)})


def net_effect(did: pd.DataFrame, new: pd.DataFrame, dep: pd.DataFrame, cfg: Config) -> dict:
    """New station's own departures vs the change at its neighbours, per week of post-period.

    Only events that kept at least one treated neighbour are used. The DiD for each
    neighbour is a weekly change; summing it over an event's neighbours gives the event's
    total change in neighbour departures per week.
    """
    ev = new.set_index("station_id")
    own = {}
    for eid in did["event_id"].unique():
        ow = ev.loc[eid, "open_week"]
        post = [ow + pd.Timedelta(weeks=k) for k in range(1, cfg.post_weeks + 1)]
        own[eid] = float(dep.loc[eid, post].mean()) if eid in dep.index else 0.0
    per_event = did.groupby("event_id")["did"].sum().rename("neighbour_change").reset_index()
    per_event["own_departures"] = per_event["event_id"].map(own)
    return net_from_events(per_event, cfg), per_event


def net_from_events(per_event: pd.DataFrame, cfg: Config) -> dict:
    """Pooled own vs neighbour change, CI by resampling events."""
    def stat(d):
        own_t, nb = d["own_departures"].sum(), d["neighbour_change"].sum()
        return np.array([own_t / len(d), nb / len(d), (own_t + nb) / len(d), -nb / own_t if own_t else np.nan])
    est, lo, hi = cluster_bootstrap(per_event, stat, cfg.n_boot, cfg.seed)
    keys = ["own_departures_per_week", "neighbour_change_per_week", "net_change_per_week", "cannibalised_share"]
    out = {"n_events": int(len(per_event))}
    for k, e, l, h in zip(keys, est, lo, hi):
        out[k], out[k + "_ci95_low"], out[k + "_ci95_high"] = round(float(e), 3), round(float(l), 3), round(float(h), 3)
    return out


# ----------------------------------------------------------------------------- main

def exclude_non_stations(st: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split off rows whose name marks them as not a docked station (Divvy public racks,
    operator test/demo points). Returns (kept, excluded)."""
    bad = st["station_name"].fillna("").str.contains(cfg.exclude_name_regex, regex=True)
    return st[~bad].reset_index(drop=True), st[bad].reset_index(drop=True)


def stitch_replacements(p: pd.DataFrame, st: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Join each replacement ID onto the station it replaced, so a re-numbered or moved
    station keeps one continuous history (Divvy re-numbered most stations in 2025)."""
    mapping = dict(zip(new.loc[new["status"] == "replacement", "station_id"],
                       new.loc[new["status"] == "replacement", "replaces_station_id"]))
    if not mapping:
        return p, st
    p = p.assign(station_id=p["station_id"].replace(mapping))
    p = p.groupby(["system", "station_id", "week"], as_index=False)[["departures", "arrivals"]].sum()
    st = st.copy()
    last = st.set_index("station_id")["last_day"]
    for new_id, old_id in mapping.items():
        st.loc[st["station_id"] == old_id, "last_day"] = max(last[old_id], last[new_id])
    return p, st[~st["station_id"].isin(mapping)].reset_index(drop=True)


def run_system(system: str, panel: pd.DataFrame, coords: pd.DataFrame, cfg: Config, out: Path) -> dict:
    p = panel[panel["system"] == system]
    st = coords[(coords["system"] == system) & coords["station_id"].isin(p["station_id"])]
    st, excluded = exclude_non_stations(st, cfg)
    p = p[p["station_id"].isin(st["station_id"])]
    dep = wide(p, "departures")
    first_pass = classify_new_stations(st, dep, cfg)
    replaced = first_pass[first_pass["status"] == "replacement"]
    p, st = stitch_replacements(p, st, first_pass)
    dep, arr = wide(p, "departures"), wide(p, "arrivals")
    new = classify_new_stations(st, dep, cfg)
    excluded = excluded[excluded["first_day"] >= pd.Timestamp(cfg.data_start) + pd.DateOffset(months=cfg.burn_in_months)]
    new_all = pd.concat([new, replaced, excluded.assign(status="excluded_not_a_dock")], ignore_index=True)
    groups = build_groups(st, new, dep, cfg)
    res = {"new": new_all.sort_values("first_day").reset_index(drop=True), "groups": groups}
    if groups.empty:
        return res
    ser_dep, ser_arr = group_series(groups, dep, cfg), group_series(groups, arr, cfg)
    res["did_dep"], res["did_arr"] = group_did(ser_dep), group_did(ser_arr)
    res["series_dep"] = ser_dep
    res["rings"] = {}
    for ring in [(0, 250), (250, 500), (500, 1000)]:
        if ring[1] <= cfg.treat_m:  # a slice of the main design, by the treated station's distance
            t = groups[groups["role"] == "treated"]
            keep = t[(t["dist_to_new_m"] > ring[0]) & (t["dist_to_new_m"] <= ring[1])]["group_id"]
            g = groups[groups["group_id"].isin(keep)]
        else:  # beyond the treated radius: a separate design (spillover / placebo distance)
            g = build_groups(st, new, dep, cfg, ring=ring)
        res["rings"][ring] = group_did(group_series(g, dep, cfg)) if not g.empty else pd.DataFrame()
    gp = build_groups(st, new, dep, cfg, shift_weeks=cfg.placebo_shift_weeks)
    res["placebo"] = group_did(group_series(gp, dep, cfg)) if not gp.empty else pd.DataFrame()
    res["net"], res["net_events"] = net_effect(res["did_dep"], new, dep, cfg)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=REPO / "data")
    ap.add_argument("--out", type=Path, default=REPO / "analysis" / "results" / "new_stations")
    ap.add_argument("--systems", nargs="+", default=["bluebikes", "citibike", "divvy"])
    ap.add_argument("--n-boot", type=int, default=None)
    args = ap.parse_args(argv)
    cfg = Config()
    if args.n_boot:
        cfg.n_boot = args.n_boot
    args.out.mkdir(parents=True, exist_ok=True)
    panel, coords = load_panel(args.data_dir)

    summary, rings, events_all, es_all, net_all, did_all = [], [], [], [], [], []
    pooled: dict[str, list[pd.DataFrame]] = {}

    def add_pool(key, d, system):
        if d is not None and not d.empty:
            pooled.setdefault(key, []).append(d.assign(event_id=system + ":" + d["event_id"].astype(str)))
    for system in args.systems:
        print(f"{system}: building design", flush=True)
        r = run_system(system, panel, coords, cfg, args.out)
        new = r["new"].assign(system=system)
        if "did_dep" in r:
            kept = r["did_dep"].groupby("event_id").size().rename("treated_neighbours_used")
            new = new.merge(kept, left_on="station_id", right_index=True, how="left")
            new["treated_neighbours_used"] = new["treated_neighbours_used"].fillna(0).astype(int)
        events_all.append(new)
        if "did_dep" not in r:
            continue
        did_all.append(r["did_dep"].assign(system=system))
        summary.append(summarise(r["did_dep"], cfg, {"system": system, "outcome": "departures", "test": "main"}))
        summary.append(summarise(r["did_arr"], cfg, {"system": system, "outcome": "arrivals", "test": "main"}))
        summary.append(summarise(r["placebo"], cfg, {"system": system, "outcome": "departures",
                                                     "test": f"placebo_{cfg.placebo_shift_weeks}w_earlier"}))
        for ring, d in r["rings"].items():
            rings.append(summarise(d, cfg, {"system": system, "ring_m": f"{ring[0]}-{ring[1]}"}))
        es_all.append(event_study(r["series_dep"], cfg).assign(system=system))
        net_all.append({"system": system, **r["net"]})
        add_pool("departures", r["did_dep"], system)
        add_pool("arrivals", r["did_arr"], system)
        add_pool("placebo", r["placebo"], system)
        add_pool("net", r["net_events"], system)
        for ring, d in r["rings"].items():
            add_pool(f"ring_{ring[0]}-{ring[1]}", d, system)
        print(f"{system}: {summary[-3]}", flush=True)

    # All three cities together: each event counts once, CI resamples events across cities.
    # The percent effect is total change / total pre-period rides, so busy NYC stations weigh more.
    if pooled:
        cat = {k: pd.concat(v, ignore_index=True) for k, v in pooled.items()}
        summary.append(summarise(cat["departures"], cfg, {"system": "all", "outcome": "departures", "test": "main"}))
        summary.append(summarise(cat["arrivals"], cfg, {"system": "all", "outcome": "arrivals", "test": "main"}))
        if "placebo" in cat:
            summary.append(summarise(cat["placebo"], cfg, {"system": "all", "outcome": "departures",
                                                            "test": f"placebo_{cfg.placebo_shift_weeks}w_earlier"}))
        for k in [k for k in cat if k.startswith("ring_")]:
            rings.append(summarise(cat[k], cfg, {"system": "all", "ring_m": k[5:]}))
        net_all.append({"system": "all", **net_from_events(cat["net"], cfg)})
    pd.DataFrame(summary).to_csv(args.out / "did_summary.csv", index=False)
    pd.DataFrame(rings).to_csv(args.out / "did_by_distance.csv", index=False)
    pd.concat(es_all).to_csv(args.out / "event_study.csv", index=False)
    pd.DataFrame(net_all).to_csv(args.out / "net_effect.csv", index=False)
    pd.concat(did_all).round(3).to_csv(args.out / "group_did.csv", index=False)
    ev = pd.concat(events_all)
    ev["first_day"] = ev["first_day"].dt.date
    ev["last_day"] = ev["last_day"].dt.date
    ev["open_week"] = pd.to_datetime(ev["first_day"]) - pd.to_timedelta(pd.to_datetime(ev["first_day"]).dt.weekday, "D")
    ev["open_week"] = ev["open_week"].dt.date
    ev[["system", "station_id", "station_name", "lat", "lng", "first_day", "last_day", "open_week", "status",
        "replaces_station_id", "existing_within_treat_m", "treated_neighbours_used"]].round(
        {"lat": 6, "lng": 6}).to_csv(args.out / "new_stations.csv", index=False)
    (args.out / "config.json").write_text(json.dumps(asdict(cfg), indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
