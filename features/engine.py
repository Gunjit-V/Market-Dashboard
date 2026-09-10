"""Building a :class:`~features.state.MarketState` at a decision time.

The entry point Phase 2 exists to provide:

    build_market_state(conn, "Nifty 50", decision_time, "5m")

Every feature reads through :func:`marketdata.access.get_market_data` with
``as_of=decision_time``, so the point-in-time cut-off lives in SQL and a bar
that had not closed cannot reach a computation. Look-ahead is prevented
structurally rather than reviewed for.

Two entry points, deliberately:

* :func:`build_market_state` fetches and computes -- the ordinary path.
* :func:`build_state_from_bars` computes from bars already in hand -- pure,
  database-free, and what the leakage tests drive.

Both produce identical states from identical inputs, which is what keeps the
historical and live paths in agreement.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Iterable, Sequence

from marketdata.access import Bar, get_market_data
from marketdata.sessions import DEFAULT_SESSION_CLOSE, DEFAULT_SESSION_OPEN

from features.quality import SourceQuality
from features.registry import feature_set_version, label_set, state_features
from features.spec import TRAILING, FeatureSet, FeatureSpec
from features.state import (
    FeatureStatus,
    FeatureValue,
    MarketState,
    empty_state,
    summarize_quality,
)
from features.windows import BarWindow, window_start_for

#: Prior sessions the read window must cover even when no trailing feature asks
#: for more -- ``overnight_gap`` needs the previous session's close.
MIN_PRIOR_SESSIONS = 1


def trailing_sessions_needed(fs: FeatureSet) -> int:
    """The longest trailing baseline in *fs*, floored at one prior session."""
    requested = [
        spec.trailing_sessions or 0 for spec in fs if spec.scope == TRAILING
    ]
    return max([*requested, MIN_PRIOR_SESSIONS])


def in_trading_window(
    moment: datetime,
    open_time: time = DEFAULT_SESSION_OPEN,
    close_time: time = DEFAULT_SESSION_CLOSE,
) -> bool:
    """Whether *moment* falls in the regular session, close exclusive.

    Time-of-day only. Whether the *date* was a trading day is left to the data:
    ``scheduler/nse_calendar`` carries holidays for 2026 alone, while most
    stored history predates that year, so asking it would report confident
    nonsense for 2024. A genuine holiday surfaces as absent history instead,
    which is both true and derived from what was actually observed.
    """
    return open_time <= moment.time() < close_time


def _evaluate(spec: FeatureSpec, window: BarWindow) -> FeatureValue:
    """Run one spec, converting a computation failure into an explicit status.

    A feature that raises must not take the whole state with it: the remaining
    features are still valid, and the failure is more useful recorded against
    the column that produced it than as a traceback that loses the other
    seventeen.
    """
    if spec.fn is None:
        return FeatureValue.absent(
            spec.name, FeatureStatus.MISSING, "no computation registered"
        )
    try:
        return spec.fn(spec, window)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        return FeatureValue.absent(
            spec.name,
            FeatureStatus.INVALID_SOURCE,
            f"{type(exc).__name__}: {exc}",
        )


def build_state_from_bars(
    instrument: str,
    decision_time: datetime,
    bars: Iterable[Bar],
    fs: FeatureSet,
    source_quality: SourceQuality | None = None,
) -> MarketState:
    """Compute a state from bars already fetched. Pure; touches no database.

    ``bars`` must contain only bars that had **closed** at ``decision_time``.
    That is the caller's guarantee -- ordinarily the access layer's ``as_of``
    filter -- and this function does not re-derive it.
    """
    version = feature_set_version(fs)

    if not in_trading_window(decision_time):
        return empty_state(
            instrument,
            decision_time,
            fs,
            FeatureStatus.MARKET_CLOSED,
            "decision time is outside the regular session",
            feature_set_version=version,
        )

    window = BarWindow.build(bars, decision_time, fs.timeframe)
    values = tuple(_evaluate(spec, window) for spec in fs)

    return MarketState(
        instrument=instrument,
        decision_time=decision_time,
        timeframe=fs.timeframe,
        values=values,
        feature_set_version=version,
        quality=summarize_quality(
            values,
            source_validation=source_quality.as_dict() if source_quality else None,
        ),
    )


def build_market_state(
    conn,
    instrument: str,
    decision_time: datetime,
    timeframe: str = "5m",
    fs: FeatureSet | None = None,
    source_quality: SourceQuality | None = None,
) -> MarketState:
    """Read history up to *decision_time* and compute the state there.

    The read window reaches back far enough to cover the longest trailing
    baseline in *fs*, measured generously in calendar days: sessions are
    separated by up to four days over a long weekend, and under-fetching would
    silently shorten a baseline rather than fail visibly.
    """
    fs = fs or state_features(timeframe)
    start = window_start_for(decision_time, trailing_sessions_needed(fs))
    bars = get_market_data(
        conn,
        instrument,
        start,
        decision_time,
        timeframe,
        as_of=decision_time,
    )
    return build_state_from_bars(instrument, decision_time, bars, fs, source_quality)


def compute_labels(
    decision_bar: Bar,
    forward: Sequence[Bar],
    labels: FeatureSet,
) -> tuple[FeatureValue, ...]:
    """Evaluate forward-looking labels for one decision.

    ``forward`` are the bars **after** ``decision_bar``, within the same
    session. Labels are computed separately from the state and never enter it:
    they are what happened next, not what was knowable.
    """
    out: list[FeatureValue] = []
    for spec in labels:
        if spec.fn is None:
            out.append(
                FeatureValue.absent(
                    spec.name, FeatureStatus.MISSING, "no computation registered"
                )
            )
            continue
        try:
            out.append(spec.fn(spec, decision_bar, forward))
        except Exception as exc:  # noqa: BLE001 - see _evaluate
            out.append(
                FeatureValue.absent(
                    spec.name,
                    FeatureStatus.INVALID_SOURCE,
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return tuple(out)


def labels_for(timeframe: str) -> FeatureSet:
    """The label set for *timeframe*."""
    return label_set(timeframe)


def state_row(
    state: MarketState,
    labels: Sequence[FeatureValue] = (),
) -> dict[str, Any]:
    """One flat dataset row: identity, features, then labels.

    Labels carry their own ``__status`` columns too, so "the market did not
    move" stays distinguishable from "the session ended before the horizon".
    """
    row = state.to_row()
    for value in labels:
        row[value.name] = value.value
        row[f"{value.name}__status"] = value.status.value
    return row


__all__ = [
    "MIN_PRIOR_SESSIONS",
    "build_market_state",
    "build_state_from_bars",
    "compute_labels",
    "in_trading_window",
    "labels_for",
    "state_row",
    "trailing_sessions_needed",
]
