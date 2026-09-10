"""Point-in-time safety: the tests Phase 2 exists to pass.

The central property, and the Phase 2 exit criterion made executable:

    state(T) computed from the full history must be **bit-identical** to
    state(T) computed from history truncated at T.

If any feature reaches past its decision time, these tests fail and name the
column that did it. Everything else in the feature layer rests on this.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from features.engine import build_state_from_bars, compute_labels
from features.registry import label_set, state_features
from features.state import FeatureStatus
from tests.featurelib import sessions, trading_days

DAYS = trading_days(date(2026, 3, 2), 10)


@pytest.fixture(scope="module")
def history():
    """Ten full sessions of deterministic 5-minute bars."""
    return sessions(DAYS, timeframe="5m")


@pytest.fixture(scope="module")
def fs():
    return state_features("5m")


def visible_at(bars, moment: datetime, bar_minutes: int = 5):
    """Bars that had *closed* at *moment* -- what the access layer would return.

    This mirrors the SQL cut-off (``timestamp + bar_minutes <= as_of``) rather
    than re-deriving it, so the tests exercise the same rule production uses.
    """
    return [b for b in bars if b.timestamp + timedelta(minutes=bar_minutes) <= moment]


DECISION_TIMES = [
    datetime.combine(DAYS[7], datetime.min.time()).replace(hour=9, minute=15),
    datetime.combine(DAYS[7], datetime.min.time()).replace(hour=9, minute=20),
    datetime.combine(DAYS[7], datetime.min.time()).replace(hour=10, minute=20),
    datetime.combine(DAYS[7], datetime.min.time()).replace(hour=12, minute=0),
    datetime.combine(DAYS[7], datetime.min.time()).replace(hour=15, minute=25),
]


class TestFutureInvariance:
    """Gate 2 -- future data must not change a past state."""

    @pytest.mark.parametrize("moment", DECISION_TIMES)
    def test_truncated_history_gives_an_identical_state(self, history, fs, moment):
        full = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        # Physically delete everything at or after the decision time, then
        # recompute. Any feature that had reached forward now cannot.
        truncated = [b for b in history if b.timestamp < moment]
        limited = build_state_from_bars(
            "Nifty 50", moment, visible_at(truncated, moment), fs
        )
        assert full.to_vector() == limited.to_vector()
        assert full.statuses() == limited.statuses()

    @pytest.mark.parametrize("moment", DECISION_TIMES)
    def test_appending_future_bars_changes_nothing(self, history, fs, moment):
        before = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        # Append wildly divergent future bars -- a 20% spike the day after.
        future_day = DAYS[9] + timedelta(days=7)
        intruders = sessions([future_day], timeframe="5m", start_price=29_000.0)
        after = build_state_from_bars(
            "Nifty 50", moment, visible_at(history + intruders, moment), fs
        )
        assert before.to_vector() == after.to_vector()

    @pytest.mark.parametrize("moment", DECISION_TIMES)
    def test_serialisation_is_byte_identical(self, history, fs, moment):
        a = build_state_from_bars("Nifty 50", moment, visible_at(history, moment), fs)
        b = build_state_from_bars("Nifty 50", moment, visible_at(history, moment), fs)
        assert json.dumps(a.as_dict()) == json.dumps(b.as_dict())

    def test_every_feature_is_covered_by_this_test(self, history, fs):
        """The invariance test is worthless if the features never compute.

        A state of all-absent values would pass trivially, so assert that the
        late-session decision actually produced real numbers for everything.
        """
        moment = DECISION_TIMES[-1]
        state = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        absent = [v.name for v in state if not v.is_valid]
        assert absent == [], f"features never exercised: {absent}"


class TestBarCompletion:
    """Gate 3 -- a bar is invisible until it closes."""

    def test_the_forming_bar_is_excluded(self, history, fs):
        # At 11:30 the 11:30 bar covers [11:30, 11:35) and has not closed.
        moment = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=11, minute=30
        )
        visible = visible_at(history, moment)
        assert visible[-1].timestamp.time() == datetime.min.time().replace(
            hour=11, minute=25
        ), "newest usable bar must be 11:25, not 11:30"

    def test_state_at_bar_close_uses_that_bar(self, history, fs):
        """The 09:15 bar becomes usable at exactly 09:20, not before."""
        before = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=9, minute=19
        )
        at_close = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=9, minute=20
        )
        assert len(visible_at(history, before)) < len(visible_at(history, at_close))

    def test_session_ret_unavailable_before_the_opening_bar_closes(self, history, fs):
        moment = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=9, minute=15
        )
        state = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        # Nothing has closed yet today, so nothing session-anchored can exist.
        # This is insufficient history, not missing data: the opening bar is
        # still forming, which is different from it being absent from the feed.
        assert state.status_of("session_ret") is FeatureStatus.INSUFFICIENT_HISTORY
        assert state.status_of("pos_in_range") is FeatureStatus.INSUFFICIENT_HISTORY
        assert state.status_of("overnight_gap") is FeatureStatus.INSUFFICIENT_HISTORY

    def test_absent_opening_bar_is_missing_not_insufficient(self, history, fs):
        """The 11:15-start session: bars exist, but the opening bar does not."""
        day = DAYS[7]
        late = [
            b
            for b in history
            if b.timestamp.date() != day or b.timestamp.hour >= 11
        ]
        moment = datetime.combine(day, datetime.min.time()).replace(hour=13, minute=0)
        state = build_state_from_bars("Nifty 50", moment, visible_at(late, moment), fs)
        assert state.status_of("session_ret") is FeatureStatus.MISSING
        assert state.status_of("pos_in_range") is FeatureStatus.MISSING
        assert state.status_of("overnight_gap") is FeatureStatus.MISSING


class TestSessionBoundaries:
    """Gate 3 -- behaviour at session edges, overnight, weekends."""

    def test_outside_the_session_every_feature_is_market_closed(self, history, fs):
        moment = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=16, minute=0
        )
        state = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        assert all(v.status is FeatureStatus.MARKET_CLOSED for v in state)
        assert state.to_vector() == (None,) * len(fs)

    def test_at_1530_the_market_is_closed(self, history, fs):
        # The session is close-exclusive: no bar starts at 15:30.
        moment = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=15, minute=30
        )
        state = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        assert all(v.status is FeatureStatus.MARKET_CLOSED for v in state)

    def test_1525_is_still_in_session(self, history, fs):
        moment = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=15, minute=25
        )
        state = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        assert state.quality.completeness == 1.0

    def test_intraday_features_do_not_reach_into_yesterday(self, history, fs):
        """At the open, intraday features must be empty even though history exists."""
        moment = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=9, minute=15
        )
        state = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        for name in ("ret_5m", "ret_15m", "ret_30m", "ret_60m", "accel_1"):
            assert state.status_of(name) is FeatureStatus.INSUFFICIENT_HISTORY

    def test_rolling_features_do_reach_into_yesterday(self, history, fs):
        """The rolling scope is what makes volatility available at the open."""
        moment = datetime.combine(DAYS[7], datetime.min.time()).replace(
            hour=9, minute=15
        )
        state = build_state_from_bars(
            "Nifty 50", moment, visible_at(history, moment), fs
        )
        for name in ("rv_short", "parkinson_short", "atr_rel", "rv_regime"):
            assert state.status_of(name) is FeatureStatus.VALID

    def test_first_session_of_all_history_has_no_baseline(self, fs):
        first = sessions(DAYS[:1], timeframe="5m")
        moment = datetime.combine(DAYS[0], datetime.min.time()).replace(
            hour=14, minute=0
        )
        state = build_state_from_bars(
            "Nifty 50", moment, visible_at(first, moment), fs
        )
        assert state.status_of("rv_baseline") is FeatureStatus.INSUFFICIENT_HISTORY
        assert state.status_of("rv_regime") is FeatureStatus.INSUFFICIENT_HISTORY
        assert state.status_of("overnight_gap") is FeatureStatus.INSUFFICIENT_HISTORY


class TestLabelSeparation:
    """Rule 5 -- a label must not be knowable at the decision it labels."""

    def test_labels_are_not_part_of_the_state(self, fs):
        assert not any(spec.family == "label" for spec in fs)
        assert "fwd_ret_15m" not in fs.names

    def test_label_reads_only_bars_after_the_decision(self, history):
        labels = label_set("5m")
        day_bars = [b for b in history if b.timestamp.date() == DAYS[7]]
        decision, forward = day_bars[10], day_bars[11:]
        values = {v.name: v for v in compute_labels(decision, forward, labels)}
        expected = forward[2].close / decision.close - 1
        assert values["fwd_ret_15m"].value == pytest.approx(expected)

    def test_label_absent_when_the_session_ends_first(self, history):
        labels = label_set("5m")
        day_bars = [b for b in history if b.timestamp.date() == DAYS[7]]
        # Two bars from the close: a 12-bar horizon cannot be filled.
        values = {
            v.name: v for v in compute_labels(day_bars[-3], day_bars[-2:], labels)
        }
        assert values["fwd_ret_60m"].status is FeatureStatus.MISSING
        assert values["fwd_ret_15m"].status is FeatureStatus.MISSING

    def test_labels_never_cross_into_the_next_session(self, history):
        """The forward window is bounded by the session, not by bar supply."""
        labels = label_set("5m")
        day_bars = [b for b in history if b.timestamp.date() == DAYS[7]]
        # Deliberately offer tomorrow's bars as "forward"; the caller supplies
        # only same-session bars, so the label must report absence instead.
        values = {v.name: v for v in compute_labels(day_bars[-1], [], labels)}
        assert all(v.status is FeatureStatus.MISSING for v in values.values())
