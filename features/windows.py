"""Slicing completed bars into the windows features actually read.

Every feature computation receives a :class:`BarWindow`: the completed,
in-session bars available at one decision time, plus the operations needed to
carve them up correctly.

Three carving rules, each with a reason:

**Intraday** -- bars from the current session only. Point-to-point features
(``close[0]/close[-12]``) must use this, because an overnight move sitting
inside a single ratio cannot be separated from it.

**Rolling** -- the most recent *n* bars regardless of session, with any term
spanning a session boundary dropped. Path-dependent aggregates (volatility) may
use this: a sum over per-bar terms survives losing one term. On Nifty 50
5-minute bars a boundary log return carries 58x the variance of an intraday
one, so keeping it would overstate realized volatility 2.0x every morning.

**Trailing** -- whole prior sessions, selected by the dates actually **observed
in the data** rather than predicted from a calendar. ``scheduler/nse_calendar``
ships holidays for 2026 only, while ~75% of stored history predates that year;
asking the calendar for "the last five trading days" in 2024 would name dates
that were holidays, find no bars, and silently shorten the baseline.

Nothing here reads the database. A window is built from bars the access layer
already returned for ``as_of=decision_time``, so point-in-time safety is
inherited rather than re-implemented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Sequence

from marketdata.access import Bar
from marketdata.sessions import DEFAULT_SESSION_CLOSE, DEFAULT_SESSION_OPEN

#: Calendar days to fetch per trailing session requested. Sessions are
#: separated by up to 4 calendar days in the observed data (long weekends), so
#: a factor of 3 plus a fixed cushion comfortably spans a holiday cluster. Over-
#: fetching costs a little I/O; under-fetching would silently shorten a
#: baseline, which is the error worth avoiding.
CALENDAR_DAYS_PER_SESSION = 3
CALENDAR_DAY_CUSHION = 10


def fetch_days_for(trailing_sessions: int) -> int:
    """Calendar days to read back to be sure of finding *trailing_sessions*."""
    return trailing_sessions * CALENDAR_DAYS_PER_SESSION + CALENDAR_DAY_CUSHION


def in_session(
    moment: datetime,
    open_time: time = DEFAULT_SESSION_OPEN,
    close_time: time = DEFAULT_SESSION_CLOSE,
) -> bool:
    """Whether a bar label falls inside the regular session, close exclusive.

    Bars outside 09:15-15:30 exist in the tables -- a handful of pre-open bars
    as early as 09:05, and rows at or after the close. They are excluded from
    every feature: a "session open" derived from the first observed bar would
    otherwise occasionally be a 09:10 pre-open print.
    """
    return open_time <= moment.time() < close_time


@dataclass(frozen=True)
class BarWindow:
    """Completed, in-session bars available at one decision time.

    Bars are ascending by timestamp and every one of them had already closed at
    ``decision_time`` -- that is the access layer's guarantee, not something
    re-checked here.
    """

    bars: tuple[Bar, ...]
    decision_time: datetime
    timeframe: str

    @classmethod
    def build(
        cls,
        bars: Iterable[Bar],
        decision_time: datetime,
        timeframe: str,
    ) -> BarWindow:
        """Filter to in-session bars and order them deterministically."""
        kept = tuple(
            sorted(
                (b for b in bars if in_session(b.timestamp)),
                key=lambda b: b.timestamp,
            )
        )
        return cls(bars=kept, decision_time=decision_time, timeframe=timeframe)

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def session_date(self) -> date:
        """The session the decision belongs to."""
        return self.decision_time.date()

    @property
    def session_dates(self) -> tuple[date, ...]:
        """Every session date present, ascending. Observed, not predicted."""
        seen: list[date] = []
        for bar in self.bars:
            day = bar.timestamp.date()
            if not seen or seen[-1] != day:
                seen.append(day)
        return tuple(seen)

    # -- intraday -----------------------------------------------------------

    def intraday(self) -> tuple[Bar, ...]:
        """Bars from the decision's own session."""
        today = self.session_date
        return tuple(b for b in self.bars if b.timestamp.date() == today)

    def session_open_bar(self) -> Bar | None:
        """The session's opening bar, or ``None`` if it is absent.

        Requires the bar labelled at the session open itself. One Nifty 50
        session in 697 begins at 11:15 rather than 09:15, and anchoring
        ``session_ret`` to that bar would report a number that looks like a
        session return but measures four hours of an unknown day. Absence is
        reported instead.
        """
        for bar in self.intraday():
            if bar.timestamp.time() == DEFAULT_SESSION_OPEN:
                return bar
        return None

    # -- rolling ------------------------------------------------------------

    def rolling(self, n: int) -> tuple[Bar, ...]:
        """The most recent *n* completed bars, session boundaries ignored."""
        if n <= 0:
            return ()
        return self.bars[-n:]

    # -- trailing -----------------------------------------------------------

    def prior_session_dates(self, n: int) -> tuple[date, ...]:
        """The last *n* session dates strictly before the decision's own.

        Selected from dates observed in the data. Fewer than *n* may come back,
        and the caller is expected to treat that as insufficient history rather
        than quietly proceeding with a shorter baseline.
        """
        today = self.session_date
        earlier = [d for d in self.session_dates if d < today]
        return tuple(earlier[-n:]) if n > 0 else ()

    def trailing(self, n: int) -> tuple[Bar, ...]:
        """All bars from the last *n* observed sessions before this one."""
        wanted = set(self.prior_session_dates(n))
        if not wanted:
            return ()
        return tuple(b for b in self.bars if b.timestamp.date() in wanted)

    def previous_session_close(self) -> float | None:
        """Close of the last bar of the most recent prior session."""
        prior = self.prior_session_dates(1)
        if not prior:
            return None
        day = prior[0]
        closes = [b.close for b in self.bars if b.timestamp.date() == day]
        return closes[-1] if closes else None


