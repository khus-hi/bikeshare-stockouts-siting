"""Poll GBFS station_status and station_information for Bluebikes, Divvy and Citi Bike.

What it does (one "poll" = one run of this script with --once):
  1. Discovers the current feed URLs from each system's GBFS root (gbfs.json).
     Discovered URLs are cached so a failed discovery falls back to the last good URLs.
  2. Downloads station_status and keeps only stations whose counts or service flags
     changed since the previous poll ("change-only" storage). The first poll of each
     UTC day writes every station (a "keyframe") so each day's file stands on its own.
  3. Saves station_information only when its content changes (it rarely does).
  4. Writes one row per system per poll to a poll log, success or failure. Because
     storage is change-only, a station with no row could mean "nothing changed" or
     "we missed the poll"; the poll log is what tells those apart.
  5. Optionally writes the same rows to Postgres (set POSTGRES_DSN in .env).

Storage layout (Parquet, partitioned by UTC date, read with DuckDB):
  data/gbfs/station_status/system=<id>/date=YYYY-MM-DD/<part>.parquet
  data/gbfs/station_information/system=<id>/date=YYYY-MM-DD/<part>.parquet
  data/gbfs/polls/date=YYYY-MM-DD/<part>.parquet
Each poll writes a small part file; finished days are compacted into one file per
partition by the next poll (or by the health check).

Usage:
  python ingest/gbfs_collector.py --once            # one poll (use with cron / Task Scheduler)
  python ingest/gbfs_collector.py --loop            # poll every 5 minutes until stopped
  python ingest/gbfs_collector.py --once --compact  # also compact finished days now

Network errors never crash the run: each request is retried with backoff, each
system is handled independently, and failures are recorded in the poll log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is in requirements.txt, but don't crash without it
    load_dotenv = None

# GBFS roots. Bluebikes and Divvy roots are the ones given in the project brief; the
# Citi Bike root is the operator's published one. All three currently redirect to
# Lyft-hosted feeds (gbfs.lyft.com), which is why discovery runs on every poll.
SYSTEMS = {
    "bluebikes": "https://gbfs.bluebikes.com/gbfs/gbfs.json",
    "divvy": "https://gbfs.divvybikes.com/gbfs/2.3/gbfs.json",
    "citibike": "https://gbfs.citibikenyc.com/gbfs/gbfs.json",
}
PREFERRED_LANGUAGE = "en"
POLL_INTERVAL_SECONDS = 300
REQUEST_TIMEOUT = (5, 20)  # (connect, read) seconds
USER_AGENT = "bikeshare-stockouts research collector (github.com/khus-hi)"

# A change in any of these fields means the station row is saved.
# last_reported is stored but is not part of the change key: it ticks on every
# station heartbeat even when nothing a rider would notice has changed.
TRACKED_FIELDS = [
    "num_bikes_available",
    "num_ebikes_available",
    "num_bikes_disabled",
    "num_docks_available",
    "num_docks_disabled",
    "is_installed",
    "is_renting",
    "is_returning",
]

STATUS_SCHEMA = pa.schema(
    [
        ("system", pa.string()),
        ("poll_ts", pa.timestamp("s", tz="UTC")),
        ("feed_last_updated", pa.timestamp("s", tz="UTC")),
        ("station_id", pa.string()),
        *[(f, pa.int32()) for f in TRACKED_FIELDS],
        ("last_reported", pa.timestamp("s", tz="UTC")),
        ("is_keyframe", pa.bool_()),
    ]
)

INFO_SCHEMA = pa.schema(
    [
        ("system", pa.string()),
        ("poll_ts", pa.timestamp("s", tz="UTC")),
        ("station_id", pa.string()),
        ("short_name", pa.string()),
        ("name", pa.string()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("capacity", pa.int32()),
        ("region_id", pa.string()),
    ]
)

POLL_SCHEMA = pa.schema(
    [
        ("system", pa.string()),
        ("poll_ts", pa.timestamp("s", tz="UTC")),
        ("ok", pa.bool_()),
        ("error", pa.string()),
        ("feed_last_updated", pa.timestamp("s", tz="UTC")),
        ("n_stations", pa.int32()),
        ("n_status_rows_written", pa.int32()),
        ("is_keyframe", pa.bool_()),
        ("info_changed", pa.bool_()),
        ("fetch_seconds", pa.float64()),
    ]
)

log = logging.getLogger("gbfs_collector")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_data_dir() -> Path:
    return Path(os.environ.get("GBFS_DATA_DIR") or repo_root() / "data" / "gbfs")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_ts(epoch) -> datetime | None:
    """GBFS timestamps are POSIX seconds (v1/v2) or RFC 3339 strings (v3)."""
    if epoch is None:
        return None
    if isinstance(epoch, str):
        try:
            return datetime.fromisoformat(epoch.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def to_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_parquet(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


def make_session() -> requests.Session:
    """HTTP session that retries connection errors and 429/5xx with exponential backoff."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,  # waits about 1, 2, 4 s between attempts (urllib3 2.x)
        backoff_max=10,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = USER_AGENT
    return session


