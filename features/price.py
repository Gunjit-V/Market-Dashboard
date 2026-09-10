"""Price and return features.

Eleven features describing where price is and how it got there, all computed
from bars of the decision's **own session**.

Why intraday rather than rolling: every feature here is a point-to-point span
(``close[0]/close[-n]``) or a property of a single bar. A span that reached
back into yesterday would carry the overnight move *inside* a single ratio,
where it cannot be separated out -- unlike a volatility sum, which can simply
drop the offending term. The overnight move is not discarded; it is reported by
``overnight_gap`` in its own column, which is precisely what lets these columns
keep one meaning.

Horizons are named by elapsed time rather than bar count, so ``ret_15m`` means
the same thing on both timeframes (3 bars at 5m, 15 bars at 1m).
"""

from __future__ import annotations

from features.registry import register
from features.spec import INTRADAY, SESSION, FeatureSpec
from features.state import FeatureStatus, FeatureValue, insufficient_history
from features.windows import BarWindow, simple_return

#: Return horizons per timeframe, as (name, bars). Each timeframe starts at its
#: own bar size, so no two columns are the same quantity under two names.
RETURN_HORIZONS = {
    "5m": (("ret_5m", 1), ("ret_15m", 3), ("ret_30m", 6), ("ret_60m", 12)),
    "1m": (("ret_1m", 1), ("ret_5m", 5), ("ret_15m", 15), ("ret_30m", 30)),
}


def _missing(spec: FeatureSpec, detail: str) -> FeatureValue:
    return FeatureValue.absent(spec.name, FeatureStatus.MISSING, detail)


