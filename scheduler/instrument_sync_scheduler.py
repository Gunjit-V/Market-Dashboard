"""Run the instrument master sync once daily before market open.

This module intentionally uses only the Python standard library, matching
scheduler.ohlcv_scheduler. It wakes up once a minute, and on the first
qualifying trading day tick at or after SYNC_TIME it runs
downloader.sync_instruments.sync_all() (deactivating expired contracts,
activating current-month instruments, and picking up newly listed ones).

Configure with environment variables:

    INSTRUMENT_SYNC_TIME=08:45
    INSTRUMENT_SYNC_SAVE_CSV=true

Trading-day logic comes from scheduler.nse_calendar (see that module for the
NSE_HOLIDAYS / NSE_SPECIAL_TRADING_DAYS environment variables).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo

from downloader.sync_instruments import sync_all
from scheduler.nse_calendar import is_nse_trading_day


LOGGER = logging.getLogger("instrument_sync_scheduler")
IST = ZoneInfo("Asia/Kolkata")


def _parse_time(value: str) -> clock_time:
    hour, minute = (int(part) for part in value.split(":"))
    return clock_time(hour, minute)


SYNC_TIME = _parse_time(os.getenv("INSTRUMENT_SYNC_TIME", "08:45"))
SAVE_CSV = os.getenv("INSTRUMENT_SYNC_SAVE_CSV", "true").strip().lower() in ("1", "true", "yes")
POLL_SECONDS = 30

# How long to wait before re-attempting a failed sync, and how many times.
# sync_all() deactivates the previous option set before computing the new one,
# so a run that fails partway leaves FEWER active instruments than before —
# strictly worse than not running. Marking the day complete on failure (as this
# did until 2026-09-07) therefore costs a full day of option OHLCV coverage.
RETRY_SECONDS = int(os.getenv("INSTRUMENT_SYNC_RETRY_SECONDS", "600"))
MAX_DAILY_ATTEMPTS = int(os.getenv("INSTRUMENT_SYNC_MAX_ATTEMPTS", "6"))


def _run_sync() -> bool:
    """Run one sync. Returns True on success so the caller can retry failures."""
    try:
        LOGGER.info("Starting instrument sync")
        sync_all(save_csv=SAVE_CSV)
        LOGGER.info("Finished instrument sync")
        return True
    except Exception:
        LOGGER.exception("Unhandled error during instrument sync")
        return False


def run_forever() -> None:
    LOGGER.info(
        "Instrument sync scheduler started; sync_time=%s, retry every %ss "
        "up to %s attempts/day",
        SYNC_TIME, RETRY_SECONDS, MAX_DAILY_ATTEMPTS,
    )
    last_success_date: date | None = None
    attempt_date: date | None = None
    attempts = 0
    next_attempt_at: datetime | None = None

    while True:
        now = datetime.now(IST)
        today = now.date()

        # Reset the per-day attempt counter when the date rolls over.
        if attempt_date != today:
            attempt_date = today
            attempts = 0
            next_attempt_at = None

        due = (
            last_success_date != today
            and is_nse_trading_day(today)
            and now.timetz().replace(tzinfo=None) >= SYNC_TIME
            and attempts < MAX_DAILY_ATTEMPTS
            and (next_attempt_at is None or now >= next_attempt_at)
        )

        if due:
            attempts += 1
            # Only a SUCCESSFUL run marks the day done. A failure schedules a
            # retry instead of silently skipping until tomorrow.
            if _run_sync():
                last_success_date = today
            else:
                next_attempt_at = now + timedelta(seconds=RETRY_SECONDS)
                if attempts >= MAX_DAILY_ATTEMPTS:
                    LOGGER.error(
                        "Instrument sync failed %s times today; giving up until "
                        "tomorrow. Active instruments may be stale — run "
                        "downloader/sync_instruments.py by hand to recover.",
                        attempts,
                    )
                else:
                    LOGGER.warning(
                        "Instrument sync attempt %s/%s failed; retrying at %s",
                        attempts, MAX_DAILY_ATTEMPTS,
                        next_attempt_at.strftime("%H:%M:%S"),
                    )

        time.sleep(POLL_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("INSTRUMENT_SYNC_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
