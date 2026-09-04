"""Shared fixtures. Everything here is deterministic — no DB, no network.

The tests never rely on the ambient NSE holiday calendar or on today's date:
each test that cares about trading days injects its own ``is_trading_day``
predicate, so results cannot drift as the bundled calendar is extended.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

# Make the project root importable when pytest is run from anywhere.
ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 2026-09-03 is a Thursday; 2026-09-05/06 is the following weekend.
TRADING_DAY = date(2026, 9, 3)
NEXT_TRADING_DAY = date(2026, 9, 4)
SATURDAY = date(2026, 9, 5)
MONDAY = date(2026, 9, 7)
HOLIDAY = date(2026, 9, 14)  # in NSE_HOLIDAYS_2026


@pytest.fixture
def weekday_calendar():
    """Trading-day predicate: weekdays only, with one fixed holiday."""

    def is_trading_day(day: date) -> bool:
        return day.weekday() < 5 and day != HOLIDAY

    return is_trading_day


def bar(
    timestamp: datetime,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: int = 1_000,
    instrument_id: int = 1,
) -> dict:
    """A contract-valid OHLCV bar, overridable field by field."""
    return {
        "instrument_id": instrument_id,
        "timestamp": timestamp,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def bars(
    start: datetime,
    count: int,
    bar_minutes: int = 5,
    instrument_id: int = 1,
) -> list[dict]:
    """A run of *count* consecutive valid bars starting at *start*."""
    return [
        bar(start + timedelta(minutes=bar_minutes * i), instrument_id=instrument_id)
        for i in range(count)
    ]


def tick(
    timestamp: datetime,
    ltp: float = 100.0,
    instrument_id: int = 1,
    **extra,
) -> dict:
    """A contract-valid tick, overridable field by field."""
    record = {
        "instrument_id": instrument_id,
        "timestamp": timestamp,
        "ltp": ltp,
    }
    record.update(extra)
    return record
