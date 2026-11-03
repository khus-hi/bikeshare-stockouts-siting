"""Boston tract siting score: the Python twin of the Excel workbook.

Khushi builds the score by hand in Excel (docs/siting_excel_guide.md). This script applies
the same formulas so the workbook can be checked against it, and adds what is awkward in
Excel: a sensitivity check on the weights, the top-N list with data-backed reasons, and the
equity-gap tables.

Inputs
  analysis/results/siting/tracts_acs2024.csv          (ingest/census_download.py)
  analysis/results/siting/tract_station_distance.csv  (analysis/tract_station_distance.py)
  analysis/siting_config.json                          weights and parameters

Score (same as the Excel sheet)
  1. Eligible tracts: in Boston, population >= min_population (ACS shares from very small
     tracts have huge margins of error), and every weighted component present.
  2. Each component is min-max scaled to 0-1 across eligible Boston tracts:
        z = (x - min) / (max - min)
     Station gap uses distance capped at distance_cap_m, so one remote tract does not squash
     every other tract toward 0. Low income is reversed: z = (max - x) / (max - min).
  3. score = SUMPRODUCT(z, weights); rank 1 = highest score.

Outputs (analysis/results/siting/)
  boston_siting_scores.csv   every Boston tract with z-scores, score, rank, flags
  boston_siting_summary.md   top N with reasons, sensitivity, equity gaps

Usage (from the repo root):
  python analysis/siting_score.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
RES = REPO / "analysis" / "results" / "siting"
CONFIG = REPO / "analysis" / "siting_config.json"

# component -> (source column, direction). "up": more is better for a new station.
COMPONENTS = {
    "pop_density": ("pop_density_sq_mi", "up"),
    "commute_transit_bike_walk": ("share_commute_transit_bike_walk", "up"),
    "no_vehicle": ("share_households_no_vehicle", "up"),
    "age_18_34": ("share_18_34", "up"),
    "station_gap": ("dist_capped_m", "up"),
    "low_income": ("median_household_income", "down"),
}

LABELS = {
    "pop_density": "population density",
    "commute_transit_bike_walk": "share of commuters using transit, bike or foot",
    "no_vehicle": "share of households without a car",
    "age_18_34": "share of residents aged 18-34",
    "station_gap": "distance to the nearest Bluebikes station",
    "low_income": "low median household income",
}


def load_config(path: Path = CONFIG) -> dict:
    cfg = json.loads(path.read_text())
    w = cfg["weights"]
    unknown = set(w) - set(COMPONENTS)
    if unknown:
        raise ValueError(f"Unknown weight names in config: {sorted(unknown)}")
    if not np.isclose(sum(w.values()), 1.0):
        raise ValueError(f"Weights must sum to 1, got {sum(w.values())}")
    return cfg


def minmax(x: pd.Series, direction: str = "up") -> pd.Series:
    lo, hi = x.min(), x.max()
    if hi == lo:
        return pd.Series(0.0, index=x.index)
    return (x - lo) / (hi - lo) if direction == "up" else (hi - x) / (hi - lo)


def score_tracts(df: pd.DataFrame, weights: dict, min_population: float, distance_cap_m: float) -> pd.DataFrame:
    """Add eligible, z_<component>, score and rank columns. Pure function (tested)."""
    df = df.copy()
    df["dist_capped_m"] = np.minimum(df["dist_nearest_station_m"], distance_cap_m)
    # Same rule as the Excel Eligible column: the five core factors must be present.
    # Income may be missing (its z counts as 0), so switching on the income weight
    # does not change which tracts are eligible.
    core = [k for k in COMPONENTS if k != "low_income"]
    present = df[[COMPONENTS[k][0] for k in core]].notna().all(axis=1)
    df["eligible"] = (df["population"] >= min_population) & present
    el = df["eligible"]
    for k, (col, direction) in COMPONENTS.items():
        df[f"z_{k}"] = np.nan
        df.loc[el, f"z_{k}"] = minmax(df.loc[el, col], direction)
    z = df.loc[el, [f"z_{k}" for k in weights]].fillna(0).to_numpy()
    df["score"] = np.nan
    df.loc[el, "score"] = z @ np.array([weights[k] for k in weights])
    df["rank"] = df["score"].rank(ascending=False, method="min").astype("Int64")
    return df


def leave_one_out(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Re-score with each weighted component dropped (others reweighted equally)."""
    used = [k for k, v in cfg["weights"].items() if v > 0]
    n = cfg["top_n"]
    base = score_tracts(df, cfg["weights"], cfg["min_population"], cfg["distance_cap_m"])
    base_top = set(base.nsmallest(n, "rank")["geoid"])
    rows, ranks = [], {"base": base.set_index("geoid")["rank"]}
    for drop in used:
        keep = [k for k in used if k != drop]
        w = {k: (1 / len(keep) if k in keep else 0.0) for k in cfg["weights"]}
        s = score_tracts(df, w, cfg["min_population"], cfg["distance_cap_m"])
        top = set(s.nsmallest(n, "rank")["geoid"])
        rows.append({"scenario": f"drop {LABELS[drop]}", "top_n_kept": len(base_top & top)})
        ranks[f"drop_{drop}"] = s.set_index("geoid")["rank"]
    return pd.DataFrame(rows), pd.DataFrame(ranks)


