"""NSE trading-day and five-minute-bar scheduling helpers."""

import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

# NSE F&O holidays for 2026, plus the January 15 Maharashtra municipal
# election holiday that affected the market.  The environment variables below
# allow the calendar to be extended without changing code.
NSE_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 15),
        date(2026, 1, 26),
        date(2026, 3, 3),
        date(2026, 3, 26),
        date(2026, 3, 31),
        date(2026, 4, 3),
        date(2026, 4, 14),
        date(2026, 5, 1),
        date(2026, 5, 28),
        date(2026, 6, 26),
        date(2026, 9, 14),
        date(2026, 10, 2),
        date(2026, 10, 20),
        date(2026, 11, 10),
        date(2026, 11, 24),
        date(2026, 12, 25),
    }
)

# The Union Budget had a normal NSE live session on this Sunday.
NSE_SPECIAL_TRADING_DAYS_2026 = frozenset({date(2026, 2, 1)})

FIRST_DOWNLOAD_SLOT = time(9, 20)  # the 09:15 candle has closed
LAST_DOWNLOAD_SLOT = time(15, 30)  # the 15:25 candle has closed


def _parse_dates(value: str | None) -> set[date]:
    if not value:
        return set()

    parsed = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            parsed.add(date.fromisoformat(item))
    return parsed


def configured_holidays() -> set[date]:
    """Return built-in holidays plus ``NSE_HOLIDAYS`` overrides."""
    return set(NSE_HOLIDAYS_2026) | _parse_dates(os.getenv("NSE_HOLIDAYS"))


def configured_special_trading_days() -> set[date]:
    """Return known special sessions plus ``NSE_SPECIAL_TRADING_DAYS``."""
    return set(NSE_SPECIAL_TRADING_DAYS_2026) | _parse_dates(
        os.getenv("NSE_SPECIAL_TRADING_DAYS")
    )


def in_ist(value: datetime) -> datetime:
    """Interpret a naive datetime as IST and convert aware values to IST."""
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def is_nse_trading_day(
    value: date | datetime,
    holidays: set[date] | None = None,
    special_trading_days: set[date] | None = None,
) -> bool:
    """Return whether NSE has a regular trading session on the given date."""
    trading_date = value.date() if isinstance(value, datetime) else value
    holidays = configured_holidays() if holidays is None else holidays
    special_trading_days = (
        configured_special_trading_days()
        if special_trading_days is None
        else special_trading_days
    )

    if trading_date in special_trading_days:
        return True
    return trading_date.weekday() < 5 and trading_date not in holidays


def due_download_slot(
    now: datetime,
    settle_delay_seconds: int = 30,
) -> datetime | None:
    """Return the current eligible five-minute slot, otherwise ``None``.

    A short delay allows Angel One to finish publishing the just-closed bar.
    """
    if not 0 <= settle_delay_seconds < 60:
        raise ValueError("settle_delay_seconds must be between 0 and 59")

    now = in_ist(now)
    if not is_nse_trading_day(now):
        return None
    if now.second < settle_delay_seconds or now.minute % 5:
        return None

    slot = now.replace(second=0, microsecond=0)
    if FIRST_DOWNLOAD_SLOT <= slot.time() <= LAST_DOWNLOAD_SLOT:
        return slot
    return None
