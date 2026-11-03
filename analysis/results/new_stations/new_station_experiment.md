# Do new stations add rides or cannibalize nearby ones?

An experiment-style write-up of a stacked difference-in-differences study on official trip
history for Bluebikes (Boston), Citi Bike (New York) and Divvy (Chicago).

Every number below comes from the CSVs in this folder, written by
`analysis/new_station_did.py` (run 2026-11-03). Charts: `event_study.png`, `did_by_distance.png`.

## 1. Answer first

**New stations mostly add rides.** Across 165 openings in three cities, existing stations
within 500 m changed by **-0.8%** in weekly departures relative to matched controls (95% CI
-4.1% to +2.8%). Meanwhile each new station averaged **172 departures a week** of its own.
The net change for the area is **+160 departures a week per new station** (95% CI +105 to
+223). The estimated share of the new station's rides taken from neighbours within 500 m is
7% (95% CI -24% to +35%).

**Cannibalization is real but local.** It shows up only at stations very close by. Existing
stations **within 250 m** lost **7.3%** (95% CI -11.7% to -3.4%, all cities). Stations 250 to
500 m away show no measurable change (+1.1%, CI -2.3% to +4.5%).

**Boston, the stakeholder city:** 49 openings and 123 existing neighbours. The neighbours
changed by -3.4% (CI -7.1% to +1.4%), or -8.3 departures a week (CI -19.3 to +3.0). On the
log scale, which weights small stations more, the change is -7.4% and its CI excludes zero
(log change -0.077, CI -0.134 to -0.024). So Boston leans toward mild cannibalization. Even
so, a new station's own 102 departures a week (CI 80 to 128) far outweigh it, and the net
change is **+82 a week** (CI +45 to +121). For siting, this argues against placing new
stations within about 250 m of a busy existing one. It does not argue against filling gaps
of 250 m or more.

## 2. Question and hypotheses

* **Question.** When a station opens, how does ridership change at existing stations
  within 500 m, compared with what would have happened without it?
* **H0 (no spillover).** The change at nearby stations equals the change at matched far-away
  stations (DiD = 0).
* **H1a (cannibalization).** DiD < 0: riders switch to the new station.
* **H1b (network effect).** DiD > 0: a denser network creates trips that start or end nearby.
* **Decision metric.** The net area effect: the new station's own departures plus the change
  at its neighbours.

## 3. Data

| Item | Value |
|---|---|
| Source | Operators' official monthly trip files (public S3: hubway-data, tripdata, divvy-tripdata) |
| Months | 2024-04 to 2026-09 (30 months; 2026-09 is the latest published) |
| Table used | Station x day departures and arrivals (`ingest/station_daily.py`) |
| Unit of analysis | Station x week (Monday to Sunday, local time); only full weeks are used |
| Outcome | Weekly departures (trips started). Arrivals are a secondary outcome |

Station identity uses the station code in the trip files, and coordinates are the median trip
coordinate. Dockless Divvy trips with no station are not counted.

## 4. Design

**Detecting openings.** A station counts as new if its first departure falls between
2025-04-01 and 2026-09-30, the last 18 months. The 12 months before that are a burn-in, so a
station that was only shut for the winter is not counted as new. Every detected station is
listed in `new_stations.csv` with its status:

| Status | Boston | New York | Chicago | Meaning |
|---|---:|---:|---:|---|
| analysed | 76 | 171 | 154 | A permanent new station with a full 12-week post window |
| ...with ≥1 usable neighbour | 49 | 41 | 75 | These enter the DiD |
| replacement | 2 | 32 | 1,088 | An old station within 150 m stopped within 60 days, so it is a move or a new ID, not new capacity. Its history is joined to the old ID |
| too_recent | 16 | 96 | 53 | Opened after early July 2026, so 12 post-weeks are not available yet |
| temporary | 3 | 3 | 31 | Departures in fewer than half of its first 12 weeks |
| excluded_not_a_dock | 0 | 3 | 781 | Divvy "Public Rack" e-bike parking, event "Corral" valets, charging and test/demo points |

Divvy renumbered almost its whole network in mid-2025, which is where the 1,088 replacements
come from. Without the replacement rule, each of those would have looked like a new station.
In New York, 106 of the 171 analysed openings had no existing station within 500 m: they
were expansion into areas without stations, so they have no neighbours to study.

**Event window.** 12 weeks before and 12 weeks after the opening week. The opening week
itself is dropped, because it is partly before and partly after.

**Treated units.** Existing stations within 500 m of the new station that meet all of these:
* opened before the window and still open after it;
* had rides in at least 80% of pre-weeks and 80% of post-weeks, which drops seasonal and
  closed stations;
* had no other new station open within 500 m of them during the same window. Two openings
  close together in time can't be told apart.

