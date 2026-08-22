"""
Paper trading engine — reuses the same Strategy/Simulator machinery as the
backtest runner, but evaluates forward from "now" one bar at a time instead
of replaying history. State (open position, cash) is reconstructed from the
`trades` table on each run rather than kept in memory, since this is invoked
periodically by scheduler.paper_trading_scheduler as a short-lived process.

Design choice: positions are per (strategy_id, instrument_id) with is_paper=
TRUE. Only one open position per pair at a time, mirroring the backtest
Simulator's constraint, so paper trading and backtesting stay comparable.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from api.utils.iv import time_to_expiry_years
from backtest.data import get_instrument_id, get_or_create_strategy, load_candles
from backtest.rv import Candle
from backtest.strategies.rv_breakout import RVBreakoutStrategy
from backtest.strategies.vrp_reversion import VRPReversionStrategy
from db.connection import ConnectionManager

PAPER_STARTING_CAPITAL = 500_000.0
PAPER_CAPITAL_PER_TRADE = 100_000.0


def _get_open_position(conn, strategy_id: int, instrument_id: int) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, side, entry_time, entry_price, quantity, signal_reason, metadata
            FROM trades
            WHERE strategy_id = %s AND instrument_id = %s AND is_paper = TRUE AND status = 'open'
            ORDER BY entry_time DESC LIMIT 1
            """,
            (strategy_id, instrument_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "side": row[1], "entry_time": row[2], "entry_price": float(row[3]),
        "quantity": row[4], "signal_reason": row[5], "metadata": row[6] or {},
    }


def _bars_held_since(entry_time: datetime, candles: list[Candle]) -> int:
    return sum(1 for c in candles if c.timestamp > entry_time)


def _paper_cash(conn, strategy_id: int) -> float:
    """Reconstruct current paper cash from realized PnL of closed trades."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE strategy_id = %s AND is_paper = TRUE AND status = 'closed'",
            (strategy_id,),
        )
        realized_pnl = float(cur.fetchone()[0])
    return PAPER_STARTING_CAPITAL + realized_pnl


def _open_paper_trade(conn, strategy_id: int, instrument_id: int, side: str, timestamp: datetime, price: float, quantity: int, reason: str, metadata: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades (
                strategy_id, is_paper, instrument_id, side, signal_reason,
                entry_time, entry_price, quantity, status, metadata
            ) VALUES (%s, TRUE, %s, %s, %s, %s, %s, %s, 'open', %s)
            """,
            (strategy_id, instrument_id, side, reason, timestamp, price, quantity, json.dumps(metadata, default=str)),
        )
    conn.commit()


def _close_paper_trade(conn, trade_id: int, side: str, entry_price: float, quantity: int, timestamp: datetime, price: float, reason: str) -> float:
    direction = 1 if side == "LONG" else -1
    pnl = direction * (price - entry_price) * quantity
    pnl_pct = (pnl / (entry_price * quantity)) * 100 if entry_price * quantity else 0.0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trades SET
                exit_time = %s, exit_price = %s, exit_reason = %s,
                pnl = %s, pnl_pct = %s, status = 'closed'
            WHERE id = %s
            """,
            (timestamp, price, reason, pnl, pnl_pct, trade_id),
        )
    conn.commit()
    return pnl


def _record_equity(conn, strategy_id: int, timestamp: datetime, cash: float, open_value: float) -> None:
    equity = cash + open_value
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO equity_curve (strategy_id, is_paper, timestamp, equity, cash, open_positions_value)
            VALUES (%s, TRUE, %s, %s, %s, %s)
            """,
            (strategy_id, timestamp, equity, cash, open_value),
        )
    conn.commit()


def _log_signal(conn, strategy_id: int, instrument_id: int, timestamp: datetime, signal_type: str, reason: str, metrics: dict, acted_on: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signals (strategy_id, instrument_id, timestamp, signal_type, reason, metrics, acted_on)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (strategy_id, instrument_id, timestamp, signal_type, reason, json.dumps(metrics, default=str), acted_on),
        )
    conn.commit()


