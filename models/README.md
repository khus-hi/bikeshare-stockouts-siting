# models/: station net-flow forecast and rebalancing priority list

Two questions, answered in order:

1. **How many bikes will leave and arrive at each station, hour by hour?** A LightGBM
   forecast of departures and arrivals per station per hour, judged against a simple
   "same hour last week" baseline.
2. **Which stations will run out of bikes or docks in the next 3 hours, and how many
   bikes should be moved?** The forecast applied to the live bike counts from our GBFS
   snapshots.

Results (real numbers, from the commands below) are in
[`analysis/results/netflow_metrics.md`](../analysis/results/netflow_metrics.md).

| File | What it does |
|---|---|
| `netflow_common.py` | Feature list, metric definitions, calendar features (shared by everything below) |
| `netflow_features.py` | Builds the hourly station panel from trip history + weather |
| `train_netflow.py` | Trains LightGBM, scores it and the baseline on a held-out month, writes metrics |
| `rebalance.py` | Next-3-hours empty/full projection and bikes-to-move list (runs where GBFS is collected) |
| `../ingest/weather_download.py` | Hourly weather from Open-Meteo (history for training, forecast for live use) |

## How to run

From the repo root, after `ingest/trips_download.py` (trip history):

```bash
python ingest/weather_download.py history     # 3 small files, one per city, no API key
python models/netflow_features.py              # ~1 min; Citi Bike is ~19M station-hours
python models/train_netflow.py --ablation      # with and without weather, writes analysis/results/
python -m pytest tests/                        # logic tests
```

Then, on the machine running the GBFS collector:

```bash
python models/rebalance.py                     # all systems, as of the latest poll
python models/rebalance.py --systems bluebikes --top 25
```

`rebalance.py` needs only `data/gbfs/` (collector), `data/models/netflow/` (from training:
6 model files, 3 profile files, `buffers.json`) and internet access to Open-Meteo. It
does not need the trip history, so the models can be trained on one machine and copied.

## Step 1: the forecast, in plain English

**What is forecast.** For every station and every hour: departures (trips that start
there) and arrivals (trips that end there). Net flow = departures − arrivals. Positive
net flow means the station is losing bikes.

**Why two models instead of one.** A station with 20 bikes out and 20 in has net flow 0,
same as a station nobody uses, but it needs very different attention. Forecasting both
sides keeps that information, and the rebalancing step uses both.

**The data.** One row per station per hour for 12 months of official trip history
(October 2025 to September 2026), *including the hours with zero trips*. Most
station-hours are 0, 1 or 2 trips, so leaving out the empty hours would teach the model
that every hour is busy. Hours are in UTC so the daylight-saving switches don't create
missing or doubled hours; calendar features are then computed in local time.

**The features**, all things that are known at the time the forecast is made:

| Group | Features | Why |
|---|---|---|
| Calendar | local hour, weekday, weekend flag, US federal holiday | Commute peaks, weekend leisure riding |
| Station | latitude, longitude | Lets the model learn neighbourhood patterns |
| Lags | same hour 1 day, 1 week, 2 weeks, 3 weeks earlier; 3-week same-hour average; total of the previous day | The station's own recent level and rhythm |
| Weather | temperature, feels-like temperature, humidity, precipitation, rain, snowfall, cloud cover, wind | People ride less in rain, cold and wind |

The shortest lag is 24 hours on purpose. With 1-hour lags the model would need last
hour's trips, which for the 3-hour-ahead use would mean feeding its own guesses back in.
A 24-hour minimum lets one model forecast any of the next 24 hours.

