"""Volatility features.

Six features, and the only ones in this project permitted to read across a
session boundary.

They may because they are *path-dependent aggregates* -- sums over per-bar
terms -- so a term that spans two sessions can simply be dropped and the rest
of the sum remains valid. That exclusion is mandatory, not cosmetic: on Nifty
50 5-minute bars a session-boundary log return carries 7.6x the standard
deviation of an intraday one, which is **58x the variance**. Leaving one inside
a 20-return window inflates the variance 3.9x and overstates realized
volatility 2.0x -- every morning, in a pattern indistinguishable from the real
opening-session volatility it would be mistaken for.

The payoff for crossing sessions is availability. Reset each morning, a 20-bar
estimate is absent for the first 26.8% of every 5-minute session; rolling, it
is absent only for the first 20 bars of all history.

Estimators mirror ``backtest/rv.py`` so the dashboard, the backtester and the
feature layer report the same numbers. Annualisation is timeframe-aware here
(``backtest/rv.py`` hardcodes the 5-minute constant), and a test asserts the
two agree on 5-minute input.
"""

from __future__ import annotations

import math

from features.registry import register
from features.spec import ROLLING, TRAILING, FeatureSpec
from features.state import FeatureStatus, FeatureValue
from features.windows import BarWindow, log_returns, stdev, true_ranges

#: Trading days per year, matching ``backtest.rv.TRADING_DAYS``.
TRADING_DAYS = 252

#: Feature windows in *minutes*, so a window means the same elapsed time on
#: both timeframes rather than the same number of bars.
RV_WINDOW_MINUTES = 100
ATR_WINDOW_MINUTES = 70
RV_ACCEL_LAG_MINUTES = 30

_MINUTES_PER_BAR = {"1m": 1, "5m": 5}
_BARS_PER_SESSION = {"1m": 375, "5m": 75}


