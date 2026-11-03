# Pitch outline: City of Boston Analytics Team (6 slides)

Audience: City of Boston Analytics Team, who work on Bluebikes station siting.
Length: 10 minutes + questions. One message per slide; the dashboard is the appendix.

Every number below comes from a committed file, named in brackets. Numbers marked
**[PENDING]** don't exist yet; fill them from the named export when it does, or drop the
bullet. Don't round them into something they aren't.

---

## Slide 1. The problem: an empty dock is a ride nobody records

**Headline:** "Bluebikes' trip data can't see the rides it loses. I measured them."

* Bluebikes logged 4,573,597 trips from October 2025 to September 2026 across ~630 stations
  [analysis/README.md trip table; 629 stations in the 2026-11-03 GBFS snapshot,
  analysis/results/siting/boston_siting_findings.md].
* When a station is empty or full, the would-be rider leaves no record, so trip data alone
  undercounts demand exactly where service fails.
* Two decisions depend on that hidden demand: **where to rebalance today** and **where to
  build next**.
* Visual: one station's bikes-available line over a weekday, dropping to 0 at the morning
  peak (from the GBFS snapshots once collection finishes).

Speaker note: "I collected live station status every 5 minutes myself, because operators
don't publish history of it."

## Slide 2. What I built (method in one picture)

**Headline:** "Public data only, rebuilt from scratch, every step tested."

* Pipeline diagram: GBFS feed every 5 min → change-only Parquet → DuckDB views
  (empty/full minutes, lost rides) + 12 months of trip history → LightGBM forecast →
  rebalancing list; ACS 2020–2024 tracts → siting score.
* Three systems (Boston, NYC, Chicago) in one format, so Boston can be benchmarked.
* All free tools, all code public: github.com/khus-hi/bikeshare-stockouts-siting.

Speaker note: keep this to 45 seconds; it's credibility, not the point.

## Slide 3. Findings: where and when stations fail

**Headline:** "[PENDING] stations account for [PENDING]% of Boston's empty and full hours."

* Station-hours empty and full over the 21-day window, per 100 stations per day
  **[PENDING, problem_stations.csv, after ~2026-11-23]**.
* Estimated rides lost to empty stations, with its range
  **[PENDING, problem_stations.csv `est_lost_rides`, `_low`, `_high`]**. Say it's
  conservative: demand is estimated from Nov 2025 trips, which already miss empty hours.
* Top 5 problem stations at weekday 7–9am and 4–7pm **[PENDING, peak_problem_stations.csv]**.
* Visual: the dashboard heatmap (stations × hour of day).
* Caveat on the slide: one November window, not a full year.

## Slide 4. Findings: demand can be forecast, and the core is covered

**Headline:** "The forecast beats 'same hour last week' at 89% of stations, and Boston's
siting gaps sit at the edges, not the core."

Forecast [analysis/results/netflow_metrics.csv; dashboard/exports/forecast_station_accuracy.csv],
September 2026 held-out month, 618 stations:
* Station-hour departures error (WAPE) **64.7%** vs **85.2%** for the baseline.
* 3-hour net-flow error **1.60 bikes** vs **2.20** per station, the quantity a rebalancing
  crew acts on.
* Better than the baseline at **550 of 618 stations (89%)**.
* City-wide hourly departures error **14.1%** vs **25.6%** (system-hour WAPE; label it as such).

Siting [analysis/results/siting/boston_siting_findings.md]:
* Half of Boston tracts have a station within **236 m**; **154 of 189** populated tracts have
  one within 400 m.
* The 15 highest-demand tracts (Fenway, Downtown, North End, Beacon Hill) already have
  **2 to 10 stations** within 400 m each: they need docks and rebalancing, not new stations.
* **35** populated tracts have no station within 400 m. Top 3: North End (25025030500),
  Brighton (25025000506), Mission Hill (25025081102); they stay in the top 51 under every
  leave-one-out weighting.
* Equity: low-income tracts are **better** covered (**13%** lack a station within 400 m vs
  **20%** elsewhere). Six low-income tracts still do; the clearest is Roxbury 25025082100
  (median income **$29,719**, **50%** of households car-free).

Visual: dashboard siting map (left) + forecast vs actual line for one week (right).

## Slide 5. Recommendation

**Headline:** "Fix capacity in the core, extend coverage at the edges, and test before scaling."

1. **Core (Fenway, Downtown, North End):** add docks or more frequent rebalancing at the
   [PENDING] top problem stations; use the 3-hour forecast list (`models/rebalance.py`) to
   send crews before a station empties.
2. **New stations:** start with the top 3 candidates plus Roxbury 25025082100 (equity).
   Check each on a walking-network map first: 11 of the top 15 are only 400–500 m
   (straight line) from a station.
3. **West Roxbury / Hyde Park gaps (1.2–2.2 km):** a coverage-vs-ridership policy call.
   Show both rankings (income weight 0 vs >0) and let the city decide.
4. **New stations add rides; they don't just move them.** Boston, 49 new-station openings,
   123 existing stations nearby [analysis/results/new_stations/, PR #6]:
   * Each new station drew **102 departures/week** of its own (95% CI 80 to 128).
   * Existing stations within 500 m changed by **-8.3 departures/week, -3.4%**
     (95% CI -7.1% to +1.4%), not statistically different from zero on that scale
     [did_summary.csv, system=bluebikes, outcome=departures, test=main].
   * Net: **+82 rides/week per new station** (95% CI +45 to +121); about **20%** of a new
     station's rides come from neighbours (CI -7% to 52%) [net_effect.csv].
   * The loss is concentrated within 250 m: **-7.0%** (CI -13.9% to +1.5%); 250-500 m is
     -0.9% [did_by_distance.csv]. So space new stations more than 250 m from existing ones.
   * Placebo (same test 26 weeks earlier): +0.5% (CI -2.6% to +4.5%), so the design isn't
     finding effects where there shouldn't be any.
   * Honest caveat: on the log scale the 500 m effect is a small, significant decline
     (did_log -0.077, CI -0.134 to -0.024). Say "a small loss nearby, far outweighed by
     the new station's own rides".

## Slide 6. What I'd do with your internal data

**Headline:** "Public data got this far. Four internal sources would make it decision-grade."

Ask for whichever of these the city or operator actually holds (I don't know which do):
* **Rebalancing logs** (truck pick-ups and drop-offs by station and time): separates "empty
  because nobody refilled it" from "empty despite refills", and measures how fast crews respond.
* **Planned and rejected station sites, with reasons** (permits, sidewalk width, curb use):
  turns the tract score into a ranked list of feasible street locations.
* **Station install and removal history**: an exact opening date per station would sharpen
  the new-station experiment beyond "first trip in the public files".
* **Rider requests and 311 complaints about empty/full stations**: a direct check on the
  lost-rides estimate.
* Proposal: a 4-week pilot. Rebalance the top problem stations from the forecast list,
  compare stockout hours against matched stations, and report back with the same dashboard.

Close: "Here's the live dashboard link; the code and every number trace back to the repo."

---

## Before presenting

* Re-run `python analysis/build_views.py && python dashboard/export_tables.py` after the
  collector stops (~2026-11-23) and fill every [PENDING] from the exports.
* Experiment numbers above were read from PR #6's `analysis/results/new_stations/` files
  on 2026-11-03; re-check them if that PR changes before it merges.
* Requirements doc assumption still applies: the Analytics Team's interest is as stated by the
  project sponsor; confirm the right contact before sending.
