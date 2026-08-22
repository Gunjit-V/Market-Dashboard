"""
Backtest runner: replays historical candles bar-by-bar through a strategy
and a Simulator, then persists the run summary, trades, and equity curve.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from api.utils.iv import time_to_expiry_years
from backtest.data import get_instrument_id, get_or_create_strategy, load_candles, load_option_chain_at
from backtest.engine import ClosedTrade, Simulator, compute_metrics
from backtest.rv import Candle
from backtest.strategies.rv_breakout import RVBreakoutStrategy
from backtest.strategies.vrp_reversion import VRPReversionStrategy
from db.connection import ConnectionManager


def _create_backtest_run(conn, strategy_id: int, params: dict, from_date: datetime, to_date: datetime, starting_capital: float) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_runs
                (strategy_id, params, from_date, to_date, starting_capital, status)
            VALUES (%s, %s, %s, %s, %s, 'running')
            RETURNING id
            """,
            (strategy_id, json.dumps(params, default=str), from_date, to_date, starting_capital),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def _finalize_run(conn, run_id: int, strategy_id: int, sim: Simulator, instrument_id: int, status: str = "completed", error: Optional[str] = None) -> dict:
    metrics = compute_metrics(sim)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE backtest_runs SET
                ending_capital = %s, total_trades = %s, winning_trades = %s,
                losing_trades = %s, total_pnl = %s, max_drawdown_pct = %s,
                sharpe_ratio = %s, win_rate_pct = %s, status = %s,
                error_message = %s, completed_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                metrics["ending_capital"], metrics["total_trades"], metrics["winning_trades"],
                metrics["losing_trades"], metrics["total_pnl"], metrics["max_drawdown_pct"],
                metrics["sharpe_ratio"], metrics["win_rate_pct"], status, error, run_id,
            ),
        )

        trade_rows = [
            (
                run_id, strategy_id, False, instrument_id, t.side, t.signal_reason,
                t.entry_time, t.entry_price, t.quantity, t.exit_time, t.exit_price,
                t.exit_reason, t.pnl, t.pnl_pct, "closed", json.dumps(t.metadata, default=str),
            )
            for t in sim.closed_trades
        ]
        if trade_rows:
            cur.executemany(
                """
                INSERT INTO trades (
                    backtest_run_id, strategy_id, is_paper, instrument_id, side,
                    signal_reason, entry_time, entry_price, quantity, exit_time,
                    exit_price, exit_reason, pnl, pnl_pct, status, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                trade_rows,
            )

        equity_rows = [
            (run_id, strategy_id, False, ts, equity, cash, open_val, 0.0)
            for ts, equity, cash, open_val in sim.equity_curve
        ]
        if equity_rows:
            cur.executemany(
                """
                INSERT INTO equity_curve (
                    backtest_run_id, strategy_id, is_paper, timestamp, equity,
                    cash, open_positions_value, drawdown_pct
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                equity_rows,
            )
    conn.commit()
    return metrics


def run_rv_breakout_backtest(
    symbol: str = "Nifty 50",
    from_date: datetime = None,
    to_date: datetime = None,
    starting_capital: float = 500_000.0,
    capital_per_trade: float = 100_000.0,
    strategy: RVBreakoutStrategy = None,
) -> dict:
    strategy = strategy or RVBreakoutStrategy()
    conn = ConnectionManager().get_connection()
    try:
        instrument_id = get_instrument_id(conn, symbol)
        if instrument_id is None:
            raise ValueError(f"Instrument '{symbol}' not found")

        candles = load_candles(conn, instrument_id, from_date, to_date)
        if len(candles) < strategy.long_window + 1:
            raise ValueError(
                f"Not enough candles ({len(candles)}) for long_window={strategy.long_window}"
            )

        params = {
            "symbol": symbol, "method": strategy.method,
            "short_window": strategy.short_window, "long_window": strategy.long_window,
            "entry_ratio": strategy.entry_ratio, "exit_ratio": strategy.exit_ratio,
            "max_hold_bars": strategy.max_hold_bars,
        }
        strategy_id = get_or_create_strategy(
            conn, f"{strategy.name}_{symbol}", f"RV regime-shift breakout on {symbol}", params
        )
        run_id = _create_backtest_run(conn, strategy_id, params, from_date, to_date, starting_capital)

        sim = Simulator(starting_capital, capital_per_trade)
        bars_held = 0

        try:
            for i in range(strategy.long_window, len(candles)):
                window = candles[: i + 1]
                bar = candles[i]

                signal = strategy.evaluate(
                    window, sim.position is not None,
                    sim.position.side if sim.position else None,
                    bars_held=bars_held,
                )

                if signal.action == "close" and sim.position is not None:
                    sim.close_position(bar.timestamp, bar.close, signal.reason)
                    bars_held = 0
                elif signal.action in ("open_long", "open_short") and sim.position is None:
                    side = "LONG" if signal.action == "open_long" else "SHORT"
                    sim.open_position(side, bar.timestamp, bar.close, signal.reason, signal.metrics)
                    bars_held = 0
                elif sim.position is not None:
                    bars_held += 1

                sim.record_equity(bar.timestamp, bar.close)

            # Close any still-open position at the final bar (mark-to-close).
            if sim.position is not None:
                sim.close_position(candles[-1].timestamp, candles[-1].close, "backtest_end")
                sim.record_equity(candles[-1].timestamp, candles[-1].close)

            metrics = _finalize_run(conn, run_id, strategy_id, sim, instrument_id, status="completed")
            return {"run_id": run_id, "strategy_id": strategy_id, **metrics}

        except Exception as e:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE backtest_runs SET status='failed', error_message=%s, completed_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (str(e), run_id),
                )
            conn.commit()
            raise
    finally:
        conn.close()