def compute_return(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """``close[0] / close[-n] - 1`` over the current session."""
    bars = window.intraday()
    n = spec.params["bars"]
    if len(bars) < n + 1:
        return insufficient_history(spec, len(bars))
    value = simple_return(bars[-1 - n].close, bars[-1].close)
    if value is None:
        return _missing(spec, "non-positive reference close")
    return FeatureValue.valid(spec.name, value)


def compute_acceleration(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Change in the one-bar return: is the move speeding up or fading?"""
    bars = window.intraday()
    if len(bars) < 3:
        return insufficient_history(spec, len(bars))
    latest = simple_return(bars[-2].close, bars[-1].close)
    previous = simple_return(bars[-3].close, bars[-2].close)
    if latest is None or previous is None:
        return _missing(spec, "non-positive reference close")
    return FeatureValue.valid(spec.name, latest - previous)


def compute_range_rel(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Bar span as a fraction of its close -- scale-free bar width.

    A flat bar gives 0.0, which is a genuine measurement (nothing moved), not
    an absence. Only a non-positive close makes this undefined.
    """
    bars = window.intraday()
    if not bars:
        return insufficient_history(spec, 0)
    bar = bars[-1]
    if bar.close <= 0:
        return _missing(spec, "non-positive close")
    return FeatureValue.valid(spec.name, (bar.high - bar.low) / bar.close)


def _bar_shape(spec: FeatureSpec, window: BarWindow, part: str) -> FeatureValue:
    """Candle geometry as a fraction of the bar's own span.

    All three shape features divide by ``high - low``, which is zero on a flat
    bar -- 31 of 52,011 Nifty 50 5-minute bars, and ~6.6% of option bars. There
    is no meaningful body-to-range ratio when there is no range, so the value is
    reported absent rather than defaulted to something plausible.
    """
    bars = window.intraday()
    if not bars:
        return insufficient_history(spec, 0)
    bar = bars[-1]
    span = bar.high - bar.low
    if span <= 0:
        return _missing(spec, "flat bar: high equals low")
    if part == "body":
        value = abs(bar.close - bar.open) / span
    elif part == "upper":
        value = (bar.high - max(bar.open, bar.close)) / span
    else:
        value = (min(bar.open, bar.close) - bar.low) / span
    return FeatureValue.valid(spec.name, value)


def compute_body_ratio(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Decisive bar versus indecisive chop."""
    return _bar_shape(spec, window, "body")


def compute_upper_wick(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Rejection from above."""
    return _bar_shape(spec, window, "upper")


def compute_lower_wick(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Rejection from below."""
    return _bar_shape(spec, window, "lower")


def _opening_bar_or_absence(
    spec: FeatureSpec, window: BarWindow
) -> tuple[object, FeatureValue | None]:
    """Resolve the session's opening bar, distinguishing two kinds of absence.

    Before the opening bar has *closed* -- at 09:15 on a 5-minute series --
    there is simply no history yet, which is INSUFFICIENT_HISTORY. Once other
    bars of the session exist but the opening one does not, the bar is genuinely
    absent from the data, which is MISSING. One Nifty 50 session in 697 begins
    at 11:15 and is the second case; every session is briefly the first.
    """
    opening = window.session_open_bar()
    if opening is not None:
        return opening, None
    bars = window.intraday()
    if not bars:
        return None, insufficient_history(spec, 0)
    return None, _missing(spec, "session opening bar absent from the data")


def compute_session_ret(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Return from the session's opening price.

    Requires the bar labelled at the session open. One Nifty 50 session in 697
    starts at 11:15; anchoring to that bar would produce a number that looks
    like a session return but measures part of an unknown day.
    """
    opening, absence = _opening_bar_or_absence(spec, window)
    if absence is not None:
        return absence
    bars = window.intraday()
    value = simple_return(opening.open, bars[-1].close)
    if value is None:
        return _missing(spec, "non-positive session open")
    return FeatureValue.valid(spec.name, value)


def compute_pos_in_range(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Where price sits in the session's range so far: 0 at the low, 1 at the high."""
    _, absence = _opening_bar_or_absence(spec, window)
    if absence is not None:
        return absence
    bars = window.intraday()
    low = min(b.low for b in bars)
    high = max(b.high for b in bars)
    span = high - low
    if span <= 0:
        return _missing(spec, "session range is zero")
    return FeatureValue.valid(spec.name, (bars[-1].close - low) / span)


def compute_overnight_gap(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Session open versus the previous session's close.

    The overnight move, isolated in its own column so that the intraday columns
    never carry an 18-hour move in a slot that otherwise holds minutes.

    Available once the *opening bar closes* -- 09:20 on a 5-minute series, not
    09:15. With bar data alone the opening print is not knowable before then.
    The previous session is whichever one most recently appears in the data,
    which may be up to four calendar days back over a long weekend.
    """
    opening, absence = _opening_bar_or_absence(spec, window)
    if absence is not None:
        return absence
    previous_close = window.previous_session_close()
    if previous_close is None:
        return insufficient_history(spec, len(window.prior_session_dates(1)))
    value = simple_return(previous_close, opening.open)
    if value is None:
        return _missing(spec, "non-positive previous close")
    return FeatureValue.valid(spec.name, value)


def _register_for(timeframe: str) -> None:
    for name, bars in RETURN_HORIZONS[timeframe]:
        register(
            FeatureSpec(
                name=name,
                family="price",
                timeframe=timeframe,
                lookback_bars=bars + 1,
                scope=INTRADAY,
                params={"bars": bars},
                description=f"Simple return over the last {bars} completed bar(s)",
                fn=compute_return,
            )
        )
    register(
        FeatureSpec(
            name="accel_1",
            family="price",
            timeframe=timeframe,
            lookback_bars=3,
            scope=INTRADAY,
            description="Change in the one-bar return (momentum acceleration)",
            fn=compute_acceleration,
        )
    )
    register(
        FeatureSpec(
            name="range_rel",
            family="price",
            timeframe=timeframe,
            lookback_bars=1,
            scope=INTRADAY,
            description="Bar high-low span as a fraction of its close",
            fn=compute_range_rel,
        )
    )
    for name, part, text in (
        ("body_ratio", "body", "Absolute body as a fraction of the bar's span"),
        ("upper_wick", "upper", "Upper wick as a fraction of the bar's span"),
        ("lower_wick", "lower", "Lower wick as a fraction of the bar's span"),
    ):
        register(
            FeatureSpec(
                name=name,
                family="price",
                timeframe=timeframe,
                lookback_bars=1,
                scope=INTRADAY,
                params={"part": part},
                description=text,
                fn={
                    "body": compute_body_ratio,
                    "upper": compute_upper_wick,
                    "lower": compute_lower_wick,
                }[part],
            )
        )
    register(
        FeatureSpec(
            name="session_ret",
            family="price",
            timeframe=timeframe,
            lookback_bars=1,
            scope=INTRADAY,
            description="Return from the session's opening price",
            fn=compute_session_ret,
        )
    )
    register(
        FeatureSpec(
            name="pos_in_range",
            family="price",
            timeframe=timeframe,
            lookback_bars=1,
            scope=INTRADAY,
            description="Position of close within the session range so far (0=low, 1=high)",
            fn=compute_pos_in_range,
        )
    )
    register(
        FeatureSpec(
            name="overnight_gap",
            family="price",
            timeframe=timeframe,
            lookback_bars=1,
            scope=SESSION,
            description="Session open versus the previous session's close",
            fn=compute_overnight_gap,
        )
    )


for _timeframe in ("1m", "5m"):
    _register_for(_timeframe)


__all__ = [
    "RETURN_HORIZONS",
    "compute_acceleration",
    "compute_body_ratio",
    "compute_lower_wick",
    "compute_overnight_gap",
    "compute_pos_in_range",
    "compute_range_rel",
    "compute_return",
    "compute_session_ret",
    "compute_upper_wick",
]
