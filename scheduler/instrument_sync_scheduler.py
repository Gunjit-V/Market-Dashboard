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
from datetime import date, datetime, time as clock_time
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


def _run_sync() -> None:
    try:
        LOGGER.info("Starting instrument sync")
        sync_all(save_csv=SAVE_CSV)
        LOGGER.info("Finished instrument sync")
    except Exception:
        LOGGER.exception("Unhandled error during instrument sync")


def run_forever() -> None:
    LOGGER.info("Instrument sync scheduler started; sync_time=%s", SYNC_TIME)
    last_run_date: date | None = None

    while True:
        now = datetime.now(IST)

        if (
            last_run_date != now.date()
            and is_nse_trading_day(now.date())
            and now.timetz().replace(tzinfo=None) >= SYNC_TIME
        ):
            last_run_date = now.date()
            _run_sync()

        time.sleep(POLL_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("INSTRUMENT_SYNC_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