**Controls.** Existing stations 2 to 5 km from the new station that meet all of these:
* far enough that riders are unlikely to switch, and near enough to share weather, season and
  citywide trends;
* open for the whole window, and at least 1 km from any station that opened during it.

Each treated station is matched to the **3 nearest controls** by Mahalanobis distance on two
features: its pre-period level (log mean weekly departures) and its pre-period trend (log
change from the first half to the second half of the pre-window). Matching is with
replacement.

**Estimator (stacked DiD).** Each treated station and its three controls form a group, and
for each group:

```
DiD = (treated post mean - treated pre mean) - (control post mean - control pre mean)
```

The headline is the mean over groups. With a balanced panel this equals a two-way fixed
effects regression on the stacked data, with unit-by-group and group-by-week fixed effects.
Stacking (Cengiz et al. 2019) compares each opening only with its own clean controls. This
avoids the bias of a single staggered-adoption TWFE regression, where earlier-treated
stations end up as controls for later ones.

**Inference.** 95% CIs come from a cluster bootstrap: 2,000 resamples of whole events (new
stations), because the neighbours of one new station share the same shocks. An analytic
cluster-robust SE (CR1) is reported next to it (`cluster_se`). Percent effects are total DiD
divided by total pre-period treated departures.

## 5. Results

### Main effect on existing stations within 500 m (weekly departures)

| City | Openings | Treated stations | Pre mean / week | DiD rides / week (95% CI) | DiD % (95% CI) |
|---|---:|---:|---:|---|---|
| Boston | 49 | 123 | 245 | -8.3 (-19.3, +3.0) | -3.4% (-7.1, +1.4) |
| New York | 41 | 241 | 709 | -2.7 (-34.6, +33.0) | -0.4% (-5.1, +4.8) |
| Chicago | 75 | 175 | 235 | -1.6 (-13.1, +9.1) | -0.7% (-5.7, +3.9) |
| **All** | **165** | **539** | **449** | **-3.6 (-18.4, +12.4)** | **-0.8% (-4.1, +2.8)** |

Arrivals tell the same story: Boston -3.9% (-7.0, +0.3) and all cities -1.5% (-4.8, +1.8).

### By distance from the new station (DiD % of pre-period departures, 95% CI)

| Ring | Boston | New York | Chicago | All |
|---|---|---|---|---|
| 0 to 250 m | -7.0 (-13.9, +1.5) | **-8.0 (-14.0, -3.2)** | -2.4 (-12.0, +6.7) | **-7.3 (-11.7, -3.4)** |
| 250 to 500 m | -0.9 (-5.2, +4.4) | +1.8 (-2.5, +6.4) | -0.3 (-6.0, +4.4) | +1.1 (-2.3, +4.5) |
| 500 to 1,000 m | **+7.5 (+3.5, +11.5)** | +0.5 (-1.8, +2.7) | -3.2 (-8.6, +1.9) | +0.5 (-1.5, +2.5) |

The 500 to 1,000 m ring is a separate design. Its treated stations are 500 to 1,000 m from
the new station and it uses the same control rules. It works as a check: if cannibalization
is about walking distance, the effect should fade with distance. It does fade, in all three
cities. Boston's +7.5% at 500 to 1,000 m is unexpected. It is consistent with a network
effect (more places to ride to), but it appears only in Boston, so treat it as a lead to
check, not a finding.

### Net effect per opening (departures per week, post-period)

| City | New station's own | Change at neighbours ≤500 m | Net | Share cannibalized |
|---|---|---|---|---|
| Boston | 102 (80, 128) | -20.8 (-50.3, +7.1) | **+81.6 (+44.5, +120.7)** | 20% (-7%, 52%) |
| New York | 378 (257, 515) | -15.8 (-195, +200) | **+363 (+163, +607)** | 4% (-58%, 52%) |
| Chicago | 104 (81, 132) | -3.8 (-29.2, +21.9) | **+101 (+61, +144)** | 4% (-19%, 31%) |
| All | 172 (135, 212) | -11.8 (-60.0, +41.7) | **+160 (+105, +223)** | 7% (-24%, 35%) |

The neighbour change is the sum of DiDs over the event's treated neighbours.

### Robustness

* **Placebo in time.** Every opening date is moved 26 weeks earlier and the whole design is
  re-run, so no real opening falls in the window. The results are Boston +0.5% (-2.6, +4.5),
  New York -1.0% (-3.4, +1.8), Chicago +3.7% (-3.1, +10.5) and all cities +0.1% (-2.0,
  +2.6). None differs from zero. The design does not invent effects where none should exist.
* **Event study** (`event_study.png`). New York's pre-period is flat. Chicago's sits about 10 rides below
  week -1 in weeks -12 to -8, then near 0 from week -7. Boston's
  pre-period is noisy: weeks -12 to -2 sit 0 to 15 rides below week -1, and only
  week -10 is individually significant. Its post-period sits about 10 to 25 below. The pooled DiD uses
  the mean of all pre-weeks rather than week -1 alone, which makes it less sensitive to that
  one noisy week.