def get_json(session: requests.Session, url: str) -> dict:
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Lock: stop two polls running at once (e.g. a slow poll overlapping the next cron tick)
# ---------------------------------------------------------------------------

class PollLock:
    STALE_AFTER_SECONDS = 15 * 60

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.STALE_AFTER_SECONDS:
            log.warning("Removing stale lock %s", self.path)
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.acquired = True
        except FileExistsError:
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.acquired:
            self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Feed discovery
# ---------------------------------------------------------------------------

def parse_feed_list(gbfs_json: dict) -> dict[str, str]:
    """Return {feed_name: url} from a gbfs.json document (GBFS v1.x, v2.x or v3.x)."""
    data = gbfs_json.get("data", {})
    if isinstance(data.get("feeds"), list):  # v3: data.feeds
        feeds = data["feeds"]
    else:  # v1/v2: data.<language>.feeds
        lang = PREFERRED_LANGUAGE if PREFERRED_LANGUAGE in data else next(iter(data), None)
        feeds = data.get(lang, {}).get("feeds", []) if lang else []
    return {f["name"]: f["url"] for f in feeds if "name" in f and "url" in f}


def discover_feeds(session, system: str, root_url: str, cache_path: Path) -> dict[str, str]:
    """Discover feed URLs; on failure fall back to the last URLs that worked."""
    try:
        cached = json.loads(cache_path.read_text()) if cache_path.exists() else None
    except (json.JSONDecodeError, OSError):
        cached = None
    try:
        feeds = parse_feed_list(get_json(session, root_url))
        needed = {"station_status", "station_information"}
        if not needed.issubset(feeds):
            raise ValueError(f"gbfs.json is missing feeds: {sorted(needed - set(feeds))}")
        if cached != feeds:
            if cached is not None:
                log.info("%s: feed URLs changed", system)
            atomic_write_text(cache_path, json.dumps(feeds, indent=2, sort_keys=True))
        return feeds
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the cache
        if cached:
            log.warning("%s: discovery failed (%s); using cached feed URLs", system, exc)
            return cached
        raise


# ---------------------------------------------------------------------------
# Parsing feeds into rows
# ---------------------------------------------------------------------------

def status_rows(system: str, poll_ts: datetime, doc: dict) -> tuple[datetime | None, list[dict]]:
    feed_ts = to_ts(doc.get("last_updated"))
    rows = []
    for st in doc.get("data", {}).get("stations", []):
        sid = st.get("station_id")
        if sid is None:
            continue
        row = {
            "system": system,
            "poll_ts": poll_ts,
            "feed_last_updated": feed_ts,
            "station_id": str(sid),
            "num_bikes_available": to_int(st.get("num_bikes_available", st.get("num_vehicles_available"))),
            "num_ebikes_available": to_int(st.get("num_ebikes_available")),
            "num_bikes_disabled": to_int(st.get("num_bikes_disabled", st.get("num_vehicles_disabled"))),
            "num_docks_available": to_int(st.get("num_docks_available")),
            "num_docks_disabled": to_int(st.get("num_docks_disabled")),
            "is_installed": to_int(st.get("is_installed")),
            "is_renting": to_int(st.get("is_renting")),
            "is_returning": to_int(st.get("is_returning")),
            "last_reported": to_ts(st.get("last_reported")),
            "is_keyframe": False,
        }
        rows.append(row)
    return feed_ts, rows