def run_vrp_reversion_backtest(
    option_symbol: str,
    underlying_symbol: str = "NIFTY25AUG26FUT",
    from_date: datetime = None,
    to_date: datetime = None,
    starting_capital: float = 500_000.0,
    capital_per_trade: float = 50_000.0,
    strategy: VRPReversionStrategy = None,
) -> dict:
    strategy = strategy or VRPReversionStrategy()
    conn = ConnectionManager().get_connection()
    try:
        option_id = get_instrument_id(conn, option_symbol)
        underlying_id = get_instrument_id(conn, underlying_symbol)
        if option_id is None:
            raise ValueError(f"Option '{option_symbol}' not found")
        if underlying_id is None:
            raise ValueError(f"Underlying '{underlying_symbol}' not found")

        with conn.cursor() as cur:
            cur.execute("SELECT strike, expiry FROM instruments WHERE id = %s", (option_id,))
            strike, expiry = cur.fetchone()
        strike = float(strike)
        option_type = "CE" if option_symbol.upper().endswith("CE") else "PE"

        option_candles = load_candles(conn, option_id, from_date, to_date)
        underlying_candles = load_candles(conn, underlying_id, from_date, to_date)
        underlying_by_ts = {c.timestamp: c for c in underlying_candles}

        if not option_candles:
            raise ValueError(f"No candle data for option '{option_symbol}' in range")

        params = {
            "option_symbol": option_symbol, "underlying_symbol": underlying_symbol,
            "strike": strike, "expiry": str(expiry), "option_type": option_type,
            "rv_method": strategy.rv_method, "rv_window": strategy.rv_window,
            "entry_vrp": strategy.entry_vrp, "entry_vrp_cheap": strategy.entry_vrp_cheap,
            "exit_vrp": strategy.exit_vrp, "max_hold_bars": strategy.max_hold_bars,
        }
        strategy_id = get_or_create_strategy(
            conn, f"{strategy.name}_{option_symbol}",
            f"VRP mean-reversion on {option_symbol}", params
        )
        run_id = _create_backtest_run(conn, strategy_id, params, from_date, to_date, starting_capital)

        sim = Simulator(starting_capital, capital_per_trade)
        bars_held = 0
        underlying_window: list[Candle] = []

        try:
            for opt_bar in option_candles:
                u_bar = underlying_by_ts.get(opt_bar.timestamp)
                if u_bar is None:
                    continue  # misaligned timestamps between independent downloads
                underlying_window.append(u_bar)
                if len(underlying_window) < strategy.rv_window:
                    continue

                T = time_to_expiry_years(expiry, opt_bar.timestamp)
                if T <= 0:
                    if sim.position is not None:
                        sim.close_position(opt_bar.timestamp, opt_bar.close, "expiry")
                        bars_held = 0
                    sim.record_equity(opt_bar.timestamp, opt_bar.close)
                    continue

                signal = strategy.evaluate_vrp(
                    underlying_window, opt_bar.close, u_bar.close, strike, T, option_type,
                    in_position=sim.position is not None, bars_held=bars_held,
                )

                if signal.action == "close" and sim.position is not None:
                    sim.close_position(opt_bar.timestamp, opt_bar.close, signal.reason)
                    bars_held = 0
                elif signal.action in ("open_long", "open_short") and sim.position is None:
                    side = "LONG" if signal.action == "open_long" else "SHORT"
                    sim.open_position(side, opt_bar.timestamp, opt_bar.close, signal.reason, signal.metrics)
                    bars_held = 0
                elif sim.position is not None:
                    bars_held += 1

                sim.record_equity(opt_bar.timestamp, opt_bar.close)

            if sim.position is not None:
                last = option_candles[-1]
                sim.close_position(last.timestamp, last.close, "backtest_end")
                sim.record_equity(last.timestamp, last.close)

            metrics = _finalize_run(conn, run_id, strategy_id, sim, option_id, status="completed")
            return {"run_id": run_id, "strategy_id": strategy_id, **metrics}

        except Exception as e:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE backtest_runs SET status='failed', error_message=%s, completed_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (str(e), run_id),
                )
            conn.commit()
            raise
    finally:
        conn.close()
