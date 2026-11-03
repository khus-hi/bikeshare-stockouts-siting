"""Static PNG charts from the export tables, for the README, the pitch deck and interviews.

No BI tool needed: every chart is drawn with matplotlib from dashboard/exports/*.csv, which
dashboard/export_tables.py builds from the repo's tested SQL and model outputs. No number
is typed in here.

Charts (dashboard/charts/):
  forecast_week_boston.png     one week of Boston departures: actual vs forecast vs last week
  forecast_error_by_city.png   station-hour WAPE, model vs baseline, per city
  siting_map_boston.png        Boston tracts: covered / new-station candidate / equity gap
  new_station_effect.png       change at existing stations by distance from a new one (95% CI)
  stockout_heatmap_boston.png  top 25 stations x hour of day, share of time empty or full  [needs snapshots]
  problem_stations_boston.png  top 15 stations by hours empty and full                      [needs snapshots]

The last two need the stockout exports, which only exist on the machine running the GBFS
collector; each chart is skipped with a message when its CSV is missing.

Usage (repo root):
  python dashboard/export_tables.py      # refresh the CSVs first
  python dashboard/make_charts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

DASH = Path(__file__).resolve().parent
EXPORTS = DASH / "exports"
CHARTS = DASH / "charts"

# Validated categorical slots 1-3 (blue, orange, aqua) plus neutrals; text stays in ink.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK_2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#a3a29c", "#e6e5e0", "#fcfcfb"
SEQ_BLUE = LinearSegmentedColormap.from_list("seq_blue", ["#f4f8fd", "#a9c8ef", "#2a78d6", "#0d3b73"])

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK_2,
    "xtick.color": INK_2, "ytick.color": INK_2, "axes.titlesize": 12, "axes.titleweight": "bold",
    "axes.titlecolor": INK, "axes.titlelocation": "left",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "legend.frameon": False,
})


def read(name: str) -> pd.DataFrame | None:
    path = EXPORTS / f"{name}.csv"
    if not path.exists():
        print(f"  skipped: {path.relative_to(DASH.parent)} not found")
        return None
    return pd.read_csv(path)


def save(fig, name: str, source: str) -> None:
    fig.text(0.01, -0.03, f"Source: {source}", fontsize=8, color=INK_2, ha="left", va="top")
    CHARTS.mkdir(exist_ok=True)
    fig.savefig(CHARTS / name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  {name}")


def forecast_week() -> None:
    h = read("forecast_vs_actual_hourly")
    if h is None:
        return
    b = h[h.city == "boston"].copy()
    b["t"] = pd.to_datetime(b.hour_local_ts)
    # First full Monday-Sunday week of the test month, so the chart shows a normal week.
    start = b.t[b.t.dt.dayofweek.eq(0) & b.t.dt.hour.eq(0)].min()
    w = b[(b.t >= start) & (b.t < start + pd.Timedelta(days=7))]
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot(w.t, w.naive_departures, color=MUTED, lw=1.5, ls=(0, (4, 3)), label="Same hour last week (baseline)")
    ax.plot(w.t, w.actual_departures, color=INK_2, lw=2, label="Actual")
    ax.plot(w.t, w.forecast_departures, color=BLUE, lw=2, label="LightGBM forecast")
    ax.set_ylabel("Departures per hour, all stations")
    ax.set_ylim(bottom=0)
    ax.grid(axis="x", visible=False)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%a %b %d"))
    ax.legend(loc="upper left", ncols=3, bbox_to_anchor=(0, 1.0))
    ax.set_title(f"Boston: hourly departures, week of {start:%b %d, %Y} (held-out test month)", pad=24)
    save(fig, "forecast_week_boston.png", "Bluebikes trip history; models/train_netflow.py saved models")


def forecast_error() -> None:
    m = read("forecast_metrics")
    if m is None:
        return
    m = m[(m.variant == "with_weather") & (m.metric == "departures_wape")]
    p = m.pivot_table(index="city", columns="forecast", values="value")
    p = p.loc[[c for c in ("boston", "nyc", "chicago") if c in p.index]]
    names = {"boston": "Boston", "nyc": "New York", "chicago": "Chicago"}
    y = np.arange(len(p))
    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.barh(y - 0.19, p.lightgbm, height=0.36, color=BLUE, label="LightGBM forecast")
    ax.barh(y + 0.19, p.seasonal_naive, height=0.36, color=MUTED, label="Baseline: same hour last week")
    for i, (base, model) in enumerate(zip(p.seasonal_naive, p.lightgbm)):
        ax.text(model + 1, i - 0.19, f"{model:.1f}%", va="center", color=INK, fontsize=9)
        ax.text(base + 1, i + 0.19, f"{base:.1f}%", va="center", color=INK_2, fontsize=9)
    ax.set_yticks(y, [names[c] for c in p.index])
    ax.invert_yaxis()
    ax.set_xlim(0, max(p.seasonal_naive.max(), p.lightgbm.max()) * 1.15)
    ax.set_xlabel("Station-hour departures error, WAPE (lower is better)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=2)
    ax.set_title("Forecast error by city, Sep 2026 test month", pad=28)
    save(fig, "forecast_error_by_city.png", "analysis/results/netflow_metrics.csv")


def siting_map() -> None:
    s = read("siting_tracts")
    if s is None:
        return
    groups = [("Covered (station within 400 m)", MUTED, s.map_group == "Covered"),
              ("New-station candidate", BLUE, s.map_group == "New-station candidate"),
              ("Equity gap (low income, no station)", ORANGE, s.map_group.str.startswith("Equity"))]
    fig, ax = plt.subplots(figsize=(8, 8.5))
    scale = 120 / s.population.max()
    for label, color, mask in groups:
        d = s[mask]
        ax.scatter(d.lon, d.lat, s=d.population * scale + 8, color=color, alpha=0.85,
                   edgecolors=SURFACE, linewidths=1, label=f"{label} ({mask.sum()})", zorder=3)
    top = s[s.candidate_rank.between(1, 5)].sort_values("candidate_rank")
    for r in top.itertuples():
        ax.annotate(f"#{int(r.candidate_rank)} {r.neighborhood}", (r.lon, r.lat), xytext=(6, 4),
                    textcoords="offset points", fontsize=8.5, color=INK, zorder=4)
    ax.set_aspect(1 / np.cos(np.radians(s.lat.mean())))
    ax.set_xticks([]), ax.set_yticks([])
    ax.grid(False)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.01), title="Census tract (dot size = population)",
              title_fontsize=9, fontsize=9, alignment="left")
    ax.set_title("Boston: where a new Bluebikes station would close a gap")
    save(fig, "siting_map_boston.png",
         "ACS 2020-2024 5-yr tracts; GBFS station locations; analysis/siting_score.py. Points are tract centres.")


def experiment() -> None:
    d = read("experiment_did_by_distance")
    if d is None:
        return
    rings = ["0-250", "250-500", "500-1000"]
    series = [("bluebikes", "Boston", BLUE), ("all", "All 3 cities", ORANGE)]
    x = np.arange(len(rings))
    fig, ax = plt.subplots(figsize=(8, 4))
    for k, (sys_, label, color) in enumerate(series):
        r = d[d.system == sys_].set_index("ring_m").reindex(rings)
        off = (k - 0.5) * 0.36
        err = [r.did_pct_of_pre - r.pct_ci95_low, r.pct_ci95_high - r.did_pct_of_pre]
        ax.bar(x + off, r.did_pct_of_pre, width=0.32, color=color, label=label, zorder=3)
        ax.errorbar(x + off, r.did_pct_of_pre, yerr=err, fmt="none", ecolor=INK_2, elinewidth=1.2,
                    capsize=4, zorder=4)
        for xi, v, lo, hi in zip(x + off, r.did_pct_of_pre, r.pct_ci95_low, r.pct_ci95_high):
            # label beyond the end of the error bar so the two never collide
            ax.text(xi, hi + 0.5 if v >= 0 else lo - 0.5, f"{v:+.1f}%", ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=8.5, color=INK)
    ax.axhline(0, color=INK_2, lw=1)
    ax.margins(y=0.12)
    ax.set_xticks(x, [f"{r} m" for r in rings])
    ax.set_xlabel("Distance from the new station to an existing station")
    ax.set_ylabel("Change in weekly departures (%)")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper left")
    ax.set_title("What a new station does to existing stations nearby (95% CI)")
    save(fig, "new_station_effect.png", "analysis/new_station_did.py (stacked diff-in-diff)")


def stockout_heatmap() -> None:
    h = read("stockout_heatmap")
    if h is None:
        return
    b = h[(h.city == "boston") & (h.day_type == "weekday")]
    if b.empty:
        print("  skipped: no Boston weekday rows in stockout_heatmap.csv")
        return
    g = b.groupby(["station_name", "hour_of_day"])[["empty_hours", "full_hours", "observed_hours"]].sum()
    share = ((g.empty_hours + g.full_hours) / g.observed_hours).unstack("hour_of_day").reindex(columns=range(24))
    lost = b.groupby("station_name").est_lost_rides.sum()
    top = lost.sort_values(ascending=False).head(25).index
    share = share.loc[[s for s in top if s in share.index]]
    fig, ax = plt.subplots(figsize=(11, 8))
    im = ax.imshow(share.to_numpy() * 100, cmap=SEQ_BLUE, vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(24), [f"{h}" for h in range(24)])
    ax.set_yticks(range(len(share)), share.index, fontsize=8.5)
    ax.set_xlabel("Hour of day (local)")
    ax.grid(False)
    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    cb.set_label("% of observed time empty or full", color=INK_2)
    period = f"{pd.to_datetime(b.first_hour_local).min():%b %d} to {pd.to_datetime(b.last_hour_local).max():%b %d, %Y}"
    ax.set_title(f"Boston weekdays: when the 25 stations losing the most rides are empty or full ({period})")
    save(fig, "stockout_heatmap_boston.png", "self-collected Bluebikes GBFS snapshots every 5 min; analysis/sql/stockout_views.sql")


def problem_stations() -> None:
    p = read("problem_stations")
    if p is None:
        return
    b = p[p.city == "boston"].sort_values("rank_stockout_hours").head(15).iloc[::-1]
    fig, ax = plt.subplots(figsize=(9, 6))
    y = np.arange(len(b))
    ax.barh(y, b.station_hours_empty, color=BLUE, height=0.7, label="Hours empty (no bikes)",
            edgecolor=SURFACE, linewidth=2, zorder=3)
    ax.barh(y, b.station_hours_full, left=b.station_hours_empty, color=ORANGE, height=0.7,
            label="Hours full (no docks)", edgecolor=SURFACE, linewidth=2, zorder=3)
    ax.set_yticks(y, b.station_name, fontsize=8.5)
    ax.set_xlabel("Station-hours")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right")
    period = f"{pd.to_datetime(b.period_from_local).min():%b %d} to {pd.to_datetime(b.period_to_local).max():%b %d, %Y}"
    ax.set_title(f"Boston: 15 stations empty or full the longest ({period})")
    save(fig, "problem_stations_boston.png", "self-collected Bluebikes GBFS snapshots; analysis/sql/stockout_views.sql")


def main() -> int:
    for f in (forecast_week, forecast_error, siting_map, experiment, stockout_heatmap, problem_stations):
        f()
    return 0


if __name__ == "__main__":
    sys.exit(main())
