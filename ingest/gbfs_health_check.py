"""Daily health check for the GBFS collector: are snapshots missing?

Reads the collector's poll log with DuckDB and reports, per system:
  * coverage: share of the day's 5-minute slots that had a successful poll
  * gaps longer than 15 minutes (the KPI rule in docs/requirements.md excludes these)
  * stale feeds: the feed's own last_updated stopped moving although polls succeeded
  * whether the collector looks stopped right now (no successful poll in 15 minutes)
  * measured storage so far, and the 21-day projection against the 1 GB budget

It writes a Markdown report to data/gbfs/health/<date>.md, prints it, shows a desktop
notification when something is wrong (macOS, Linux or Windows; best effort), and exits
with code 1 so a scheduler can flag the failure too.

Usage:
  python ingest/gbfs_health_check.py                 # check yesterday (UTC) + liveness now
  python ingest/gbfs_health_check.py --date 2026-11-03
  python ingest/gbfs_health_check.py --no-notify
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gbfs_collector as gc  # noqa: E402

SLOT_MINUTES = 5
GAP_MINUTES = 15
STALE_FEED_MINUTES = 30
COLLECTION_DAYS = 21
STORAGE_BUDGET_BYTES = 1_000_000_000
MIN_COVERAGE = 0.95  # overridden by --min-coverage


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def check_day(con, polls_glob: str, day: date, now: datetime) -> tuple[list[str], list[str]]:
    """Return (report lines, problems) for one UTC day."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = min(start + timedelta(days=1), now)
    expected_slots = int((end - start).total_seconds() // (SLOT_MINUTES * 60))
    lines = [f"## Coverage for {day.isoformat()} (UTC)", "",
             f"Expected 5-minute slots: {expected_slots}", "",
             "| System | Slots with a good poll | Coverage | Failed polls | Gaps > 15 min | Longest gap (min) | Stale-feed polls |",
             "|---|---|---|---|---|---|---|"]
    problems = []

    con.execute("CREATE OR REPLACE TEMP TABLE p AS SELECT * EXCLUDE (date) FROM read_parquet(?, hive_partitioning = true) "
                "WHERE poll_ts >= ? AND poll_ts < ?", [polls_glob, start, end])
    for system in gc.SYSTEMS:
        good_slots, failed = con.execute(
            "SELECT count(DISTINCT time_bucket(INTERVAL 5 MINUTE, poll_ts)) FILTER (WHERE ok), "
            "count(*) FILTER (WHERE NOT ok) FROM p WHERE system = ?", [system]).fetchone()
        # Gaps between consecutive good polls, including from midnight to the first
        # poll and from the last poll to the end of the window.
        gaps = con.execute(
            """
            WITH t AS (
                SELECT ?::TIMESTAMPTZ AS ts UNION ALL
                SELECT poll_ts FROM p WHERE system = ? AND ok UNION ALL
                SELECT ?::TIMESTAMPTZ
            ), g AS (
                SELECT ts, lead(ts) OVER (ORDER BY ts) AS nxt FROM t
            )
            SELECT ts, nxt, date_diff('second', ts, nxt) / 60.0 AS minutes
            FROM g WHERE nxt IS NOT NULL AND date_diff('second', ts, nxt) > ? * 60
            ORDER BY minutes DESC
            """, [start, system, end, GAP_MINUTES]).fetchall()
        stale = con.execute(
            "SELECT count(*) FROM p WHERE system = ? AND ok AND feed_last_updated IS NOT NULL "
            "AND date_diff('second', feed_last_updated, poll_ts) > ? * 60",
            [system, STALE_FEED_MINUTES]).fetchone()[0]
        coverage = good_slots / expected_slots if expected_slots else 1.0
        longest = f"{gaps[0][2]:.0f}" if gaps else "0"
        lines.append(f"| {system} | {good_slots} | {coverage:.1%} | {failed} | {len(gaps)} | {longest} | {stale} |")
        if expected_slots:
            if good_slots == 0:
                problems.append(f"{system}: no successful polls on {day}")
            elif coverage < MIN_COVERAGE:
                problems.append(f"{system}: coverage {coverage:.1%} on {day} (target {MIN_COVERAGE:.0%})")
        if stale:
            problems.append(f"{system}: {stale} polls where the feed was over {STALE_FEED_MINUTES} min old")
        for ts, nxt, minutes in gaps[:5]:
            lines.append(f"|  ↳ gap | {ts:%H:%M} to {nxt:%H:%M} UTC | {minutes:.0f} min | | | | |")
    return lines, problems


def check_liveness(con, polls_glob: str, now: datetime) -> tuple[list[str], list[str]]:
    lines, problems = ["## Collector right now", ""], []
    try:
        rows = con.execute(
            "SELECT system, max(poll_ts) FILTER (WHERE ok) FROM read_parquet(?, hive_partitioning = true) GROUP BY system",
            [polls_glob]).fetchall()
    except duckdb.IOException:
        rows = []
    last = dict(rows)
    for system in gc.SYSTEMS:
        ts = last.get(system)
        if ts is None:
            lines.append(f"- {system}: no successful poll recorded yet")
            problems.append(f"{system}: no successful poll recorded yet")
            continue
        age = (now - ts).total_seconds() / 60
        lines.append(f"- {system}: last good poll {ts:%Y-%m-%d %H:%M} UTC ({age:.0f} min ago)")
        if age > GAP_MINUTES:
            problems.append(f"{system}: last good poll was {age:.0f} min ago; collector may be stopped or the laptop asleep")
    return lines, problems


def storage_report(data_dir: Path, con, polls_glob: str) -> tuple[list[str], list[str]]:
    total = sum(dir_bytes(data_dir / d) for d in ("station_status", "station_information", "polls"))
    try:
        first, last = con.execute(
            "SELECT min(poll_ts), max(poll_ts) FROM read_parquet(?, hive_partitioning = true)", [polls_glob]).fetchone()
    except duckdb.IOException:
        first = last = None
    lines = ["## Storage (measured on disk)", "", f"- Parquet data so far: {total / 1e6:.1f} MB"]
    problems = []
    if first and last and last > first:
        days = (last - first).total_seconds() / 86400
        projected = total / days * COLLECTION_DAYS
        lines.append(f"- Collected over {days:.2f} days, so {total / days / 1e6:.1f} MB/day")
        lines.append(f"- Projected for {COLLECTION_DAYS} days: {projected / 1e6:.0f} MB "
                     f"(budget {STORAGE_BUDGET_BYTES / 1e6:.0f} MB)")
        if days >= 1 and projected > STORAGE_BUDGET_BYTES:
            problems.append(f"storage projected at {projected / 1e6:.0f} MB for {COLLECTION_DAYS} days, over the 1 GB budget")
    free = shutil.disk_usage(data_dir).free
    lines.append(f"- Free disk space: {free / 1e9:.1f} GB")
    if free < 2e9:
        problems.append(f"only {free / 1e9:.1f} GB free disk space")
    return lines, problems


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification; silently does nothing if unavailable."""
    message = message[:240]
    try:
        system = platform.system()
        if system == "Darwin":
            safe = message.replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'],
                           check=False, timeout=10)
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], check=False, timeout=10)
        elif system == "Windows":
            safe = message.replace("'", "")
            ps = ("Add-Type -AssemblyName System.Windows.Forms; "
                  "$n = New-Object System.Windows.Forms.NotifyIcon; "
                  "$n.Icon = [System.Drawing.SystemIcons]::Warning; $n.Visible = $true; "
                  f"$n.ShowBalloonTip(15000, '{title}', '{safe}', 'Warning'); Start-Sleep 16; $n.Dispose()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False, timeout=30)
    except Exception:  # noqa: BLE001
        pass



def main(argv=None) -> int:
    global MIN_COVERAGE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="UTC day to check (default: yesterday)")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--min-coverage", type=float, default=MIN_COVERAGE)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)
    MIN_COVERAGE = args.min_coverage

    if gc.load_dotenv:
        gc.load_dotenv(gc.repo_root() / ".env")
    data_dir = (args.data_dir or gc.default_data_dir()).resolve()
    now = gc.utc_now()
    day = args.date or (now.date() - timedelta(days=1))
    polls_glob = str(data_dir / "polls" / "*" / "*.parquet")

    # Compact finished days first (same code the collector uses), under the collector's
    # lock so the two never touch the same files at once.
    with gc.PollLock(data_dir / "_state" / "collector.lock") as lock:
        if lock.acquired:
            gc.compact_finished_days(data_dir, f"{now:%Y-%m-%d}")

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    report = [f"# GBFS collector health check, run {now:%Y-%m-%d %H:%M} UTC", ""]
    problems: list[str] = []
    try:
        day_lines, day_problems = check_day(con, polls_glob, day, now)
    except duckdb.IOException:
        day_lines, day_problems = [f"No poll log found under {data_dir / 'polls'}"], ["no poll log found"]
    for lines, probs in (
        (day_lines, day_problems),
        check_liveness(con, polls_glob, now),
        storage_report(data_dir, con, polls_glob),
    ):
        report += lines + [""]
        problems += probs

    status = "PROBLEMS FOUND" if problems else "OK"
    report[1:1] = ["", f"**Status: {status}**", ""] + [f"- {p}" for p in problems]
    text = "\n".join(report) + "\n"
    out = data_dir / "health" / f"{day.isoformat()}.md"
    gc.atomic_write_text(out, text)
    print(text)
    print(f"Report saved to {out}")

    if problems and not args.no_notify:
        notify("Bike-share collector: snapshots missing", "; ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
