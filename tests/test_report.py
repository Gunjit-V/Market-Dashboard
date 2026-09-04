"""End-to-end assembly of the quality report, against a stub connection."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from marketdata.access import InstrumentNotFound
from marketdata.quality import FAIL, PASS, WARNING
from marketdata.report import assess_instrument, build_parser, main

START = datetime(2026, 9, 3, 9, 15)
END = datetime(2026, 9, 3, 10, 0)


def _bar_row(minute, high="101.0"):
    return (
        datetime(2026, 9, 3, 9, minute), Decimal("100.0"), Decimal(high),
        Decimal("99.0"), Decimal("100.5"), 1000,
    )


class RoutingStub:
    """Serves rows per table, so each dataset gets its own canned data."""

    def __init__(self, tables: dict[str, list], instrument_row=(42,)):
        self.tables = tables
        self.instrument_row = instrument_row
        self.queries: list[str] = []
        self._last = ""

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self.queries.append(self._last)

    def fetchone(self):
        return self.instrument_row

    def fetchall(self):
        for table, rows in self.tables.items():
            if f"FROM {table}" in self._last:
                return rows
        return []

    def close(self):
        pass


def test_a_clean_instrument_reports_pass(weekday_calendar):
    conn = RoutingStub({
        "ohlcv_1min": [_bar_row(15), _bar_row(16), _bar_row(17)],
        "ohlcv_5min": [_bar_row(15), _bar_row(20)],
    })
    report = assess_instrument(
        conn, "Nifty 50", START, END, ("1m", "5m"), is_trading_day=weekday_calendar
    )
    assert report.status == PASS
    assert report.instrument == "Nifty 50"
    assert [s.dataset for s in report.sections] == ["ohlcv_1m", "ohlcv_5m"]
    assert report.section("ohlcv_5m").records == 2


def test_bad_data_in_one_timeframe_fails_the_report(weekday_calendar):
    conn = RoutingStub({
        "ohlcv_1min": [_bar_row(15)],
        "ohlcv_5min": [_bar_row(15, high="1.0")],  # high below open
    })
    report = assess_instrument(
        conn, "Nifty 50", START, END, ("1m", "5m"), is_trading_day=weekday_calendar
    )
    assert report.status == FAIL
    assert report.section("ohlcv_1m").status == PASS
    assert report.section("ohlcv_5m").status == FAIL


def test_a_gap_produces_a_warning(weekday_calendar):
    conn = RoutingStub({"ohlcv_5min": [_bar_row(15), _bar_row(25)]})
    report = assess_instrument(
        conn, "Nifty 50", START, END, ("5m",), is_trading_day=weekday_calendar
    )
    assert report.status == WARNING
    assert report.section("ohlcv_5m").unexpected_gaps == 1


def test_only_the_requested_timeframes_are_read(weekday_calendar):
    conn = RoutingStub({"ohlcv_5min": [_bar_row(15)]})
    report = assess_instrument(
        conn, "Nifty 50", START, END, ("5m",), is_trading_day=weekday_calendar
    )
    assert report.section("ohlcv_1m") is None
    assert report.section("tick") is None
    assert not any("ohlcv_1min" in q for q in conn.queries)
    assert not any("tick_data" in q for q in conn.queries)


def test_ticks_are_validated_when_requested(weekday_calendar):
    conn = RoutingStub({
        "tick_data": [(datetime(2026, 9, 3, 9, 30), Decimal("100.0"), 5, 900)],
    })
    report = assess_instrument(
        conn, "Nifty 50", START, END, ("tick",), is_trading_day=weekday_calendar
    )
    assert report.section("tick").records == 1
    assert report.status == PASS


def test_tick_validation_is_truncated_and_says_so(weekday_calendar):
    rows = [
        (datetime(2026, 9, 3, 9, 30, second), Decimal("100.0"), 1, 100 + second)
        for second in range(5)
    ]
    conn = RoutingStub({"tick_data": rows})
    report = assess_instrument(
        conn, "Nifty 50", START, END, ("tick",),
        is_trading_day=weekday_calendar, max_ticks=3,
    )
    assert report.section("tick").records == 3
    assert any("truncated" in note for note in report.notes)


def test_an_unknown_symbol_raises_before_reading_data():
    conn = RoutingStub({}, instrument_row=None)
    with pytest.raises(InstrumentNotFound):
        assess_instrument(conn, "NOPE", START, END, ("5m",))


def test_an_unknown_timeframe_is_rejected():
    conn = RoutingStub({})
    with pytest.raises(ValueError, match="Unknown timeframe"):
        assess_instrument(conn, "Nifty 50", START, END, ("15m",))


def test_a_period_outside_the_bundled_calendar_carries_a_caveat(weekday_calendar):
    # scheduler/nse_calendar.py only ships 2026 holidays; the report must say
    # so rather than quietly over-counting gaps.
    conn = RoutingStub({"ohlcv_5min": [
        (datetime(2025, 9, 3, 9, 15), Decimal("100.0"), Decimal("101.0"),
         Decimal("99.0"), Decimal("100.5"), 10),
    ]})
    report = assess_instrument(
        conn, "Nifty 50", datetime(2025, 9, 1), datetime(2025, 9, 4), ("5m",),
        is_trading_day=weekday_calendar,
    )
    assert any("2025" in note for note in report.notes)


def test_no_caveat_for_a_covered_period(weekday_calendar):
    conn = RoutingStub({"ohlcv_5min": [_bar_row(15)]})
    report = assess_instrument(
        conn, "Nifty 50", START, END, ("5m",), is_trading_day=weekday_calendar
    )
    assert not any("holiday calendar" in note for note in report.notes)


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_parses_dates_and_datetimes():
    args = build_parser().parse_args([
        "--instrument", "Nifty 50", "--from", "2026-01-01",
        "--to", "2026-09-01T15:30:00",
    ])
    assert args.instrument == "Nifty 50"
    assert args.start == datetime(2026, 1, 1)
    assert args.end == datetime(2026, 9, 1, 15, 30)
    assert args.timeframes == ["1m", "5m"]


def test_cli_parses_a_timeframe_list():
    args = build_parser().parse_args([
        "--instrument", "X", "--from", "2026-01-01", "--to", "2026-01-02",
        "--timeframes", "tick, 5m",
    ])
    assert args.timeframes == ["tick", "5m"]


def test_cli_rejects_a_malformed_date():
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "--instrument", "X", "--from", "01/01/2026", "--to", "2026-01-02",
        ])


def test_cli_exit_code_reflects_the_report_status(capsys):
    code = main(
        [
            "--instrument", "Nifty 50", "--from", "2026-09-03T09:15:00",
            "--to", "2026-09-03T10:00:00", "--timeframes", "5m",
        ],
        connect=lambda: RoutingStub({"ohlcv_5min": [_bar_row(15), _bar_row(25)]}),
    )
    out = capsys.readouterr().out
    assert "Instrument: Nifty 50" in out
    assert "Overall status: WARNING" in out
    assert code == 1  # WARNING


def test_cli_can_emit_json(capsys):
    import json

    code = main(
        [
            "--instrument", "Nifty 50", "--from", "2026-09-03T09:15:00",
            "--to", "2026-09-03T10:00:00", "--timeframes", "5m", "--json",
        ],
        connect=lambda: RoutingStub({"ohlcv_5min": [_bar_row(15)]}),
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["instrument"] == "Nifty 50"
    assert code == 0


def test_cli_reports_an_unknown_symbol_without_a_traceback(capsys):
    code = main(
        ["--instrument", "NOPE", "--from", "2026-09-03", "--to", "2026-09-04"],
        connect=lambda: RoutingStub({}, instrument_row=None),
    )
    assert code == 3
    assert "error:" in capsys.readouterr().err
