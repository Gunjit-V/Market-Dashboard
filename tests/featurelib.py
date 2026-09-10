"""Deterministic synthetic bars for feature tests.

No database, no network, no randomness that varies between runs: prices follow
a seeded walk, so every test sees identical input on every machine.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from marketdata.access import Bar

SESSION_OPEN = time(9, 15)
BARS_PER_SESSION = {"1m": 375, "5m": 75}


def make_bar(
    timestamp: datetime,
    close: float,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: int = 0,
    timeframe: str = "5m",
) -> Bar:
    """One contract-valid bar, with sensible geometry derived from *close*."""
    open_ = close if open_ is None else open_
    high = max(open_, close) if high is None else high
    low = min(open_, close) if low is None else low
    return Bar(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        instrument_id=1,
        timeframe=timeframe,
    )


def session(
    day: date,
    count: int | None = None,
    start_price: float = 24_000.0,
    timeframe: str = "5m",
    seed: int = 0,
    step_minutes: int | None = None,
) -> list[Bar]:
    """One session of bars on the grid anchored at 09:15."""
    step = step_minutes or (5 if timeframe == "5m" else 1)
    count = count if count is not None else BARS_PER_SESSION[timeframe]
    rng = random.Random(seed)
    bars: list[Bar] = []
    price = start_price
    base = datetime.combine(day, SESSION_OPEN)
    for index in range(count):
        drift = rng.uniform(-0.0008, 0.0008)
        open_ = price
        close = price * (1 + drift)
        spread = abs(close - open_) + price * 0.0004
        bars.append(
            make_bar(
                base + timedelta(minutes=step * index),
                close=close,
                open_=open_,
                high=max(open_, close) + spread * 0.5,
                low=min(open_, close) - spread * 0.5,
                timeframe=timeframe,
            )
        )
        price = close
    return bars


def sessions(
    days: list[date],
    count: int | None = None,
    start_price: float = 24_000.0,
    timeframe: str = "5m",
    gap: float = 0.0,
) -> list[Bar]:
    """Consecutive sessions, optionally separated by an overnight gap.

    *gap* is a fractional jump applied at each session open, so tests can make
    the boundary return large enough to be unmistakable in a volatility figure.
    """
    out: list[Bar] = []
    price = start_price
    for index, day in enumerate(days):
        day_bars = session(
            day, count=count, start_price=price, timeframe=timeframe, seed=index
        )
        out.extend(day_bars)
        price = day_bars[-1].close * (1 + gap)
    return out


def trading_days(start: date, n: int) -> list[date]:
    """*n* consecutive weekdays from *start*, skipping weekends."""
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def bar_close_time(day: date, index: int, timeframe: str = "5m") -> datetime:
    """The instant bar *index* of *day* completes -- a valid decision time."""
    step = 5 if timeframe == "5m" else 1
    return datetime.combine(day, SESSION_OPEN) + timedelta(minutes=step * (index + 1))
