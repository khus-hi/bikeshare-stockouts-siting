# ingest/: GBFS station status collector

Operators publish live station status but no history, so this project builds its own
history by polling the GBFS feeds of Bluebikes, Divvy and Citi Bike every 5 minutes for
3 weeks. Stockout KPIs (station-hours empty/full, lost rides) are computed from it.

| File | What it does |
|---|---|
| `gbfs_collector.py` | One poll of all three systems (`--once`), or a loop (`--loop`) |
| `gbfs_health_check.py` | Daily report: missing snapshots, gaps, stale feeds, storage |
| `sql/duckdb_views.sql` | DuckDB views over the Parquet files |
| `sql/postgres_schema.sql` | Tables for the optional Postgres sink |
| `scheduling/` | cron (macOS/Linux) and Task Scheduler (Windows) setup |
| `kubeflow/` | The same job as Kubeflow recurring runs on minikube |

## Feeds

Discovery starts from each system's GBFS root and reads `station_status` and
`station_information` URLs from it on every poll, so a moved feed is picked up
automatically. Last good URLs are cached in `data/gbfs/_state/` as a fallback.

| System | Root | Version | Feeds found there (checked 2026-11-01) |
|---|---|---|---|
| Bluebikes | `https://gbfs.bluebikes.com/gbfs/gbfs.json` | 1.1 | `https://gbfs.lyft.com/gbfs/1.1/bos/en/…` |
| Divvy | `https://gbfs.divvybikes.com/gbfs/2.3/gbfs.json` | 2.3 | `https://gbfs.lyft.com/gbfs/2.3/chi/en/…` |
| Citi Bike | `https://gbfs.citibikenyc.com/gbfs/gbfs.json` | 1.1 | `https://gbfs.lyft.com/gbfs/1.1/bkn/en/…` |

All three feeds have `ttl` = 60 s, so a 5-minute poll is well within what the operator allows.

## Run it on your laptop (free)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip ...
.venv/bin/python ingest/gbfs_collector.py --once  # try one poll
.venv/bin/python ingest/gbfs_health_check.py --date $(date -u +%F)   # check today so far
```

Then schedule it:

* **macOS / Linux:** paste `scheduling/crontab.example` into `crontab -e`, with your paths.
* **Windows:** `powershell -ExecutionPolicy Bypass -File ingest\scheduling\windows_task_scheduler.ps1`
* **No scheduler:** `python ingest/gbfs_collector.py --loop` in a terminal that stays open.

**Keep the laptop awake.** Cron and Task Scheduler cannot poll while the laptop sleeps,
and those gaps cannot be filled later. On a Mac, keep it plugged in and either run
`caffeinate -s` in a terminal or turn on "Prevent automatic sleeping when the display is
off" (System Settings > Battery > Options). On Windows the task is registered with "wake
the computer to run". Gaps that still happen are measured by the health check and
excluded from KPIs, as `docs/requirements.md` specifies.

On macOS, if cron does nothing, the repo is probably in a protected folder (Documents,
Desktop): either move it or give `/usr/sbin/cron` Full Disk Access.

## How storage works

```
data/gbfs/
  station_status/system=bluebikes/date=2026-11-02/data.parquet   # one file per finished day
  station_status/system=bluebikes/date=2026-11-03/part-080500.parquet  # today: one small file per poll
  station_information/system=.../date=.../...                     # only when station info changes
  polls/date=.../...                                              # one row per system per poll
  health/2026-11-02.md                                            # daily health reports
  _state/  logs/                                                  # change-detection state, logs
```

* **Change-only rows.** A station row is saved only when one of its counts or service
  flags changes (`num_bikes_available`, `num_ebikes_available`, `num_bikes_disabled`,
  `num_docks_available`, `num_docks_disabled`, `is_installed`, `is_renting`, `is_returning`).
  Most stations don't change in any 5 minutes, so this is far smaller than saving every
  station every poll.
* **Daily keyframe.** The first poll of each UTC day saves every station, so one day's
  file can be analysed without reading the day before.
* **Poll log.** Change-only storage makes "no row" ambiguous: nothing changed, or the
  poll never happened? Every poll writes one row per system to `polls/` (success or
  failure), which is what the health check and the KPI coverage rule use.
* **Parquet + DuckDB.** Columnar, compressed (zstd) and partitioned by system and date,
  so DuckDB reads only the days a query needs. Each poll writes a small part file
  (Parquet files can't be appended to); finished days are merged into one file per
  partition automatically.

Query it:

```bash
duckdb data/gbfs.duckdb < ingest/sql/duckdb_views.sql
duckdb data/gbfs.duckdb -c "SELECT system, count(*) FROM station_status_changes GROUP BY 1"
```

### Will it stay under 1 GB?

Not measured yet: this repo was built in an environment that could not reach the live
feeds, so there is no real feed size to quote. The size depends on how many station rows
change per poll, which only a real run shows. The health check measures it: every report
has a "Storage" section with MB on disk, MB per day, and the projection for 21 days
against the 1 GB budget, and it raises an alarm once a full day of data projects over
budget. Check the first day's report before relying on the estimate.

## Optional Postgres sink

Set `POSTGRES_DSN` in `.env` (for example `postgresql://khushi@localhost:5432/gbfs`) and
the collector also writes every saved row to Postgres (tables in
`sql/postgres_schema.sql`, created automatically). Leave it empty for Parquet only.

