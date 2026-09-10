"""Forward-looking labels: what a model would be asked to predict.

A label is **not** part of a :class:`~features.state.MarketState`. The state
answers "what was knowable at T"; a label is by construction what happened
*after* T, and mixing the two is how a dataset ends up training on its own
answer. They are computed separately, from bars strictly after the decision
bar, and stored in their own columns.

Rule 5 of ``docs/point-in-time-data.md`` is what the separation enforces: the
label window starts after the last bar any feature touched, so no single bar is
both an input and part of the answer.

**Labels never cross a session boundary.** A 12-bar forward return at 15:20
would otherwise reach into the next morning and be dominated by the overnight
gap -- the same 58x-variance term the volatility features exclude, except here
it cannot be dropped, because a forward return is a single point-to-point
ratio. The last *h* bars of each session therefore carry no label, which costs
roughly 16% of rows at the longest horizon and keeps the remainder meaning one
thing.
"""

from __future__ import annotations

from typing import Sequence

from features.registry import register
from features.spec import INTRADAY, MINUTES_PER_BAR, FeatureSpec
from features.state import FeatureStatus, FeatureValue
from features.volatility import annualization
from features.windows import log_returns, simple_return, span_minutes, stdev
from marketdata.access import Bar

#: Prediction horizons per timeframe, as (suffix, bars). Named by elapsed time
#: so a horizon means the same span on both timeframes.
LABEL_HORIZONS = {
    "5m": (("15m", 3), ("30m", 6), ("60m", 12)),
    "1m": (("5m", 5), ("15m", 15), ("30m", 30)),
}

#: Label kinds. ``dir`` is deliberately three-valued -- see
#: :func:`compute_forward_direction`.
LABEL_KINDS = ("ret", "dir", "rv")


def _unavailable(spec: FeatureSpec, detail: str) -> FeatureValue:
    return FeatureValue.absent(spec.name, FeatureStatus.MISSING, detail)


def _forward_slice(
    forward: Sequence[Bar],
    horizon: int,
) -> tuple[Bar, ...] | None:
    """The next *horizon* bars, or ``None`` if the session ends first."""
    if len(forward) < horizon:
        return None
    return tuple(forward[:horizon])


def _stretched(
    spec: FeatureSpec,
    current: Bar,
    window: Sequence[Bar],
) -> FeatureValue | None:
    """Reject a horizon that a missing bar has stretched past its name.

    On 2026-09-10 -- a Thursday expiry -- the 15:15 five-minute bar is absent,
    so "three bars after 15:00" reaches 15:20. The resulting number is a
    twenty-minute return sitting in a column named ``fwd_ret_15m``. Since the
    label is what a model is asked to predict, a horizon that does not mean
    what it says would be learned as though it did.
    """
    horizon = spec.params["bars"]
    bar_minutes = MINUTES_PER_BAR[spec.timeframe]
    expected = horizon * bar_minutes
    actual = span_minutes(current, window[-1])
    if actual != expected:
        return _unavailable(
            spec,
            f"gap stretched the horizon to {actual:.0f} min, expected {expected}",
        )
    return None


def compute_forward_return(
    spec: FeatureSpec,
    current: Bar,
    forward: Sequence[Bar],
) -> FeatureValue:
    """``close[t+h] / close[t] - 1`` -- how far price travels next."""
    horizon = spec.params["bars"]
    window = _forward_slice(forward, horizon)
    if window is None:
        return _unavailable(
            spec, f"only {len(forward)} bars remain in the session, need {horizon}"
        )
    stretched = _stretched(spec, current, window)
    if stretched is not None:
        return stretched
    value = simple_return(current.close, window[-1].close)
    if value is None:
        return _unavailable(spec, "non-positive close at the decision bar")
    return FeatureValue.valid(spec.name, value)


def compute_forward_direction(
    spec: FeatureSpec,
    current: Bar,
    forward: Sequence[Bar],
) -> FeatureValue:
    """The sign of the forward return: -1, 0 or +1.

    Three-valued on purpose. 0.181% of Nifty 50 5-minute bars close exactly
    unchanged, and folding those into "up" or "down" would invent a direction
    the market did not take. Whether to merge the zero class is a modelling
    decision for a later phase, and discarding the distinction now would make
    it unrecoverable.
    """
    forward_return = compute_forward_return(spec, current, forward)
    if not forward_return.is_valid:
        return FeatureValue.absent(
            spec.name, forward_return.status, forward_return.detail
        )
    value = forward_return.value
    return FeatureValue.valid(spec.name, float((value > 0) - (value < 0)))


def compute_forward_rv(
    spec: FeatureSpec,
    current: Bar,
    forward: Sequence[Bar],
) -> FeatureValue:
    """Annualised realized volatility over the next *h* bars.

    Pairs naturally with the existing ``vrp_reversion`` strategy, which trades
    the gap between implied and realized volatility.
    """
    horizon = spec.params["bars"]
    window = _forward_slice(forward, horizon)
    if window is None:
        return _unavailable(
            spec, f"only {len(forward)} bars remain in the session, need {horizon}"
        )
    stretched = _stretched(spec, current, window)
    if stretched is not None:
        return stretched
    returns = log_returns((current,) + window)
    value = stdev(returns)
    if value is None:
        return _unavailable(spec, f"needs 2 forward returns, {len(returns)} available")
    return FeatureValue.valid(spec.name, value * annualization(spec.timeframe))


_COMPUTE = {
    "ret": compute_forward_return,
    "dir": compute_forward_direction,
    "rv": compute_forward_rv,
}

_DESCRIPTION = {
    "ret": "Forward simple return over the next {label}",
    "dir": "Sign of the forward return over the next {label} (-1, 0 or +1)",
    "rv": "Annualised realized volatility over the next {label}",
}


def _register_for(timeframe: str) -> None:
    for label, bars in LABEL_HORIZONS[timeframe]:
        for kind in LABEL_KINDS:
            register(
                FeatureSpec(
                    name=f"fwd_{kind}_{label}",
                    family="label",
                    timeframe=timeframe,
                    lookback_bars=1,
                    scope=INTRADAY,
                    params={"bars": bars, "horizon": label, "kind": kind},
                    description=_DESCRIPTION[kind].format(label=label),
                    fn=_COMPUTE[kind],
                )
            )


for _timeframe in ("1m", "5m"):
    _register_for(_timeframe)


__all__ = [
    "LABEL_HORIZONS",
    "LABEL_KINDS",
    "compute_forward_direction",
    "compute_forward_return",
    "compute_forward_rv",
]
