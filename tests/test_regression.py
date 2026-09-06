"""Guards on pre-existing HereWeGoAgain behaviour.

Phase 1 added a validation layer; it must not have changed how the existing
downloader, scheduler or backtest code behaves. Modules that require the
Angel One SDK, psycopg2 or FastAPI are checked statically (via ``ast``) or
skipped, so this file runs in a bare environment.
"""

from __future__ import annotations

import ast
import importlib.util
from datetime import date, datetime, time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module_ast(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text())


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found")


def _signature(func: ast.FunctionDef) -> list[str]:
    return [arg.arg for arg in func.args.args]


def _has_dependency(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


# ── NSE calendar (unchanged, and now shared with marketdata.sessions) ─────────

def test_nse_calendar_still_classifies_days_the_same_way():
    from scheduler.nse_calendar import is_nse_trading_day

    holidays = {date(2026, 1, 26)}
    specials = {date(2026, 2, 1)}  # a Sunday budget session

    assert is_nse_trading_day(date(2026, 9, 3), holidays, specials)      # Thursday
    assert not is_nse_trading_day(date(2026, 9, 5), holidays, specials)  # Saturday
    assert not is_nse_trading_day(date(2026, 1, 26), holidays, specials)  # holiday
    assert is_nse_trading_day(date(2026, 2, 1), holidays, specials)      # special


def test_nse_download_slots_are_unchanged():
    from scheduler.nse_calendar import (
        FIRST_DOWNLOAD_SLOT,
        LAST_DOWNLOAD_SLOT,
        due_download_slot,
    )

    assert FIRST_DOWNLOAD_SLOT == time(9, 20)
    assert LAST_DOWNLOAD_SLOT == time(15, 30)

    # A Thursday, on a five-minute boundary, past the settle delay.
    slot = due_download_slot(datetime(2026, 9, 3, 10, 5, 45))
    assert slot is not None and slot.time() == time(10, 5)
    # Off-boundary and before the settle delay are both ignored.
    assert due_download_slot(datetime(2026, 9, 3, 10, 6, 45)) is None
    assert due_download_slot(datetime(2026, 9, 3, 10, 5, 5)) is None
    # Weekends never produce a slot.
    assert due_download_slot(datetime(2026, 9, 5, 10, 5, 45)) is None


def test_session_model_agrees_with_the_scheduler_slots():
    # marketdata.sessions must not contradict the schedule the downloader
    # already runs on: the last 5m bar starts at 15:25 and is downloaded at
    # the 15:30 slot.
    from scheduler.nse_calendar import LAST_DOWNLOAD_SLOT
    from marketdata.sessions import REGULAR_SESSION, bar_close

    last_bar = REGULAR_SESSION.bar_starts(date(2026, 9, 3), 5)[-1]
    assert bar_close(last_bar, 5).time() == LAST_DOWNLOAD_SLOT


# ── Realized-volatility estimators (untouched) ───────────────────────────────

def test_realized_volatility_constants_are_unchanged():
    from backtest.rv import ANNUALIZATION, CANDLES_PER_DAY, TRADING_DAYS

    assert CANDLES_PER_DAY == 75
    assert TRADING_DAYS == 252
    assert ANNUALIZATION == pytest.approx(137.47727, rel=1e-6)


def test_close_to_close_still_computes_on_a_known_series():
    from backtest.rv import Candle, close_to_close

    candles = [
        Candle(timestamp=i, open=100.0, high=101.0, low=99.0, close=close)
        for i, close in enumerate([100.0, 101.0, 100.0, 101.0, 100.0])
    ]
    value = close_to_close(candles)
    assert value == pytest.approx(1.367944, rel=1e-5)


def test_close_to_close_still_needs_two_candles():
    from backtest.rv import Candle, close_to_close

    import math

    assert math.isnan(close_to_close([Candle(0, 1.0, 1.0, 1.0, 1.0)]))


# ── Downloader: public API preserved ─────────────────────────────────────────

def test_download_historical_data_signature_is_unchanged():
    tree = _module_ast("downloader/ohlcv.py")
    func = _function(tree, "download_historical_data")
    assert _signature(func) == [
        "interval", "instrument_types", "names", "symbols", "days", "smart_api"
    ]


def test_save_candles_to_db_stayed_backward_compatible():
    # Phase 1 added an `interval` parameter; it must have a default so every
    # existing call site keeps working unchanged.
    tree = _module_ast("downloader/ohlcv.py")
    func = _function(tree, "save_candles_to_db")
    args = _signature(func)
    assert args[:4] == ["conn", "instrument_id", "candles", "table"]
    assert args[4] == "interval"
    assert len(func.args.defaults) == 1  # only `interval` is defaulted


def test_interval_table_mapping_is_unchanged():
    tree = _module_ast("downloader/ohlcv.py")
    intervals = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", None) == "INTERVALS"
    )
    assert ast.literal_eval(intervals.value) == {
        "1m": {"api_interval": "ONE_MINUTE", "table": "ohlcv_1min", "minutes": 1},
        "5m": {"api_interval": "FIVE_MINUTE", "table": "ohlcv_5min", "minutes": 5},
    }


def test_ingestion_defaults_to_report_only():
    # The default mode must not withhold anything, or Phase 1 would silently
    # change what the downloader writes.
    from marketdata.ingest import REPORT, configured_mode

    assert configured_mode({}) == REPORT


# ── Database schema is untouched ─────────────────────────────────────────────

def test_phase_1_did_not_alter_the_schema():
    schema = (ROOT / "db" / "init_schema.sql").read_text()
    # `signals` was removed on master alongside the paper-trading feature.
    for table in ("ohlcv_1min", "ohlcv_5min", "tick_data", "instruments",
                  "download_log", "strategies", "backtest_runs", "trades",
                  "equity_curve"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema
    # The bootstrap script stays declarative: schema changes ship as numbered
    # files under db/migrations/ instead (see 001_tick_sequence_number.sql).
    assert "ALTER TABLE" not in schema
    assert "DROP TABLE" not in schema


# ── API surface ──────────────────────────────────────────────────────────────

def test_all_pre_existing_routers_are_still_registered():
    main = (ROOT / "api" / "main.py").read_text()
    # /paper-trading was removed on master with the paper-trading feature.
    for prefix in ("/health", "/instruments", "/ohlcv", "/ticks", "/download",
                   "/volatility", "/strategies"):
        assert f'prefix="{prefix}"' in main
    assert 'prefix="/paper-trading"' not in main


def test_the_quality_router_was_added_read_only():
    main = (ROOT / "api" / "main.py").read_text()
    assert 'prefix="/quality"' in main

    tree = _module_ast("api/routes/quality.py")
    decorators = [
        d.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for d in node.decorator_list
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
    ]
    assert decorators and set(decorators) == {"get"}, "quality endpoints must be read-only"


@pytest.mark.skipif(
    not (_has_dependency("fastapi") and _has_dependency("psycopg2")),
    reason="FastAPI/psycopg2 not installed in this environment",
)
def test_the_api_application_still_imports_and_exposes_every_route():
    from api.main import app

    paths = {route.path for route in app.routes}
    assert "/ohlcv/{symbol}" in paths
    assert "/ticks/{symbol}" in paths
    assert "/quality/{symbol}" in paths


def test_ingestion_survives_a_validation_failure():
    # A bug in the validation layer must never stop candles from being stored.
    tree = _module_ast("downloader/ohlcv.py")
    func = _function(tree, "save_candles_to_db")
    handlers = [
        node for node in ast.walk(func)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "Exception"
    ]
    assert handlers, "screening must be wrapped in a broad except"
