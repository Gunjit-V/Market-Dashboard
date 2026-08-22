"""
RV Breakout / Regime-Shift strategy.

Idea: compare short-window realized vol against a longer baseline on the
underlying itself. When short RV expands well past its baseline (a volatility
regime shift), treat it as confirmation of an emerging directional move and
trade in the direction of the recent price change; exit once the regime
normalizes (ratio falls back toward 1) or a max hold period elapses.

This is a pure price-action strategy — it only needs index/future OHLCV, so
it can be backtested over the full available history (years, not weeks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backtest.engine import StrategySignal
from backtest.rv import Candle, compute_rv


@dataclass
class RVBreakoutStrategy:
    name: str = "rv_breakout"
    method: str = "garman_klass"
    short_window: int = 5 * 75   # 5 trading days of 5-min candles
    long_window: int = 20 * 75   # 20 trading days
    entry_ratio: float = 1.5     # short/long RV ratio that triggers entry
    exit_ratio: float = 1.1      # ratio falling below this exits
    max_hold_bars: int = 75      # exit after ~1 trading day regardless
    params: dict = field(default_factory=dict)

    def evaluate(
        self,
        window: list[Candle],
        in_position: bool,
        position_side: Optional[str],
        bars_held: int = 0,
    ) -> StrategySignal:
        if len(window) < self.long_window + 1:
            return StrategySignal(action="hold", reason="warming_up")

        long_slice = window[-self.long_window:]
        short_slice = window[-self.short_window:]

        rv_long = compute_rv(long_slice, self.method)
        rv_short = compute_rv(short_slice, self.method)

        if rv_long != rv_long or rv_long == 0 or rv_short != rv_short:
            # NaN guard (rv != rv is the NaN check without importing math here)
            return StrategySignal(action="hold", reason="invalid_rv")

        ratio = rv_short / rv_long
        metrics = {"rv_short": round(rv_short, 4), "rv_long": round(rv_long, 4), "ratio": round(ratio, 4)}

        if in_position:
            if ratio <= self.exit_ratio:
                return StrategySignal(action="close", reason="regime_normalized", metrics=metrics)
            if bars_held >= self.max_hold_bars:
                return StrategySignal(action="close", reason="max_hold_reached", metrics=metrics)
            return StrategySignal(action="hold", reason="in_position", metrics=metrics)

        if ratio >= self.entry_ratio:
            # Direction: trade with the sign of the recent price move that
            # drove the vol expansion.
            recent_change = short_slice[-1].close - short_slice[0].close
            side = "open_long" if recent_change >= 0 else "open_short"
            return StrategySignal(action=side, reason="vol_regime_shift", metrics=metrics)

        return StrategySignal(action="hold", reason="no_signal", metrics=metrics)