def info_rows(system: str, poll_ts: datetime, doc: dict) -> list[dict]:
    rows = []
    for st in doc.get("data", {}).get("stations", []):
        if st.get("station_id") is None:
            continue
        name = st.get("name")
        if isinstance(name, list):  # v3 localized names: [{"text": ..., "language": ...}]
            name = next((n.get("text") for n in name if n.get("language", "").startswith(PREFERRED_LANGUAGE)),
                        name[0].get("text") if name else None)
        rows.append(
            {
                "system": system,
                "poll_ts": poll_ts,
                "station_id": str(st["station_id"]),
                "short_name": None if st.get("short_name") is None else str(st.get("short_name")),
                "name": name,
                "lat": st.get("lat"),
                "lon": st.get("lon"),
                "capacity": to_int(st.get("capacity")),
                "region_id": None if st.get("region_id") is None else str(st.get("region_id")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Change detection state (last saved values per station)
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("State file %s unreadable; next poll will be a full keyframe", path)
        return {}


def change_key(row: dict) -> list:
    return [row[f] for f in TRACKED_FIELDS]


# ---------------------------------------------------------------------------
# Optional Postgres sink
# ---------------------------------------------------------------------------

class PostgresSink:
    """Writes the same rows to Postgres. Parquet stays the source of truth: if Postgres
    is down, the poll still succeeds and the error is logged."""

    def __init__(self, dsn: str):
        import psycopg  # imported only when Postgres is switched on

        self.psycopg = psycopg
        self.dsn = dsn
        schema_sql = (Path(__file__).parent / "sql" / "postgres_schema.sql").read_text()
        with psycopg.connect(dsn) as conn:
            conn.execute(schema_sql)

    def write(self, table_name: str, rows: list[dict], columns: list[str]) -> None:
        if not rows:
            return
        cols = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        with self.psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, [[r[c] for c in columns] for r in rows])


# ---------------------------------------------------------------------------
# One poll
# ---------------------------------------------------------------------------

def part_path(base: Path, system: str | None, poll_ts: datetime) -> Path:
    date_dir = f"date={poll_ts:%Y-%m-%d}"
    parts = [base] + ([f"system={system}"] if system else []) + [date_dir]
    return Path(*parts) / f"part-{poll_ts:%H%M%S}.parquet"


def poll_system(session, system, root_url, data_dir, poll_ts, pg) -> dict:
    """Poll one system. Returns its poll-log row; raises nothing."""
    started = time.monotonic()
    rec = {
        "system": system, "poll_ts": poll_ts, "ok": False, "error": None,
        "feed_last_updated": None, "n_stations": None, "n_status_rows_written": 0,
        "is_keyframe": False, "info_changed": False, "fetch_seconds": None,
    }
    state_dir = data_dir / "_state"
    try:
        feeds = discover_feeds(session, system, root_url, state_dir / f"{system}_feed_urls.json")

        # --- station_status: change-only -------------------------------------
        feed_ts, rows = status_rows(system, poll_ts, get_json(session, feeds["station_status"]))
        if not rows:
            raise ValueError("station_status returned no stations")
        state_path = state_dir / f"{system}_status_state.json"
        state = load_state(state_path)
        keyframe = state.get("_date") != f"{poll_ts:%Y-%m-%d}"
        last = state.get("stations", {})
        changed = []
        for row in rows:
            if keyframe or last.get(row["station_id"]) != change_key(row):
                row["is_keyframe"] = keyframe
                changed.append(row)
        if changed:
            atomic_write_parquet(pa.Table.from_pylist(changed, schema=STATUS_SCHEMA),
                                 part_path(data_dir / "station_status", system, poll_ts))
        # Save state only after the Parquet write succeeded, so a failed write is retried.
        new_state = {"_date": f"{poll_ts:%Y-%m-%d}",
                     "stations": {**last, **{r["station_id"]: change_key(r) for r in rows}}}
        atomic_write_text(state_path, json.dumps(new_state))

        # --- station_information: save only when content changes -------------
        irows = info_rows(system, poll_ts, get_json(session, feeds["station_information"]))
        digest = hashlib.sha256(
            json.dumps([{k: v for k, v in r.items() if k != "poll_ts"} for r in irows],
                       sort_keys=True, default=str).encode()
        ).hexdigest()
        info_hash_path = state_dir / f"{system}_info_hash.txt"
        old_digest = info_hash_path.read_text().strip() if info_hash_path.exists() else None
        info_changed = digest != old_digest
        if info_changed and irows:
            atomic_write_parquet(pa.Table.from_pylist(irows, schema=INFO_SCHEMA),
                                 part_path(data_dir / "station_information", system, poll_ts))
            atomic_write_text(info_hash_path, digest)

        if pg is not None:
            try:
                pg.write("station_status_changes", changed, STATUS_SCHEMA.names)
                if info_changed:
                    pg.write("station_information_snapshots", irows, INFO_SCHEMA.names)
            except Exception as exc:  # noqa: BLE001
                log.error("%s: Postgres write failed (Parquet saved fine): %s", system, exc)

        rec.update(ok=True, feed_last_updated=feed_ts, n_stations=len(rows),
                   n_status_rows_written=len(changed), is_keyframe=keyframe,
                   info_changed=info_changed)
        log.info("%s: %d stations, %d rows saved%s", system, len(rows), len(changed),
                 " (keyframe)" if keyframe else "")
    except Exception as exc:  # noqa: BLE001 - one failing system must not stop the others
        rec["error"] = f"{type(exc).__name__}: {exc}"[:500]
        log.error("%s: poll failed: %s", system, rec["error"])
    rec["fetch_seconds"] = round(time.monotonic() - started, 2)
    return rec


def compact_finished_days(data_dir: Path, today: str) -> None:
    """Merge each finished day's part files into one data.parquet (fewer, larger files
    are faster for DuckDB). Today's partition is left alone because it is still growing."""
    for dataset in ("station_status", "station_information", "polls"):
        base = data_dir / dataset
        if not base.exists():
            continue
        for day_dir in base.rglob("date=*"):
            if not day_dir.is_dir() or day_dir.name == f"date={today}":
                continue
            parts = sorted(day_dir.glob("part-*.parquet"))
            if not parts:
                continue
            existing = day_dir / "data.parquet"
            tables = ([pq.read_table(existing)] if existing.exists() else []) + [pq.read_table(p) for p in parts]
            merged = pa.concat_tables(tables, promote_options="default").sort_by("poll_ts")
            atomic_write_parquet(merged, existing)
            for p in parts:
                p.unlink()
            log.info("Compacted %d part files in %s", len(parts), day_dir.relative_to(data_dir))


def run_once(data_dir: Path, compact: bool = True) -> list[dict]:
    with PollLock(data_dir / "_state" / "collector.lock") as lock:
        if not lock.acquired:
            log.warning("Another poll is still running; skipping this one")
            return []
        poll_ts = utc_now()
        dsn = os.environ.get("POSTGRES_DSN", "").strip()
        pg = None
        if dsn:
            try:
                pg = PostgresSink(dsn)
            except Exception as exc:  # noqa: BLE001
                log.error("Postgres unavailable, continuing with Parquet only: %s", exc)
        # The three systems are polled in parallel so one slow or failing host does not
        # delay the others. Each system has its own state files, so threads never share one.
        def poll(item):
            system, url = item
            with make_session() as session:
                return poll_system(session, system, url, data_dir, poll_ts, pg)

        with ThreadPoolExecutor(max_workers=len(SYSTEMS)) as pool:
            recs = list(pool.map(poll, SYSTEMS.items()))
        atomic_write_parquet(pa.Table.from_pylist(recs, schema=POLL_SCHEMA),
                             part_path(data_dir / "polls", None, poll_ts))
        if pg is not None:
            try:
                pg.write("polls", recs, POLL_SCHEMA.names)
            except Exception as exc:  # noqa: BLE001
                log.error("Postgres poll-log write failed: %s", exc)
        if compact:
            try:
                compact_finished_days(data_dir, f"{poll_ts:%Y-%m-%d}")
            except Exception as exc:  # noqa: BLE001 - compaction can wait for the next poll
                log.error("Compaction failed (will retry next poll): %s", exc)
        return recs


def setup_logging(data_dir: Path) -> None:
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(data_dir / "logs" / "collector.log", maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.handlers[:] = [file_handler, stream]
    log.setLevel(logging.INFO)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="poll once and exit (for cron / Task Scheduler)")
    mode.add_argument("--loop", action="store_true", help="poll every --interval seconds until stopped")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--no-compact", action="store_true", help="skip compacting finished days")
    args = parser.parse_args(argv)

    if load_dotenv:
        load_dotenv(repo_root() / ".env")
    data_dir = (args.data_dir or default_data_dir()).resolve()
    setup_logging(data_dir)

    if args.once:
        run_once(data_dir, compact=not args.no_compact)
        # Exit 0 even if a system failed: the failure is in the poll log, and a non-zero
        # exit would only make cron send mail every 5 minutes during an outage.
        return 0

    log.info("Looping every %d s; Ctrl+C to stop", args.interval)
    while True:
        try:
            run_once(data_dir, compact=not args.no_compact)
        except Exception as exc:  # noqa: BLE001 - e.g. disk full; keep the loop alive
            log.exception("Unexpected error in poll: %s", exc)
        # Sleep to the next interval boundary so polls land on :00, :05, :10, ...
        time.sleep(args.interval - (time.time() % args.interval))


if __name__ == "__main__":
    sys.exit(main())
