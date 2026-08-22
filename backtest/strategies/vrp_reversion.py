"""
VRP (Volatility Risk Premium) mean-reversion strategy.

Idea: VRP = IV(strike, expiry) - RV(underlying). When VRP is strongly
positive, options are priced rich relative to what the underlying has
actually been doing — sell premium (short the option). When VRP is strongly
negative, options are cheap — buy premium (long the option). Exit when VRP
reverts back toward zero, or at a max hold, or at expiry.

Unlike RVBreakoutStrategy this does not operate on a single instrument's
candle window — it needs the option's close (for IV) and the underlying's
RV at the same timestamp, so it is driven directly by backtest.runner rather
than through the generic Strategy.evaluate(window) path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from api.utils.iv import RISK_FREE_RATE, implied_volatility
from backtest.engine import StrategySignal
from backtest.rv import Candle, compute_rv


@dataclass
class VRPReversionStrategy:
    name: str = "vrp_reversion"
    rv_method: str = "garman_klass"
    rv_window: int = 5 * 75       # 5-day RV on the underlying
    entry_vrp: float = 0.05       # 5 vol points (decimal) rich -> sell
    entry_vrp_cheap: float = -0.05  # 5 vol points cheap -> buy
    exit_vrp: float = 0.01        # revert-to-zero exit band
    max_hold_bars: int = 150      # ~2 trading days
    params: dict = field(default_factory=dict)

    def evaluate_vrp(
        self,
        underlying_window: list[Candle],
        option_price: float,
        underlying_price: float,
        strike: float,
        time_to_expiry_years: float,
        option_type: str,
        in_position: bool,
        bars_held: int = 0,
    ) -> StrategySignal:
        if len(underlying_window) < self.rv_window:
            return StrategySignal(action="hold", reason="warming_up")

        rv = compute_rv(underlying_window[-self.rv_window:], self.rv_method)
        if rv != rv or rv <= 0:  # NaN or zero guard
            return StrategySignal(action="hold", reason="invalid_rv")

        iv = implied_volatility(
            option_price=option_price,
            S=underlying_price,
            K=strike,
            T=time_to_expiry_years,
            r=RISK_FREE_RATE,
            option_type=option_type,
        )
        if iv is None:
            return StrategySignal(action="hold", reason="iv_unavailable")

        vrp = iv - rv
        metrics = {"iv": round(iv, 4), "rv": round(rv, 4), "vrp": round(vrp, 4)}

        if in_position:
            if abs(vrp) <= self.exit_vrp:
                return StrategySignal(action="close", reason="vrp_reverted", metrics=metrics)
            if bars_held >= self.max_hold_bars:
                return StrategySignal(action="close", reason="max_hold_reached", metrics=metrics)
            return StrategySignal(action="hold", reason="in_position", metrics=metrics)

        if vrp >= self.entry_vrp:
            # Options rich vs realized vol -> sell premium (short the option).
            return StrategySignal(action="open_short", reason="vrp_rich", metrics=metrics)
        if vrp <= self.entry_vrp_cheap:
            # Options cheap vs realized vol -> buy premium (long the option).
            return StrategySignal(action="open_long", reason="vrp_cheap", metrics=metrics)

        return StrategySignal(action="hold", reason="no_signal", metrics=metrics)
