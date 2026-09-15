"""Deterministic, point-in-time-aware access to stored market data.

This is a thin, testable read layer over the tables that already exist — not a
new data framework.  It exists so that every future feature/ML pipeline asks
for history in exactly one way, with the temporal rules of
``docs/02-market-data.md`` enforced in one place instead of being
re-implemented (and re-broken) per notebook.

Guarantees
----------
* **Deterministic** — rows always come back ordered by ``timestamp`` ascending;
  the SQL carries an explicit ``ORDER BY`` and a tie-breaking primary key.
* **Half-open window** — ``[start, end)`` on the bar label, so consecutive
  windows tile without overlapping or double-counting a boundary bar.
* **Point-in-time** — with ``as_of`` set, a bar is returned only if it had
  already *closed* by that instant, which is what stops a model from seeing a
  bar that was still forming.
* **Read-only** — no statement here writes, and no value is transformed beyond
  ``Decimal`` → ``float`` for the numeric columns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from marketdata.contracts import contract_for
from marketdata.sessions import bar_close

# Whitelisted so a timeframe string can never reach SQL unchecked.
_BAR_TABLES = {"1m": "ohlcv_1min", "5m": "ohlcv_5min"}
TICK_TABLE = "tick_data"


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar, left-labelled at its opening minute (naive IST)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    instrument_id: int | None = None
    timeframe: str | None = None

    @property
    def bar_minutes(self) -> int:
        return contract_for(self.timeframe or "1m").bar_minutes or 1

    @property
    def close_time(self) -> datetime:
        """The instant this bar became complete and safe to use."""
        return bar_close(self.timestamp, self.bar_minutes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class Tick:
    """One market snapshot as stored in ``tick_data``."""

    timestamp: datetime
    ltp: float
    ltq: int | None = None
    volume: int | None = None
    instrument_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "timestamp": self.timestamp,
            "ltp": self.ltp,
            "ltq": self.ltq,
            "volume": self.volume,
        }


class InstrumentNotFound(LookupError):
    """Raised when a symbol cannot be resolved to an instruments row."""


def resolve_instrument_id(conn, instrument: str | int) -> int:
    """Resolve a symbol (case-insensitive) or a raw id to ``instruments.id``."""
    if isinstance(instrument, int):
        return instrument
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM instruments WHERE UPPER(symbol) = UPPER(%s)",
            (instrument,),
        )
        row = cur.fetchone()
    if not row:
        raise InstrumentNotFound(f"No instrument with symbol {instrument!r}")
    return row[0]


def _float(value: Any) -> float:
    return float(value)


def _rows_to_bars(rows: Sequence[Sequence[Any]], timeframe: str, instrument_id: int) -> list[Bar]:
    return [
        Bar(
            timestamp=row[0],
            open=_float(row[1]),
            high=_float(row[2]),
            low=_float(row[3]),
            close=_float(row[4]),
            volume=int(row[5]),
            instrument_id=instrument_id,
            timeframe=timeframe,
        )
        for row in rows
    ]


def _rows_to_ticks(rows: Sequence[Sequence[Any]], instrument_id: int) -> list[Tick]:
    return [
        Tick(
            timestamp=row[0],
            ltp=_float(row[1]),
            ltq=row[2],
            volume=row[3],
            instrument_id=instrument_id,
        )
        for row in rows
    ]


def get_market_data(
    conn,
    instrument: str | int,
    start: datetime,
    end: datetime,
    timeframe: str = "1m",
    *,
    as_of: datetime | None = None,
    limit: int | None = None,
) -> list[Bar] | list[Tick]:
    """Return stored market data for one instrument over ``[start, end)``.

    Parameters
    ----------
    conn
        An open DB-API connection (``db.connection.ConnectionManager``).
    instrument
        Symbol as stored in ``instruments.symbol`` (case-insensitive), or an
        ``instruments.id``.
    start, end
        Naive IST bounds. ``start`` is inclusive, ``end`` is **exclusive**, so
        ``[09:15, 09:20)`` returns exactly the 09:15 five-minute bar.
    timeframe
        ``"1m"``, ``"5m"`` or ``"tick"``.
    as_of
        Point-in-time cut-off. Bars are returned only when they had already
        closed at ``as_of`` (``timestamp + bar_minutes <= as_of``); ticks only
        when ``timestamp <= as_of``. Leave ``None`` for plain historical reads.
    limit
        Optional cap on rows, applied after ordering (oldest first).

    Raises
    ------
    ValueError
        On an unknown timeframe or an inverted window.
    InstrumentNotFound
        When the symbol has no ``instruments`` row.
    """
    if end < start:
        raise ValueError("end must not be before start")

    instrument_id = resolve_instrument_id(conn, instrument)
    key = timeframe.lower()

    if key in ("tick", "ticks"):
        return _get_ticks(conn, instrument_id, start, end, as_of, limit)
    if key not in _BAR_TABLES:
        valid = ", ".join(sorted(_BAR_TABLES) + ["tick"])
        raise ValueError(f"Unknown timeframe {timeframe!r}. Choose from: {valid}")

    return _get_bars(conn, instrument_id, start, end, key, as_of, limit)


def _get_bars(
    conn,
    instrument_id: int,
    start: datetime,
    end: datetime,
    timeframe: str,
    as_of: datetime | None,
    limit: int | None,
) -> list[Bar]:
    table = _BAR_TABLES[timeframe]
    bar_minutes = contract_for(timeframe).bar_minutes or 1

    clauses = ["instrument_id = %s", "timestamp >= %s", "timestamp < %s"]
    params: list[Any] = [instrument_id, start, end]

    if as_of is not None:
        # Only bars that had already CLOSED by `as_of` are visible. The
        # interval arithmetic happens in SQL so the cut-off is identical
        # whichever client asks.
        clauses.append("timestamp + make_interval(mins => %s) <= %s")
        params.extend([bar_minutes, as_of])

    sql = f"""
        SELECT timestamp, open, high, low, close, volume
        FROM {table}
        WHERE {' AND '.join(clauses)}
        ORDER BY timestamp ASC, id ASC
    """
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return _rows_to_bars(rows, timeframe, instrument_id)


def _get_ticks(
    conn,
    instrument_id: int,
    start: datetime,
    end: datetime,
    as_of: datetime | None,
    limit: int | None,
) -> list[Tick]:
    clauses = ["instrument_id = %s", "timestamp >= %s", "timestamp < %s"]
    params: list[Any] = [instrument_id, start, end]

    if as_of is not None:
        # A tick is a point event: it is knowable the moment it is stamped.
        clauses.append("timestamp <= %s")
        params.append(as_of)

    sql = f"""
        SELECT timestamp, ltp, ltq, volume
        FROM {TICK_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY timestamp ASC, id ASC
    """
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return _rows_to_ticks(rows, instrument_id)


__all__ = [
    "Bar",
    "Tick",
    "InstrumentNotFound",
    "get_market_data",
    "resolve_instrument_id",
]
