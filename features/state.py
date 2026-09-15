"""The market state at a decision time, and the status of each value in it.

The Phase 2 north-star question is:

    What was the state of the market at decision time T, using only
    information that was actually available at T?

:class:`MarketState` is the answer object. It carries the instrument, the
decision time, the timeframe, one :class:`FeatureValue` per registered feature,
and a data-quality summary.

Why a status rather than a bare ``NaN``
---------------------------------------
A single ``NaN`` collapses several genuinely different situations into one
symbol: "the 20-bar window is not full yet", "the market was closed", "the
source bar was missing", and "the source data violated its contract" all become
the same nothing. A model, or a person debugging one, cannot tell them apart --
and the first is expected while the last is a defect.

So every value carries a :class:`FeatureStatus`. The requirement this satisfies
is that the system never silently turns absence into a plausible-looking
number; a value is either ``VALID`` and finite, or it is ``None`` with a status
saying exactly why.

Determinism
-----------
A ``MarketState`` is built only from data the Phase 1 access layer returned for
``as_of=decision_time``. It holds no wall-clock reads, no randomness and no
mutable shared state, so the same inputs always produce an identical object and
an identical serialisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from features.spec import FeatureSet, FeatureSpec


class FeatureStatus(str, Enum):
    """Why a feature value is, or is not, a number.

    A ``str`` enum so the value serialises to a readable token in JSON and
    Parquet without a conversion step -- matching ``marketdata.validation``'s
    ``Severity``.
    """

    #: Computed from a full window of contract-valid data. The only status
    #: that carries a finite value.
    VALID = "valid"

    #: The window is not full yet: fewer completed bars exist than the feature
    #: declares it needs. Expected at the start of a session for intraday
    #: features, and for the first sessions of a newly listed instrument. This
    #: is normal, not a defect.
    INSUFFICIENT_HISTORY = "insufficient_history"

    #: The source data the feature needs is absent -- an in-session bar that
    #: was never recorded, or a counterpart instrument with no rows in the
    #: window. Absence is information (nobody traded), never something to fill.
    MISSING = "missing"

    #: Source data exists but is older than the feature tolerates, so using it
    #: would describe a market that has since moved on. Distinct from MISSING:
    #: something is there, it is just no longer current.
    STALE = "stale"

    #: Source data exists but violates its Phase 1 contract, so any number
    #: derived from it would be arithmetic on known-bad input.
    INVALID_SOURCE = "invalid_source"

    #: The decision time falls outside a live trading session, so the feature
    #: has no meaning there rather than merely lacking data.
    MARKET_CLOSED = "market_closed"


#: Statuses that represent an absent value. Everything except VALID.
ABSENT_STATUSES = frozenset(s for s in FeatureStatus if s is not FeatureStatus.VALID)


class StateError(ValueError):
    """Raised when a state or feature value is internally inconsistent."""


@dataclass(frozen=True)
class FeatureValue:
    """One feature's value at one decision time, with why it is what it is.

    Parameters
    ----------
    name
        The feature's name, matching its :class:`~features.spec.FeatureSpec`.
    value
        The number, or ``None`` for every status other than
        :attr:`FeatureStatus.VALID`.
    status
        Why this value is present or absent.
    detail
        Optional one-line explanation, for a human reading a puzzling row.
        Never parsed; never part of equality-critical logic.
    """

    name: str
    value: float | None
    status: FeatureStatus = FeatureStatus.VALID
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise StateError("name must not be empty")
        if self.status is FeatureStatus.VALID:
            if self.value is None:
                raise StateError(
                    f"{self.name}: status is VALID but value is None; use an "
                    f"absent status to say why the value is missing"
                )
            # NaN and infinity would propagate silently through any downstream
            # arithmetic and are exactly the "plausible-looking nothing" the
            # status model exists to prevent.
            if self.value != self.value:  # NaN
                raise StateError(f"{self.name}: VALID value must not be NaN")
            if self.value in (float("inf"), float("-inf")):
                raise StateError(f"{self.name}: VALID value must be finite")
        elif self.value is not None:
            raise StateError(
                f"{self.name}: status {self.status.value!r} is an absent "
                f"status, so value must be None (got {self.value!r})"
            )

    @property
    def is_valid(self) -> bool:
        return self.status is FeatureStatus.VALID

    @classmethod
    def valid(cls, name: str, value: float) -> FeatureValue:
        return cls(name=name, value=float(value), status=FeatureStatus.VALID)

    @classmethod
    def absent(
        cls,
        name: str,
        status: FeatureStatus,
        detail: str | None = None,
    ) -> FeatureValue:
        """An absent value. Rejects :attr:`FeatureStatus.VALID` explicitly."""
        if status is FeatureStatus.VALID:
            raise StateError(
                f"{name}: absent() needs a reason, not VALID; use valid() to "
                f"record a number"
            )
        return cls(name=name, value=None, status=status, detail=detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "status": self.status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class StateQuality:
    """Feature-level quality, summarised across one state.

    This is the state's own account of how much of itself is real. It is
    deliberately about *the features*; the upstream market-data quality report
    lives in ``marketdata.quality`` and is referenced rather than duplicated.
    """

    total: int
    valid: int
    status_counts: Mapping[str, int]
    source_validation: Mapping[str, Any] | None = None

    @property
    def absent(self) -> int:
        return self.total - self.valid

    @property
    def completeness(self) -> float:
        """Fraction of features that carry a real number, in ``[0, 1]``.

        An empty state is 0.0 rather than 1.0: nothing was computed, so
        claiming full completeness would overstate it.
        """
        return (self.valid / self.total) if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "valid": self.valid,
            "absent": self.absent,
            "completeness": self.completeness,
            "status_counts": dict(sorted(self.status_counts.items())),
            "source_validation": self.source_validation,
        }


@dataclass(frozen=True)
class MarketState:
    """Everything knowable about one instrument at one decision time.

    Parameters
    ----------
    instrument
        Symbol as stored in ``instruments.symbol``.
    decision_time
        The instant a decision would have been made, naive IST. Only data that
        had already become knowable at this instant contributed to any value --
        for bars that means bars which had *closed*, not merely started. See
        ``docs/02-market-data.md``.
    timeframe
        ``"1m"`` or ``"5m"``: the bar series the features were computed from.
    values
        One :class:`FeatureValue` per feature, in feature-set order. Order is
        part of the contract, because the vector form is positional.
    feature_set_version
        The version hash of the definitions used, so a state can always be
        traced back to what its columns meant.
    quality
        Feature-level quality summary.
    """

    instrument: str
    decision_time: datetime
    timeframe: str
    values: tuple[FeatureValue, ...]
    feature_set_version: str | None = None
    quality: StateQuality | None = field(default=None)

    def __post_init__(self) -> None:
        if not self.instrument:
            raise StateError("instrument must not be empty")
        if self.decision_time.tzinfo is not None:
            # The whole codebase stores naive IST; an aware datetime here would
            # silently mean something different from every stored timestamp.
            raise StateError(
                "decision_time must be naive (IST by convention), not "
                "timezone-aware"
            )
        seen: set[str] = set()
        for value in self.values:
            if value.name in seen:
                raise StateError(f"duplicate feature value {value.name!r}")
            seen.add(value.name)
        if self.quality is None:
            object.__setattr__(self, "quality", summarize_quality(self.values))

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self) -> Iterator[FeatureValue]:
        return iter(self.values)

    def __contains__(self, name: object) -> bool:
        return any(v.name == name for v in self.values)

    @property
    def names(self) -> tuple[str, ...]:
        """Feature names, in vector order."""
        return tuple(v.name for v in self.values)

    def get(self, name: str) -> FeatureValue:
        for value in self.values:
            if value.name == name:
                return value
        raise KeyError(f"no feature {name!r} in this state")

    def value_of(self, name: str) -> float | None:
        """The number for *name*, or ``None`` if it is absent."""
        return self.get(name).value

    def status_of(self, name: str) -> FeatureStatus:
        return self.get(name).status

    def to_vector(self) -> tuple[float | None, ...]:
        """The positional feature vector, absent values as ``None``.

        ``None`` rather than ``NaN`` so that "no value" stays distinguishable
        from a genuine numeric result all the way to the dataset. Callers that
        need a float array convert deliberately, and the statuses remain
        available alongside to say why each hole is there.
        """
        return tuple(v.value for v in self.values)

    def valid_values(self) -> dict[str, float]:
        """Only the features that carry a real number."""
        return {v.name: v.value for v in self.values if v.is_valid and v.value is not None}

    def statuses(self) -> dict[str, FeatureStatus]:
        return {v.name: v.status for v in self.values}

    def as_dict(self) -> dict[str, Any]:
        """A deterministic, JSON-serialisable representation.

        ``decision_time`` is ISO-8601. Feature order is preserved, so two
        equal states always serialise to byte-identical JSON.
        """
        return {
            "instrument": self.instrument,
            "decision_time": self.decision_time.isoformat(),
            "timeframe": self.timeframe,
            "feature_set_version": self.feature_set_version,
            "features": [v.as_dict() for v in self.values],
            "quality": self.quality.as_dict() if self.quality else None,
        }

    def to_row(self) -> dict[str, Any]:
        """A flat row for tabular storage.

        One column per feature, plus a parallel ``<name>__status`` column, so a
        dataset never loses the reason a value is absent. Identity columns come
        first and keep their names stable across timeframes.
        """
        row: dict[str, Any] = {
            "instrument": self.instrument,
            "decision_time": self.decision_time,
            "timeframe": self.timeframe,
            "feature_set_version": self.feature_set_version,
        }
        for value in self.values:
            row[value.name] = value.value
            row[f"{value.name}__status"] = value.status.value
        return row


def summarize_quality(
    values: Sequence[FeatureValue],
    source_validation: Mapping[str, Any] | None = None,
) -> StateQuality:
    """Summarise feature-level quality across *values*.

    Every status is counted, including zero counts, so a reader can tell
    "no features were STALE" from "STALE was never considered".
    """
    counts = {status.value: 0 for status in FeatureStatus}
    for value in values:
        counts[value.status.value] += 1
    return StateQuality(
        total=len(values),
        valid=counts[FeatureStatus.VALID.value],
        status_counts=counts,
        source_validation=source_validation,
    )


def empty_state(
    instrument: str,
    decision_time: datetime,
    feature_set: FeatureSet,
    status: FeatureStatus,
    detail: str | None = None,
    feature_set_version: str | None = None,
) -> MarketState:
    """A state where every feature is absent for the same reason.

    Used when a single condition rules out the whole state -- the decision time
    falls outside a session, or no source data exists at all. It returns a
    fully-shaped state rather than ``None`` so that callers keep a consistent
    column set and the reason travels with the row instead of being dropped.
    """
    if status is FeatureStatus.VALID:
        raise StateError("empty_state needs an absent status, not VALID")
    values = tuple(
        FeatureValue.absent(spec.name, status, detail) for spec in feature_set
    )
    return MarketState(
        instrument=instrument,
        decision_time=decision_time,
        timeframe=feature_set.timeframe,
        values=values,
        feature_set_version=feature_set_version,
    )


def insufficient_history(spec: FeatureSpec, available: int) -> FeatureValue:
    """An :attr:`FeatureStatus.INSUFFICIENT_HISTORY` value for *spec*.

    The detail records both what was needed and what existed, which is the
    difference between a row a reader can act on and one they have to
    reverse-engineer.
    """
    return FeatureValue.absent(
        spec.name,
        FeatureStatus.INSUFFICIENT_HISTORY,
        f"needs {spec.required_bars} completed bars, {available} available",
    )


__all__ = [
    "ABSENT_STATUSES",
    "FeatureStatus",
    "FeatureValue",
    "MarketState",
    "StateError",
    "StateQuality",
    "empty_state",
    "insufficient_history",
    "summarize_quality",
]
