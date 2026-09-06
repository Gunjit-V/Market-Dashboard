"""Tick timestamp resolution and feed-field mapping.

These cover the two bugs found by probing the live tick_data table:

* 98.7% of stored ticks carried ``datetime.now()`` instead of a market clock,
  because ``last_traded_timestamp`` is SNAP_QUOTE-only and is 0 (falsy) until
  an instrument actually trades.
* ``avg_trade_price`` was null for ~89% of rows because the collector read
  ``average_trade_price`` while the SDK emits ``average_traded_price``.

The module under test imports the Angel One SDK at import time, so it is
skipped wherever that dependency is absent.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("SmartApi", reason="Angel One SDK not installed")

td = pytest.importorskip("downloader.tick_downloader")

# 2026-09-03 10:00:00 IST == 2026-09-03 04:30:00 UTC.
# Derived, not hand-computed: int(datetime(2026,9,3,10,0,tzinfo=IST).timestamp())
EPOCH_10AM_IST = 1788409800
TEN_AM_IST = datetime(2026, 9, 3, 10, 0, 0)


def _raw(**overrides) -> dict:
    raw = {"token": "99926000", "last_traded_price": 10_000}
    raw.update(overrides)
    return raw


# ── Timestamp source preference ──────────────────────────────────────────────

def test_exchange_timestamp_is_preferred_and_read_as_milliseconds():
    ts, source = td.resolve_tick_timestamp(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000)
    )
    assert ts == TEN_AM_IST
    assert source == "exchange"


def test_last_traded_timestamp_is_used_when_no_exchange_clock():
    ts, source = td.resolve_tick_timestamp(
        _raw(last_traded_timestamp=EPOCH_10AM_IST)
    )
    assert ts == TEN_AM_IST
    assert source == "last_traded"


def test_exchange_timestamp_wins_over_last_traded():
    ts, source = td.resolve_tick_timestamp(_raw(
        exchange_timestamp=EPOCH_10AM_IST * 1000,
        last_traded_timestamp=EPOCH_10AM_IST - 3600,
    ))
    assert ts == TEN_AM_IST
    assert source == "exchange"


def test_zero_last_traded_timestamp_is_not_midnight_1970():
    # The original bug: 0 is "has not traded yet", not a real timestamp.
    ts, source = td.resolve_tick_timestamp(_raw(last_traded_timestamp=0))
    assert source == "received"
    assert ts.year > 2000


def test_zero_exchange_timestamp_falls_through_to_the_next_source():
    ts, source = td.resolve_tick_timestamp(_raw(
        exchange_timestamp=0, last_traded_timestamp=EPOCH_10AM_IST
    ))
    assert ts == TEN_AM_IST
    assert source == "last_traded"


def test_wall_clock_is_the_last_resort_and_is_labelled_as_such():
    ts, source = td.resolve_tick_timestamp(_raw())
    assert source == "received"
    assert isinstance(ts, datetime) and ts.tzinfo is None


@pytest.mark.parametrize("value", [None, "", "abc", -1, float("nan")])
def test_unusable_epoch_values_never_raise(value):
    ts, source = td.resolve_tick_timestamp(_raw(exchange_timestamp=value))
    assert source == "received"
    assert isinstance(ts, datetime)


# ── Timezone independence (the container landmine) ───────────────────────────

def test_timestamp_is_ist_regardless_of_process_timezone(monkeypatch):
    """The same epoch must map to the same IST wall time under any TZ.

    Previously datetime.fromtimestamp() bound the value to the collector's
    local zone, so the tick-downloader container (no TZ set) would have stored
    timestamps 5h30m behind the IST-naive OHLCV tables.
    """
    import time

    results = []
    for tz in ("UTC", "Asia/Kolkata", "America/New_York"):
        monkeypatch.setenv("TZ", tz)
        if hasattr(time, "tzset"):
            time.tzset()
        ts, _ = td.resolve_tick_timestamp(
            _raw(exchange_timestamp=EPOCH_10AM_IST * 1000)
        )
        results.append(ts)

    monkeypatch.delenv("TZ", raising=False)
    if hasattr(time, "tzset"):
        time.tzset()

    assert results == [TEN_AM_IST] * 3


def test_resolved_timestamps_land_inside_the_trading_session():
    from marketdata.sessions import REGULAR_SESSION

    ts, _ = td.resolve_tick_timestamp(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000)
    )
    assert REGULAR_SESSION.contains(ts)


# ── parse_tick wiring ────────────────────────────────────────────────────────

def test_parse_tick_uses_the_exchange_clock():
    parsed = td.parse_tick(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000), {"99926000": 42}
    )
    assert parsed["timestamp"] == TEN_AM_IST
    assert parsed["time_source"] == "exchange"
    assert parsed["instrument_id"] == 42


def test_parse_tick_reads_the_sdk_vwap_field_name():
    parsed = td.parse_tick(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000,
             average_traded_price=1_234_50),
        {"99926000": 42},
    )
    assert parsed["avg_trade_price"] == pytest.approx(1234.50)


def test_parse_tick_still_accepts_the_legacy_vwap_spelling():
    parsed = td.parse_tick(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000,
             average_trade_price=1_000_00),
        {"99926000": 42},
    )
    assert parsed["avg_trade_price"] == pytest.approx(1000.00)


def test_parse_tick_prefers_the_sdk_spelling_when_both_are_present():
    parsed = td.parse_tick(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000,
             average_traded_price=1_111_00, average_trade_price=2_222_00),
        {"99926000": 42},
    )
    assert parsed["avg_trade_price"] == pytest.approx(1111.00)


def test_parse_tick_still_drops_unknown_tokens_and_bad_prices():
    assert td.parse_tick(_raw(exchange_timestamp=1), {"other": 1}) is None
    assert td.parse_tick(
        _raw(last_traded_price=0, exchange_timestamp=EPOCH_10AM_IST * 1000),
        {"99926000": 42},
    ) is None


def test_parsed_tick_satisfies_the_tick_contract():
    from marketdata.validation import validate_ticks

    parsed = td.parse_tick(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000), {"99926000": 42}
    )
    record = {k: v for k, v in parsed.items() if k != "time_source"}
    result = validate_ticks([record], is_trading_day=lambda d: True)
    assert result.ok, result.by_code()


# ── sequence_number (migration 001) ──────────────────────────────────────────

def test_parse_tick_captures_the_feed_sequence_number():
    parsed = td.parse_tick(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000, sequence_number=987654321),
        {"99926000": 42},
    )
    assert parsed["sequence_number"] == 987654321


def test_missing_sequence_number_falls_back_to_zero():
    # 0 is the "collected before migration 001" marker and keeps the NOT NULL
    # column satisfied; the feed never sends 0 for a real packet.
    parsed = td.parse_tick(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000), {"99926000": 42}
    )
    assert parsed["sequence_number"] == 0


def test_two_snapshots_in_one_second_are_distinct_records():
    """The bug migration 001 fixes: same instrument, same second, two packets.

    Under UNIQUE(instrument_id, timestamp) these collided and one was dropped.
    Including sequence_number makes them distinct rows.
    """
    ts_ms = EPOCH_10AM_IST * 1000
    first = td.parse_tick(
        _raw(exchange_timestamp=ts_ms, sequence_number=100, last_traded_price=10_000),
        {"99926000": 42},
    )
    second = td.parse_tick(
        _raw(exchange_timestamp=ts_ms, sequence_number=101, last_traded_price=10_050),
        {"99926000": 42},
    )

    assert first["timestamp"] == second["timestamp"]      # same second
    assert first["ltp"] != second["ltp"]                  # genuinely different
    old_key = ("instrument_id", "timestamp")
    new_key = ("instrument_id", "timestamp", "sequence_number")
    assert tuple(first[k] for k in old_key) == tuple(second[k] for k in old_key)
    assert tuple(first[k] for k in new_key) != tuple(second[k] for k in new_key)


def test_parsed_tick_still_satisfies_the_contract_with_sequence_number():
    from marketdata.validation import validate_ticks

    parsed = td.parse_tick(
        _raw(exchange_timestamp=EPOCH_10AM_IST * 1000, sequence_number=5),
        {"99926000": 42},
    )
    record = {k: v for k, v in parsed.items() if k != "time_source"}
    result = validate_ticks([record], is_trading_day=lambda d: True)
    assert result.ok, result.by_code()


# ── Session boundary (post-close snapshot suppression) ───────────────────────

@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 9, 3, 9, 15, 0), True),      # open, inclusive
    (datetime(2026, 9, 3, 12, 0, 0), True),      # midday
    (datetime(2026, 9, 3, 15, 29, 59), True),    # last live second
    (datetime(2026, 9, 3, 15, 30, 0), False),    # close, exclusive
    (datetime(2026, 9, 3, 15, 40, 51), False),   # the observed post-close drift
    (datetime(2026, 9, 3, 18, 53, 0), False),    # the observed 18:53 rows
    (datetime(2026, 9, 3, 9, 14, 59), False),    # pre-open
])
def test_market_hours_boundary(moment, expected):
    assert td.in_market_hours(moment) is expected


def test_weekends_are_never_market_hours():
    # 2026-09-05 is a Saturday.
    assert td.in_market_hours(datetime(2026, 9, 5, 12, 0)) is False


def test_holidays_are_never_market_hours():
    # 2026-09-14 is in the bundled NSE holiday calendar.
    assert td.in_market_hours(datetime(2026, 9, 14, 12, 0)) is False


def test_session_loop_ends_at_close_and_always_cleans_up():
    """The loop must break at 15:30, and shutdown must run on every path."""
    import ast
    import inspect

    src = inspect.getsource(td.start_realtime_tick_collection)
    tree = ast.parse(src.lstrip())

    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    session_try = next(
        t for t in tries
        if any(isinstance(h.type, ast.Name) and h.type.id == "KeyboardInterrupt"
               for h in t.handlers)
    )
    # Cleanup lives in `finally`, so a normal close cleans up like Ctrl+C does.
    assert session_try.finalbody, "shutdown must be in a finally block"
    finally_src = "".join(ast.unparse(n) for n in session_try.finalbody)
    assert "close_connection" in finally_src
    assert "worker_thread.join" in finally_src
    # And the loop consults market hours rather than spinning until the socket dies.
    assert "in_market_hours" in ast.unparse(session_try.body)
