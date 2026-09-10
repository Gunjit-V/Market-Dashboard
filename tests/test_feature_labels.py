"""Forward-looking labels: formulas, the zero class, and session bounds."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from features.labels import (
    LABEL_HORIZONS,
    compute_forward_direction,
    compute_forward_return,
    compute_forward_rv,
)
from features.registry import label_set, state_features
from features.spec import INTRADAY, FeatureSpec
from features.state import FeatureStatus
from tests.featurelib import make_bar, session, trading_days

DAYS = trading_days(date(2026, 3, 2), 4)


def spec(name="fwd_ret_15m", fn=compute_forward_return, bars=3):
    return FeatureSpec(
        name=name,
        family="label",
        timeframe="5m",
        lookback_bars=1,
        scope=INTRADAY,
        params={"bars": bars, "horizon": "15m", "kind": "ret"},
        fn=fn,
    )


def chain(prices, day=None):
    """Bars at consecutive 5-minute slots with the given closes."""
    day = day or DAYS[0]
    base = datetime.combine(day, time(9, 15))
    return [
        make_bar(base + timedelta(minutes=5 * i), price) for i, price in enumerate(prices)
    ]


class TestForwardReturn:
    def test_measures_from_the_decision_bar_to_the_horizon(self):
        bars = chain([100.0, 101.0, 102.0, 110.0])
        value = compute_forward_return(spec(bars=3), bars[0], bars[1:])
        assert value.value == pytest.approx(0.1)

    def test_uses_the_horizon_bar_not_the_last_available(self):
        bars = chain([100.0, 110.0, 200.0, 300.0])
        value = compute_forward_return(spec(bars=1), bars[0], bars[1:])
        assert value.value == pytest.approx(0.1)

    def test_absent_when_the_session_ends_first(self):
        bars = chain([100.0, 101.0])
        value = compute_forward_return(spec(bars=6), bars[0], bars[1:])
        assert value.status is FeatureStatus.MISSING
        assert "remain in the session" in value.detail

    def test_absent_with_no_forward_bars_at_all(self):
        bars = chain([100.0])
        value = compute_forward_return(spec(bars=1), bars[0], [])
        assert value.status is FeatureStatus.MISSING


class TestForwardDirection:
    @pytest.mark.parametrize(
        "prices,expected",
        [
            ([100.0, 110.0], 1.0),
            ([100.0, 90.0], -1.0),
            ([100.0, 100.0], 0.0),
        ],
    )
    def test_sign(self, prices, expected):
        bars = chain(prices)
        value = compute_forward_direction(
            spec("fwd_dir_15m", compute_forward_direction, bars=1), bars[0], bars[1:]
        )
        assert value.value == expected

    def test_zero_is_its_own_class(self):
        """0.181% of Nifty 50 bars close unchanged; folding them would invent
        a direction the market did not take."""
        bars = chain([100.0, 100.0])
        value = compute_forward_direction(
            spec("fwd_dir_15m", compute_forward_direction, bars=1), bars[0], bars[1:]
        )
        assert value.is_valid
        assert value.value == 0.0

    def test_inherits_absence_from_the_return(self):
        bars = chain([100.0])
        value = compute_forward_direction(
            spec("fwd_dir_15m", compute_forward_direction, bars=6), bars[0], bars[1:]
        )
        assert value.status is FeatureStatus.MISSING


class TestForwardVolatility:
    def test_computed_over_the_forward_window(self):
        from features.volatility import annualization
        from features.windows import log_returns, stdev

        bars = session(DAYS[0], count=10)
        value = compute_forward_rv(
            spec("fwd_rv_15m", compute_forward_rv, bars=6), bars[0], bars[1:]
        )
        expected = stdev(log_returns(bars[:7])) * annualization("5m")
        assert value.value == pytest.approx(expected)

    def test_absent_when_the_horizon_does_not_fit(self):
        bars = session(DAYS[0], count=3)
        value = compute_forward_rv(
            spec("fwd_rv_15m", compute_forward_rv, bars=6), bars[0], bars[1:]
        )
        assert value.status is FeatureStatus.MISSING


class TestRegistration:
    def test_nine_labels_per_timeframe(self):
        for timeframe in ("1m", "5m"):
            assert len(label_set(timeframe)) == 9

    def test_three_kinds_across_three_horizons(self):
        names = label_set("5m").names
        for horizon in ("15m", "30m", "60m"):
            for kind in ("ret", "dir", "rv"):
                assert f"fwd_{kind}_{horizon}" in names

    def test_horizons_are_time_matched_across_timeframes(self):
        five = {s.name: s for s in label_set("5m")}
        one = {s.name: s for s in label_set("1m")}
        assert five["fwd_ret_15m"].params["bars"] == 3
        assert one["fwd_ret_15m"].params["bars"] == 15

    def test_labels_are_excluded_from_the_state(self):
        """A label is the answer; letting it into the state trains on itself."""
        state_names = set(state_features("5m").names)
        label_names = set(label_set("5m").names)
        assert not (state_names & label_names)

    def test_every_label_declares_the_label_family(self):
        assert all(s.family == "label" for s in label_set("5m"))

    def test_horizon_table_covers_both_timeframes(self):
        assert set(LABEL_HORIZONS) == {"1m", "5m"}


class TestGapStretchedHorizons:
    """A label's horizon must mean what its name says.

    On 2026-09-10 the 15:15 bar is absent, so three bars after 15:00 reaches
    15:20 -- a twenty-minute return in a column named fwd_ret_15m. Since a
    label is what a model is asked to predict, a horizon that does not mean
    what it says would be learned as though it did.
    """

    def gapped_forward(self):
        bars = session(DAYS[0], count=10)
        return bars[0], [b for i, b in enumerate(bars[1:], start=1) if i != 2]

    def test_stretched_horizon_is_missing(self):
        current, forward = self.gapped_forward()
        value = compute_forward_return(spec(bars=3), current, forward)
        assert value.status is FeatureStatus.MISSING
        assert "stretched the horizon" in value.detail
        assert "expected 15" in value.detail

    def test_contiguous_horizon_is_valid(self):
        bars = session(DAYS[0], count=10)
        assert compute_forward_return(spec(bars=3), bars[0], bars[1:]).is_valid

    def test_direction_inherits_the_rejection(self):
        current, forward = self.gapped_forward()
        value = compute_forward_direction(
            spec("fwd_dir_15m", compute_forward_direction, bars=3), current, forward
        )
        assert value.status is FeatureStatus.MISSING

    def test_forward_rv_inherits_the_rejection(self):
        current, forward = self.gapped_forward()
        value = compute_forward_rv(
            spec("fwd_rv_15m", compute_forward_rv, bars=3), current, forward
        )
        assert value.status is FeatureStatus.MISSING

    def test_short_horizon_before_the_gap_still_works(self):
        current, forward = self.gapped_forward()
        assert compute_forward_return(spec(bars=1), current, forward).is_valid