def pct_rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True)


def reasons(row: pd.Series, pct: pd.DataFrame) -> str:
    """Plain-English reason from the tract's own numbers and where they sit among Boston tracts."""
    g = row["geoid"]
    parts = []
    if pct.loc[g, "dist_nearest_station_m"] >= 0.75:
        parts.append(f"{row['dist_nearest_station_m']:,.0f} m to the nearest station "
                     f"(among the farthest {max(1 - pct.loc[g, 'dist_nearest_station_m'], 0.01):.0%} of tracts)")
    elif row["stations_within_400m"] == 0:
        parts.append(f"no station within 400 m (nearest {row['dist_nearest_station_m']:,.0f} m)")
    for col, fmt in [
        ("pop_density_sq_mi", "{:,.0f} people/sq mi"),
        ("share_households_no_vehicle", "{:.0%} of households car-free"),
        ("share_commute_transit_bike_walk", "{:.0%} of commuters ride transit, bike or walk"),
        ("share_18_34", "{:.0%} aged 18-34"),
    ]:
        if pct.loc[g, col] >= 0.75:
            parts.append(fmt.format(row[col]) + f" (top {max(1 - pct.loc[g, col], 0.01):.0%} of tracts)")
    if not parts:
        parts.append("above-average on several factors without topping any one")
    return "; ".join(parts)


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def main() -> None:
    cfg = load_config()
    tracts = pd.read_csv(RES / "tracts_acs2024.csv", dtype={"geoid": str, "county_fips": str})
    dist = pd.read_csv(RES / "tract_station_distance.csv", dtype={"geoid": str})
    df = tracts[tracts["city"] == cfg["city"]].merge(
        dist.drop(columns="city"), on="geoid", how="left", validate="one_to_one")
    if df["dist_nearest_station_m"].isna().any():
        raise SystemExit("Some tracts have no station distance; re-run analysis/tract_station_distance.py")

    s = score_tracts(df, cfg["weights"], cfg["min_population"], cfg["distance_cap_m"])
    el = s[s["eligible"]].copy()
    radius = cfg["walk_radius_m"]
    low_cut = el["median_household_income"].quantile(cfg["low_income_quantile"])
    s["low_income"] = s["eligible"] & (s["median_household_income"] <= low_cut)
    s["poor_coverage"] = s["eligible"] & (s[f"stations_within_{radius}m"] == 0)
    s["equity_gap"] = s["low_income"] & s["poor_coverage"]
    s.sort_values(["rank", "geoid"]).to_csv(RES / "boston_siting_scores.csv", index=False)

    el = s[s["eligible"]].copy()
    pct = el.set_index("geoid")[["dist_nearest_station_m", "pop_density_sq_mi", "share_households_no_vehicle",
                                 "share_commute_transit_bike_walk", "share_18_34"]].apply(pct_rank)
    n = cfg["top_n"]
    top = el.nsmallest(n, "rank").copy()
    top["reason"] = top.apply(lambda r: reasons(r, pct), axis=1)
    sens, ranks = leave_one_out(df, cfg)
    top["rank_range_in_sensitivity"] = top["geoid"].map(
        lambda g: f"{int(ranks.loc[g].min())}-{int(ranks.loc[g].max())}")

    # New-station candidates: the same score, but only tracts with no station within the
    # walk radius. The overall top N turned out to be dense tracts that already have several
    # stations nearby (high demand, not a coverage gap), so "where to add a NEW station" is
    # answered from the uncovered tracts. The overall list stays as a demand/capacity view.
    cand = el[el["poor_coverage"]].nsmallest(n, "rank").copy()
    cand["reason"] = cand.apply(lambda r: reasons(r, pct), axis=1)
    cand["rank_range_in_sensitivity"] = cand["geoid"].map(
        lambda g: f"{int(ranks.loc[g].min())}-{int(ranks.loc[g].max())}")
    cand["candidate_rank"] = range(1, len(cand) + 1)

    # Equity: coverage among low-income tracts vs the rest
    grp = el.assign(group=np.where(el["low_income"], f"Low income (bottom {cfg['low_income_quantile']:.0%})",
                                   "All other eligible tracts"))
    eq = grp.groupby("group").agg(
        tracts=("geoid", "size"),
        population=("population", "sum"),
        median_dist_m=("dist_nearest_station_m", "median"),
        share_no_station_400m=("poor_coverage", "mean"),
    ).reset_index()
    eq["population"] = eq["population"].map("{:,.0f}".format)
    eq["median_dist_m"] = eq["median_dist_m"].map("{:,.0f}".format)
    eq["share_no_station_400m"] = eq["share_no_station_400m"].map("{:.0%}".format)
    gaps = el[el["equity_gap"]].sort_values("dist_nearest_station_m", ascending=False)

    w = ", ".join(f"{LABELS[k]} {v:g}" for k, v in cfg["weights"].items() if v > 0)
    md = [
        "# Boston siting score: results",
        "",
        "Generated by `analysis/siting_score.py` from ACS 2020-2024 5-year tract estimates and GBFS "
        f"station locations (snapshot {df['stations_snapshot_utc'].iloc[0]}). Straight-line distances.",
        "",
        f"Eligible tracts: {len(el)} of {len(s)} Boston tracts (population >= {cfg['min_population']}). "
        f"Weights: {w}. Distance capped at {cfg['distance_cap_m']:,} m before scaling.",
        "",
        f"## Top {n} new-station candidates (no station within {radius} m)",
        "",
        f"{int(el['poor_coverage'].sum())} eligible tracts have no station within {radius} m. Ranked by the same score.",
        "",
        md_table(cand.assign(
            score=cand["score"].round(3),
            income=cand["median_household_income"].map(lambda v: "n/a" if pd.isna(v) else f"${v:,.0f}"),
        )[["candidate_rank", "rank", "geoid", "neighborhood", "score", "income", "reason", "rank_range_in_sensitivity"]]),
        "",
        f"## Top {n} tracts overall (demand view; these already have stations nearby)",
        "",
        md_table(top.assign(
            score=top["score"].round(3),
            income=top["median_household_income"].map(lambda v: "n/a" if pd.isna(v) else f"${v:,.0f}"),
            stations_400m=top[f"stations_within_{radius}m"],
        )[["rank", "geoid", "neighborhood", "score", "income", "stations_400m", "reason", "rank_range_in_sensitivity"]]),
        "",
        "## Sensitivity (leave one component out, others reweighted equally)",
        "",
        md_table(sens.assign(top_n_kept=sens["top_n_kept"].map(lambda k: f"{k} of {n}"))),
        "",
        f"## Equity gaps (low income = bottom {cfg['low_income_quantile']:.0%} of eligible tracts by median "
        f"household income, at or below ${low_cut:,.0f}; poor coverage = no station within {radius} m)",
        "",
        md_table(eq),
        "",
        f"{len(gaps)} low-income tracts have no station within {radius} m:",
        "",
        md_table(gaps.assign(
            income=gaps["median_household_income"].map("${:,.0f}".format),
            no_car=gaps["share_households_no_vehicle"].map("{:.0%}".format),
            dist_m=gaps["dist_nearest_station_m"].map("{:,.0f}".format),
            population=gaps["population"].map("{:,.0f}".format),
        )[["geoid", "neighborhood", "population", "income", "no_car", "dist_m", "rank"]]) if len(gaps) else "(none)",
        "",
    ]
    (RES / "boston_siting_summary.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