**The model.** LightGBM (gradient-boosted decision trees): it builds hundreds of small
decision trees, each one correcting the errors of the ones before. It handles mixed
features (hours, temperatures, counts) and interactions ("rain matters more at 5 pm on a
weekday") without manual feature engineering. The Poisson objective tells it the target
is a count: never negative, often zero. One pair of models per city, because the three
systems differ in size and rhythm. Settings are in `PARAMS` in `train_netflow.py`; the
models used all or nearly all of the 1,000-tree cap, so a longer run (`--max-rounds`)
may gain a little more accuracy at the cost of training time (about 40 minutes for all
three cities on a 4-core machine).

**The test.** Time-based, never random: the model is trained on data up to mid-August
2026, uses the next two weeks only to decide when to stop adding trees (early stopping),
and is scored on September 2026, which it never saw. A random split would let it peek at
the same week's neighbouring hours and look better than it is.

**The baseline.** Seasonal naive: "this hour will look exactly like the same hour last
week." It is hard to beat for commute stations and needs no model at all, so a forecast
is only worth having if it beats it. `docs/requirements.md` sets this as the bar.

**The metrics** (definitions in `docs/requirements.md`, code in `netflow_common.py`):

* **MAPE** (mean absolute percentage error): the average of |actual − forecast| / actual.
  It is undefined when actual = 0, and most station-hours are 0, so it is computed only
  on station-hours with at least 1 trip. That makes it harsh: forecasting 2 when the
  actual is 1 is a 100% error.
* **WAPE** (weighted absolute percentage error): total absolute error / total actual,
  over *all* station-hours. It doesn't blow up at small counts, so it is the better
  headline number for bike counts. Both are reported, as the requirements ask.
* **Net-flow MAPE / WAPE / MAE**: the same for departures − arrivals. Net flow is often
  0 and can be negative, so its MAPE is on hours with |net| ≥ 1, and the WAPE divides by
  the total |net flow|. Net flow is a small difference of two bigger, noisy numbers, so
  its percentage errors are much larger than for departures or arrivals. That is
  expected and doesn't mean the model is broken. **MAE** (bikes per station-hour) is
  the easiest net-flow number to read.
* **3-hour net-flow MAE**: error on the 3-hour total, the quantity the rebalancing step
  actually uses. Hour-to-hour errors partly cancel over 3 hours.

`analysis/results/netflow_feature_importance.csv` shows which features the trees relied
on most (share of total gain), per city and target.

## Step 2: the rebalancing list, in plain English

For each station that is in service right now:

1. **Start from now.** Bikes available and empty docks from the latest GBFS snapshot.
2. **Forecast the next 3 hours** of departures and arrivals. If it is 10:40, the window
   is 10:40 to 13:40: one third of the 10:00 hour, the 11:00 and 12:00 hours, and two
   thirds of the 13:00 hour.
3. **Walk forward.** Projected bikes = bikes now − cumulative net outflow, hour by hour.
4. **Flag it.** If projected bikes reach 0, the station is **projected empty**. If they
   reach the number of slots (bikes now + empty docks), it is **projected full**.
5. **Size the move.** Bikes to add (or remove) = how far past the limit the projection
   goes, **plus a safety buffer**. The buffer is the model's measured 3-hour net-flow MAE
   on the September test month (`data/models/netflow/buffers.json`), so a move covers a
   typical forecast miss. A move can't exceed the empty docks (adding) or the bikes there
   (removing). Stations that stay within limits but come within the buffer are listed as
   **near empty / near full**, after the projected ones.
6. **Rank.** Projected stockouts first, largest shortfall first (shortfall = riders the
   forecast says would find no bike or no dock), then soonest. Near-limit stations follow.

Output: `data/rebalancing/priority_<UTC time>.csv` with, per station, bikes and docks
now, forecast departures and arrivals for the 3 hours, projected low and high bike
counts, status, when the limit is hit (local time), action and bikes to move.

**Where recent demand comes from at run time.** The lag features need last week's
trips, but trip history is published monthly. So at run time they are estimated from
the GBFS snapshots: a drop in bikes between two 5-minute polls counts as departures, a
rise as arrivals. Hours the collector didn't observe (before collection started, or
gaps over 15 minutes) fall back to the station's average for that hour of the week over
the last 4 weeks of published trips. The printout says what share came from each.

## Limitations (say these before an interviewer does)

* **GBFS demand is a proxy.** Polling every 5 minutes undercounts busy stations (a bike
  out and a bike in within the same 5 minutes cancel out), and staff rebalancing trucks
  show up as "rides". The model was trained on trip counts, so the live lags are a
  slightly different measurement. Check: once the November 2026 trip file is published
  (early December), compare GBFS-derived hourly counts with actual trips for Nov 2-23.
* **The fallback profile is from September.** In November, riding is lower, so until the
  collector has 3 weeks of history the fallback overstates demand somewhat.
* **Trip history is censored demand.** When a station is empty nobody can depart, so the
  model learns "observed" departures, not "wanted" departures. It understates demand at
  chronically empty stations, the same caveat as the lost-rides KPI.
* **One weather point per city.** Rain in the Bronx and in Brooklyn count as the same rain.
* **Training weather vs live weather.** Training uses reanalysis (what the weather was);
  live use has a forecast (what it is expected to be). Forecast error adds to model error.
* **Station closures.** A station's panel runs from its first to its last trip; any
  closure in between counts as zero-trip hours.
* **No truck routing.** The list says what each station needs, not which van goes where
  or which full station should feed which empty one. Routing is out of scope
  (`docs/requirements.md` section 7); pairing nearby surplus and deficit stations is
  the natural next step.
* **Hour-level resolution.** A station can empty and refill within an hour. The
  projection only sees hourly totals.
