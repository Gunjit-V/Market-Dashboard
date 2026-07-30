"""Keep selected five-minute instruments current during NSE market hours."""

import logging
import os
import time
from datetime import datetime

from downloader.ohlcv_5min import authenticate, download_historical_data
from scheduler.nse_calendar import (
    IST,
    LAST_DOWNLOAD_SLOT,
    due_download_slot,
    is_nse_trading_day,
)


LOG = logging.getLogger(__name__)
DEFAULT_SYMBOLS = "Nifty 50"
DEFAULT_INSTRUMENT_TYPES = "AMXIDX"


def csv_setting(name: str, default: str) -> list[str]:
    """Read a comma-separated environment setting."""
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def run_scheduler() -> None:
    """Run one incremental pull per completed five-minute NSE candle."""
    symbols = csv_setting("FIVE_MINUTE_SYMBOLS", DEFAULT_SYMBOLS)
    instrument_types = csv_setting(
        "FIVE_MINUTE_INSTRUMENT_TYPES", DEFAULT_INSTRUMENT_TYPES
    )
    poll_seconds = max(1, int(os.getenv("FIVE_MINUTE_POLL_SECONDS", "15")))
    settle_delay_seconds = int(os.getenv("FIVE_MINUTE_SETTLE_DELAY_SECONDS", "30"))

    if not symbols and not instrument_types:
        raise ValueError(
            "Set FIVE_MINUTE_SYMBOLS and/or FIVE_MINUTE_INSTRUMENT_TYPES"
        )

    LOG.info(
        "Starting five-minute scheduler for types=%s symbols=%s",
        instrument_types,
        symbols,
    )

    smart_api = None
    session_date = None
    last_slot = None

    while True:
        now = datetime.now(IST)

        # Windows Task Scheduler starts this process once each morning.  Leave
        # promptly on a weekend/holiday and after the final 15:25 candle has
        # had a chance to settle, rather than retaining a background process
        # overnight.
        if not is_nse_trading_day(now):
            LOG.info("No NSE session on %s; exiting", now.date())
            return
        if now.time() > LAST_DOWNLOAD_SLOT:
            LOG.info("NSE five-minute session finished for %s; exiting", now.date())
            return

        slot = due_download_slot(now, settle_delay_seconds)

        if slot and slot != last_slot:
            # Claim the slot before downloading so a slow API call cannot
            # cause duplicate runs. The database cursor remains idempotent
            # across process restarts.
            last_slot = slot

            if smart_api is None or session_date != now.date():
                LOG.info("Authenticating Angel One session for %s", now.date())
                smart_api = authenticate()
                session_date = now.date() if smart_api else None

            if smart_api:
                LOG.info("Running five-minute pull for completed slot %s", slot)
                download_historical_data(
                    instrument_types=instrument_types,
                    symbols=symbols,
                    days=1,
                    smart_api=smart_api,
                )
            else:
                LOG.error("Skipping slot %s because authentication failed", slot)

        time.sleep(poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_scheduler()
