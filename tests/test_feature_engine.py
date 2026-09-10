"""The engine: assembling a MarketState, and failing safely when it cannot."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from features.engine import (
    build_state_from_bars,
    compute_labels,
    in_trading_window,
    state_row,
    trailing_sessions_needed,
)
from features.quality import clean_window
from features.registry import label_set, state_features
from features.spec import TRAILING, FeatureSet, FeatureSpec
from features.state import FeatureStatus
from tests.featurelib import sessions, trading_days

DAYS = trading_days(date(2026, 3, 2), 10)


@pytest.fixture(scope="module")
def history():
    return sessions(DAYS, timeframe="5m")


@pytest.fixture(scope="module")
def fs():
    return state_features("5m")


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute))


class TestTradingWindow:
    @pytest.mark.parametrize(
        "moment,expected",
        [
            (at(DAYS[0], 9, 14), False),
            (at(DAYS[0], 9, 15), True),
            (at(DAYS[0], 15, 29), True),
            (at(DAYS[0], 15, 30), False),
            (at(DAYS[0], 18, 0), False),
        ],
    )
    def test_boundaries(self, moment, expected):
        assert in_trading_window(moment) is expected


class TestBuildState:
    def test_state_carries_identity_and_version(self, history, fs):
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        assert state.instrument == "Nifty 50"
        assert state.timeframe == "5m"
        assert state.feature_set_version.startswith("fs_5m_")

    def test_state_has_one_value_per_spec_in_order(self, history, fs):
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        assert state.names == fs.names

    def test_all_features_valid_late_in_a_well_supplied_session(self, history, fs):
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        assert state.quality.completeness == 1.0

    def test_outside_the_session_everything_is_market_closed(self, history, fs):
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 18), history, fs)
        assert all(v.status is FeatureStatus.MARKET_CLOSED for v in state)
        assert "outside the regular session" in state.values[0].detail

    def test_empty_history_yields_absent_values_not_an_error(self, fs):
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), [], fs)
        assert len(state) == len(fs)
        assert state.quality.completeness == 0.0

    def test_source_quality_is_carried_into_the_state(self, history, fs):
        quality = clean_window(
            "ohlcv_5m", "Nifty 50", at(DAYS[0], 9, 15), at(DAYS[7], 15, 30)
        )
        state = build_state_from_bars(
            "Nifty 50", at(DAYS[7], 14), history, fs, source_quality=quality
        )
        assert state.quality.source_validation["verdict"] == "PASS"

    def test_out_of_session_bars_are_ignored(self, history, fs):
        """Pre-open and post-close rows exist in the tables; they must not count."""
        from tests.featurelib import make_bar

        intruder = make_bar(at(DAYS[7], 9, 10), 30_000.0)
        with_intruder = build_state_from_bars(
            "Nifty 50", at(DAYS[7], 14), list(history) + [intruder], fs
        )
        without = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        assert with_intruder.to_vector() == without.to_vector()


class TestFailureIsolation:
    def test_one_broken_feature_does_not_lose_the_others(self, history):
        def explode(spec, window):
            raise ZeroDivisionError("boom")

        broken = FeatureSpec(
            name="broken",
            family="price",
            timeframe="5m",
            lookback_bars=1,
            fn=explode,
        )
        good = state_features("5m").specs[0]
        fs = FeatureSet("5m", (good, broken))
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        assert state.status_of("broken") is FeatureStatus.INVALID_SOURCE
        assert state.get("broken").detail == "ZeroDivisionError: boom"
        assert state.get(good.name).is_valid

    def test_spec_without_a_computation_is_absent_not_fatal(self, history):
        fs = FeatureSet(
            "5m",
            (FeatureSpec(name="undefined", family="price", timeframe="5m",
                         lookback_bars=1),),
        )
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        assert state.status_of("undefined") is FeatureStatus.MISSING


class TestFetchPlanning:
    def test_trailing_need_is_the_longest_baseline(self):
        fs = FeatureSet(
            "5m",
            (
                FeatureSpec(name="a", family="price", timeframe="5m", lookback_bars=1),
                FeatureSpec(name="b", family="volatility", timeframe="5m",
                            lookback_bars=1, scope=TRAILING, trailing_sessions=14),
            ),
        )
        assert trailing_sessions_needed(fs) == 14

    def test_at_least_one_prior_session_is_always_needed(self):
        """overnight_gap needs yesterday even when nothing declares trailing."""
        fs = FeatureSet(
            "5m",
            (FeatureSpec(name="a", family="price", timeframe="5m", lookback_bars=1),),
        )
        assert trailing_sessions_needed(fs) >= 1

    def test_the_real_feature_set_needs_five(self, fs):
        assert trailing_sessions_needed(fs) == 5


class TestDatasetRow:
    def test_row_carries_features_labels_and_statuses(self, history, fs):
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        day_bars = [b for b in history if b.timestamp.date() == DAYS[7]]
        labels = compute_labels(day_bars[10], day_bars[11:], label_set("5m"))
        row = state_row(state, labels)

        assert row["instrument"] == "Nifty 50"
        assert "ret_5m" in row and "ret_5m__status" in row
        assert "fwd_ret_15m" in row and "fwd_ret_15m__status" in row

    def test_every_column_has_a_status_partner(self, history, fs):
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        row = state_row(state)
        for name in fs.names:
            assert f"{name}__status" in row

    def test_row_is_flat_and_serialisable(self, history, fs):
        state = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        row = state_row(state)
        assert all(not isinstance(v, (list, dict)) for v in row.values())


class TestDeterminism:
    def test_same_inputs_produce_identical_states(self, history, fs):
        a = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        b = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        assert a.to_vector() == b.to_vector()
        assert a.as_dict() == b.as_dict()

    def test_bar_order_does_not_affect_the_result(self, history, fs):
        shuffled = list(reversed(history))
        a = build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        b = build_state_from_bars("Nifty 50", at(DAYS[7], 14), shuffled, fs)
        assert a.to_vector() == b.to_vector()

    def test_source_bars_are_not_mutated(self, history, fs):
        """Gate 7 -- feature computation never touches raw data."""
        before = [(b.timestamp, b.open, b.high, b.low, b.close) for b in history]
        build_state_from_bars("Nifty 50", at(DAYS[7], 14), history, fs)
        after = [(b.timestamp, b.open, b.high, b.low, b.close) for b in history]
        assert before == after
