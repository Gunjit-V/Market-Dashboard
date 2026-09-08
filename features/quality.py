"""Source-data quality, carried from Phase 1 validation into feature states.

Phase 1 already validates market data; this module is the bridge, not a second
validator. It converts a :class:`~marketdata.validation.ValidationResult` for
an extraction window into a compact :class:`SourceQuality` that every
:class:`~features.state.MarketState` built from that window can carry.

Where validation runs
---------------------
Once per **extraction window**, not once per state. Building 52,000 states for
a single instrument-year would otherwise mean 52,000 validation passes over
largely the same bars -- the same answer, recomputed. A window is validated
when it is read, and its verdict is attached to every state built from it and
recorded in the dataset manifest.

The cost of that choice is precision: the verdict describes the window, so a
state cannot say "the specific bars *I* used were clean" unless the whole
window was. :func:`window_quality` therefore also carries the timestamps of the
invalid bars, which lets a caller ask the sharper question when it matters --
see :func:`affects_decision_time`.

What a bad window does *not* do
-------------------------------
It does not silently suppress features. A window carrying ERROR-severity issues
still produces states; those states are *labelled*, and the labelling reaches
the dataset manifest so the problem is visible before training rather than
discovered afterwards. Deciding what to do about it is the researcher's call,
which is the same stance Phase 1 takes on ingestion: detect and report, never
silently repair.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from marketdata.validation import Severity, ValidationResult

# Verdicts, matching marketdata.quality's vocabulary so one word means one
# thing across the project.
PASS = "PASS"
WARNING = "WARNING"
FAIL = "FAIL"


@dataclass(frozen=True)
class SourceQuality:
    """The quality of the source data behind one extraction window.

    Attributes
    ----------
    dataset
        Which dataset was validated (``"ohlcv_1m"``, ``"ohlcv_5m"``).
    instrument
        The symbol the window was read for.
    start, end
        The window bounds, half-open ``[start, end)`` as everywhere else.
    verdict
        :data:`PASS`, :data:`WARNING` or :data:`FAIL`.
    record_count
        Bars read.
    error_count, warning_count
        Issue counts by severity.
    invalid_timestamps
        Bars carrying at least one ERROR-level issue. Kept so a caller can ask
        whether a *particular* decision time was affected rather than
        discarding a whole window over one bad bar.
    missing_timestamps
        In-session bars the session model expected but that are absent. These
        are warnings, never errors: an absent bar in an illiquid option means
        nobody traded, which is information rather than damage.
    issue_counts
        Issue code to count, for a reader who wants the detail.
    """

    dataset: str
    instrument: str
    start: datetime
    end: datetime
    verdict: str
    record_count: int
    error_count: int = 0
    warning_count: int = 0
    invalid_timestamps: tuple[datetime, ...] = ()
    missing_timestamps: tuple[datetime, ...] = ()
    issue_counts: Mapping[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.issue_counts is None:
            object.__setattr__(self, "issue_counts", {})
        else:
            object.__setattr__(self, "issue_counts", dict(self.issue_counts))

    @property
    def ok(self) -> bool:
        """True when nothing at ERROR severity was found in the window."""
        return self.error_count == 0

    @property
    def usable(self) -> bool:
        """True when features built from this window are trustworthy.

        Warnings do not make a window unusable. Gaps and out-of-session records
        are reported at WARNING precisely because they are usually facts about
        the market rather than defects in the data.
        """
        return self.verdict != FAIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "instrument": self.instrument,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "verdict": self.verdict,
            "record_count": self.record_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "invalid_timestamps": [t.isoformat() for t in self.invalid_timestamps],
            "missing_timestamps": [t.isoformat() for t in self.missing_timestamps],
            "issue_counts": dict(sorted(self.issue_counts.items())),
        }


def verdict_for(result: ValidationResult) -> str:
    """The verdict for one validation result.

    FAIL on any ERROR, WARNING on any warning, PASS otherwise -- the same
    escalation ``marketdata.quality`` uses, so a verdict means the same thing
    whether it came from the quality report or from here.
    """
    if result.errors:
        return FAIL
    if result.warnings:
        return WARNING
    return PASS


def window_quality(
    result: ValidationResult,
    instrument: str,
    start: datetime,
    end: datetime,
    records: Sequence[Mapping[str, Any]] = (),
) -> SourceQuality:
    """Summarise one window's validation result.

    ``records`` is the batch that was validated, used only to recover the
    timestamps of the bars flagged by ``result.invalid_indices``. Passing it is
    optional; without it the summary still reports counts, just not which bars.
    """
    invalid: list[datetime] = []
    for index in sorted(result.invalid_indices):
        if 0 <= index < len(records):
            timestamp = records[index].get("timestamp")
            if isinstance(timestamp, datetime):
                invalid.append(timestamp)

    return SourceQuality(
        dataset=result.dataset,
        instrument=instrument,
        start=start,
        end=end,
        verdict=verdict_for(result),
        record_count=result.record_count,
        error_count=len(result.errors),
        warning_count=len(result.warnings),
        invalid_timestamps=tuple(invalid),
        missing_timestamps=tuple(result.missing_timestamps),
        issue_counts=result.by_code(),
    )


def clean_window(
    dataset: str,
    instrument: str,
    start: datetime,
    end: datetime,
    record_count: int = 0,
) -> SourceQuality:
    """A PASS summary, for a window validated with nothing to report."""
    return SourceQuality(
        dataset=dataset,
        instrument=instrument,
        start=start,
        end=end,
        verdict=PASS,
        record_count=record_count,
    )


def affects_decision_time(
    quality: SourceQuality,
    decision_time: datetime,
    lookback_start: datetime | None = None,
) -> bool:
    """Whether an invalid bar falls in the history behind *decision_time*.

    A window-level FAIL says something in the window was broken; it does not
    say every state built from it is. This narrows the question to one
    decision: was a bad bar actually inside the history this state read?

    Bars are half-open on the left and bounded by the decision time on the
    right, matching the access layer's ``[start, end)`` and ``as_of``
    semantics. ``lookback_start`` of ``None`` means "any history up to
    ``decision_time``".
    """
    for timestamp in quality.invalid_timestamps:
        if timestamp > decision_time:
            continue
        if lookback_start is not None and timestamp < lookback_start:
            continue
        return True
    return False


def worst_verdict(qualities: Iterable[SourceQuality]) -> str:
    """The most severe verdict across several windows.

    A dataset spanning several instruments or timeframes is only as sound as
    its worst window, so the manifest reports that rather than an average --
    averaging quality is how a single broken instrument disappears into a
    reassuring number.
    """
    verdicts = {q.verdict for q in qualities}
    if FAIL in verdicts:
        return FAIL
    if WARNING in verdicts:
        return WARNING
    return PASS


def summarize_sources(qualities: Sequence[SourceQuality]) -> dict[str, Any]:
    """A manifest-ready summary of every window a dataset was built from."""
    return {
        "verdict": worst_verdict(qualities),
        "window_count": len(qualities),
        "error_count": sum(q.error_count for q in qualities),
        "warning_count": sum(q.warning_count for q in qualities),
        "windows": [q.as_dict() for q in qualities],
    }


__all__ = [
    "FAIL",
    "PASS",
    "WARNING",
    "Severity",
    "SourceQuality",
    "affects_decision_time",
    "clean_window",
    "summarize_sources",
    "verdict_for",
    "window_quality",
    "worst_verdict",
]
