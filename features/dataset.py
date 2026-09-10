"""Generating historical feature datasets, session by session.

The naive builder calls :func:`~features.engine.build_market_state` once per
decision time. Measured against the live database that costs ~89 ms each --
77 minutes for one 5-minute instrument-history, and roughly six hours at 1m --
because almost all of it is a database round trip fetching bars that the
previous decision time had already read.

So this module reads **once per session** and slides the decision time through
the bars in memory. Around 700 queries replace 52,000.

That optimisation is only safe if it changes nothing. The rows it produces must
be identical to what the one-at-a-time path returns, and
``tests/test_feature_dataset.py`` asserts exactly that -- otherwise the fast
path could drift from the live path, and a model trained on this dataset would
be served by different arithmetic than it learned from, in the least visible
way possible.

Decision times come from the **observed** session dates, not from a predicted
calendar: ``scheduler/nse_calendar`` ships holidays for 2026 alone while most
stored history predates it.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterator, Sequence

from features.engine import (
    build_state_from_bars,
    compute_labels,
    state_row,
    trailing_sessions_needed,
)
from features.quality import SourceQuality, window_quality
from features.registry import label_set, state_features
from features.spec import MINUTES_PER_BAR, FeatureSet
from features.state import FeatureStatus, FeatureValue
from features.windows import fetch_days_for, in_session
from marketdata.access import Bar, get_market_data
from marketdata.sessions import DEFAULT_SESSION_CLOSE, DEFAULT_SESSION_OPEN
from marketdata.validation import validate_ohlcv

#: Dataset name per timeframe, matching ``marketdata.validation``'s vocabulary.
_DATASET = {"1m": "ohlcv_1m", "5m": "ohlcv_5m"}


def session_decision_times(day: date, timeframe: str) -> list[datetime]:
    """Every decision time in one session, on the grid anchored at 09:15.

    The session open is included: at 09:15 nothing has closed today, but the
    rolling volatility features carry yesterday's history and are already
    valid, so the row is informative rather than empty.
    """
    step = MINUTES_PER_BAR[timeframe]
    moment = datetime.combine(day, DEFAULT_SESSION_OPEN)
    close = datetime.combine(day, DEFAULT_SESSION_CLOSE)
    out: list[datetime] = []
    while moment < close:
        out.append(moment)
        moment += timedelta(minutes=step)
    return out


def observed_sessions(
    conn,
    instrument: str,
    start: date,
    end: date,
    timeframe: str,
) -> list[date]:
    """Session dates that actually carry in-session bars, ascending."""
    table = {"1m": "ohlcv_1min", "5m": "ohlcv_5min"}[timeframe]
    sql = f"""
        SELECT DISTINCT o.timestamp::date
        FROM {table} o
        JOIN instruments i ON i.id = o.instrument_id
        WHERE UPPER(i.symbol) = UPPER(%s)
          AND o.timestamp >= %s AND o.timestamp < %s
          AND o.timestamp::time >= %s AND o.timestamp::time < %s
        ORDER BY 1
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                instrument,
                datetime.combine(start, time(0, 0)),
                datetime.combine(end + timedelta(days=1), time(0, 0)),
                DEFAULT_SESSION_OPEN,
                DEFAULT_SESSION_CLOSE,
            ),
        )
        return [row[0] for row in cur.fetchall()]


@dataclass
class BuildStats:
    """What a build actually produced, for the manifest."""

    rows: int = 0
    sessions: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    label_status_counts: dict[str, int] = field(default_factory=dict)
    source_quality: list[SourceQuality] = field(default_factory=list)
    first_decision: datetime | None = None
    last_decision: datetime | None = None

    def record(self, values, counter: dict[str, int]) -> None:
        for value in values:
            key = f"{value.name}:{value.status.value}"
            counter[key] = counter.get(key, 0) + 1


