"""
Realized volatility estimators for 5-minute OHLCV candles.

Mirrors frontend/src/utils/rv.ts so the backtest engine and the dashboard
agree on the same numbers. All functions take a sequence of candle dicts
with keys open/high/low/close (values must be positive floats) and return
an annualised decimal (e.g. 0.142 = 14.2%).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

CANDLES_PER_DAY = 75          # 09:15-15:30 IST / 5-min bars
TRADING_DAYS = 252
ANNUALIZATION = math.sqrt(CANDLES_PER_DAY * TRADING_DAYS)  # ~137.5


@dataclass(frozen=True)
class Candle:
    timestamp: object
    open: float
    high: float
    low: float
    close: float


def close_to_close(candles: Sequence[Candle]) -> float:
    if len(candles) < 2:
        return float("nan")
    log_returns = [
        math.log(candles[i].close / candles[i - 1].close)
        for i in range(1, len(candles))
        if candles[i - 1].close > 0 and candles[i].close > 0
    ]
    if not log_returns:
        return float("nan")
    return _std_dev(log_returns) * ANNUALIZATION


def parkinson(candles: Sequence[Candle]) -> float:
    if len(candles) < 1:
        return float("nan")
    k = 1 / (4 * math.log(2))
    total = 0.0
    for c in candles:
        if c.high <= 0 or c.low <= 0:
            continue
        total += math.log(c.high / c.low) ** 2
    return math.sqrt(k * total / len(candles)) * ANNUALIZATION


def garman_klass(candles: Sequence[Candle]) -> float:
    if len(candles) < 1:
        return float("nan")
    ln2 = math.log(2)
    total = 0.0
    for c in candles:
        if c.high <= 0 or c.low <= 0 or c.open <= 0 or c.close <= 0:
            continue
        hl = 0.5 * math.log(c.high / c.low) ** 2
        co = (2 * ln2 - 1) * math.log(c.close / c.open) ** 2
        total += hl - co
    return math.sqrt(total / len(candles)) * ANNUALIZATION


def rogers_satchell(candles: Sequence[Candle]) -> float:
    if len(candles) < 1:
        return float("nan")
    total = 0.0
    for c in candles:
        if c.high <= 0 or c.low <= 0 or c.open <= 0 or c.close <= 0:
            continue
        u = math.log(c.high / c.open)
        d = math.log(c.low / c.open)
        f = math.log(c.close / c.open)
        total += u * (u - f) + d * (d - f)
    return math.sqrt(total / len(candles)) * ANNUALIZATION


ESTIMATORS = {
    "close_close": close_to_close,
    "parkinson": parkinson,
    "garman_klass": garman_klass,
    "rogers_satchell": rogers_satchell,
}


def compute_rv(candles: Sequence[Candle], method: str = "garman_klass") -> float:
    try:
        fn = ESTIMATORS[method]
    except KeyError:
        raise ValueError(f"Unknown RV method {method!r}. Choose from: {sorted(ESTIMATORS)}")
    return fn(candles)


def _std_dev(values: Sequence[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)
