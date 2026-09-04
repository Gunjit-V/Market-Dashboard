"""The data contracts must describe the tables that actually exist."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from marketdata.contracts import OHLCV_1M, OHLCV_5M, TICK, contract_for

SCHEMA_SQL = (Path(__file__).resolve().parents[1] / "db" / "init_schema.sql").read_text()


def _columns_of(table: str) -> set[str]:
    """Pull the column names of *table* straight out of db/init_schema.sql."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
        SCHEMA_SQL,
        re.DOTALL,
    )
    assert match, f"{table} not found in db/init_schema.sql"

    columns = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith(("UNIQUE", "PRIMARY KEY", "--", "FOREIGN KEY")):
            continue
        columns.add(line.split()[0])
    return columns


@pytest.mark.parametrize(
    "contract,table",
    [(OHLCV_1M, "ohlcv_1min"), (OHLCV_5M, "ohlcv_5min"), (TICK, "tick_data")],
)
def test_contract_matches_database_schema(contract, table):
    assert contract.table == table
    assert set(contract.known_fields) == _columns_of(table)


def test_lookup_accepts_labels_names_and_tables():
    assert contract_for("5m") is OHLCV_5M
    assert contract_for("ohlcv_5min") is OHLCV_5M
    assert contract_for("OHLCV_1M") is OHLCV_1M
    assert contract_for("ticks") is TICK


def test_lookup_rejects_unknown_dataset():
    with pytest.raises(ValueError, match="Unknown dataset"):
        contract_for("15m")


def test_ohlcv_required_fields_are_the_source_columns():
    assert set(OHLCV_5M.required_fields) == {
        "instrument_id", "timestamp", "open", "high", "low", "close", "volume",
    }
    # Bookkeeping columns are never required of a caller.
    assert not OHLCV_5M.field("created_at").required
    assert OHLCV_5M.field("created_at").origin == "storage"


def test_tick_requires_only_identity_time_and_price():
    assert set(TICK.required_fields) == {"instrument_id", "timestamp", "ltp"}
    for optional in ("ltq", "volume", "open_interest", "best_5_buy"):
        assert not TICK.field(optional).required


def test_instrument_id_is_derived_not_a_feed_field():
    # The feed sends a token; instrument_id is resolved locally.
    assert OHLCV_5M.field("instrument_id").origin == "derived"
    assert TICK.field("instrument_id").origin == "derived"


def test_bar_widths_and_uniqueness_match_the_database_constraint():
    assert OHLCV_1M.bar_minutes == 1
    assert OHLCV_5M.bar_minutes == 5
    assert TICK.bar_minutes is None
    for contract in (OHLCV_1M, OHLCV_5M, TICK):
        assert contract.unique_key == ("instrument_id", "timestamp")


def test_bars_are_strictly_increasing_but_ticks_need_not_be():
    assert OHLCV_1M.strictly_increasing
    assert OHLCV_5M.strictly_increasing
    assert not TICK.strictly_increasing


def test_timestamp_field_is_typed_as_datetime():
    for contract in (OHLCV_1M, OHLCV_5M, TICK):
        assert contract.field("timestamp").types == (datetime,)


def test_tick_close_is_documented_as_the_previous_day_close():
    # This is a genuine trap in the existing feed mapping; the contract must
    # spell it out so nobody treats it as the current price.
    assert "PREVIOUS" in TICK.field("close").description
