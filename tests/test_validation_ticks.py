"""Tick validation: irregular data, so no gap checks — but strict contract."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

from marketdata import validation as V
from marketdata.validation import Severity, validate_ticks
from tests.conftest import tick

T0 = datetime(2026, 9, 3, 10, 0, 0)


def codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_a_clean_tick_stream_produces_no_issues(weekday_calendar):
    records = [
        tick(T0, ltp=100.0, volume=1000),
        tick(T0 + timedelta(seconds=1), ltp=100.5, volume=1200),
        tick(T0 + timedelta(seconds=2), ltp=100.4, volume=1200),
    ]
    result = validate_ticks(records, is_trading_day=weekday_calendar)
    assert result.issues == ()
    assert result.ok
    assert result.record_count == 3


def test_a_full_snapshot_tick_is_accepted(weekday_calendar):
    records = [tick(
        T0, ltp=100.0, ltq=25, open=99.0, high=101.0, low=98.5, close=98.0,
        avg_trade_price=100.1, volume=5000, total_buy_qty=10, total_sell_qty=12,
        open_interest=1234, best_5_buy=[{"price": 99.9, "quantity": 5, "orders": 1}],
        best_5_sell=[{"price": 100.1, "quantity": 5, "orders": 1}],
    )]
    result = validate_ticks(records, is_trading_day=weekday_calendar)
    assert result.ok
    assert result.issues == ()


def test_ticks_are_never_gap_checked(weekday_calendar):
    # A five-minute silence in an illiquid option means nobody traded.
    records = [tick(T0), tick(T0 + timedelta(minutes=5))]
    result = validate_ticks(records, is_trading_day=weekday_calendar)
    assert V.UNEXPECTED_GAP not in codes(result)
    assert result.missing_timestamps == ()
    assert result.expected_count is None


def test_a_missing_price_is_an_error(weekday_calendar):
    record = tick(T0)
    del record["ltp"]
    result = validate_ticks([record], is_trading_day=weekday_calendar)
    assert V.MISSING_FIELD in codes(result)
    assert not result.ok


def test_a_non_positive_price_is_an_error(weekday_calendar):
    result = validate_ticks([tick(T0, ltp=0.0)], is_trading_day=weekday_calendar)
    assert V.NON_POSITIVE_PRICE in codes(result)
    assert result.invalid_indices == frozenset({0})


def test_negative_quantities_are_errors(weekday_calendar):
    result = validate_ticks(
        [tick(T0, ltq=-1, volume=-5)], is_trading_day=weekday_calendar
    )
    assert V.NEGATIVE_VALUE in codes(result)
    assert not result.ok


def test_duplicate_tick_timestamps_are_warnings_not_errors(weekday_calendar):
    # UNIQUE(instrument_id, timestamp) silently drops the second row on
    # insert, so this must be visible — but a snapshot feed republishing the
    # same last-traded time is not corrupt data.
    records = [tick(T0, ltp=100.0), tick(T0, ltp=100.2)]
    result = validate_ticks(records, is_trading_day=weekday_calendar)
    assert V.DUPLICATE_TIMESTAMP in codes(result)
    duplicate = next(i for i in result.issues if i.code == V.DUPLICATE_TIMESTAMP)
    assert duplicate.severity is Severity.WARNING
    assert result.ok
    assert result.invalid_indices == frozenset()


def test_equal_consecutive_tick_timestamps_are_not_out_of_order(weekday_calendar):
    records = [tick(T0), tick(T0)]
    result = validate_ticks(records, is_trading_day=weekday_calendar)
    assert V.OUT_OF_ORDER not in codes(result)


def test_out_of_order_ticks_are_warnings(weekday_calendar):
    records = [
        tick(T0),
        tick(T0 + timedelta(seconds=5)),
        tick(T0 + timedelta(seconds=2)),
    ]
    result = validate_ticks(records, is_trading_day=weekday_calendar)
    assert V.OUT_OF_ORDER in codes(result)
    issue = next(i for i in result.issues if i.code == V.OUT_OF_ORDER)
    assert issue.severity is Severity.WARNING
    assert result.ok


def test_cumulative_volume_running_backwards_is_a_warning(weekday_calendar):
    records = [
        tick(T0, volume=5000),
        tick(T0 + timedelta(seconds=1), volume=4000),
    ]
    result = validate_ticks(records, is_trading_day=weekday_calendar)
    assert V.VOLUME_REGRESSION in codes(result)
    issue = next(i for i in result.issues if i.code == V.VOLUME_REGRESSION)
    assert issue.severity is Severity.WARNING


def test_volume_resetting_on_a_new_day_is_not_a_regression(weekday_calendar):
    # Day-cumulative volume legitimately restarts each session.
    records = [
        tick(datetime(2026, 9, 3, 15, 25), volume=900_000),
        tick(datetime(2026, 9, 4, 9, 16), volume=1_000),
    ]
    result = validate_ticks(records, is_trading_day=weekday_calendar)
    assert V.VOLUME_REGRESSION not in codes(result)
    assert result.ok


def test_volume_monotonicity_can_be_switched_off(weekday_calendar):
    records = [tick(T0, volume=5000), tick(T0 + timedelta(seconds=1), volume=4000)]
    result = validate_ticks(
        records, is_trading_day=weekday_calendar, check_volume_monotonic=False
    )
    assert V.VOLUME_REGRESSION not in codes(result)


def test_a_snapshot_high_below_its_low_is_an_error(weekday_calendar):
    result = validate_ticks(
        [tick(T0, ltp=100.0, high=98.0, low=99.0)], is_trading_day=weekday_calendar
    )
    assert V.OHLC_INVARIANT in codes(result)
    assert not result.ok


def test_a_price_outside_the_snapshot_day_range_is_only_a_warning(weekday_calendar):
    # The day range can lag the LTP by one message; that is a staleness
    # signal, not a corrupt record.
    result = validate_ticks(
        [tick(T0, ltp=105.0, high=101.0, low=99.0)], is_trading_day=weekday_calendar
    )
    assert V.LTP_OUTSIDE_DAY_RANGE in codes(result)
    assert result.ok


def test_a_tick_outside_trading_hours_is_a_warning(weekday_calendar):
    # 04:30 is 10:00 IST misread as UTC — the timezone risk documented for
    # the tick collector.
    result = validate_ticks(
        [tick(datetime(2026, 9, 3, 4, 30))], is_trading_day=weekday_calendar
    )
    assert V.OUT_OF_SESSION in codes(result)
    assert result.ok
    issue = next(i for i in result.issues if i.code == V.OUT_OF_SESSION)
    assert "timezone" in issue.message


def test_a_tick_on_a_weekend_is_a_warning(weekday_calendar):
    result = validate_ticks(
        [tick(datetime(2026, 9, 5, 10, 0))], is_trading_day=weekday_calendar
    )
    assert V.OUT_OF_SESSION in codes(result)


def test_tick_validation_never_mutates_the_records(weekday_calendar):
    records = [
        tick(T0, ltp=-1.0, volume=10),
        tick(T0 - timedelta(seconds=5), volume=1),
    ]
    before = copy.deepcopy(records)
    validate_ticks(records, is_trading_day=weekday_calendar)
    assert records == before


def test_an_empty_tick_batch_is_trivially_valid():
    result = validate_ticks([])
    assert result.ok
    assert result.record_count == 0