Trade-off: Postgres needs a server running for 3 weeks and stores row-by-row, so it uses
more disk than compressed Parquet; in return it is a familiar SQL database for
dashboards or tools that can't read Parquet. Parquet stays the source of truth: if
Postgres is down, the poll still succeeds and the error goes to the log. Free local
options: Postgres.app (Mac), `brew install postgresql`, `apt install postgresql`, or the
EDB installer (Windows).

## Daily health check

`gbfs_health_check.py` checks yesterday (UTC) by default and reports, per system:

* **coverage**: share of the day's 288 five-minute slots with a successful poll (alarm below 95%, `--min-coverage` to change)
* **gaps over 15 minutes**, with start and end times (these intervals are excluded from KPIs)
* **failed polls** (network or feed errors, with the error kept in the poll log)
* **stale feeds**: polls succeeded but the feed's own `last_updated` was over 30 minutes old
* **collector stopped**: no successful poll in the last 15 minutes
* **storage**: measured size, MB/day, 21-day projection, free disk

When anything is wrong it shows a desktop notification (macOS, Linux with `notify-send`,
Windows) and exits with code 1. The full report is saved to `data/gbfs/health/`.

## How it survives network errors

* Each request retries up to 3 times with exponential backoff on connection errors,
  timeouts, 429 and 5xx, honouring `Retry-After`, with a 5 s connect / 20 s read timeout.
* The three systems are polled in parallel and independently: one failing never stops the others.
* If feed discovery fails, the last good feed URLs are used.
* A lock file stops a slow poll from overlapping the next one (stale locks expire after 15 min).
* Files are written to a temp name and renamed, so a crash never leaves a half-written file;
  change-detection state is saved only after the Parquet write succeeds.
* A failed poll is recorded, not hidden, so the KPI step can exclude it.

## Kubeflow version (local minikube, free)

The same job as two KFP v2 recurring runs: a poll every 5 minutes and the health check
daily. Both mount one PersistentVolumeClaim, which plays the role of the `data/` folder.
Caching is turned off (otherwise KFP would skip every run after the first, since the
step's inputs never change), and `no_catchup` plus `max_concurrency=1` stop a backlog of
runs firing after the cluster has been stopped.

```bash
# 1. Cluster (Kubeflow Pipelines needs roughly 4 CPUs and 8 GB RAM)
minikube start --cpus 4 --memory 8192 --disk-size 30g

# 2. Kubeflow Pipelines standalone
export PIPELINE_VERSION=2.14.3
kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=$PIPELINE_VERSION"
kubectl wait --for condition=established --timeout=60s crd/applications.app.k8s.io
kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/env/platform-agnostic?ref=$PIPELINE_VERSION"
kubectl wait pods -l application-crd-id=kubeflow-pipelines -n kubeflow --for condition=Ready --timeout=1800s
kubectl port-forward -n kubeflow svc/ml-pipeline-ui 8080:80   # leave running

# 3. Image (built inside minikube, no registry) and volume
minikube image build -t bikeshare-gbfs:0.1 -f ingest/kubeflow/Dockerfile .
kubectl apply -n kubeflow -f ingest/kubeflow/pvc.yaml

# 4. Compile and schedule (kfp needs Python 3.9 to 3.12)
pip install -r ingest/kubeflow/requirements-kubeflow.txt
python ingest/kubeflow/gbfs_pipeline.py schedule --host http://localhost:8080

# 5. Copy the data to the laptop for analysis
kubectl cp kubeflow/gbfs-data-reader:/data/gbfs ./data/gbfs
```

For Postgres from the cluster, create a secret and pass its name:
`kubectl create secret generic gbfs-postgres -n kubeflow --from-literal=POSTGRES_DSN=postgresql://user:pw@host.minikube.internal:5432/gbfs`
then `python ingest/kubeflow/gbfs_pipeline.py schedule --postgres-secret gbfs-postgres`.

The laptop cron version is the one to use for the actual 3-week collection: it needs a
fraction of the memory, and every 5-minute KFP run starts a new pod. The Kubeflow version
shows the same job as an orchestrated, scheduled pipeline. The compiled pipeline YAML is
checked in; the cluster steps above have not yet been run end to end.

## Interview notes

* **Why poll at all?** GBFS is a live snapshot; history only exists if you record it.
* **Why change-only + poll log?** Storage shrinks to what actually changed, and the poll
  log keeps "nothing changed" distinguishable from "we weren't looking".
* **Why 5 minutes?** Fine enough to time empty/full spells, well above the 60 s feed TTL,
  and 288 polls a day is a polite load on a public API.
* **Why UTC partitions?** One clock for three time zones; convert to local time in analysis.
