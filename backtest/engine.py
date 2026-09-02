"""
Core backtest simulation engine.

Design: a Strategy is a pure function of a rolling window of candles that
either does nothing or returns a StrategyAction (open/close a position). The
Simulator owns capital, open positions, and fills — it is driven bar-by-bar
by historical replay (backtest.runner), with one fill/PnL model and one
database representation (the `trades` / `equity_curve` tables).

Position sizing here is intentionally simple (fixed notional per trade) —
this is a signal/backtesting demonstration, not a production risk engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Protocol

from backtest.rv import Candle


# ── Strategy protocol ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StrategySignal:
    """What a strategy wants to do at a given bar."""
    action: str            # 'open_long' | 'open_short' | 'close' | 'hold'
    reason: str = ""
    metrics: dict = field(default_factory=dict)


class Strategy(Protocol):
    """A strategy evaluates the latest window of candles for one instrument
    and returns a signal. Strategies are stateless between calls — any state
    needed (e.g. "are we currently in a position") is passed in via
    `in_position`, since the Simulator owns positions.
    """

    name: str

    def evaluate(
        self,
        window: list[Candle],
        in_position: bool,
        position_side: Optional[str],
    ) -> StrategySignal:
        ...


# ── Simulated position / trade ───────────────────────────────────────────────

@dataclass
class OpenPosition:
    side: str               # 'LONG' or 'SHORT'
    entry_time: datetime
    entry_price: float
    quantity: int
    signal_reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ClosedTrade:
    side: str
    entry_time: datetime
    entry_price: float
    quantity: int
    exit_time: datetime
    exit_price: float
    exit_reason: str
    pnl: float
    pnl_pct: float
    signal_reason: str
    metadata: dict = field(default_factory=dict)


def _mark_to_market_pnl(pos: OpenPosition, price: float) -> float:
    direction = 1 if pos.side == "LONG" else -1
    return direction * (price - pos.entry_price) * pos.quantity


class Simulator:
    """Tracks cash, a single open position per instrument, and an equity
    curve as bars are fed in. Fixed-notional sizing: each trade risks
    `capital_per_trade` worth of the instrument at entry price.
    """

    def __init__(self, starting_capital: float, capital_per_trade: float):
        self.starting_capital = starting_capital
        self.capital_per_trade = capital_per_trade
        self.cash = starting_capital
        self.position: Optional[OpenPosition] = None
        self.closed_trades: list[ClosedTrade] = []
        self.equity_curve: list[tuple[datetime, float, float, float]] = []
        # (timestamp, equity, cash, open_position_value)
        self._peak_equity = starting_capital

    def _quantity_for(self, price: float) -> int:
        if price <= 0:
            return 0
        return max(1, int(self.capital_per_trade / price))

    def open_position(
        self, side: str, timestamp: datetime, price: float, reason: str, metadata: dict | None = None
    ) -> None:
        if self.position is not None:
            return  # one position at a time, per instrument
        qty = self._quantity_for(price)
        self.position = OpenPosition(
            side=side,
            entry_time=timestamp,
            entry_price=price,
            quantity=qty,
            signal_reason=reason,
            metadata=metadata or {},
        )

    def close_position(self, timestamp: datetime, price: float, reason: str) -> Optional[ClosedTrade]:
        if self.position is None:
            return None
        pos = self.position
        pnl = _mark_to_market_pnl(pos, price)
        pnl_pct = (pnl / (pos.entry_price * pos.quantity)) * 100 if pos.entry_price * pos.quantity else 0.0
        trade = ClosedTrade(
            side=pos.side,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            quantity=pos.quantity,
            exit_time=timestamp,
            exit_price=price,
            exit_reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            signal_reason=pos.signal_reason,
            metadata=pos.metadata,
        )
        self.cash += pnl
        self.closed_trades.append(trade)
        self.position = None
        return trade

    def record_equity(self, timestamp: datetime, mark_price: Optional[float]) -> None:
        open_value = 0.0
        if self.position is not None and mark_price is not None:
            open_value = _mark_to_market_pnl(self.position, mark_price)
        equity = self.cash + open_value
        self._peak_equity = max(self._peak_equity, equity)
        drawdown_pct = (
            ((self._peak_equity - equity) / self._peak_equity) * 100
            if self._peak_equity > 0 else 0.0
        )
        self.equity_curve.append((timestamp, equity, self.cash, open_value))
        return drawdown_pct

    @property
    def equity(self) -> float:
        if not self.equity_curve:
            return self.starting_capital
        return self.equity_curve[-1][1]


# ── Backtest metrics ─────────────────────────────────────────────────────────

def compute_metrics(sim: Simulator) -> dict:
    trades = sim.closed_trades
    total = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    ending_capital = sim.starting_capital + total_pnl

    max_dd = 0.0
    peak = sim.starting_capital
    for _, equity, _, _ in sim.equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)

    # Sharpe on a daily-resampled equity series (simple, not annualization-perfect
    # for intraday paths, but consistent and good enough for a comparison metric).
    daily_returns = _daily_returns(sim.equity_curve)
    sharpe = _sharpe_ratio(daily_returns)

    return {
        "ending_capital": ending_capital,
        "total_trades": total,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "total_pnl": total_pnl,
        "max_drawdown_pct": max_dd,
        "sharpe_ratio": sharpe,
        "win_rate_pct": (len(wins) / total * 100) if total else 0.0,
    }


def _daily_returns(equity_curve: list[tuple[datetime, float, float, float]]) -> list[float]:
    if len(equity_curve) < 2:
        return []
    by_day: dict[object, float] = {}
    for ts, equity, _, _ in equity_curve:
        day = ts.date() if hasattr(ts, "date") else ts
        by_day[day] = equity  # last value of the day wins
    values = list(by_day.values())
    returns = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev:
            returns.append((values[i] - prev) / prev)
    return returns


def _sharpe_ratio(daily_returns: list[float], risk_free_daily: float = 0.065 / 252) -> float:
    if len(daily_returns) < 2:
        return 0.0
    excess = [r - risk_free_daily for r in daily_returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / len(excess)
    std = variance ** 0.5
    if std == 0:
        return 0.0
    return (mean / std) * (252 ** 0.5)
