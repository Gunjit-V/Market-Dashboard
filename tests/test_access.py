"""Reproducible data access — verified against a stub connection, no DB.

The stub records the SQL and parameters the layer emits, which is what makes
the point-in-time and windowing guarantees testable without PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from marketdata.access import (
    Bar,
    InstrumentNotFound,
    Tick,
    get_market_data,
    resolve_instrument_id,
)

START = datetime(2026, 9, 3, 9, 15)
END = datetime(2026, 9, 3, 15, 30)


class StubCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), list(params or [])))

    def fetchone(self):
        return self.conn.next_row()

    def fetchall(self):
        return self.conn.next_rows()


class StubConnection:
    """Returns queued results in order and remembers every statement."""

    def __init__(self, instrument_row=(42,), rows=()):
        self.instrument_row = instrument_row
        self.rows = list(rows)
        self.executed: list[tuple[str, list]] = []
        self._single_served = False

    def cursor(self):
        return StubCursor(self)

    def next_row(self):
        self._single_served = True
        return self.instrument_row

    def next_rows(self):
        return self.rows

    @property
    def data_sql(self) -> str:
        return self.executed[-1][0]

    @property
    def data_params(self) -> list:
        return self.executed[-1][1]


BAR_ROWS = [
    (datetime(2026, 9, 3, 9, 15), Decimal("100.0"), Decimal("101.0"),
     Decimal("99.0"), Decimal("100.5"), 1000),
    (datetime(2026, 9, 3, 9, 20), Decimal("100.5"), Decimal("102.0"),
     Decimal("100.0"), Decimal("101.5"), 1200),
]
TICK_ROWS = [
    (datetime(2026, 9, 3, 10, 0), Decimal("100.25"), 5, 9000),
]


# ── Instrument resolution ────────────────────────────────────────────────────

def test_symbol_is_resolved_case_insensitively():
    conn = StubConnection(instrument_row=(42,))
    assert resolve_instrument_id(conn, "nifty 50") == 42
    sql, params = conn.executed[0]
    assert "UPPER(symbol) = UPPER(%s)" in sql
    assert params == ["nifty 50"]


def test_an_integer_instrument_is_used_directly():
    conn = StubConnection()
    assert resolve_instrument_id(conn, 7) == 7
    assert conn.executed == []  # no lookup needed


def test_an_unknown_symbol_raises():
    conn = StubConnection(instrument_row=None)
    with pytest.raises(InstrumentNotFound, match="NOPE"):
        get_market_data(conn, "NOPE", START, END, "5m")


# ── Windowing ────────────────────────────────────────────────────────────────

def test_the_window_is_half_open_on_the_bar_label():
    conn = StubConnection(rows=BAR_ROWS)
    get_market_data(conn, "Nifty 50", START, END, "5m")

    assert "timestamp >= %s" in conn.data_sql
    assert "timestamp < %s" in conn.data_sql
    assert "timestamp <= %s" not in conn.data_sql
    assert conn.data_params == [42, START, END]


def test_results_are_deterministically_ordered():
    conn = StubConnection(rows=BAR_ROWS)
    get_market_data(conn, "Nifty 50", START, END, "5m")
    assert "ORDER BY timestamp ASC, id ASC" in conn.data_sql


def test_an_inverted_window_is_rejected_before_any_query():
    conn = StubConnection()
    with pytest.raises(ValueError, match="end must not be before start"):
        get_market_data(conn, "Nifty 50", END, START, "5m")
    assert conn.executed == []


def test_a_zero_width_window_is_allowed():
    conn = StubConnection(rows=[])
    assert get_market_data(conn, "Nifty 50", START, START, "5m") == []


def test_limit_is_applied_after_ordering():
    conn = StubConnection(rows=BAR_ROWS)
    get_market_data(conn, "Nifty 50", START, END, "5m", limit=10)
    assert conn.data_sql.rstrip().endswith("LIMIT %s")
    assert conn.data_params[-1] == 10


# ── Timeframe routing ────────────────────────────────────────────────────────

@pytest.mark.parametrize("timeframe,table", [("1m", "ohlcv_1min"), ("5m", "ohlcv_5min")])
def test_each_timeframe_reads_its_own_table(timeframe, table):
    conn = StubConnection(rows=BAR_ROWS)
    get_market_data(conn, "Nifty 50", START, END, timeframe)
    assert f"FROM {table}" in conn.data_sql


def test_ticks_read_the_tick_table():
    conn = StubConnection(rows=TICK_ROWS)
    get_market_data(conn, "Nifty 50", START, END, "tick")
    assert "FROM tick_data" in conn.data_sql


def test_an_unknown_timeframe_is_rejected():
    conn = StubConnection()
    with pytest.raises(ValueError, match="Unknown timeframe"):
        get_market_data(conn, "Nifty 50", START, END, "15m")


def test_the_timeframe_never_reaches_sql_unchecked():
    conn = StubConnection()
    with pytest.raises(ValueError):
        get_market_data(conn, "Nifty 50", START, END, "5m; DROP TABLE ohlcv_5min")
    assert len(conn.executed) == 1  # only the instrument lookup ran


# ── Point-in-time ────────────────────────────────────────────────────────────

def test_without_as_of_no_completion_filter_is_applied():
    conn = StubConnection(rows=BAR_ROWS)
    get_market_data(conn, "Nifty 50", START, END, "5m")
    assert "make_interval" not in conn.data_sql


def test_as_of_only_admits_bars_that_have_already_closed():
    conn = StubConnection(rows=BAR_ROWS)
    as_of = datetime(2026, 9, 3, 9, 21)
    get_market_data(conn, "Nifty 50", START, END, "5m", as_of=as_of)

    assert "timestamp + make_interval(mins => %s) <= %s" in conn.data_sql
    assert conn.data_params == [42, START, END, 5, as_of]


def test_the_completion_offset_matches_the_timeframe():
    conn = StubConnection(rows=BAR_ROWS)
    as_of = datetime(2026, 9, 3, 9, 21)
    get_market_data(conn, "Nifty 50", START, END, "1m", as_of=as_of)
    assert conn.data_params[-2] == 1


def test_ticks_are_knowable_at_their_own_timestamp():
    conn = StubConnection(rows=TICK_ROWS)
    as_of = datetime(2026, 9, 3, 10, 0)
    get_market_data(conn, "Nifty 50", START, END, "tick", as_of=as_of)

    assert "make_interval" not in conn.data_sql
    assert conn.data_params == [42, START, END, as_of]


# ── Row mapping ──────────────────────────────────────────────────────────────

def test_bars_are_returned_as_floats_not_decimals():
    conn = StubConnection(rows=BAR_ROWS)
    result = get_market_data(conn, "Nifty 50", START, END, "5m")

    assert [type(b) for b in result] == [Bar, Bar]
    first = result[0]
    assert isinstance(first.open, float) and first.open == 100.0
    assert isinstance(first.volume, int) and first.volume == 1000
    assert first.instrument_id == 42
    assert first.timeframe == "5m"


def test_a_bar_knows_when_it_became_complete():
    conn = StubConnection(rows=BAR_ROWS)
    bar = get_market_data(conn, "Nifty 50", START, END, "5m")[0]
    assert bar.close_time == datetime(2026, 9, 3, 9, 20)
    assert bar.bar_minutes == 5


def test_ticks_are_mapped_to_tick_objects():
    conn = StubConnection(rows=TICK_ROWS)
    result = get_market_data(conn, "Nifty 50", START, END, "tick")
    assert [type(t) for t in result] == [Tick]
    assert result[0].ltp == 100.25
    assert result[0].ltq == 5
    assert result[0].volume == 9000


def test_bar_dicts_use_the_database_column_names():
    conn = StubConnection(rows=BAR_ROWS)
    bar = get_market_data(conn, "Nifty 50", START, END, "5m")[0]
    assert set(bar.as_dict()) == {
        "instrument_id", "timestamp", "open", "high", "low", "close", "volume",
    }


def test_access_output_satisfies_the_validator(weekday_calendar):
    # The read layer and the validation layer must agree on field names, or
    # the quality report would be validating the wrong shape.
    from marketdata.validation import validate_ohlcv

    conn = StubConnection(rows=BAR_ROWS)
    bars = get_market_data(conn, "Nifty 50", START, END, "5m")
    result = validate_ohlcv(
        [b.as_dict() for b in bars], "5m", is_trading_day=weekday_calendar
    )
    assert result.issues == ()


def test_tick_output_satisfies_the_tick_validator(weekday_calendar):
    from marketdata.validation import validate_ticks

    conn = StubConnection(rows=TICK_ROWS)
    ticks = get_market_data(conn, "Nifty 50", START, END, "tick")
    result = validate_ticks(
        [t.as_dict() for t in ticks], is_trading_day=weekday_calendar
    )
    assert result.issues == ()