def _visible_at(
    bars: Sequence[Bar],
    close_times: Sequence[datetime],
    moment: datetime,
) -> Sequence[Bar]:
    """Bars that had closed at *moment*.

    ``close_times`` is the precomputed, ascending list of ``bar_start +
    width``, so the cut is a binary search rather than a scan -- this runs once
    per decision time, 52,000 times per instrument-history.

    This mirrors the access layer's ``as_of`` SQL exactly (a bar is visible
    once ``timestamp + bar_minutes <= as_of``), which is what lets the fast
    path stay bit-identical to the one-at-a-time path.
    """
    return bars[: bisect_right(close_times, moment)]


def build_session_rows(
    day: date,
    history: Sequence[Bar],
    instrument: str,
    fs: FeatureSet,
    labels: FeatureSet,
    stats: BuildStats | None = None,
) -> Iterator[dict[str, Any]]:
    """Rows for every decision time in one session.

    ``history`` must already span the trailing baseline and end no later than
    the session's close; it is read repeatedly in memory rather than re-fetched.
    """
    timeframe = fs.timeframe
    width = MINUTES_PER_BAR[timeframe]
    bars = sorted(
        (b for b in history if in_session(b.timestamp)), key=lambda b: b.timestamp
    )
    close_times = [b.timestamp + timedelta(minutes=width) for b in bars]
    today = [b for b in bars if b.timestamp.date() == day]

    for moment in session_decision_times(day, timeframe):
        visible = _visible_at(bars, close_times, moment)
        state = build_state_from_bars(instrument, moment, visible, fs)

        # Labels read forward from the last bar knowable at this decision,
        # within this session only. At the session open no bar has closed yet,
        # so there is nothing to measure forward *from* -- but the row must
        # still carry every label column. A ragged row would give consumers a
        # KeyError and would leave the absence unexplained.
        knowable_today = [
            b for b in today if b.timestamp + timedelta(minutes=width) <= moment
        ]
        if knowable_today:
            decision_bar = knowable_today[-1]
            forward = [b for b in today if b.timestamp > decision_bar.timestamp]
            label_values = compute_labels(decision_bar, forward, labels)
        else:
            label_values = tuple(
                FeatureValue.absent(
                    spec.name,
                    FeatureStatus.INSUFFICIENT_HISTORY,
                    "no bar has closed yet in this session",
                )
                for spec in labels
            )

        if stats is not None:
            stats.rows += 1
            stats.record(state, stats.status_counts)
            stats.record(label_values, stats.label_status_counts)
            if stats.first_decision is None:
                stats.first_decision = moment
            stats.last_decision = moment

        yield state_row(state, label_values)


def build_dataset(
    conn,
    instrument: str,
    start: date,
    end: date,
    timeframe: str = "5m",
    fs: FeatureSet | None = None,
    labels: FeatureSet | None = None,
    stats: BuildStats | None = None,
    validate: bool = True,
    progress=None,
) -> Iterator[dict[str, Any]]:
    """Rows for every decision time across every observed session in range.

    One database read per session. Each session's bars are validated once and
    the verdict recorded in *stats*, which is the window-level policy chosen
    for Phase 2: validating per decision time would repeat the same work 75
    times for the same answer.
    """
    fs = fs or state_features(timeframe)
    labels = labels or label_set(timeframe)
    stats = stats if stats is not None else BuildStats()
    lookback_days = fetch_days_for(trailing_sessions_needed(fs))

    for day in observed_sessions(conn, instrument, start, end, timeframe):
        window_start = datetime.combine(day - timedelta(days=lookback_days), time(0, 0))
        window_end = datetime.combine(day, DEFAULT_SESSION_CLOSE)
        history = get_market_data(
            conn, instrument, window_start, window_end, timeframe
        )

        if validate:
            todays = [
                b.as_dict()
                for b in history
                if b.timestamp.date() == day and in_session(b.timestamp)
            ]
            result = validate_ohlcv(todays, timeframe=timeframe)
            stats.source_quality.append(
                window_quality(
                    result,
                    instrument,
                    datetime.combine(day, DEFAULT_SESSION_OPEN),
                    window_end,
                    todays,
                )
            )

        stats.sessions += 1
        if progress is not None:
            progress(day, stats)

        yield from build_session_rows(day, history, instrument, fs, labels, stats)


__all__ = [
    "BuildStats",
    "build_dataset",
    "build_session_rows",
    "observed_sessions",
    "session_decision_times",
]
