"""
Strategies API — registry, backtest triggering/results, live paper-trading
performance. Backed by backtest.runner and backtest.paper_trading, which own
the actual simulation logic; this module is a thin DB read/write layer plus
a way to kick off a backtest run.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from api.dependencies import get_db, require_api_key
from api.models.schemas import BacktestRequest, Response

router = APIRouter()


@router.get("", response_model=Response)
def list_strategies(conn=Depends(get_db)):
    """List all registered strategies (created by running a backtest or paper cycle)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, description, params, is_active, created_at
            FROM strategies ORDER BY id
            """
        )
        rows = cur.fetchall()
    return Response(
        status="success",
        data=[
            {
                "id": r[0], "name": r[1], "description": r[2],
                "params": r[3], "is_active": r[4], "created_at": r[5],
            }
            for r in rows
        ],
    )


@router.get("/{strategy_id}/backtests", response_model=Response)
def list_backtests(strategy_id: int, conn=Depends(get_db)):
    """List backtest runs for a strategy, most recent first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, strategy_id, params, from_date, to_date, starting_capital,
                   ending_capital, total_trades, winning_trades, losing_trades,
                   total_pnl, max_drawdown_pct, sharpe_ratio, win_rate_pct,
                   status, error_message, started_at, completed_at
            FROM backtest_runs WHERE strategy_id = %s ORDER BY started_at DESC
            """,
            (strategy_id,),
        )
        rows = cur.fetchall()
    return Response(
        status="success",
        data=[
            {
                "id": r[0], "strategy_id": r[1], "params": r[2], "from_date": r[3],
                "to_date": r[4], "starting_capital": float(r[5]),
                "ending_capital": float(r[6]) if r[6] is not None else None,
                "total_trades": r[7], "winning_trades": r[8], "losing_trades": r[9],
                "total_pnl": float(r[10]) if r[10] is not None else None,
                "max_drawdown_pct": float(r[11]) if r[11] is not None else None,
                "sharpe_ratio": float(r[12]) if r[12] is not None else None,
                "win_rate_pct": float(r[13]) if r[13] is not None else None,
                "status": r[14], "error_message": r[15],
                "started_at": r[16], "completed_at": r[17],
            }
            for r in rows
        ],
    )


@router.get("/backtests/{run_id}/equity-curve", response_model=Response)
def get_backtest_equity_curve(run_id: int, conn=Depends(get_db)):
    """Equity curve points for one backtest run, for charting."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, equity, cash, open_positions_value, drawdown_pct
            FROM equity_curve WHERE backtest_run_id = %s ORDER BY timestamp
            """,
            (run_id,),
        )
        rows = cur.fetchall()
    return Response(
        status="success",
        data=[
            {"timestamp": r[0], "equity": float(r[1]), "cash": float(r[2]),
             "open_positions_value": float(r[3]), "drawdown_pct": float(r[4]) if r[4] is not None else None}
            for r in rows
        ],
    )


@router.get("/backtests/{run_id}/trades", response_model=Response)
def get_backtest_trades(run_id: int, conn=Depends(get_db)):
    """Individual trades for one backtest run."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.backtest_run_id, t.strategy_id, t.is_paper, t.instrument_id,
                   i.symbol, t.side, t.signal_reason, t.entry_time, t.entry_price,
                   t.quantity, t.exit_time, t.exit_price, t.exit_reason, t.pnl,
                   t.pnl_pct, t.status, t.metadata
            FROM trades t JOIN instruments i ON i.id = t.instrument_id
            WHERE t.backtest_run_id = %s ORDER BY t.entry_time
            """,
            (run_id,),
        )
        rows = cur.fetchall()
    return Response(status="success", data=[_trade_row_to_dict(r) for r in rows])


def _trade_row_to_dict(r) -> dict:
    return {
        "id": r[0], "backtest_run_id": r[1], "strategy_id": r[2], "is_paper": r[3],
        "instrument_id": r[4], "symbol": r[5], "side": r[6], "signal_reason": r[7],
        "entry_time": r[8], "entry_price": float(r[9]), "quantity": r[10],
        "exit_time": r[11], "exit_price": float(r[12]) if r[12] is not None else None,
        "exit_reason": r[13], "pnl": float(r[14]) if r[14] is not None else None,
        "pnl_pct": float(r[15]) if r[15] is not None else None, "status": r[16],
        "metadata": r[17],
    }


def _run_backtest_task(req: BacktestRequest) -> None:
    """Background task wrapper — imports are local so importing this module
    (and therefore api.main at startup) never requires scipy/pandas unless a
    backtest actually runs.
    """
    from backtest.runner import run_rv_breakout_backtest, run_vrp_reversion_backtest
    from backtest.strategies.rv_breakout import RVBreakoutStrategy
    from backtest.strategies.vrp_reversion import VRPReversionStrategy

    params = req.params or {}
    try:
        if req.strategy == "rv_breakout":
            strategy = RVBreakoutStrategy(**{k: v for k, v in params.items() if k in RVBreakoutStrategy.__dataclass_fields__})
            run_rv_breakout_backtest(
                symbol=req.symbol or "Nifty 50",
                from_date=req.from_date,
                to_date=req.to_date,
                starting_capital=req.starting_capital,
                capital_per_trade=req.capital_per_trade or 100_000.0,
                strategy=strategy,
            )
        elif req.strategy == "vrp_reversion":
            if not req.option_symbol:
                return
            strategy = VRPReversionStrategy(**{k: v for k, v in params.items() if k in VRPReversionStrategy.__dataclass_fields__})
            run_vrp_reversion_backtest(
                option_symbol=req.option_symbol,
                underlying_symbol=req.underlying_symbol or "NIFTY25AUG26FUT",
                from_date=req.from_date,
                to_date=req.to_date,
                starting_capital=req.starting_capital,
                capital_per_trade=req.capital_per_trade or 50_000.0,
                strategy=strategy,
            )
    except Exception:
        # run_*_backtest already records the failure on the backtest_runs
        # row itself; nothing else to do with a background task's exception.
        pass


@router.post("/backtests/run", response_model=Response, dependencies=[Depends(require_api_key)])
def trigger_backtest(req: BacktestRequest, background_tasks: BackgroundTasks):
    """Kick off a backtest in the background. Poll GET /strategies/{id}/backtests
    for results, or GET /strategies to find the strategy_id first.
    """
    if req.strategy not in ("rv_breakout", "vrp_reversion"):
        raise HTTPException(400, f"Unknown strategy '{req.strategy}'")
    if req.strategy == "vrp_reversion" and not req.option_symbol:
        raise HTTPException(400, "vrp_reversion requires option_symbol")

    # Default to the full available history if no range given, so a
    # first-time caller gets a meaningful backtest without guessing dates.
    from_date = req.from_date or (datetime.now() - timedelta(days=365 * 3))
    to_date = req.to_date or datetime.now()
    req = req.model_copy(update={"from_date": from_date, "to_date": to_date})

    background_tasks.add_task(_run_backtest_task, req)
    return Response(
        status="success",
        message="Backtest started in background. Poll /strategies/{id}/backtests for the result.",
    )
