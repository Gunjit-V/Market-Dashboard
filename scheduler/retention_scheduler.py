"""Roll tick_data into Parquet archives on a schedule, keeping recent days hot.

Tick volume grows fast: a full session across the tracked instruments runs to
roughly half a million rows, and with same-second snapshots now preserved
(migration 001) it grows faster still. At ~1 KB/row that is several hundred MB
per week in a PostgreSQL instance living on the C: drive.

This service archives ticks older than a retention window to compressed
Parquet, then deletes them from the table. It reuses scripts/purge_tick_data.py
rather than duplicating that logic, so the archive-verify-then-delete ordering
and the batched deletes are identical to the manual path.

Scheduling
    Runs once per day at RETENTION_RUN_TIME (default 16:30 IST), which is after
    the 15:30 close, so it never competes with the live collector for locks.
    A day is only processed once: the last completed run date is tracked in
    memory and re-checked against the table, so a restart mid-day does not
    re-run a purge that already happened.

Configuration (.env)
    TICK_RETENTION_DAYS=7        Days of ticks to keep in PostgreSQL.
    RETENTION_RUN_TIME=16:30     IST clock time to run at.
    RETENTION_ARCHIVE_DIR=       Where Parquet files land. Defaults to
                                 data/archive/ inside the project.
    RETENTION_VACUUM=false       Run VACUUM FULL after purging. Off by default:
                                 it takes an exclusive lock and rewrites the
                                 whole table. Plain autovacuum reclaims space
                                 for reuse; VACUUM FULL returns it to the OS.

Run outside Docker with:
    python -m scheduler.retention_scheduler
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path

from db.connection import ConnectionManager
from scheduler.nse_calendar import IST

# scripts/ is not a package; add it to the path so the purge logic can be
# imported rather than reimplemented.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import purge_tick_data as purge_module  # noqa: E402

LOGGER = logging.getLogger("retention_scheduler")

DEFAULT_RETENTION_DAYS = 7
DEFAULT_RUN_TIME = "16:30"
POLL_SECONDS = 60


def _retention_days() -> int:
    raw = os.getenv("TICK_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    try:
        days = int(raw)
    except ValueError:
        LOGGER.warning("Invalid TICK_RETENTION_DAYS=%r; using %d", raw, DEFAULT_RETENTION_DAYS)
        return DEFAULT_RETENTION_DAYS
    if days < 1:
        # A zero/negative window would purge everything including today's
        # still-accumulating session. Refuse rather than destroy live data.
        LOGGER.warning("TICK_RETENTION_DAYS=%d is below 1; using 1", days)
        return 1
    return days


def _run_time() -> clock_time:
    raw = os.getenv("RETENTION_RUN_TIME", DEFAULT_RUN_TIME).strip()
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return clock_time(hour, minute)
    except (ValueError, TypeError):
        LOGGER.warning("Invalid RETENTION_RUN_TIME=%r; using %s", raw, DEFAULT_RUN_TIME)
        hour, minute = (int(p) for p in DEFAULT_RUN_TIME.split(":"))
        return clock_time(hour, minute)


def _archive_dir() -> Path:
    raw = os.getenv("RETENTION_ARCHIVE_DIR", "").strip()
    return Path(raw) if raw else purge_module.DEFAULT_ARCHIVE_DIR


def _vacuum_enabled() -> bool:
    return os.getenv("RETENTION_VACUUM", "false").strip().lower() in {"1", "true", "yes"}


def cutoff_date(today: date, retention_days: int) -> date:
    """First date to KEEP. Everything strictly before it is archived and purged.

    With retention_days=7 and today=2026-09-14, this keeps 09-08 through 09-14
    inclusive — seven calendar days including today.
    """
    return today - timedelta(days=retention_days - 1)


def run_once(conn, today: date | None = None) -> dict:
    """Archive and purge one retention cycle. Returns a summary dict."""
    today = today or datetime.now(IST).date()
    keep_from = cutoff_date(today, _retention_days())

    stats = purge_module.summarize(conn, keep_from)
    if stats["rows_to_purge"] == 0:
        LOGGER.info("Nothing older than %s to archive", keep_from)
        return {"archived": 0, "deleted": 0, "keep_from": keep_from, "archive": None}

    stamp = datetime.now(IST).strftime("%Y%m%dT%H%M%S")
    out_path = _archive_dir() / f"tick_data_before-{keep_from}_{stamp}.parquet"

    LOGGER.info(
        "Archiving %s rows (%s -> %s) to %s",
        f"{stats['rows_to_purge']:,}", stats["first_day"], stats["last_day"], out_path,
    )
    written = purge_module.archive(conn, keep_from, out_path)

    # Never delete on the strength of an unverified archive.
    if not purge_module.verify(out_path, stats["rows_to_purge"]):
        LOGGER.error("Archive verification FAILED; keeping all rows in the table")
        return {"archived": written, "deleted": 0, "keep_from": keep_from,
                "archive": out_path, "error": "verification_failed"}

    deleted = purge_module.purge(conn, keep_from)
    LOGGER.info("Archived %s rows, deleted %s rows", f"{written:,}", f"{deleted:,}")

    if _vacuum_enabled():
        LOGGER.info("Running VACUUM FULL (exclusive lock)")
        purge_module.reclaim(conn)

    return {"archived": written, "deleted": deleted, "keep_from": keep_from,
            "archive": out_path}


def _already_ran_today(conn, keep_from: date) -> bool:
    """True when no rows older than the cutoff remain — i.e. today's run is done.

    Checking the data rather than a flag file means a container restart cannot
    cause a duplicate run, and a manual purge is correctly detected too.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM tick_data WHERE timestamp < %s)",
            (keep_from,),
        )
        return not cur.fetchone()[0]


def run_forever() -> None:
    run_at = _run_time()
    LOGGER.info(
        "Retention scheduler started: keep %d days, run daily at %s IST, archive to %s",
        _retention_days(), run_at.strftime("%H:%M"), _archive_dir(),
    )
    last_run_date: date | None = None

    while True:
        now = datetime.now(IST)
        today = now.date()

        if last_run_date != today and now.time() >= run_at:
            conn = None
            try:
                conn = ConnectionManager().get_connection()
                keep_from = cutoff_date(today, _retention_days())
                if _already_ran_today(conn, keep_from):
                    LOGGER.info("Retention already satisfied for %s", today)
                else:
                    run_once(conn, today)
                last_run_date = today
            except Exception:
                # Retry on the next poll rather than killing the service; a
                # transient DB blip must not silently stop retention forever.
                LOGGER.exception("Retention run failed; will retry")
            finally:
                if conn is not None:
                    conn.close()

        time.sleep(POLL_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("RETENTION_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
