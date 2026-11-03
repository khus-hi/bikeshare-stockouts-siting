"""Charts for the new-station study, from the CSVs written by new_station_did.py.

  python analysis/plot_new_station_did.py
writes analysis/results/new_stations/event_study.png and did_by_distance.png
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

OUT = Path(__file__).resolve().parent / "results" / "new_stations"
BLUE, INK, INK2, GRID, SURFACE = "#2a78d6", "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
CITY = {"bluebikes": "Boston (Bluebikes)", "citibike": "New York (Citi Bike)", "divvy": "Chicago (Divvy)",
        "all": "All three cities"}


def style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.axhline(0, color=INK2, linewidth=1)


def event_study():
    es = pd.read_csv(OUT / "event_study.csv")
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), sharey=False, facecolor=SURFACE)
    for ax, sys_ in zip(axes, ["bluebikes", "citibike", "divvy"]):
        d = es[es["system"] == sys_]
        style(ax)
        ax.fill_between(d["rel_week"], d["ci95_low"], d["ci95_high"], color=BLUE, alpha=0.18, linewidth=0)
        ax.plot(d["rel_week"], d["estimate"], color=BLUE, linewidth=2, marker="o", markersize=4)
        ax.axvline(0, color=INK2, linewidth=1, linestyle="--")
        ax.set_title(CITY[sys_], color=INK, fontsize=11, loc="left")
        ax.set_xlabel("weeks since the new station opened", color=INK2, fontsize=9)
    axes[0].set_ylabel("departures/week vs. matched controls\n(relative to week -1)", color=INK2, fontsize=9)
    fig.suptitle("Existing stations within 500 m of a new station: event study with 95% CI", color=INK,
                 fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT / "event_study.png", dpi=150, facecolor=SURFACE)


def rings():
    r = pd.read_csv(OUT / "did_by_distance.csv")
    order = ["0-250", "250-500", "500-1000"]
    fig, axes = plt.subplots(1, 4, figsize=(13, 3.6), sharey=True, facecolor=SURFACE)
    for ax, sys_ in zip(axes, ["bluebikes", "citibike", "divvy", "all"]):
        d = r[r["system"] == sys_].set_index("ring_m").reindex(order)
        style(ax)
        x = range(len(order))
        ax.errorbar(x, d["did_pct_of_pre"], yerr=[d["did_pct_of_pre"] - d["pct_ci95_low"],
                                                   d["pct_ci95_high"] - d["did_pct_of_pre"]],
                    fmt="o", color=BLUE, markersize=8, capsize=4, linewidth=2)
        ax.set_xticks(list(x), [o + " m" for o in order])
        ax.set_xlim(-0.5, 2.5)
        ax.set_title(CITY[sys_], color=INK, fontsize=11, loc="left")
    axes[0].set_ylabel("% change in departures\n(DiD, 95% CI)", color=INK2, fontsize=9)
    fig.suptitle("Effect of a new station by distance from it", color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT / "did_by_distance.png", dpi=150, facecolor=SURFACE)


if __name__ == "__main__":
    event_study()
    rings()
    print("wrote", OUT / "event_study.png", OUT / "did_by_distance.png")
