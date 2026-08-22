"""Database access helpers for the backtest engine."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from backtest.rv import Candle


def get_instrument_id(conn, symbol: str) -> Optional[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM instruments WHERE UPPER(symbol) = UPPER(%s)", (symbol,)
        )
        row = cur.fetchone()
        return row[0] if row else None


def load_candles(
    conn,
    instrument_id: int,
    from_date: datetime,
    to_date: datetime,
    table: str = "ohlcv_5min",
) -> list[Candle]:
    if table not in ("ohlcv_5min", "ohlcv_1min"):
        raise ValueError(f"Unsupported table {table!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT timestamp, open, high, low, close
            FROM {table}
            WHERE instrument_id = %s AND timestamp >= %s AND timestamp <= %s
            ORDER BY timestamp ASC
            """,
            (instrument_id, from_date, to_date),
        )
        rows = cur.fetchall()
    return [
        Candle(timestamp=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]))
        for r in rows
    ]


def load_option_chain_at(
    conn, name: str, expiry: date, as_of: datetime
) -> list[dict]:
    """Return the latest close (<= as_of) for every active-at-the-time option
    instrument for `name`/`expiry`, plus its strike and CE/PE type.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (i.id)
                i.id, i.symbol, i.strike, o.close, o.timestamp
            FROM instruments i
            JOIN ohlcv_5min o ON o.instrument_id = i.id
            WHERE i.instrument_type = 'OPTIDX'
              AND i.name = %s
              AND i.expiry = %s
              AND o.timestamp <= %s
            ORDER BY i.id, o.timestamp DESC
            """,
            (name, expiry, as_of),
        )
        rows = cur.fetchall()
    return [
        {
            "instrument_id": r[0],
            "symbol": r[1],
            "strike": float(r[2]),
            "close": float(r[3]),
            "timestamp": r[4],
            "option_type": "CE" if r[1].upper().endswith("CE") else "PE",
        }
        for r in rows
    ]


def get_or_create_strategy(conn, name: str, description: str, params: dict) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM strategies WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE strategies SET description = %s, params = %s WHERE id = %s",
                (description, _to_json(params), row[0]),
            )
            conn.commit()
            return row[0]
        cur.execute(
            """
            INSERT INTO strategies (name, description, params, is_active)
            VALUES (%s, %s, %s, TRUE)
            RETURNING id
            """,
            (name, description, _to_json(params)),
        )
        strategy_id = cur.fetchone()[0]
    conn.commit()
    return strategy_id


def _to_json(d: dict) -> str:
    import json
    return json.dumps(d, default=str)
