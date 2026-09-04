"""The session model decides what "missing data" even means."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from marketdata.sessions import (
    MarketSession,
    REGULAR_SESSION,
    bar_close,
    expected_bar_starts,
    is_aligned,
    is_in_session,
    trading_days,
)
from tests.conftest import HOLIDAY, MONDAY, SATURDAY, TRADING_DAY


def test_regular_session_is_the_nse_cash_session():
    assert REGULAR_SESSION.open == time(9, 15)
    assert REGULAR_SESSION.close == time(15, 30)


def test_five_minute_day_has_75_bars_from_0915_to_1525():
    starts = REGULAR_SESSION.bar_starts(TRADING_DAY, 5)
    assert len(starts) == 75
    assert starts[0] == datetime(2026, 9, 3, 9, 15)
    assert starts[-1] == datetime(2026, 9, 3, 15, 25)


def test_one_minute_day_has_375_bars_from_0915_to_1529():
    starts = REGULAR_SESSION.bar_starts(TRADING_DAY, 1)
    assert len(starts) == 375
    assert starts[-1] == datetime(2026, 9, 3, 15, 29)


def test_no_bar_starts_at_the_close():
    # 15:30 is the end of the session, so a bar starting there would never
    # complete. Both timeframes must exclude it.
    for minutes in (1, 5):
        assert datetime(2026, 9, 3, 15, 30) not in REGULAR_SESSION.bar_starts(
            TRADING_DAY, minutes
        )


def test_bars_per_day_agrees_with_bar_starts():
    for minutes in (1, 5, 15):
        assert REGULAR_SESSION.bars_per_day(minutes) == len(
            REGULAR_SESSION.bar_starts(TRADING_DAY, minutes)
        )


def test_bars_per_day_matches_the_existing_backtest_constant():
    # backtest/rv.py annualises with 75 five-minute candles per day; the
    # session model must not silently disagree with it.
    from backtest.rv import CANDLES_PER_DAY

    assert REGULAR_SESSION.bars_per_day(5) == CANDLES_PER_DAY


def test_session_close_is_exclusive_and_open_inclusive():
    assert REGULAR_SESSION.contains(datetime(2026, 9, 3, 9, 15))
    assert REGULAR_SESSION.contains(datetime(2026, 9, 3, 15, 29, 59))
    assert not REGULAR_SESSION.contains(datetime(2026, 9, 3, 15, 30))
    assert not REGULAR_SESSION.contains(datetime(2026, 9, 3, 9, 14, 59))


def test_bar_starts_rejects_a_non_positive_width():
    with pytest.raises(ValueError):
        REGULAR_SESSION.bar_starts(TRADING_DAY, 0)


def test_session_rejects_inverted_hours():
    with pytest.raises(ValueError):
        MarketSession(open=time(15, 30), close=time(9, 15))


def test_alignment_is_anchored_at_the_session_open_not_the_hour():
    # 09:15 anchoring is what makes 09:20 a valid 5-minute bar and 09:22 not.
    assert is_aligned(datetime(2026, 9, 3, 9, 15), 5)
    assert is_aligned(datetime(2026, 9, 3, 9, 20), 5)
    assert not is_aligned(datetime(2026, 9, 3, 9, 22), 5)
    assert not is_aligned(datetime(2026, 9, 3, 9, 20, 30), 5)


def test_every_minute_is_aligned_for_one_minute_bars():
    assert is_aligned(datetime(2026, 9, 3, 9, 22), 1)
    assert not is_aligned(datetime(2026, 9, 3, 9, 22, 15), 1)


def test_trading_days_skips_weekends_and_holidays(weekday_calendar):
    days = list(trading_days(SATURDAY, MONDAY, weekday_calendar))
    assert days == [MONDAY]

    span = list(trading_days(date(2026, 9, 11), date(2026, 9, 15), weekday_calendar))
    assert HOLIDAY not in span
    assert span == [date(2026, 9, 11), date(2026, 9, 15)]


def test_expected_bars_span_only_live_sessions(weekday_calendar):
    # Thursday 15:00 through Monday 09:30 crosses an overnight close, a full
    # weekend and a Friday session.
    expected = expected_bar_starts(
        datetime(2026, 9, 3, 15, 0),
        datetime(2026, 9, 7, 9, 30),
        5,
        is_trading_day=weekday_calendar,
    )
    # Thu 15:00-15:25 (6) + all of Fri (75) + Mon 09:15-09:30 (4)
    assert len(expected) == 6 + 75 + 4
    assert all(d.weekday() < 5 for d in expected)
    assert expected[0] == datetime(2026, 9, 3, 15, 0)
    assert expected[-1] == datetime(2026, 9, 7, 9, 30)


def test_expected_bars_exclude_a_holiday(weekday_calendar):
    expected = expected_bar_starts(
        datetime(2026, 9, 14, 9, 15),
        datetime(2026, 9, 14, 15, 25),
        5,
        is_trading_day=weekday_calendar,
    )
    assert expected == []


def test_expected_bars_reject_an_inverted_window():
    with pytest.raises(ValueError):
        expected_bar_starts(
            datetime(2026, 9, 4), datetime(2026, 9, 3), 5
        )


def test_in_session_needs_both_a_trading_day_and_trading_hours(weekday_calendar):
    assert is_in_session(datetime(2026, 9, 3, 10, 0), is_trading_day=weekday_calendar)
    assert not is_in_session(datetime(2026, 9, 3, 3, 45), is_trading_day=weekday_calendar)
    assert not is_in_session(datetime(2026, 9, 5, 10, 0), is_trading_day=weekday_calendar)
    assert not is_in_session(datetime(2026, 9, 14, 10, 0), is_trading_day=weekday_calendar)


def test_bar_close_is_the_completion_instant():
    assert bar_close(datetime(2026, 9, 3, 9, 15), 5) == datetime(2026, 9, 3, 9, 20)
    assert bar_close(datetime(2026, 9, 3, 9, 15), 1) == datetime(2026, 9, 3, 9, 16)