def crosses_session(earlier: Bar, later: Bar) -> bool:
    """Whether a term spanning these two bars crosses a session boundary."""
    return earlier.timestamp.date() != later.timestamp.date()


def log_returns(
    bars: Sequence[Bar],
    exclude_boundary: bool = True,
) -> list[float]:
    """Bar-to-bar log returns.

    With ``exclude_boundary`` set -- the default, and the only correct setting
    for a volatility estimate -- returns that span a session boundary are
    dropped rather than included. See this module's docstring for why.

    Non-positive closes are skipped: a logarithm of them is undefined, and the
    contract already forbids them, so their presence would be a data defect
    rather than a case to interpolate around.
    """
    out: list[float] = []
    for earlier, later in zip(bars, bars[1:]):
        if exclude_boundary and crosses_session(earlier, later):
            continue
        if earlier.close <= 0 or later.close <= 0:
            continue
        out.append(math.log(later.close / earlier.close))
    return out


def span_minutes(earlier: Bar, later: Bar) -> float:
    """Wall-clock minutes between two bar labels."""
    return (later.timestamp - earlier.timestamp).total_seconds() / 60.0


def spans_expected(earlier: Bar, later: Bar, bars: int, bar_minutes: int) -> bool:
    """Whether *bars* steps really cover *bars * bar_minutes* of clock time.

    A missing in-session bar silently stretches a span: on 2026-09-10 the
    15:15 five-minute bar is absent, so "three bars after 15:00" lands on
    15:20 -- twenty minutes later, not fifteen. A point-to-point return is
    determined entirely by its two endpoints, so that stretch is not a small
    bias but a different measurement wearing the same column name.
    """
    return span_minutes(earlier, later) == bars * bar_minutes


def simple_return(earlier: float, later: float) -> float | None:
    """``later/earlier - 1``, or ``None`` when it is undefined."""
    if earlier <= 0:
        return None
    return later / earlier - 1.0


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """True range per bar, with the boundary term dropped at a session start.

    True range normally maxes the bar's own span against two terms involving
    the *previous* close. At a session boundary that previous close is
    yesterday's, so those terms would measure the overnight gap. The first bar
    of a session therefore contributes its own high-low span only.
    """
    out: list[float] = []
    for index, bar in enumerate(bars):
        span = bar.high - bar.low
        if index == 0 or crosses_session(bars[index - 1], bar):
            out.append(span)
            continue
        previous_close = bars[index - 1].close
        out.append(
            max(
                span,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return out


def stdev(values: Sequence[float]) -> float | None:
    """Population standard deviation, or ``None`` for fewer than two values."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def session_span(bars: Sequence[Bar]) -> tuple[float, float] | None:
    """``(low, high)`` across *bars*, or ``None`` when empty."""
    if not bars:
        return None
    return min(b.low for b in bars), max(b.high for b in bars)


def window_start_for(
    decision_time: datetime,
    trailing_sessions: int,
    extra_days: int = 0,
) -> datetime:
    """The read start that reliably covers *trailing_sessions* prior sessions."""
    days = fetch_days_for(trailing_sessions) + extra_days
    return datetime.combine(decision_time.date() - timedelta(days=days), time(0, 0))


__all__ = [
    "BarWindow",
    "CALENDAR_DAYS_PER_SESSION",
    "CALENDAR_DAY_CUSHION",
    "crosses_session",
    "fetch_days_for",
    "in_session",
    "log_returns",
    "session_span",
    "simple_return",
    "span_minutes",
    "spans_expected",
    "stdev",
    "true_ranges",
    "window_start_for",
]