def bars_for(timeframe: str, minutes: int) -> int:
    """Bars spanning *minutes* on *timeframe*, at least one."""
    return max(1, minutes // _MINUTES_PER_BAR[timeframe])


def annualization(timeframe: str) -> float:
    """Scale a per-bar standard deviation to an annualised figure."""
    return math.sqrt(_BARS_PER_SESSION[timeframe] * TRADING_DAYS)


def _missing(spec: FeatureSpec, detail: str) -> FeatureValue:
    return FeatureValue.absent(spec.name, FeatureStatus.MISSING, detail)


def _short(spec: FeatureSpec, needed: int, got: int) -> FeatureValue:
    return FeatureValue.absent(
        spec.name,
        FeatureStatus.INSUFFICIENT_HISTORY,
        f"needs {needed} within-session observations, {got} available",
    )


def _fetch_bars(window: BarWindow, returns_needed: int) -> tuple:
    """A generous slice: enough bars for *returns_needed* despite boundaries.

    Boundary-spanning returns are discarded, so a window must hold more bars
    than the returns it yields. Over-fetching and then trimming the *returns*
    is simpler than predicting how many boundaries a span contains, and it
    cannot silently come up short.
    """
    return window.rolling(returns_needed * 2 + 4)


def compute_rv_short(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Close-to-close realized volatility, annualised, boundary returns dropped."""
    needed = spec.params["returns"]
    returns = log_returns(_fetch_bars(window, needed))
    if len(returns) < needed:
        return _short(spec, needed, len(returns))
    value = stdev(returns[-needed:])
    if value is None:
        return _short(spec, needed, len(returns))
    return FeatureValue.valid(spec.name, value * annualization(spec.timeframe))


def compute_parkinson(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Parkinson high-low volatility estimator.

    Structurally immune to the boundary problem: every term is ``ln(high/low)``
    *within* one bar, so no term ever spans two sessions and none needs
    dropping.
    """
    needed = spec.params["bars"]
    bars = window.rolling(needed)
    usable = [b for b in bars if b.high > 0 and b.low > 0]
    if len(usable) < needed:
        return _short(spec, needed, len(usable))
    k = 1 / (4 * math.log(2))
    total = sum(math.log(b.high / b.low) ** 2 for b in usable)
    value = math.sqrt(k * total / len(usable))
    return FeatureValue.valid(spec.name, value * annualization(spec.timeframe))


def compute_atr_rel(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Average true range as a fraction of the latest close.

    True range normally compares a bar against the *previous* close. At a
    session start that close is yesterday's, so the comparison would measure
    the overnight gap; :func:`features.windows.true_ranges` drops those terms
    and uses the bar's own span instead.
    """
    needed = spec.params["bars"]
    bars = window.rolling(needed + 1)
    if len(bars) < needed:
        return _short(spec, needed, len(bars))
    ranges = true_ranges(bars)[-needed:]
    latest = bars[-1].close
    if latest <= 0:
        return _missing(spec, "non-positive close")
    return FeatureValue.valid(spec.name, (sum(ranges) / len(ranges)) / latest)


def compute_rv_baseline(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Realized volatility over the trailing baseline sessions.

    The reference level that makes "unusual" mean something. Sessions are
    selected by the dates **observed in the data**, never predicted from the
    holiday calendar, which covers 2026 only while most stored history predates
    it.

    Uses only completed prior sessions, so it is constant through a session --
    a reference level that moved intraday would confound the thing it is meant
    to normalise.
    """
    sessions = spec.trailing_sessions or 5
    observed = window.prior_session_dates(sessions)
    if len(observed) < sessions:
        return _short(spec, sessions, len(observed))
    returns = log_returns(window.trailing(sessions))
    value = stdev(returns)
    if value is None:
        return _short(spec, sessions, len(observed))
    return FeatureValue.valid(spec.name, value * annualization(spec.timeframe))


def compute_rv_regime(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Short-window volatility divided by its trailing baseline.

    1.0 is a normal session; 2.0 is twice as volatile as the trailing week.
    This is the feature the whole trailing-baseline design exists to support.
    """
    short = compute_rv_short(spec, window)
    if not short.is_valid:
        return FeatureValue.absent(spec.name, short.status, short.detail)
    baseline = compute_rv_baseline(spec, window)
    if not baseline.is_valid:
        return FeatureValue.absent(spec.name, baseline.status, baseline.detail)
    if baseline.value <= 0:
        return _missing(spec, "trailing baseline volatility is zero")
    return FeatureValue.valid(spec.name, short.value / baseline.value)


def compute_rv_accel(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Change in short-window volatility over the last *lag* bars.

    Both endpoints are individually boundary-clean. Across a session boundary
    the comparison shifts meaning -- this morning against yesterday's late
    session -- which is a change of interpretation, not of arithmetic.
    """
    needed = spec.params["returns"]
    lag = spec.params["lag"]
    returns = log_returns(_fetch_bars(window, needed + lag))
    if len(returns) < needed + lag:
        return _short(spec, needed + lag, len(returns))
    current = stdev(returns[-needed:])
    earlier = stdev(returns[-(needed + lag) : -lag])
    if current is None or earlier is None:
        return _short(spec, needed + lag, len(returns))
    if earlier <= 0:
        return _missing(spec, "earlier volatility is zero")
    return FeatureValue.valid(spec.name, current / earlier - 1.0)


def _register_for(timeframe: str) -> None:
    rv_bars = bars_for(timeframe, RV_WINDOW_MINUTES)
    atr_bars = bars_for(timeframe, ATR_WINDOW_MINUTES)
    lag_bars = bars_for(timeframe, RV_ACCEL_LAG_MINUTES)

    register(
        FeatureSpec(
            name="rv_short",
            family="volatility",
            timeframe=timeframe,
            lookback_bars=rv_bars + 1,
            scope=ROLLING,
            params={"returns": rv_bars, "minutes": RV_WINDOW_MINUTES},
            description=(
                f"Annualised close-to-close realized volatility over "
                f"{rv_bars} within-session returns ({RV_WINDOW_MINUTES} min)"
            ),
            fn=compute_rv_short,
        )
    )
    register(
        FeatureSpec(
            name="parkinson_short",
            family="volatility",
            timeframe=timeframe,
            lookback_bars=rv_bars,
            scope=ROLLING,
            params={"bars": rv_bars, "minutes": RV_WINDOW_MINUTES},
            description=(
                f"Annualised Parkinson high-low volatility over {rv_bars} bars"
            ),
            fn=compute_parkinson,
        )
    )
    register(
        FeatureSpec(
            name="atr_rel",
            family="volatility",
            timeframe=timeframe,
            lookback_bars=atr_bars + 1,
            scope=ROLLING,
            params={"bars": atr_bars, "minutes": ATR_WINDOW_MINUTES},
            description=(
                f"Average true range over {atr_bars} bars, as a fraction of close"
            ),
            fn=compute_atr_rel,
        )
    )
    register(
        FeatureSpec(
            name="rv_baseline",
            family="volatility",
            timeframe=timeframe,
            lookback_bars=rv_bars + 1,
            scope=TRAILING,
            description="Realized volatility over the trailing baseline sessions",
            fn=compute_rv_baseline,
        )
    )
    register(
        FeatureSpec(
            name="rv_regime",
            family="volatility",
            timeframe=timeframe,
            lookback_bars=rv_bars + 1,
            scope=TRAILING,
            params={"returns": rv_bars, "minutes": RV_WINDOW_MINUTES},
            description="Short-window volatility relative to its trailing baseline",
            fn=compute_rv_regime,
        )
    )
    register(
        FeatureSpec(
            name="rv_accel",
            family="volatility",
            timeframe=timeframe,
            lookback_bars=rv_bars + lag_bars + 1,
            scope=ROLLING,
            params={
                "returns": rv_bars,
                "lag": lag_bars,
                "minutes": RV_WINDOW_MINUTES,
                "lag_minutes": RV_ACCEL_LAG_MINUTES,
            },
            description=(
                f"Change in short-window volatility over the last {lag_bars} bars"
            ),
            fn=compute_rv_accel,
        )
    )


for _timeframe in ("1m", "5m"):
    _register_for(_timeframe)


__all__ = [
    "ATR_WINDOW_MINUTES",
    "RV_ACCEL_LAG_MINUTES",
    "RV_WINDOW_MINUTES",
    "TRADING_DAYS",
    "annualization",
    "bars_for",
    "compute_atr_rel",
    "compute_parkinson",
    "compute_rv_accel",
    "compute_rv_baseline",
    "compute_rv_regime",
    "compute_rv_short",
]
