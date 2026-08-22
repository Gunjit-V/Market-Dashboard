"""
Paper Trading API — read-only views over the live paper-trading state that
scheduler.paper_trading_scheduler writes to (trades, equity_curve, signals
with is_paper=TRUE). This module never runs a strategy itself.
"""

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_db
from api.models.schemas import Response
from api.routes.strategies import _trade_row_to_dict

router = APIRouter()

PAPER_STARTING_CAPITAL = 500_000.0


@router.get("/summary", response_model=Response)
def paper_trading_summary(conn=Depends(get_db)):
    """Per-strategy paper-trading performance summary."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.name,
                   COALESCE(SUM(t.pnl) FILTER (WHERE t.status = 'closed'), 0) AS realized_pnl,
                   COUNT(t.id) FILTER (WHERE t.status = 'open') AS open_trades,
                   COUNT(t.id) FILTER (WHERE t.status = 'closed') AS closed_trades,
                   COUNT(t.id) FILTER (WHERE t.status = 'closed' AND t.pnl > 0) AS winning_trades,
                   COUNT(t.id) FILTER (WHERE t.status = 'closed' AND t.pnl <= 0) AS losing_trades
            FROM strategies s
            LEFT JOIN trades t ON t.strategy_id = s.id AND t.is_paper = TRUE
            -- Only strategies that have actually been evaluated as paper
            -- strategies at least once (an equity_curve row), not just
            -- registered via a backtest run.
            WHERE EXISTS (
                SELECT 1 FROM equity_curve ec
                WHERE ec.strategy_id = s.id AND ec.is_paper = TRUE
            )
            GROUP BY s.id, s.name
            ORDER BY s.id
            """
        )
        rows = cur.fetchall()

    summaries = []
    for strategy_id, name, realized_pnl, open_trades, closed_trades, wins, losses in rows:
        realized_pnl = float(realized_pnl)
        equity = PAPER_STARTING_CAPITAL + realized_pnl

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(drawdown_pct) FROM equity_curve
                WHERE strategy_id = %s AND is_paper = TRUE
                """,
                (strategy_id,),
            )
            max_dd_row = cur.fetchone()
            max_dd = float(max_dd_row[0]) if max_dd_row and max_dd_row[0] is not None else 0.0

        summaries.append({
            "strategy_id": strategy_id,
            "strategy_name": name,
            "starting_capital": PAPER_STARTING_CAPITAL,
            "current_equity": equity,
            "total_pnl": realized_pnl,
            "total_pnl_pct": (realized_pnl / PAPER_STARTING_CAPITAL) * 100,
            "open_trades": open_trades,
            "closed_trades": closed_trades,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate_pct": (wins / closed_trades * 100) if closed_trades else 0.0,
            "max_drawdown_pct": max_dd,
        })

    return Response(status="success", data=summaries)


@router.get("/trades", response_model=Response)
def paper_trades(
    strategy_id: int = Query(None, description="Filter by strategy"),
    status: str = Query(None, description="'open' or 'closed'"),
    limit: int = Query(200, ge=1, le=2000),
    conn=Depends(get_db),
):
    """Individual paper trades, most recent entry first."""
    filters = ["t.is_paper = TRUE"]
    params: list = []
    if strategy_id is not None:
        filters.append("t.strategy_id = %s")
        params.append(strategy_id)
    if status:
        filters.append("t.status = %s")
        params.append(status)
    where = " AND ".join(filters)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.id, t.backtest_run_id, t.strategy_id, t.is_paper, t.instrument_id,
                   i.symbol, t.side, t.signal_reason, t.entry_time, t.entry_price,
                   t.quantity, t.exit_time, t.exit_price, t.exit_reason, t.pnl,
                   t.pnl_pct, t.status, t.metadata
            FROM trades t JOIN instruments i ON i.id = t.instrument_id
            WHERE {where}
            ORDER BY t.entry_time DESC LIMIT %s
            """,
            (*params, limit),
        )
        rows = cur.fetchall()
    return Response(status="success", data=[_trade_row_to_dict(r) for r in rows])


@router.get("/equity-curve", response_model=Response)
def paper_equity_curve(
    strategy_id: int = Query(..., description="Strategy to chart"),
    conn=Depends(get_db),
):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, equity, cash, open_positions_value, drawdown_pct
            FROM equity_curve
            WHERE strategy_id = %s AND is_paper = TRUE
            ORDER BY timestamp
            """,
            (strategy_id,),
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


@router.get("/signals", response_model=Response)
def paper_signals(
    strategy_id: int = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    conn=Depends(get_db),
):
    """Live signal feed — every non-hold evaluation a paper strategy produced,
    whether or not it resulted in a trade.
    """
    filters = []
    params: list = []
    if strategy_id is not None:
        filters.append("sig.strategy_id = %s")
        params.append(strategy_id)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT sig.id, sig.strategy_id, s.name, sig.instrument_id, i.symbol,
                   sig.timestamp, sig.signal_type, sig.reason, sig.metrics, sig.acted_on
            FROM signals sig
            JOIN strategies s ON s.id = sig.strategy_id
            LEFT JOIN instruments i ON i.id = sig.instrument_id
            {where}
            ORDER BY sig.timestamp DESC LIMIT %s
            """,
            (*params, limit),
        )
        rows = cur.fetchall()
    return Response(
        status="success",
        data=[
            {
                "id": r[0], "strategy_id": r[1], "strategy_name": r[2],
                "instrument_id": r[3], "symbol": r[4], "timestamp": r[5],
                "signal_type": r[6], "reason": r[7], "metrics": r[8], "acted_on": r[9],
            }
            for r in rows
        ],
    )
