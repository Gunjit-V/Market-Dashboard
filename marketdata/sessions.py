"""NSE session model: which bar timestamps are *expected* to exist.

Gap detection is only meaningful against an explicit session model.  Weekends,
exchange holidays and the daily 15:30-09:15 close are *expected* absences and
must never be reported as missing data; only bars absent from inside a live
session are candidates for a real gap.

The trading-day calendar is reused from ``scheduler.nse_calendar`` (the same
one the schedulers already trust) rather than duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterator

from scheduler.nse_calendar import IST, is_nse_trading_day

# Regular NSE/BSE equity, index and F&O session (IST).
DEFAULT_SESSION_OPEN = time(9, 15)
DEFAULT_SESSION_CLOSE = time(15, 30)

#: Callable deciding whether a date is a trading day.
TradingDayFn = Callable[[date], bool]


@dataclass(frozen=True)
class MarketSession:
    """A single continuous trading session, expressed in naive IST clock time."""

    open: time = DEFAULT_SESSION_OPEN
    close: time = DEFAULT_SESSION_CLOSE

    def __post_init__(self) -> None:
        if self.open >= self.close:
            raise ValueError("Session open must be strictly before session close")

    def contains(self, moment: datetime) -> bool:
        """Whether *moment*'s clock time falls inside the session.

        The close boundary is exclusive: 15:30 is when the session ends, so no
        bar *starts* at 15:30 and no trade is stamped after it.
        """
        return self.open <= moment.time() < self.close

    def bar_starts(self, day: date, bar_minutes: int) -> list[datetime]:
        """Every bar-start timestamp expected on *day* for a *bar_minutes* bar.

        A bar is only expected if it closes on or before the session close, so
        a 5-minute session day yields 09:15 … 15:25 and never a truncated
        15:30 bar.
        """
        if bar_minutes <= 0:
            raise ValueError("bar_minutes must be positive")

        starts: list[datetime] = []
        cursor = datetime.combine(day, self.open)
        session_end = datetime.combine(day, self.close)
        step = timedelta(minutes=bar_minutes)
        while cursor + step <= session_end:
            starts.append(cursor)
            cursor += step
        return starts

    def bars_per_day(self, bar_minutes: int) -> int:
        seconds = (
            datetime.combine(date(2000, 1, 1), self.close)
            - datetime.combine(date(2000, 1, 1), self.open)
        ).total_seconds()
        return int(seconds // (bar_minutes * 60))


REGULAR_SESSION = MarketSession()


def _default_trading_day(day: date) -> bool:
    return is_nse_trading_day(day)


def trading_days(
    start: date,
    end: date,
    is_trading_day: TradingDayFn | None = None,
) -> Iterator[date]:
    """Yield each trading day in the inclusive range ``[start, end]``."""
    check = is_trading_day or _default_trading_day
    day = start
    while day <= end:
        if check(day):
            yield day
        day += timedelta(days=1)


def expected_bar_starts(
    start: datetime,
    end: datetime,
    bar_minutes: int,
    session: MarketSession = REGULAR_SESSION,
    is_trading_day: TradingDayFn | None = None,
) -> list[datetime]:
    """Bar-start timestamps expected in the inclusive window ``[start, end]``.

    Only in-session bars on trading days are returned, so the result is the
    correct denominator for "how much data should be here?" — weekends,
    holidays and overnight closes simply do not appear.
    """
    if end < start:
        raise ValueError("end must not be before start")

    expected: list[datetime] = []
    for day in trading_days(start.date(), end.date(), is_trading_day):
        for moment in session.bar_starts(day, bar_minutes):
            if start <= moment <= end:
                expected.append(moment)
    return expected


def is_in_session(
    moment: datetime,
    session: MarketSession = REGULAR_SESSION,
    is_trading_day: TradingDayFn | None = None,
) -> bool:
    """Whether *moment* falls inside a live trading session."""
    check = is_trading_day or _default_trading_day
    return check(moment.date()) and session.contains(moment)


def is_aligned(moment: datetime, bar_minutes: int,
               session: MarketSession = REGULAR_SESSION) -> bool:
    """Whether *moment* sits on the bar grid anchored at the session open.

    Anchoring at the session open (09:15) rather than at the top of the hour is
    what makes 09:15/09:20/… the 5-minute grid, matching both the Angel One
    feed and the ``backfill_from_one_minute`` bucketing in downloader/ohlcv.py.
    """
    if bar_minutes <= 0:
        raise ValueError("bar_minutes must be positive")
    anchor = datetime.combine(moment.date(), session.open)
    delta = moment - anchor
    if delta.microseconds or delta.seconds % 60:
        return False
    return int(delta.total_seconds()) % (bar_minutes * 60) == 0


def bar_close(bar_start: datetime, bar_minutes: int) -> datetime:
    """The instant a left-labelled bar becomes complete."""
    return bar_start + timedelta(minutes=bar_minutes)


__all__ = [
    "IST",
    "DEFAULT_SESSION_OPEN",
    "DEFAULT_SESSION_CLOSE",
    "MarketSession",
    "REGULAR_SESSION",
    "bar_close",
    "expected_bar_starts",
    "is_aligned",
    "is_in_session",
    "trading_days",
]
