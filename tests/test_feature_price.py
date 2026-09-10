"""Price and return features: formulas, edge cases, insufficient history."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from features.price import (
    compute_acceleration,
    compute_body_ratio,
    compute_lower_wick,
    compute_overnight_gap,
    compute_pos_in_range,
    compute_range_rel,
    compute_return,
    compute_session_ret,
    compute_upper_wick,
)
from features.registry import state_features
from features.spec import INTRADAY, SESSION, FeatureSpec
from features.state import FeatureStatus
from features.windows import BarWindow
from tests.featurelib import make_bar, session, sessions, trading_days

DAYS = trading_days(date(2026, 3, 2), 6)
NOON = time(12, 0)


def spec(name="ret_5m", fn=compute_return, scope=INTRADAY, **params):
    return FeatureSpec(
        name=name,
        family="price",
        timeframe="5m",
        lookback_bars=params.get("bars", 1) + 1,
        scope=scope,
        params=params,
        fn=fn,
    )


def window(bars, day=None, at=NOON):
    day = day or (bars[-1].timestamp.date() if bars else DAYS[0])
    return BarWindow.build(bars, datetime.combine(day, at), "5m")


def flat_session(day, count=3, price=100.0):
    """Bars with high == low -- the shape-feature divide-by-zero case."""
    base = datetime.combine(day, time(9, 15))
    return [
        make_bar(base + timedelta(minutes=5 * i), price, open_=price,
                 high=price, low=price)
        for i in range(count)
    ]


class TestReturns:
    def test_one_bar_return(self):
        day = DAYS[0]
        base = datetime.combine(day, time(9, 15))
        bars = [
            make_bar(base, 100.0),
            make_bar(base + timedelta(minutes=5), 110.0),
        ]
        value = compute_return(spec(bars=1), window(bars, day))
        assert value.value == pytest.approx(0.1)

    def test_multi_bar_return_spans_n_bars(self):
        day = DAYS[0]
        base = datetime.combine(day, time(9, 15))
        bars = [
            make_bar(base + timedelta(minutes=5 * i), price)
            for i, price in enumerate([100.0, 101.0, 102.0, 120.0])
        ]
        value = compute_return(spec(bars=3), window(bars, day))
        assert value.value == pytest.approx(0.2)

    def test_insufficient_history(self):
        bars = session(DAYS[0], count=2)
        value = compute_return(spec(bars=6), window(bars, DAYS[0]))
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY
        # A 6-bar return needs 7 bars: the span plus its starting point.
        assert "needs 7 completed bars, 2 available" == value.detail

    def test_returns_never_reach_into_yesterday(self):
        """Three bars today plus a full session yesterday is still insufficient."""
        bars = sessions(DAYS[:2], count=3)
        today = [b for b in bars if b.timestamp.date() == DAYS[1]]
        assert len(today) == 3
        value = compute_return(spec(bars=6), window(bars, DAYS[1]))
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY

    def test_no_bars_at_all(self):
        value = compute_return(spec(bars=1), window([], DAYS[0]))
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY


class TestAcceleration:
    def test_acceleration_is_the_change_in_one_bar_return(self):
        day = DAYS[0]
        base = datetime.combine(day, time(9, 15))
        bars = [
            make_bar(base + timedelta(minutes=5 * i), price)
            for i, price in enumerate([100.0, 110.0, 132.0])
        ]
        # latest 20%, previous 10% -> +10pp
        value = compute_acceleration(spec("accel_1", compute_acceleration), window(bars, day))
        assert value.value == pytest.approx(0.1)

    def test_needs_three_bars(self):
        bars = session(DAYS[0], count=2)
        value = compute_acceleration(
            spec("accel_1", compute_acceleration), window(bars, DAYS[0])
        )
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY


class TestBarShape:
    def test_range_rel(self):
        day = DAYS[0]
        bar = make_bar(datetime.combine(day, time(9, 15)), 100.0,
                       open_=100.0, high=102.0, low=98.0)
        value = compute_range_rel(spec("range_rel", compute_range_rel), window([bar], day))
        assert value.value == pytest.approx(0.04)

    def test_range_rel_of_a_flat_bar_is_a_valid_zero(self):
        # Nothing moved. That is a measurement, not an absence.
        value = compute_range_rel(
            spec("range_rel", compute_range_rel), window(flat_session(DAYS[0], 1), DAYS[0])
        )
        assert value.is_valid
        assert value.value == 0.0

    def test_body_ratio(self):
        day = DAYS[0]
        bar = make_bar(datetime.combine(day, time(9, 15)), 101.0,
                       open_=100.0, high=104.0, low=100.0)
        value = compute_body_ratio(spec("body_ratio", compute_body_ratio), window([bar], day))
        assert value.value == pytest.approx(0.25)

    def test_wicks(self):
        day = DAYS[0]
        bar = make_bar(datetime.combine(day, time(9, 15)), 102.0,
                       open_=101.0, high=105.0, low=100.0)
        upper = compute_upper_wick(spec("upper_wick", compute_upper_wick), window([bar], day))
        lower = compute_lower_wick(spec("lower_wick", compute_lower_wick), window([bar], day))
        assert upper.value == pytest.approx(3 / 5)
        assert lower.value == pytest.approx(1 / 5)

    @pytest.mark.parametrize(
        "name,fn",
        [
            ("body_ratio", compute_body_ratio),
            ("upper_wick", compute_upper_wick),
            ("lower_wick", compute_lower_wick),
        ],
    )
    def test_flat_bar_makes_shape_features_missing(self, name, fn):
        # No range means no meaningful ratio; a default would be invented data.
        value = fn(spec(name, fn), window(flat_session(DAYS[0], 1), DAYS[0]))
        assert value.status is FeatureStatus.MISSING
        assert "flat" in value.detail


class TestSessionAnchored:
    def test_session_ret_measures_from_the_opening_price(self):
        day = DAYS[0]
        base = datetime.combine(day, time(9, 15))
        bars = [
            make_bar(base, 110.0, open_=100.0, high=110.0, low=100.0),
            make_bar(base + timedelta(minutes=5), 120.0, open_=110.0,
                     high=120.0, low=110.0),
        ]
        value = compute_session_ret(spec("session_ret", compute_session_ret), window(bars, day))
        assert value.value == pytest.approx(0.2)

    def test_session_ret_missing_when_the_opening_bar_is_absent(self):
        # The one Nifty 50 session in 697 that starts at 11:15.
        bars = session(DAYS[0], count=10)[4:]
        value = compute_session_ret(
            spec("session_ret", compute_session_ret), window(bars, DAYS[0])
        )
        assert value.status is FeatureStatus.MISSING
        assert "opening bar absent" in value.detail

    def test_pos_in_range_at_the_extremes(self):
        day = DAYS[0]
        base = datetime.combine(day, time(9, 15))
        bars = [
            make_bar(base, 100.0, open_=100.0, high=110.0, low=90.0),
            make_bar(base + timedelta(minutes=5), 110.0, open_=100.0,
                     high=110.0, low=100.0),
        ]
        value = compute_pos_in_range(
            spec("pos_in_range", compute_pos_in_range), window(bars, day)
        )
        assert value.value == pytest.approx(1.0)

    def test_pos_in_range_missing_without_the_opening_bar(self):
        bars = session(DAYS[0], count=10)[4:]
        value = compute_pos_in_range(
            spec("pos_in_range", compute_pos_in_range), window(bars, DAYS[0])
        )
        assert value.status is FeatureStatus.MISSING


class TestOvernightGap:
    def gap_spec(self):
        return spec("overnight_gap", compute_overnight_gap, scope=SESSION)

    def test_gap_measures_open_against_the_previous_close(self):
        day_one, day_two = DAYS[0], DAYS[1]
        bars = [
            make_bar(datetime.combine(day_one, time(9, 15)), 100.0),
            make_bar(datetime.combine(day_one, time(15, 25)), 100.0),
            make_bar(datetime.combine(day_two, time(9, 15)), 105.0,
                     open_=110.0, high=110.0, low=105.0),
        ]
        value = compute_overnight_gap(self.gap_spec(), window(bars, day_two))
        assert value.value == pytest.approx(0.1)

    def test_gap_absent_on_the_first_session_of_history(self):
        bars = session(DAYS[0], count=3)
        value = compute_overnight_gap(self.gap_spec(), window(bars, DAYS[0]))
        assert value.status is FeatureStatus.INSUFFICIENT_HISTORY

    def test_gap_spans_a_long_weekend(self):
        """The previous session may be four calendar days back."""
        friday, monday = date(2026, 3, 6), date(2026, 3, 9)
        bars = [
            make_bar(datetime.combine(friday, time(9, 15)), 100.0),
            make_bar(datetime.combine(friday, time(15, 25)), 100.0),
            make_bar(datetime.combine(monday, time(9, 15)), 102.0,
                     open_=102.0, high=102.0, low=100.0),
        ]
        value = compute_overnight_gap(self.gap_spec(), window(bars, monday))
        assert value.value == pytest.approx(0.02)


class TestRegistration:
    def test_price_features_registered_for_both_timeframes(self):
        for timeframe, first in (("5m", "ret_5m"), ("1m", "ret_1m")):
            names = state_features(timeframe).names
            assert names[0] == first
            assert "overnight_gap" in names

    def test_horizons_are_named_by_elapsed_time(self):
        # ret_15m means 15 minutes on both timeframes: 3 bars at 5m, 15 at 1m.
        five = {s.name: s for s in state_features("5m")}
        one = {s.name: s for s in state_features("1m")}
        assert five["ret_15m"].params["bars"] == 3
        assert one["ret_15m"].params["bars"] == 15

    def test_no_duplicate_horizon_columns(self):
        for timeframe in ("1m", "5m"):
            names = state_features(timeframe).names
            assert len(names) == len(set(names))


class TestGapStretchedWindows:
    """A missing in-session bar must not silently widen a point-to-point span.

    Real case: on 2026-09-10 (a Thursday expiry) the 15:15 five-minute bar is
    absent from both tables, so the bars run 15:10 -> 15:20. A "one bar back"
    return computed at 15:25 then measures ten minutes, not five.
    """

    def gapped(self, day=None):
        """A session with the 4th bar removed, mimicking the real gap."""
        day = day or DAYS[0]
        bars = session(day, count=10)
        return [b for i, b in enumerate(bars) if i != 3]

    def test_return_across_a_gap_is_missing(self):
        bars = self.gapped()
        # The 5th bar sits 10 minutes after the 3rd, not 5.
        w = window(bars[:4], DAYS[0])
        value = compute_return(spec(bars=1), w)
        assert value.status is FeatureStatus.MISSING
        assert "stretched" in value.detail
        assert "10 min" in value.detail and "expected 5" in value.detail

    def test_contiguous_return_is_unaffected(self):
        bars = session(DAYS[0], count=10)
        assert compute_return(spec(bars=1), window(bars, DAYS[0])).is_valid

    def test_longer_horizon_across_a_gap_is_missing(self):
        bars = self.gapped()
        value = compute_return(spec(bars=3), window(bars[:6], DAYS[0]))
        assert value.status is FeatureStatus.MISSING

    def test_acceleration_across_a_gap_is_missing(self):
        bars = self.gapped()
        value = compute_acceleration(
            spec("accel_1", compute_acceleration), window(bars[:4], DAYS[0])
        )
        assert value.status is FeatureStatus.MISSING

    def test_single_bar_features_are_unaffected_by_a_gap(self):
        """range_rel reads one bar, so a gap elsewhere cannot stretch it."""
        bars = self.gapped()
        value = compute_range_rel(
            spec("range_rel", compute_range_rel), window(bars[:5], DAYS[0])
        )
        assert value.is_valid

    def test_window_beyond_the_gap_recovers(self):
        """Once the span no longer contains the gap, values return."""
        bars = self.gapped()
        assert compute_return(spec(bars=1), window(bars[:6], DAYS[0])).is_valid
