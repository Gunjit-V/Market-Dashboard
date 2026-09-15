"""The rows an evaluation runs over, and the label it hides from models.

A :class:`Panel` is one instrument, one timeframe, one label, and the rows for
which both the features and that label are real. It is deliberately not a
DataFrame: it is immutable, it is ordered, it knows its label's forward horizon,
and it can refuse to show its answers.

Why it carries the horizon
--------------------------
Every label in the Phase 2 catalogue is forward-looking by a known number of
minutes: ``fwd_rv_60m`` at 14:00 is not knowable until 15:00. A split that
ends training at 14:30 and starts testing at 14:35 therefore trains on rows
whose answers are drawn from the test period, even though every timestamp is in
the right order. Carrying ``horizon_minutes`` on the panel is what lets
:mod:`research.evaluate` check that the last training label was observed before
the first test decision, rather than trusting that the dates look tidy.

Why it can hide the label
-------------------------
:meth:`Panel.masked` returns the same rows with the answers withheld, and
:attr:`Panel.y` raises rather than returning ``None``. Models are handed the
masked panel; the one class of predictor that legitimately reads test labels --
the best-naive classifier of brief S4.1, whose whole definition is "the best
constant answer on this window" -- must declare it, and :mod:`research.evaluate`
refuses that declaration from anything that is not a baseline.

Rows are never filled
---------------------
A panel contains only rows where every feature and the label are ``valid`` in
the Phase 2 sense. There is no imputation, matching the Phase 1 rule that
nothing is repaired: the dropped rows are counted and reported by
:mod:`research.dataset`, so an absence stays visible instead of becoming a
plausible number.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Iterable, Sequence

from research.errors import LeakageError, ResearchError

#: What a label asks for. ``fwd_dir_*`` is a class; the rest are quantities.
CLASSIFICATION = "classification"
REGRESSION = "regression"
KINDS = (CLASSIFICATION, REGRESSION)


@dataclass(frozen=True)
class Panel:
    """Aligned features, decision times and one label's outcomes.

    Parameters
    ----------
    instrument, timeframe
        Carried through to every report, because a result is about a specific
        instrument on a specific bar width and is not portable between them.
    label
        The column being predicted, e.g. ``fwd_rv_60m``.
    kind
        :data:`CLASSIFICATION` or :data:`REGRESSION`, which decides whether the
        harness scores accuracy or RMSE-and-correlation.
    horizon_minutes
        How far past its decision time the label reaches. Used for the
        leakage check, never for arithmetic.
    feature_names
        Column names, in the order every row follows.
    decision_times
        Strictly ascending. One row per decision time.
    rows
        Feature values, finite, one tuple per decision time.
    targets
        The outcomes, or ``None`` when masked.
    """

    instrument: str
    timeframe: str
    label: str
    kind: str
    horizon_minutes: int
    feature_names: tuple[str, ...]
    decision_times: tuple[datetime, ...]
    rows: tuple[tuple[float, ...], ...]
    targets: tuple[float, ...] | None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ResearchError(
                f"kind must be one of {KINDS}, got {self.kind!r}"
            )
        if self.horizon_minutes <= 0:
            raise ResearchError(
                f"{self.label}: horizon_minutes must be positive, got "
                f"{self.horizon_minutes}"
            )
        if len(self.rows) != len(self.decision_times):
            raise ResearchError(
                f"{len(self.rows)} rows against "
                f"{len(self.decision_times)} decision times"
            )
        if self.targets is not None and len(self.targets) != len(self.rows):
            raise ResearchError(
                f"{len(self.targets)} targets against {len(self.rows)} rows"
            )
        width = len(self.feature_names)
        if len(set(self.feature_names)) != width:
            raise ResearchError("duplicate feature name in the panel")
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise ResearchError(
                    f"row {index} has {len(row)} values, expected {width}"
                )
            for name, value in zip(self.feature_names, row):
                if value != value or value in (float("inf"), float("-inf")):
                    raise ResearchError(
                        f"row {index}: {name} is not finite ({value!r}); a "
                        f"panel holds only valid values"
                    )
        if self.targets is not None:
            for index, value in enumerate(self.targets):
                if value != value or value in (float("inf"), float("-inf")):
                    raise ResearchError(
                        f"row {index}: target {self.label} is not finite "
                        f"({value!r})"
                    )
        previous: datetime | None = None
        for moment in self.decision_times:
            if previous is not None and moment <= previous:
                raise ResearchError(
                    f"decision times must ascend strictly; {moment} follows "
                    f"{previous}"
                )
            previous = moment

    # -- the answers ------------------------------------------------------

    @property
    def y(self) -> tuple[float, ...]:
        """The outcomes. Raises when the panel is masked.

        A model asking a masked panel for its answers is a bug that must stop
        the run, not return ``None`` and propagate as a puzzling metric.
        """
        if self.targets is None:
            raise LeakageError(
                f"the {self.label} outcomes are hidden from this panel; only a "
                f"baseline that declares uses_test_labels may read them"
            )
        return self.targets

    @property
    def is_masked(self) -> bool:
        return self.targets is None

    def masked(self) -> "Panel":
        """The same rows with the answers withheld."""
        return replace(self, targets=None)

    # -- shape ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def sessions(self) -> tuple[date, ...]:
        """The distinct session dates present, ascending."""
        seen: list[date] = []
        for moment in self.decision_times:
            day = moment.date()
            if not seen or seen[-1] != day:
                seen.append(day)
        return tuple(seen)

    @property
    def first_decision(self) -> datetime:
        return self.decision_times[0]

    @property
    def last_decision(self) -> datetime:
        return self.decision_times[-1]

    def column(self, name: str) -> tuple[float, ...]:
        """One feature's values, in row order."""
        try:
            index = self.feature_names.index(name)
        except ValueError:
            raise ResearchError(
                f"no feature named {name!r}; this panel holds "
                f"{', '.join(self.feature_names) or '(none)'}"
            ) from None
        return tuple(row[index] for row in self.rows)

    def has_column(self, name: str) -> bool:
        return name in self.feature_names

    def select(self, indices: Sequence[int]) -> "Panel":
        """A sub-panel of the given row positions, in decision-time order.

        The positions are sorted rather than taken as given: a panel whose
        decision times did not ascend would break every leakage check that
        reads its first and last row, so the ordering is an invariant rather
        than a convention a caller may override.
        """
        ordered = sorted(indices)
        return replace(
            self,
            decision_times=tuple(self.decision_times[i] for i in ordered),
            rows=tuple(self.rows[i] for i in ordered),
            targets=(
                None
                if self.targets is None
                else tuple(self.targets[i] for i in ordered)
            ),
        )

    def on_sessions(self, days: Iterable[date]) -> "Panel":
        """The rows falling on *days*.

        The natural way to apply a split, because every split in this harness
        is expressed in session dates rather than row positions -- a split by
        row count would move when a session's row count changes.
        """
        wanted = set(days)
        return self.select(
            [i for i, t in enumerate(self.decision_times) if t.date() in wanted]
        )

    def describe(self) -> dict[str, object]:
        """A small summary, for a report header or a manifest."""
        return {
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "label": self.label,
            "kind": self.kind,
            "horizon_minutes": self.horizon_minutes,
            "features": len(self.feature_names),
            "rows": len(self),
            "sessions": len(self.sessions),
            "first_decision": (
                self.first_decision.isoformat() if self.rows else None
            ),
            "last_decision": self.last_decision.isoformat() if self.rows else None,
        }


__all__ = ["CLASSIFICATION", "KINDS", "REGRESSION", "Panel"]
