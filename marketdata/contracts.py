"""Canonical data contracts for the market-data tables.

Every field listed here exists in ``db/init_schema.sql`` today.  Nothing is
invented: ``origin`` records whether the value comes straight from the Angel
One feed (``source``), is produced by this project (``derived``), or is a
database bookkeeping column (``storage``).

See ``docs/data-contract.md`` for the prose version of this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


# ── Field specification ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldSpec:
    """One column of a market-data record."""

    name: str
    types: tuple[type, ...]
    required: bool
    origin: str  # 'source' | 'derived' | 'storage'
    description: str
    non_negative: bool = False
    positive: bool = False

    def __post_init__(self) -> None:
        if self.origin not in {"source", "derived", "storage"}:
            raise ValueError(f"Unknown origin {self.origin!r} for field {self.name!r}")


@dataclass(frozen=True)
class DatasetContract:
    """The full contract for one dataset (ticks, 1m bars, 5m bars)."""

    name: str
    table: str
    fields: tuple[FieldSpec, ...]
    #: Bar width in minutes. ``None`` for irregular data such as ticks.
    bar_minutes: int | None
    #: Columns whose combination must be unique (matches the DB constraint).
    unique_key: tuple[str, ...]
    #: Whether timestamps are expected to be strictly increasing per instrument.
    strictly_increasing: bool
    timestamp_field: str = "timestamp"
    #: Human-readable statement of what ``timestamp`` means.
    timestamp_semantics: str = ""
    _by_name: Mapping[str, FieldSpec] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_name", {f.name: f for f in self.fields})

    def field(self, name: str) -> FieldSpec:
        return self._by_name[name]

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    @property
    def known_fields(self) -> frozenset[str]:
        return frozenset(self._by_name)

    def price_fields(self) -> tuple[str, ...]:
        """Fields that must be strictly positive when present."""
        return tuple(f.name for f in self.fields if f.positive)


# ── OHLCV (shared shape for 1m and 5m) ───────────────────────────────────────

def _ohlcv_fields() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(
            "id", (int,), required=False, origin="storage",
            description="BIGSERIAL surrogate key. Not part of the data contract.",
        ),
        FieldSpec(
            "instrument_id", (int,), required=True, origin="derived",
            description=(
                "Foreign key into instruments(id). Resolved locally from the "
                "Angel One symbol token, not supplied by the feed."
            ),
        ),
        FieldSpec(
            "timestamp", (datetime,), required=True, origin="source",
            description=(
                "Bar START (left-labelled), naive local time in Asia/Kolkata. "
                "Written by downloader.ohlcv.normalize_timestamp()."
            ),
        ),
        FieldSpec(
            "open", (int, float), required=True, origin="source", positive=True,
            description="First traded price of the bar, in rupees.",
        ),
        FieldSpec(
            "high", (int, float), required=True, origin="source", positive=True,
            description="Highest traded price of the bar, in rupees.",
        ),
        FieldSpec(
            "low", (int, float), required=True, origin="source", positive=True,
            description="Lowest traded price of the bar, in rupees.",
        ),
        FieldSpec(
            "close", (int, float), required=True, origin="source", positive=True,
            description="Last traded price of the bar, in rupees.",
        ),
        FieldSpec(
            "volume", (int, float), required=True, origin="source", non_negative=True,
            description=(
                "Contracts/shares traded during the bar. Structurally 0 for "
                "cash indices (AMXIDX) — a legitimate value, not a defect."
            ),
        ),
        FieldSpec(
            "created_at", (datetime,), required=False, origin="storage",
            description=(
                "Row insertion time (processing time). The only processing-time "
                "column on the bar tables; never use it as a feature timestamp."
            ),
        ),
    )


_OHLCV_TS_SEMANTICS = (
    "Event time. The label is the bar's opening minute; the bar covers "
    "[timestamp, timestamp + bar_minutes) and is only complete at "
    "timestamp + bar_minutes."
)

OHLCV_1M = DatasetContract(
    name="ohlcv_1m",
    table="ohlcv_1min",
    fields=_ohlcv_fields(),
    bar_minutes=1,
    unique_key=("instrument_id", "timestamp"),
    strictly_increasing=True,
    timestamp_semantics=_OHLCV_TS_SEMANTICS,
)

OHLCV_5M = DatasetContract(
    name="ohlcv_5m",
    table="ohlcv_5min",
    fields=_ohlcv_fields(),
    bar_minutes=5,
    unique_key=("instrument_id", "timestamp"),
    strictly_increasing=True,
    timestamp_semantics=_OHLCV_TS_SEMANTICS,
)


# ── Ticks ────────────────────────────────────────────────────────────────────

TICK = DatasetContract(
    name="tick",
    table="tick_data",
    bar_minutes=None,
    unique_key=("instrument_id", "timestamp", "sequence_number"),
    # A snapshot feed can legitimately publish two snapshots carrying the same
    # last-traded timestamp, so ticks are only required to be non-decreasing.
    strictly_increasing=False,
    timestamp_semantics=(
        "Event time when the feed supplies last_traded_timestamp, processing "
        "time when it does not (downloader.tick_downloader.parse_tick falls "
        "back to datetime.now()). See docs/point-in-time-data.md."
    ),
    fields=(
        FieldSpec(
            "id", (int,), required=False, origin="storage",
            description="BIGSERIAL surrogate key. Not part of the data contract.",
        ),
        FieldSpec(
            "instrument_id", (int,), required=True, origin="derived",
            description="Foreign key into instruments(id), resolved from the feed token.",
        ),
        FieldSpec(
            "timestamp", (datetime,), required=True, origin="source",
            description=(
                "Last traded timestamp of the snapshot, naive. Interpreted in "
                "the collector process's local timezone — see the known "
                "limitation in docs/point-in-time-data.md."
            ),
        ),
        FieldSpec(
            "sequence_number", (int,), required=False, origin="source",
            non_negative=True,
            description=(
                "Feed's per-packet ordering token, part of the uniqueness key. "
                "0 marks rows collected before migration 001."
            ),
        ),
        FieldSpec(
            "ltp", (int, float), required=True, origin="source", positive=True,
            description="Last traded price in rupees (feed sends paise; /100 on ingest).",
        ),
        FieldSpec(
            "ltq", (int,), required=False, origin="source", non_negative=True,
            description="Last traded quantity.",
        ),
        FieldSpec(
            "open", (int, float), required=False, origin="source", positive=True,
            description="Day open price, as published in the snapshot.",
        ),
        FieldSpec(
            "high", (int, float), required=False, origin="source", positive=True,
            description="Day high so far, as published in the snapshot.",
        ),
        FieldSpec(
            "low", (int, float), required=False, origin="source", positive=True,
            description="Day low so far, as published in the snapshot.",
        ),
        FieldSpec(
            "close", (int, float), required=False, origin="source", positive=True,
            description=(
                "PREVIOUS day's close (the feed's 'closed_price'), NOT the "
                "current price. Named 'close' for historical reasons."
            ),
        ),
        FieldSpec(
            "avg_trade_price", (int, float), required=False, origin="source", positive=True,
            description="Day VWAP as published by the feed.",
        ),
        FieldSpec(
            "volume", (int,), required=False, origin="source", non_negative=True,
            description=(
                "CUMULATIVE traded volume for the day, not per-tick volume. "
                "Expected to be non-decreasing within a session."
            ),
        ),
        FieldSpec(
            "total_buy_qty", (int,), required=False, origin="source", non_negative=True,
            description="Total pending buy quantity across the book.",
        ),
        FieldSpec(
            "total_sell_qty", (int,), required=False, origin="source", non_negative=True,
            description="Total pending sell quantity across the book.",
        ),
        FieldSpec(
            "open_interest", (int,), required=False, origin="source", non_negative=True,
            description="Open interest; derivatives only, NULL for cash indices.",
        ),
        FieldSpec(
            "best_5_buy", (list,), required=False, origin="source",
            description="Top 5 bid levels as [{price, quantity, orders}], prices in rupees.",
        ),
        FieldSpec(
            "best_5_sell", (list,), required=False, origin="source",
            description="Top 5 ask levels as [{price, quantity, orders}], prices in rupees.",
        ),
        FieldSpec(
            "created_at", (datetime,), required=False, origin="storage",
            description="Row insertion time (processing time).",
        ),
    ),
)


_BY_NAME = {
    "tick": TICK,
    "ticks": TICK,
    "1m": OHLCV_1M,
    "ohlcv_1m": OHLCV_1M,
    "ohlcv_1min": OHLCV_1M,
    "5m": OHLCV_5M,
    "ohlcv_5m": OHLCV_5M,
    "ohlcv_5min": OHLCV_5M,
}


def contract_for(name: str) -> DatasetContract:
    """Look up a contract by timeframe label, dataset name or table name."""
    try:
        return _BY_NAME[name.lower()]
    except KeyError:
        valid = ", ".join(sorted(_BY_NAME))
        raise ValueError(f"Unknown dataset {name!r}. Choose from: {valid}") from None