* **Log outcome** (`did_log` in `did_summary.csv`). This weights stations equally rather than
  by volume. Boston -0.077 (CI -0.134, -0.024), New York -0.007 (-0.053, +0.040), Chicago
  +0.020 (-0.034, +0.077). Only Boston moves: its smaller neighbours lose a larger share.
* **Determinism.** Two full runs give byte-identical outputs.

## 6. Assumptions

1. **Parallel trends.** Without the opening, nearby and matched far-away stations would have
   moved together over the 25-week window. This is supported by matching on pre-level and
   pre-trend, the flat New York pre-period, and the null placebo. It is weaker for
   Boston (see the event study).
2. **No anticipation.** Riders did not change behaviour before the first trip at the new
   station. Construction or a new dock appearing a few days early would bias the pre-period
   slightly.
3. **Controls are untouched (SUTVA).** Stations 2 to 5 km away are not affected by the
   opening, and they are at least 1 km from any other opening in the window.
4. **Opening date = first departure.** The trip data has no official opening dates. A
   station installed but not in the data until later would be dated late.
5. **Treatment is one opening.** Neighbours that also got a second new station nearby in the
   same window are dropped, rather than modelled as getting a double dose.
6. **Exchangeable events.** CIs treat openings as independent draws. Openings in the same
   expansion wave share a season and planning logic, so true uncertainty may be somewhat
   larger.

## 7. Threats to validity

* **Selection of sites (biggest threat).** Operators put stations where they expect growth:
  new housing, a new bike lane, a new transit stop. If that growth also lifts the existing
  stations nearby, the DiD is biased upward, toward "adds rides", and real cannibalization
  could be larger than estimated. Matching on pre-trend helps only if the growth had already
  started before the opening.
* **Capacity and rebalancing.** A new station also adds bikes and docks. Fewer empty or full
  hours at a neighbour would raise its departures. The operator may also change rebalancing
  around a new station. Trip data can't separate these from rider demand. The GBFS snapshots
  being collected now (from 2026-11-02) could, but they don't cover these openings.
* **Censoring.** Departures count only rides that happened. If a neighbour was often empty,
  its true demand is higher than its departures, and the measured spillover understates the
  shift in demand.
* **Seasonality.** The window spans 25 weeks, and most Boston openings are in spring, when
  ridership climbs fast. Controls share the calendar, but if busy core stations ramp up
  faster than stations 2 to 5 km out, that difference lands in the DiD. Chicago and Boston
  have stronger seasons than New York.
* **Station identity.** Moves and renumbering are caught only within 150 m and 60 days. A
  move of 200 m would count as an opening plus a station losing riders (a closure), which
  would overstate cannibalization. Divvy's 2025 renumbering makes this the largest risk in
  Chicago.
* **Short post-period.** 12 weeks covers the first season only. New stations ramp up, and
  long-run network effects may take longer to show.
* **External validity.** 2025 and 2026 openings in three Lyft-run systems. NYC's numbers are
  driven by dense Manhattan and Brooklyn infill, because its outer-borough expansion stations
  had no neighbours. In Boston, results apply to infill in the core and inner suburbs.
* **Multiple comparisons.** The design produces 3 cities, 3 rings and 2 outcomes. Read
  single significant cells (e.g. Boston 500 to 1,000 m) with that in mind; the pooled 0 to
  250 m result is the one that replicates across cities.
* **Power.** Boston's 49 openings give a 95% CI about ±11 departures a week wide (±4%). An
  effect of -3% can't be told apart from zero at that size.

## 8. Recommendation for the Boston siting work

* Use a **250 m exclusion** around busy existing stations in the siting score. Inside 250 m,
  expect the existing station to lose about 7% of departures. Beyond that, no loss is
  measurable.
* Count a new station's own ridership as **mostly additional**: about 80% in Boston, with a
  wide CI.
* Re-run in spring 2027 with the 2026-10 to 2027-03 files. That gives full post windows for
  the 165 openings marked `too_recent` (16 in Boston), and GBFS capacity data to test the
  stockout channel.

## 9. Reproduce

```bash
python ingest/station_daily.py --start 2024-04 --end 2026-09   # ~25 min; ~23 GB of downloads, 15 MB kept
python analysis/new_station_did.py                              # ~3 min, writes this folder
python analysis/plot_new_station_did.py                         # charts
python -m pytest tests/test_new_station_did.py
```

`station_daily.py` reuses `data/trips/parquet` for any month already converted by
`trips_download.py`, so on a machine that already has the last 12 months, only 18 older
months are downloaded. Settings are in `config.json` and in `Config` in the script.
