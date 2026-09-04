"""OHLCV validation: contract, timestamps, invariants, volume and gaps."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from marketdata import validation as V
from marketdata.validation import Severity, partition, validate_ohlcv
from tests.conftest import bar, bars

OPEN = datetime(2026, 9, 3, 9, 15)


def codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


# ── Valid data ───────────────────────────────────────────────────────────────

def test_a_clean_session_produces_no_issues(weekday_calendar):
    result = validate_ohlcv(bars(OPEN, 75), "5m", is_trading_day=weekday_calendar)
    assert result.issues == ()
    assert result.ok
    assert result.record_count == 75
    assert result.expected_count == 75
    assert result.missing_timestamps == ()


def test_a_clean_one_minute_session_is_accepted(weekday_calendar):
    result = validate_ohlcv(
        bars(OPEN, 375, bar_minutes=1), "1m", is_trading_day=weekday_calendar
    )
    assert result.ok
    assert result.issues == ()


def test_zero_volume_is_valid_for_a_cash_index(weekday_calendar):
    # AMXIDX index bars structurally carry volume 0 — never a defect.
    records = [bar(OPEN, volume=0), bar(OPEN + timedelta(minutes=5), volume=0)]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert result.ok
    assert V.NEGATIVE_VALUE not in codes(result)


def test_flat_bar_where_all_four_prices_are_equal_is_valid(weekday_calendar):
    records = [bar(OPEN, open_=100, high=100, low=100, close=100)]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert result.ok


def test_decimal_values_from_psycopg2_are_accepted(weekday_calendar):
    from decimal import Decimal

    records = [bar(
        OPEN,
        open_=Decimal("100.0000"), high=Decimal("101.0000"),
        low=Decimal("99.0000"), close=Decimal("100.5000"),
        volume=Decimal("1000"),
    )]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert result.ok


# ── OHLC invariants ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "kwargs,reason",
    [
        (dict(open_=105, high=101), "high below open"),
        (dict(close=105, high=101), "high below close"),
        (dict(low=101, close=99), "low above close"),
        (dict(low=101, open_=99), "low above open"),
        (dict(high=98, low=99, open_=98.5, close=98.5), "high below low"),
    ],
)
def test_broken_ohlc_invariants_are_errors(kwargs, reason, weekday_calendar):
    result = validate_ohlcv([bar(OPEN, **kwargs)], "5m", is_trading_day=weekday_calendar)
    assert not result.ok, reason
    assert V.OHLC_INVARIANT in codes(result)
    assert result.invalid_indices == frozenset({0})


def test_a_non_positive_price_is_an_error(weekday_calendar):
    result = validate_ohlcv(
        [bar(OPEN, open_=0, high=101, low=0, close=100)], "5m",
        is_trading_day=weekday_calendar,
    )
    assert V.NON_POSITIVE_PRICE in codes(result)
    assert not result.ok


def test_a_negative_price_is_an_error(weekday_calendar):
    result = validate_ohlcv(
        [bar(OPEN, low=-1, open_=100, high=101, close=100)], "5m",
        is_trading_day=weekday_calendar,
    )
    assert V.NON_POSITIVE_PRICE in codes(result)


# ── Volume ───────────────────────────────────────────────────────────────────

def test_negative_volume_is_an_error(weekday_calendar):
    result = validate_ohlcv([bar(OPEN, volume=-5)], "5m", is_trading_day=weekday_calendar)
    assert V.NEGATIVE_VALUE in codes(result)
    assert result.invalid_indices == frozenset({0})


# ── Schema ───────────────────────────────────────────────────────────────────

def test_a_missing_required_field_is_an_error(weekday_calendar):
    record = bar(OPEN)
    del record["close"]
    result = validate_ohlcv([record], "5m", is_trading_day=weekday_calendar)
    assert V.MISSING_FIELD in codes(result)
    assert not result.ok


def test_a_null_required_field_is_an_error(weekday_calendar):
    result = validate_ohlcv([bar(OPEN, volume=None)], "5m", is_trading_day=weekday_calendar)
    assert V.MISSING_FIELD in codes(result)


def test_a_non_numeric_price_is_a_type_error(weekday_calendar):
    result = validate_ohlcv([bar(OPEN, close="not-a-price")], "5m",
                            is_trading_day=weekday_calendar)
    assert V.UNEXPECTED_TYPE in codes(result)
    assert not result.ok


def test_a_numeric_string_price_is_accepted_from_the_feed(weekday_calendar):
    # The Angel One JSON payload can carry numbers as strings; that is a
    # representation detail, not a contract violation.
    result = validate_ohlcv([bar(OPEN, close="100.5")], "5m",
                            is_trading_day=weekday_calendar)
    assert V.UNEXPECTED_TYPE not in codes(result)


def test_an_unknown_column_is_only_a_warning(weekday_calendar):
    record = bar(OPEN)
    record["vwap"] = 100.2
    result = validate_ohlcv([record], "5m", is_trading_day=weekday_calendar)
    assert V.UNKNOWN_FIELD in codes(result)
    assert result.ok  # warning, not error


# ── Timestamps ───────────────────────────────────────────────────────────────

def test_a_malformed_timestamp_string_is_an_error(weekday_calendar):
    result = validate_ohlcv([bar("03-09-2026 09:15")], "5m",
                            is_trading_day=weekday_calendar)
    assert V.MALFORMED_TIMESTAMP in codes(result)
    assert not result.ok


def test_an_iso_timestamp_string_is_accepted(weekday_calendar):
    result = validate_ohlcv([bar("2026-09-03T09:15:00")], "5m",
                            is_trading_day=weekday_calendar)
    assert result.ok


def test_a_timezone_aware_timestamp_is_reported_not_converted(weekday_calendar):
    ist = timezone(timedelta(hours=5, minutes=30))
    aware = datetime(2026, 9, 3, 9, 15, tzinfo=ist)
    records = [bar(aware)]
    before = copy.deepcopy(records)

    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)

    assert V.TIMEZONE_INCONSISTENT in codes(result)
    assert not result.ok
    assert records == before, "validation must never rewrite the input"


def test_a_bar_off_the_grid_is_an_error(weekday_calendar):
    result = validate_ohlcv([bar(datetime(2026, 9, 3, 9, 22))], "5m",
                            is_trading_day=weekday_calendar)
    assert V.TIMESTAMP_NOT_ALIGNED in codes(result)


def test_grid_alignment_can_be_switched_off(weekday_calendar):
    result = validate_ohlcv(
        [bar(datetime(2026, 9, 3, 9, 22))], "5m",
        is_trading_day=weekday_calendar, check_alignment=False, check_gaps=False,
    )
    assert V.TIMESTAMP_NOT_ALIGNED not in codes(result)


def test_a_bar_outside_trading_hours_is_a_warning(weekday_calendar):
    # 03:45 is 09:15 IST misread as UTC — exactly the failure mode described
    # in docs/point-in-time-data.md.
    result = validate_ohlcv(
        [bar(datetime(2026, 9, 3, 3, 45))], "5m",
        is_trading_day=weekday_calendar, check_gaps=False,
    )
    assert V.OUT_OF_SESSION in codes(result)
    warning = next(i for i in result.issues if i.code == V.OUT_OF_SESSION)
    assert warning.severity is Severity.WARNING


def test_a_bar_on_a_holiday_is_a_warning(weekday_calendar):
    result = validate_ohlcv(
        [bar(datetime(2026, 9, 14, 9, 15))], "5m",
        is_trading_day=weekday_calendar, check_gaps=False,
    )
    assert V.OUT_OF_SESSION in codes(result)


# ── Duplicates and ordering ──────────────────────────────────────────────────

def test_duplicate_bar_timestamps_are_errors(weekday_calendar):
    records = [bar(OPEN), bar(OPEN)]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert V.DUPLICATE_TIMESTAMP in codes(result)
    assert result.count(V.DUPLICATE_TIMESTAMP) == 1
    assert result.invalid_indices == frozenset({1})


def test_out_of_order_bars_are_errors(weekday_calendar):
    records = [
        bar(OPEN),
        bar(OPEN + timedelta(minutes=10)),
        bar(OPEN + timedelta(minutes=5)),
    ]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert V.OUT_OF_ORDER in codes(result)
    assert 2 in result.invalid_indices


def test_an_out_of_order_run_does_not_double_report_as_a_gap(weekday_calendar):
    # All three bars are present, just shuffled: no bar is actually missing.
    records = [
        bar(OPEN),
        bar(OPEN + timedelta(minutes=10)),
        bar(OPEN + timedelta(minutes=5)),
    ]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert result.missing_timestamps == ()


# ── Gaps ─────────────────────────────────────────────────────────────────────

def test_the_overnight_close_is_not_a_gap(weekday_calendar):
    records = [
        bar(datetime(2026, 9, 3, 15, 25)),
        bar(datetime(2026, 9, 4, 9, 15)),
    ]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert result.missing_timestamps == ()
    assert V.UNEXPECTED_GAP not in codes(result)


def test_a_weekend_is_not_a_gap(weekday_calendar):
    records = [
        bar(datetime(2026, 9, 4, 15, 25)),   # Friday close
        bar(datetime(2026, 9, 7, 9, 15)),    # Monday open
    ]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert result.missing_timestamps == ()


def test_an_exchange_holiday_is_not_a_gap(weekday_calendar):
    records = [
        bar(datetime(2026, 9, 11, 15, 25)),  # Friday before the holiday
        bar(datetime(2026, 9, 15, 9, 15)),   # Tuesday after it
    ]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert result.missing_timestamps == ()
    assert result.ok


def test_a_missing_bar_inside_a_session_is_a_warning(weekday_calendar):
    records = [
        bar(datetime(2026, 9, 3, 9, 15)),
        bar(datetime(2026, 9, 3, 9, 20)),
        # 09:25 absent
        bar(datetime(2026, 9, 3, 9, 30)),
    ]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert result.missing_timestamps == (datetime(2026, 9, 3, 9, 25),)
    assert result.expected_count == 4
    assert result.ok  # a gap is a warning, never an error
    gap = next(i for i in result.issues if i.code == V.UNEXPECTED_GAP)
    assert gap.severity is Severity.WARNING


def test_gaps_are_only_looked_for_inside_the_observed_span(weekday_calendar):
    # Data that simply has not been downloaded past 09:25 is not a gap.
    result = validate_ohlcv(bars(OPEN, 3), "5m", is_trading_day=weekday_calendar)
    assert result.missing_timestamps == ()
    assert result.expected_count == 3


def test_gap_checking_can_be_switched_off_for_partial_batches(weekday_calendar):
    records = [
        bar(datetime(2026, 9, 3, 9, 15)),
        bar(datetime(2026, 9, 3, 9, 30)),
    ]
    result = validate_ohlcv(
        records, "5m", is_trading_day=weekday_calendar, check_gaps=False
    )
    assert result.missing_timestamps == ()
    assert result.expected_count is None


# ── Non-destructiveness ──────────────────────────────────────────────────────

def test_validation_never_mutates_the_records(weekday_calendar):
    records = [
        bar(OPEN, high=1),                      # broken invariant
        bar(OPEN + timedelta(minutes=5), volume=-3),
        bar("2026-09-03T09:25:00"),
    ]
    before = copy.deepcopy(records)
    validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    assert records == before


def test_partition_splits_without_altering_rows(weekday_calendar):
    good = bar(OPEN)
    bad = bar(OPEN + timedelta(minutes=5), high=1)
    records = [good, bad]

    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    split = partition(records, result)

    assert split.accepted == (good,)
    assert split.rejected == (bad,)
    assert split.rejected[0] is bad  # the very same object, untouched


def test_an_empty_batch_is_trivially_valid():
    result = validate_ohlcv([], "5m")
    assert result.ok
    assert result.record_count == 0
    assert result.expected_count is None


def test_ticks_cannot_be_validated_as_bars():
    with pytest.raises(ValueError, match="not a bar dataset"):
        validate_ohlcv([], "tick")


def test_a_non_datetime_timestamp_is_a_type_error(weekday_calendar):
    result = validate_ohlcv([bar(1757000000)], "5m", is_trading_day=weekday_calendar)
    assert V.UNEXPECTED_TYPE in codes(result)
    assert not result.ok
