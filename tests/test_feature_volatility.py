"""Volatility features, and the session-boundary exclusion they depend on.

The exclusion is the reason this family may cross sessions at all, so it gets
the most direct tests here: a large overnight gap is injected and each
estimator is asserted to be unmoved by it.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time

import pytest

from features.registry import state_features
from features.spec import ROLLING, TRAILING, FeatureSpec
from features.state import FeatureStatus
from features.volatility import (
    TRADING_DAYS,
    annualization,
    bars_for,
    compute_atr_rel,
    compute_parkinson,
    compute_rv_accel,
    compute_rv_baseline,
    compute_rv_regime,
    compute_rv_short,
)
from features.windows import BarWindow
from tests.featurelib import session, sessions, trading_days

DAYS = trading_days(date(2026, 3, 2), 10)
NOON = time(12, 0)


def spec(name, fn, scope=ROLLING, trailing=None, **params):
    return FeatureSpec(
        name=name,
        family="volatility",
        timeframe="5m",
        lookback_bars=max(2, params.get("returns", params.get("bars", 1)) + 1),
        scope=scope,
        trailing_sessions=trailing,
        params=params,
        fn=fn,
    )


def window(bars, day=None, at=NOON):
    day = day or bars[-1].timestamp.date()
    return BarWindow.build(bars, datetime.combine(day, at), "5m")


class TestBoundaryExclusion:
    """The core correctness property of the rolling scope."""

    def test_overnight_gap_does_not_inflate_rv(self):
        """A 5% overnight jump must leave close-to-close volatility unchanged."""
        quiet = sessions(DAYS[:3], count=30, gap=0.0)
        jumpy = sessions(DAYS[:3], count=30, gap=0.05)
        s = spec("rv_short", compute_rv_short, returns=20)
        without = compute_rv_short(s, window(quiet, DAYS[2]))
        with_gap = compute_rv_short(s, window(jumpy, DAYS[2]))
        assert without.is_valid and with_gap.is_valid
        assert without.value == pytest.approx(with_gap.value, rel=1e-9)

    def test_rv_would_be_inflated_without_the_exclusion(self):
        """Proves the test above is not vacuous: the gap really is enormous.

        Measured on real Nifty 50 data, a boundary return carries 58x the
        variance of an intraday one. Including it here inflates the estimate
        several-fold, which is exactly what the exclusion prevents.
        """
        from features.windows import log_returns, stdev

        jumpy = sessions(DAYS[:3], count=30, gap=0.05)
        bars = window(jumpy, DAYS[2]).rolling(44)
        clean = stdev(log_returns(bars, exclude_boundary=True))
        contaminated = stdev(log_returns(bars, exclude_boundary=False))
        assert contaminated > clean * 3

    def test_parkinson_is_structurally_immune(self):
        """Every term is within one bar, so no boundary handling is needed."""
        quiet = sessions(DAYS[:3], count=30, gap=0.0)
        jumpy = sessions(DAYS[:3], count=30, gap=0.05)
        s = spec("parkinson_short", compute_parkinson, bars=20)
        a = compute_parkinson(s, window(quiet, DAYS[2]))
        b = compute_parkinson(s, window(jumpy, DAYS[2]))
        assert a.value == pytest.approx(b.value, rel=1e-9)

    def test_atr_drops_the_previous_close_term_at_a_session_start(self):
        quiet = sessions(DAYS[:3], count=30, gap=0.0)
        jumpy = sessions(DAYS[:3], count=30, gap=0.05)
        s = spec("atr_rel", compute_atr_rel, bars=14)
        a = compute_atr_rel(s, window(quiet, DAYS[2]))
        b = compute_atr_rel(s, window(jumpy, DAYS[2]))
        # Prices differ after the gap, so compare the ratio, not the level.
        assert a.value == pytest.approx(b.value, rel=1e-6)


class TestAvailability:
    def test_rolling_volatility_is_available_at_the_session_open(self):
        """The payoff for crossing sessions: no morning blackout."""
        bars = sessions(DAYS[:3], count=75)
        at_open = BarWindow.build(
            [b for b in bars if b.timestamp.date() < DAYS[2]],
            datetime.combine(DAYS[2], time(9, 15)),
            "5m",
        )
        value = compute_rv_short(spec("rv_short", compute_rv_short, returns=20), at_open)
        assert value.is_valid

    def test_short_session_still_yields_a_full_window(self):
        """A 17-bar session borrows from the previous one rather than starving."""
        bars = sessions(DAYS[:2], count=75) + session(DAYS[2], count=5)
        value = compute_rv_short(
            spec("rv_short", compute_rv_short, returns=20), window(bars, DAYS[2])
        )
        assert value.is_valid

    def test_insufficient_at_the_very_start_of_history(self):
        bars = session(DAYS[0], count=5)
        value = compute_rv_short(
            spec("rv_short", compute_rv_short, returns=20), window(bars, DAYS[0])
        )
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY
        assert "20" in value.detail


class TestEstimators:
    def test_rv_matches_a_hand_computed_standard_deviation(self):
        from features.windows import log_returns, stdev

        bars = session(DAYS[0], count=40)
        returns = log_returns(bars)[-20:]
        expected = stdev(returns) * annualization("5m")
        value = compute_rv_short(
            spec("rv_short", compute_rv_short, returns=20), window(bars, DAYS[0])
        )
        assert value.value == pytest.approx(expected)

    def test_agrees_with_backtest_rv_on_five_minute_input(self):
        """One volatility number across the dashboard, backtester and features."""
        from backtest.rv import Candle, close_to_close, parkinson

        bars = session(DAYS[0], count=21)
        candles = [
            Candle(timestamp=b.timestamp, open=b.open, high=b.high, low=b.low,
                   close=b.close)
            for b in bars
        ]
        ours = compute_rv_short(
            spec("rv_short", compute_rv_short, returns=20), window(bars, DAYS[0])
        )
        assert ours.value == pytest.approx(close_to_close(candles), rel=1e-9)

        theirs = parkinson(candles)
        mine = compute_parkinson(
            spec("parkinson_short", compute_parkinson, bars=21), window(bars, DAYS[0])
        )
        assert mine.value == pytest.approx(theirs, rel=1e-9)

    def test_annualisation_is_timeframe_aware(self):
        # backtest/rv.py hardcodes the 5-minute constant; 1m needs its own.
        assert annualization("5m") == pytest.approx(math.sqrt(75 * TRADING_DAYS))
        assert annualization("1m") == pytest.approx(math.sqrt(375 * TRADING_DAYS))

    def test_bars_for_converts_minutes_per_timeframe(self):
        assert bars_for("5m", 100) == 20
        assert bars_for("1m", 100) == 100
        assert bars_for("5m", 3) == 1  # never zero


class TestTrailingBaseline:
    def base_spec(self, trailing=5):
        return spec("rv_baseline", compute_rv_baseline, scope=TRAILING,
                    trailing=trailing)

    def test_baseline_uses_whole_prior_sessions(self):
        bars = sessions(DAYS[:8], count=30)
        value = compute_rv_baseline(self.base_spec(), window(bars, DAYS[7]))
        assert value.is_valid

    def test_baseline_insufficient_without_enough_observed_sessions(self):
        bars = sessions(DAYS[:3], count=30)
        value = compute_rv_baseline(self.base_spec(), window(bars, DAYS[2]))
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY
        assert "5" in value.detail and "2" in value.detail

    def test_baseline_counts_observed_sessions_not_calendar_days(self):
        """Holidays absent from the 2026-only calendar must not shorten it."""
        observed = [DAYS[0], DAYS[1], DAYS[4], DAYS[5], DAYS[6], DAYS[7]]
        bars = sessions(observed, count=30)
        value = compute_rv_baseline(self.base_spec(), window(bars, DAYS[7]))
        assert value.is_valid

    def test_baseline_excludes_today(self):
        """A reference level that moved intraday would confound what it normalises."""
        bars = sessions(DAYS[:8], count=40)
        morning = compute_rv_baseline(
            self.base_spec(),
            BarWindow.build(bars, datetime.combine(DAYS[7], time(10, 0)), "5m"),
        )
        afternoon = compute_rv_baseline(
            self.base_spec(),
            BarWindow.build(bars, datetime.combine(DAYS[7], time(15, 0)), "5m"),
        )
        assert morning.value == pytest.approx(afternoon.value)


class TestRegimeAndAcceleration:
    def test_regime_is_the_ratio_of_short_to_baseline(self):
        bars = sessions(DAYS[:8], count=40)
        w = window(bars, DAYS[7])
        s = spec("rv_regime", compute_rv_regime, scope=TRAILING, trailing=5,
                 returns=20)
        short = compute_rv_short(spec("rv_short", compute_rv_short, returns=20), w)
        base = compute_rv_baseline(
            spec("rv_baseline", compute_rv_baseline, scope=TRAILING, trailing=5), w
        )
        regime = compute_rv_regime(s, w)
        assert regime.value == pytest.approx(short.value / base.value)

    def test_regime_inherits_the_reason_it_cannot_be_computed(self):
        bars = sessions(DAYS[:2], count=40)
        s = spec("rv_regime", compute_rv_regime, scope=TRAILING, trailing=5,
                 returns=20)
        value = compute_rv_regime(s, window(bars, DAYS[1]))
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY

    def test_acceleration_compares_two_volatility_windows(self):
        bars = sessions(DAYS[:4], count=40)
        s = spec("rv_accel", compute_rv_accel, returns=20, lag=6)
        value = compute_rv_accel(s, window(bars, DAYS[3]))
        assert value.is_valid

    def test_acceleration_needs_the_window_plus_its_lag(self):
        bars = session(DAYS[0], count=22)
        s = spec("rv_accel", compute_rv_accel, returns=20, lag=6)
        value = compute_rv_accel(s, window(bars, DAYS[0]))
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY


class TestRegistration:
    def test_all_six_registered_per_timeframe(self):
        for timeframe in ("1m", "5m"):
            names = state_features(timeframe).names
            for name in ("rv_short", "parkinson_short", "atr_rel",
                         "rv_baseline", "rv_regime", "rv_accel"):
                assert name in names

    def test_windows_are_time_matched_across_timeframes(self):
        five = {s.name: s for s in state_features("5m")}
        one = {s.name: s for s in state_features("1m")}
        assert five["rv_short"].params["returns"] == 20   # 100 min at 5m
        assert one["rv_short"].params["returns"] == 100   # 100 min at 1m
        assert five["rv_short"].params["minutes"] == one["rv_short"].params["minutes"]

    def test_volatility_specs_declare_rolling_or_trailing_scope(self):
        by_name = {s.name: s for s in state_features("5m")}
        assert by_name["rv_short"].scope == ROLLING
        assert by_name["parkinson_short"].scope == ROLLING
        assert by_name["atr_rel"].scope == ROLLING
        assert by_name["rv_baseline"].scope == TRAILING
        assert by_name["rv_regime"].scope == TRAILING
