"""Run the 1-minute and 5-minute OHLCV downloads during market hours.

This module intentionally uses only the Python standard library, so it can be
run as a Windows service (for example through NSSM) without Windows Task
Scheduler.  The downloader is incremental, therefore invoking it once per bar
is safe: rows already present are skipped by the downloader's database logic.

Configure the instrument filters with environment variables:

    OHLCV_INSTRUMENT_TYPES=AMXIDX
    OHLCV_NAMES=NIFTY,BANKNIFTY,SENSEX,BANKEX

Trading-day and holiday logic comes from scheduler.nse_calendar, which ships
a curated NSE holiday calendar and can be extended without a code change via
the NSE_HOLIDAYS / NSE_SPECIAL_TRADING_DAYS environment variables (see that
module for details).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo

from downloader.ohlcv import authenticate, download_historical_data
from scheduler.nse_calendar import is_nse_trading_day


LOGGER = logging.getLogger("ohlcv_scheduler")
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = clock_time(9, 15)
MARKET_CLOSE = clock_time(15, 30)
BAR_DELAY_SECONDS = int(os.getenv("OHLCV_BAR_DELAY_SECONDS", "10"))


def _csv(name: str, default: str = "") -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


INSTRUMENT_TYPES = _csv("OHLCV_INSTRUMENT_TYPES", "AMXIDX,FUTIDX,OPTIDX")
NAMES = _csv("OHLCV_NAMES", "NIFTY,BANKNIFTY,SENSEX,BANKEX")
SYMBOLS = _csv("OHLCV_SYMBOLS")


def in_market_hours(moment: datetime) -> bool:
    current = moment.timetz().replace(tzinfo=None)
    return MARKET_OPEN <= current < MARKET_CLOSE


class SessionCache:
    """Holds one authenticated SmartAPI session, shared across downloads.

    Without this, download_historical_data() authenticates on every call:
    1m runs every trading minute and 5m every five, so a session produced
    ~450 logins and Angel One rate-limited the login endpoint (49 transient
    "Authentication error" retries on 2026-09-07 alone). The downloader has
    always accepted a `smart_api` argument for exactly this purpose; it was
    simply never passed.

    The session is re-created when a download reports it is unusable, so an
    expired token still recovers on the next bar rather than wedging.
    """

    def __init__(self, ttl_minutes: int = 60) -> None:
        self._lock = threading.Lock()
        self._session = None
        self._created_at: datetime | None = None
        self._ttl = timedelta(minutes=ttl_minutes)

    def get(self):
        """Return a live session, authenticating only when needed."""
        with self._lock:
            now = datetime.now(IST)
            if (
                self._session is not None
                and self._created_at is not None
                and now - self._created_at < self._ttl
            ):
                return self._session

            session = authenticate()
            if session is not None:
                self._session = session
                self._created_at = now
            return session

    def invalidate(self) -> None:
        """Drop the cached session so the next download re-authenticates."""
        with self._lock:
            self._session = None
            self._created_at = None


class IntervalRunner:
    """Runs at most one download for each interval at a time."""

    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ohlcv")
        self.locks = {interval: threading.Lock() for interval in ("1m", "5m")}
        self.futures: set[Future] = set()
        self.sessions = SessionCache()

    def submit(self, interval: str) -> None:
        lock = self.locks[interval]
        if not lock.acquire(blocking=False):
            LOGGER.warning("Skipping %s run: previous run is still active", interval)
            return

        future = self.executor.submit(self._run, interval, lock, self.sessions)
        self.futures.add(future)
        future.add_done_callback(self.futures.discard)

    @staticmethod
    def _run(interval: str, lock: threading.Lock, sessions: "SessionCache") -> None:
        try:
            LOGGER.info("Starting %s download", interval)
            session = sessions.get()
            if session is None:
                LOGGER.error("Skipping %s download: authentication failed", interval)
                return
            download_historical_data(
                interval=interval,
                instrument_types=INSTRUMENT_TYPES or None,
                names=NAMES or None,
                symbols=SYMBOLS or None,
                smart_api=session,
            )
            LOGGER.info("Finished %s download", interval)
        except Exception:
            # The session may be the reason; force a fresh login next bar
            # rather than reusing a token that might be dead.
            sessions.invalidate()
            LOGGER.exception("Unhandled error in %s download", interval)
        finally:
            lock.release()

    def close(self) -> None:
        self.executor.shutdown(wait=True)


def _next_tick(now: datetime) -> datetime:
    """Return the next minute boundary plus the configured bar-finalization delay."""
    next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return next_minute + timedelta(seconds=BAR_DELAY_SECONDS)


def run_forever() -> None:
    LOGGER.info("OHLCV scheduler started")
    runner = IntervalRunner()
    last_tick: tuple[date, int] | None = None

    try:
        while True:
            now = datetime.now(IST)
            tick = (now.date(), now.hour * 60 + now.minute)

            if (
                tick != last_tick
                and now.second >= BAR_DELAY_SECONDS
                and is_nse_trading_day(now.date())
                and in_market_hours(now)
            ):
                # 1m runs on every market minute; 5m runs on boundaries aligned
                # with the 09:15 session open (09:15, 09:20, ...).
                runner.submit("1m")
                if (now.hour * 60 + now.minute - 9 * 60 - 15) % 5 == 0:
                    runner.submit("5m")
                last_tick = tick

            if now.second < BAR_DELAY_SECONDS:
                sleep_for = BAR_DELAY_SECONDS - now.second
            else:
                sleep_for = (_next_tick(now) - now).total_seconds()
            sleep_for = max(0.2, sleep_for)
            time.sleep(min(sleep_for, 30.0))
    finally:
        runner.close()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("OHLCV_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
