"""Window slicing: the three carving rules features depend on."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from features.windows import (
    BarWindow,
    crosses_session,
    fetch_days_for,
    in_session,
    log_returns,
    session_span,
    simple_return,
    stdev,
    true_ranges,
    window_start_for,
)
from tests.featurelib import make_bar, session, sessions, trading_days

DAYS = trading_days(date(2026, 3, 2), 8)


def window_at(bars, moment, timeframe="5m") -> BarWindow:
    return BarWindow.build(bars, moment, timeframe)


class TestInSession:
    @pytest.mark.parametrize(
        "moment,expected",
        [
            (datetime(2026, 3, 2, 9, 14), False),
            (datetime(2026, 3, 2, 9, 15), True),
            (datetime(2026, 3, 2, 15, 25), True),
            (datetime(2026, 3, 2, 15, 29), True),
            (datetime(2026, 3, 2, 15, 30), False),
            (datetime(2026, 3, 2, 16, 0), False),
        ],
    )
    def test_boundaries(self, moment, expected):
        assert in_session(moment) is expected

    def test_pre_open_bars_are_filtered_out(self):
        """A 09:10 print must never become 'the session open'."""
        day = DAYS[0]
        bars = [make_bar(datetime.combine(day, time(9, 10)), 24_000.0)]
        bars += session(day, count=3)
        window = window_at(bars, datetime.combine(day, time(10, 0)))
        assert len(window) == 3
        assert window.bars[0].timestamp.time() == time(9, 15)

    def test_post_close_bars_are_filtered_out(self):
        day = DAYS[0]
        bars = session(day, count=3)
        bars.append(make_bar(datetime.combine(day, time(15, 35)), 24_000.0))
        window = window_at(bars, datetime.combine(day, time(15, 29)))
        assert len(window) == 3


class TestSlicing:
    def test_intraday_returns_only_todays_bars(self):
        bars = sessions(DAYS[:3], count=10)
        window = window_at(bars, datetime.combine(DAYS[2], time(12, 0)))
        assert len(window.intraday()) == 10
        assert {b.timestamp.date() for b in window.intraday()} == {DAYS[2]}

    def test_rolling_crosses_sessions(self):
        bars = sessions(DAYS[:3], count=10)
        window = window_at(bars, datetime.combine(DAYS[2], time(12, 0)))
        assert len({b.timestamp.date() for b in window.rolling(25)}) == 3

    def test_rolling_returns_the_most_recent(self):
        bars = sessions(DAYS[:2], count=10)
        window = window_at(bars, datetime.combine(DAYS[1], time(12, 0)))
        assert window.rolling(3) == window.bars[-3:]

    def test_rolling_of_zero_is_empty(self):
        bars = sessions(DAYS[:1], count=5)
        window = window_at(bars, datetime.combine(DAYS[0], time(12, 0)))
        assert window.rolling(0) == ()

    def test_session_dates_are_observed_not_predicted(self):
        # A calendar would call Wednesday a trading day; only the data decides.
        observed = [DAYS[0], DAYS[1], DAYS[3]]
        bars = sessions(observed, count=5)
        window = window_at(bars, datetime.combine(DAYS[3], time(12, 0)))
        assert window.session_dates == tuple(observed)

    def test_prior_sessions_exclude_today(self):
        bars = sessions(DAYS[:5], count=5)
        window = window_at(bars, datetime.combine(DAYS[4], time(12, 0)))
        assert window.prior_session_dates(3) == tuple(DAYS[1:4])

    def test_prior_sessions_returns_fewer_when_history_is_short(self):
        bars = sessions(DAYS[:2], count=5)
        window = window_at(bars, datetime.combine(DAYS[1], time(12, 0)))
        assert window.prior_session_dates(5) == (DAYS[0],)

    def test_trailing_gathers_whole_sessions(self):
        bars = sessions(DAYS[:5], count=7)
        window = window_at(bars, datetime.combine(DAYS[4], time(12, 0)))
        assert len(window.trailing(2)) == 14

    def test_trailing_skips_a_missing_day(self):
        """Five *observed* sessions, not five calendar days back."""
        observed = [DAYS[0], DAYS[1], DAYS[4], DAYS[5], DAYS[6], DAYS[7]]
        bars = sessions(observed, count=5)
        window = window_at(bars, datetime.combine(DAYS[7], time(12, 0)))
        assert window.prior_session_dates(3) == (DAYS[4], DAYS[5], DAYS[6])

    def test_session_open_bar_found(self):
        bars = session(DAYS[0], count=5)
        window = window_at(bars, datetime.combine(DAYS[0], time(12, 0)))
        assert window.session_open_bar().timestamp.time() == time(9, 15)

    def test_session_open_bar_absent_when_session_starts_late(self):
        bars = session(DAYS[0], count=10)[4:]
        window = window_at(bars, datetime.combine(DAYS[0], time(12, 0)))
        assert window.session_open_bar() is None

    def test_previous_session_close(self):
        bars = sessions(DAYS[:2], count=5)
        window = window_at(bars, datetime.combine(DAYS[1], time(12, 0)))
        yesterday = [b for b in bars if b.timestamp.date() == DAYS[0]]
        assert window.previous_session_close() == yesterday[-1].close

    def test_previous_session_close_absent_on_the_first_session(self):
        bars = session(DAYS[0], count=5)
        window = window_at(bars, datetime.combine(DAYS[0], time(12, 0)))
        assert window.previous_session_close() is None


class TestLogReturns:
    def test_boundary_return_is_excluded(self):
        # Two 3-bar sessions: 4 consecutive pairs, of which one spans the gap.
        bars = sessions(DAYS[:2], count=3)
        assert len(log_returns(bars)) == 4
        assert len(log_returns(bars, exclude_boundary=False)) == 5

    def test_the_excluded_return_is_the_overnight_one(self):
        """A 10% overnight jump must not appear among the returns."""
        bars = sessions(DAYS[:2], count=3, gap=0.10)
        kept = log_returns(bars)
        assert max(abs(r) for r in kept) < 0.01
        with_boundary = log_returns(bars, exclude_boundary=False)
        assert max(abs(r) for r in with_boundary) > 0.09

    def test_non_positive_closes_are_skipped(self):
        day = DAYS[0]
        base = datetime.combine(day, time(9, 15))
        bars = [
            make_bar(base, 100.0),
            make_bar(base + timedelta(minutes=5), 0.0, high=100.0, low=0.0),
            make_bar(base + timedelta(minutes=10), 100.0),
        ]
        assert log_returns(bars) == []

    def test_single_bar_yields_no_returns(self):
        assert log_returns(session(DAYS[0], count=1)) == []


class TestTrueRanges:
    def test_first_bar_uses_its_own_span(self):
        bars = session(DAYS[0], count=3)
        assert true_ranges(bars)[0] == pytest.approx(bars[0].high - bars[0].low)

    def test_session_start_drops_the_previous_close_term(self):
        """Yesterday's close must not become today's true range."""
        bars = sessions(DAYS[:2], count=3, gap=0.10)
        ranges = true_ranges(bars)
        first_of_day_two = ranges[3]
        bar = bars[3]
        assert first_of_day_two == pytest.approx(bar.high - bar.low)

    def test_mid_session_bar_uses_the_previous_close(self):
        day = DAYS[0]
        base = datetime.combine(day, time(9, 15))
        bars = [
            make_bar(base, 100.0, open_=100.0, high=100.5, low=99.5),
            make_bar(base + timedelta(minutes=5), 105.0, open_=104.0, high=105.5, low=104.0),
        ]
        # high - prev_close = 105.5 - 100 = 5.5, wider than the 1.5 own span.
        assert true_ranges(bars)[1] == pytest.approx(5.5)


class TestHelpers:
    def test_crosses_session(self):
        a = make_bar(datetime.combine(DAYS[0], time(15, 25)), 100.0)
        b = make_bar(datetime.combine(DAYS[1], time(9, 15)), 100.0)
        c = make_bar(datetime.combine(DAYS[1], time(9, 20)), 100.0)
        assert crosses_session(a, b)
        assert not crosses_session(b, c)

    def test_simple_return(self):
        assert simple_return(100.0, 110.0) == pytest.approx(0.1)
        assert simple_return(0.0, 110.0) is None

    def test_stdev_needs_two_values(self):
        assert stdev([1.0]) is None
        assert stdev([]) is None
        assert stdev([1.0, 3.0]) == pytest.approx(1.0)

    def test_session_span(self):
        bars = session(DAYS[0], count=5)
        low, high = session_span(bars)
        assert low == min(b.low for b in bars)
        assert high == max(b.high for b in bars)
        assert session_span([]) is None

    def test_fetch_days_covers_long_weekends(self):
        # Observed gaps between sessions reach 4 calendar days.
        assert fetch_days_for(5) >= 5 * 4

    def test_window_start_is_before_the_decision(self):
        moment = datetime(2026, 3, 10, 12, 0)
        assert window_start_for(moment, 5) < moment