def evaluate_rv_breakout_paper(symbol: str = "Nifty 50", strategy: RVBreakoutStrategy = None) -> Optional[dict]:
    """Evaluate the RV breakout strategy against the latest candle and act
    on the signal (open/close a paper position) if warranted. Intended to be
    called once per new 5-min bar by the paper trading scheduler.
    """
    strategy = strategy or RVBreakoutStrategy()
    conn = ConnectionManager().get_connection()
    try:
        instrument_id = get_instrument_id(conn, symbol)
        if instrument_id is None:
            return None

        strategy_id = get_or_create_strategy(
            conn, f"{strategy.name}_{symbol}", f"RV regime-shift breakout on {symbol}",
            {"symbol": symbol, "method": strategy.method, "short_window": strategy.short_window,
             "long_window": strategy.long_window, "entry_ratio": strategy.entry_ratio,
             "exit_ratio": strategy.exit_ratio, "max_hold_bars": strategy.max_hold_bars},
        )

        with conn.cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM ohlcv_5min WHERE instrument_id = %s", (instrument_id,))
            latest_ts = cur.fetchone()[0]
        if latest_ts is None:
            return None

        from datetime import timedelta
        # long_window is in 5-min bars (75/trading day); convert to trading
        # days then pad generously for weekends/holidays in the calendar
        # window actually queried.
        trading_days_needed = (strategy.long_window // 75) + 1
        lookback_days = int(trading_days_needed * 1.6) + 5
        window_candles = load_candles(conn, instrument_id, latest_ts - timedelta(days=lookback_days), latest_ts)
        if len(window_candles) < strategy.long_window + 1:
            return {"status": "warming_up", "candles": len(window_candles)}

        open_pos = _get_open_position(conn, strategy_id, instrument_id)
        bars_held = _bars_held_since(open_pos["entry_time"], window_candles) if open_pos else 0

        signal = strategy.evaluate(
            window_candles, open_pos is not None,
            open_pos["side"] if open_pos else None, bars_held=bars_held,
        )
        latest_bar = window_candles[-1]

        acted_on = False
        if signal.action == "close" and open_pos is not None:
            pnl = _close_paper_trade(
                conn, open_pos["id"], open_pos["side"], open_pos["entry_price"],
                open_pos["quantity"], latest_bar.timestamp, latest_bar.close, signal.reason,
            )
            acted_on = True
        elif signal.action in ("open_long", "open_short") and open_pos is None:
            side = "LONG" if signal.action == "open_long" else "SHORT"
            qty = max(1, int(PAPER_CAPITAL_PER_TRADE / latest_bar.close))
            _open_paper_trade(
                conn, strategy_id, instrument_id, side, latest_bar.timestamp,
                latest_bar.close, qty, signal.reason, signal.metrics,
            )
            acted_on = True

        signal_type = "exit" if signal.action == "close" else ("entry_long" if signal.action == "open_long" else "entry_short" if signal.action == "open_short" else "hold")
        if signal.action != "hold":
            _log_signal(conn, strategy_id, instrument_id, latest_bar.timestamp, signal_type, signal.reason, signal.metrics, acted_on)

        cash = _paper_cash(conn, strategy_id)
        open_pos_after = _get_open_position(conn, strategy_id, instrument_id)
        open_value = 0.0
        if open_pos_after:
            direction = 1 if open_pos_after["side"] == "LONG" else -1
            open_value = direction * (latest_bar.close - open_pos_after["entry_price"]) * open_pos_after["quantity"]
        _record_equity(conn, strategy_id, latest_bar.timestamp, cash, open_value)

        return {"status": "evaluated", "action": signal.action, "reason": signal.reason, "metrics": signal.metrics, "timestamp": str(latest_bar.timestamp)}
    finally:
        conn.close()


def evaluate_vrp_reversion_paper(
    option_symbol: str, underlying_symbol: str, strategy: VRPReversionStrategy = None
) -> Optional[dict]:
    """Evaluate the VRP reversion strategy for one option against the latest
    aligned option/underlying bar.
    """
    strategy = strategy or VRPReversionStrategy()
    conn = ConnectionManager().get_connection()
    try:
        option_id = get_instrument_id(conn, option_symbol)
        underlying_id = get_instrument_id(conn, underlying_symbol)
        if option_id is None or underlying_id is None:
            return None

        with conn.cursor() as cur:
            cur.execute("SELECT strike, expiry FROM instruments WHERE id = %s", (option_id,))
            strike, expiry = cur.fetchone()
        strike = float(strike)
        option_type = "CE" if option_symbol.upper().endswith("CE") else "PE"

        strategy_id = get_or_create_strategy(
            conn, f"{strategy.name}_{option_symbol}",
            f"VRP mean-reversion on {option_symbol}",
            {"option_symbol": option_symbol, "underlying_symbol": underlying_symbol,
             "strike": strike, "expiry": str(expiry), "option_type": option_type,
             "rv_method": strategy.rv_method, "rv_window": strategy.rv_window,
             "entry_vrp": strategy.entry_vrp, "entry_vrp_cheap": strategy.entry_vrp_cheap,
             "exit_vrp": strategy.exit_vrp, "max_hold_bars": strategy.max_hold_bars},
        )

        with conn.cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM ohlcv_5min WHERE instrument_id = %s", (option_id,))
            latest_ts = cur.fetchone()[0]
        if latest_ts is None:
            return None

        from datetime import timedelta
        trading_days_needed = (strategy.rv_window // 75) + 1
        lookback_days = int(trading_days_needed * 1.6) + 5
        underlying_window = load_candles(conn, underlying_id, latest_ts - timedelta(days=lookback_days), latest_ts)
        option_candles = load_candles(conn, option_id, latest_ts, latest_ts)
        if not option_candles or len(underlying_window) < strategy.rv_window:
            return {"status": "warming_up"}

        opt_bar = option_candles[-1]
        T = time_to_expiry_years(expiry, opt_bar.timestamp)
        underlying_bar = underlying_window[-1]

        open_pos = _get_open_position(conn, strategy_id, option_id)
        bars_held = _bars_held_since(open_pos["entry_time"], option_candles) if open_pos else 0

        if T <= 0:
            if open_pos is not None:
                _close_paper_trade(
                    conn, open_pos["id"], open_pos["side"], open_pos["entry_price"],
                    open_pos["quantity"], opt_bar.timestamp, opt_bar.close, "expiry",
                )
            cash = _paper_cash(conn, strategy_id)
            _record_equity(conn, strategy_id, opt_bar.timestamp, cash, 0.0)
            return {"status": "expired"}

        signal = strategy.evaluate_vrp(
            underlying_window, opt_bar.close, underlying_bar.close, strike, T, option_type,
            in_position=open_pos is not None, bars_held=bars_held,
        )

        acted_on = False
        if signal.action == "close" and open_pos is not None:
            _close_paper_trade(
                conn, open_pos["id"], open_pos["side"], open_pos["entry_price"],
                open_pos["quantity"], opt_bar.timestamp, opt_bar.close, signal.reason,
            )
            acted_on = True
        elif signal.action in ("open_long", "open_short") and open_pos is None:
            side = "LONG" if signal.action == "open_long" else "SHORT"
            qty = max(1, int(PAPER_CAPITAL_PER_TRADE / opt_bar.close))
            _open_paper_trade(
                conn, strategy_id, option_id, side, opt_bar.timestamp,
                opt_bar.close, qty, signal.reason, signal.metrics,
            )
            acted_on = True

        signal_type = "exit" if signal.action == "close" else ("entry_long" if signal.action == "open_long" else "entry_short" if signal.action == "open_short" else "hold")
        if signal.action != "hold":
            _log_signal(conn, strategy_id, option_id, opt_bar.timestamp, signal_type, signal.reason, signal.metrics, acted_on)

        cash = _paper_cash(conn, strategy_id)
        open_pos_after = _get_open_position(conn, strategy_id, option_id)
        open_value = 0.0
        if open_pos_after:
            direction = 1 if open_pos_after["side"] == "LONG" else -1
            open_value = direction * (opt_bar.close - open_pos_after["entry_price"]) * open_pos_after["quantity"]
        _record_equity(conn, strategy_id, opt_bar.timestamp, cash, open_value)

        return {"status": "evaluated", "action": signal.action, "reason": signal.reason, "metrics": signal.metrics, "timestamp": str(opt_bar.timestamp)}
    finally:
        conn.close()
