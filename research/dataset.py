"""Turning a Phase 2 feature dataset into a panel, and counting what is lost.

Brief S4.7: *inherit Phase 2's point-in-time machinery, do not rebuild it*.
This module is the only door between the two phases, and it is narrow on
purpose. It reads a dataset through :func:`features.store.read_dataset` and
resolves names through :func:`features.registry`; it never touches ``ohlcv_*``,
never recomputes a feature, and never decides what a column means.

What it refuses
---------------
* A label that is not in the Phase 2 label set for that timeframe -- a typo in
  a label name would otherwise produce a study of a column that does not exist.
* A label used as a feature. ``fwd_dir_30m`` is the sign of ``fwd_ret_30m``;
  training on one to predict the other is leakage that would look like a
  spectacular result.
* A feature that is not in the registered state set, for the same reason the
  registry exists: a column that is not a declared feature has no version, no
  definition and no point-in-time guarantee.

What it drops, and says so
--------------------------
A panel holds only rows where the label and every requested feature are
``valid`` in the Phase 2 sense. Every other row is dropped -- never imputed,
matching the Phase 1 rule that nothing is repaired -- and :class:`Usability`
records how many went and which column sent them. The first quarter-hour of
each session and the last hour are systematically absent (rolling windows are
not full yet; the forward horizon would cross the close), so a drop count that
looks alarming is usually structural. Reading it is how you tell the two apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from features.registry import label_set, state_features
from features.spec import MINUTES_PER_BAR
from features.state import FeatureStatus
from features.store import DEFAULT_ROOT, read_dataset
from research.errors import LeakageError, ResearchError
from research.panel import CLASSIFICATION, REGRESSION, Panel

#: The only status a panel accepts. Everything else is an absence with a
#: reason, and a reason is not a number.
VALID = FeatureStatus.VALID.value

#: Label kinds, from ``features.labels``, mapped to what the harness scores.
_KIND = {"dir": CLASSIFICATION, "ret": REGRESSION, "rv": REGRESSION}


def _label_spec(label: str, timeframe: str):
    labels = label_set(timeframe)
    for spec in labels:
        if spec.name == label:
            return spec
    raise ResearchError(
        f"{label!r} is not a registered {timeframe} label; choose from "
        f"{', '.join(labels.names)}"
    )


def label_kind(label: str, timeframe: str) -> str:
    """Whether *label* is scored as a class or as a quantity."""
    spec = _label_spec(label, timeframe)
    kind = spec.params.get("kind")
    if kind not in _KIND:
        raise ResearchError(f"{label!r} has an unrecognised label kind {kind!r}")
    return _KIND[kind]


def label_horizon_minutes(label: str, timeframe: str) -> int:
    """How far past its decision time *label* reaches, in minutes.

    Read from the spec's bar count rather than parsed out of the name, so a
    horizon renamed or re-parameterised in Phase 2 cannot leave the leakage
    check working from a stale number.
    """
    spec = _label_spec(label, timeframe)
    bars = spec.params.get("bars")
    if not isinstance(bars, int) or bars <= 0:
        raise ResearchError(f"{label!r} declares no usable horizon: bars={bars!r}")
    return bars * MINUTES_PER_BAR[timeframe]


@dataclass(frozen=True)
class Usability:
    """What the dataset held, what survived, and why the rest did not."""

    rows_in: int
    rows_out: int
    label: str
    reasons: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def dropped(self) -> int:
        return self.rows_in - self.rows_out

    @property
    def kept_fraction(self) -> float:
        return self.rows_out / self.rows_in if self.rows_in else 0.0

    def render(self) -> str:
        lines = [
            f"{self.rows_out} of {self.rows_in} rows usable for {self.label} "
            f"({self.kept_fraction:.1%}); {self.dropped} dropped"
        ]
        for column in sorted(self.reasons):
            counts = self.reasons[column]
            detail = ", ".join(
                f"{status} {count}" for status, count in sorted(counts.items())
            )
            lines.append(f"  {column}: {detail}")
        return "\n".join(lines)

    def describe(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "dropped": self.dropped,
            "kept_fraction": self.kept_fraction,
            "reasons": {c: dict(v) for c, v in self.reasons.items()},
        }


def _check_columns(label: str, features: Sequence[str], timeframe: str) -> None:
    # Resolving the label first means a typo is an error naming the available
    # labels, rather than a Parquet reader complaining about a missing column.
    _label_spec(label, timeframe)
    registered = set(state_features(timeframe).names)
    labels = set(label_set(timeframe).names)
    for name in features:
        if name in labels:
            raise LeakageError(
                f"{name!r} is a label, not a feature; a label is what happened "
                f"after the decision time and cannot be an input"
            )
        if name not in registered:
            raise ResearchError(
                f"{name!r} is not a registered {timeframe} feature; the panel "
                f"only accepts columns the Phase 2 registry defines"
            )
    if label in features:
        raise LeakageError(f"{label!r} cannot be both the label and a feature")


def panel_from_frame(
    frame,
    label: str,
    features: Sequence[str] | None = None,
    instrument: str | None = None,
    timeframe: str | None = None,
) -> tuple[Panel, Usability]:
    """A panel of the usable rows of *frame*, and an account of the rest.

    Parameters
    ----------
    frame
        A dataset as written by :mod:`features.store`: identity columns, then
        each value beside its ``__status`` partner.
    label
        The column to predict.
    features
        The columns to offer a model. Defaults to every registered state
        feature for the timeframe, which is what a first pass wants; a study
        narrowing to two or three passes them explicitly.
    instrument, timeframe
        Read from the frame when absent. Passed explicitly only for a frame
        assembled by hand, as the tests do.
    """
    if instrument is None:
        instrument = _single(frame, "instrument")
    if timeframe is None:
        timeframe = _single(frame, "timeframe")

    names = tuple(features) if features is not None else state_features(timeframe).names
    _check_columns(label, names, timeframe)

    required = [label, *names]
    missing = [
        column
        for name in required
        for column in (name, f"{name}__status")
        if column not in frame.columns
    ]
    if missing:
        raise ResearchError(
            f"the dataset is missing {len(missing)} column(s): "
            f"{', '.join(missing[:6])}"
        )

    ordered = frame.sort_values("decision_time", kind="stable")

    reasons: dict[str, dict[str, int]] = {}
    usable = None
    for name in required:
        status = ordered[f"{name}__status"]
        valid = status == VALID
        usable = valid if usable is None else (usable & valid)
        counts = status[~valid].value_counts().to_dict()
        if counts:
            reasons[name] = {str(k): int(v) for k, v in counts.items()}

    kept = ordered[usable]
    decision_times = tuple(_as_datetime(t) for t in kept["decision_time"])
    rows = tuple(
        tuple(float(value) for value in row)
        for row in kept[list(names)].itertuples(index=False, name=None)
    )
    targets = tuple(float(value) for value in kept[label])

    panel = Panel(
        instrument=str(instrument),
        timeframe=str(timeframe),
        label=label,
        kind=label_kind(label, timeframe),
        horizon_minutes=label_horizon_minutes(label, timeframe),
        feature_names=tuple(names),
        decision_times=decision_times,
        rows=rows,
        targets=targets,
    )
    return panel, Usability(
        rows_in=len(ordered),
        rows_out=len(kept),
        label=label,
        reasons=reasons,
    )


def _single(frame, column: str) -> Any:
    """The one value *column* holds, or an error naming what else is there.

    A panel describes one instrument on one timeframe. Two of either in a
    frame means two different things have been concatenated, and averaging a
    metric across them would answer neither question.
    """
    if column not in frame.columns:
        raise ResearchError(f"the dataset has no {column!r} column")
    distinct = frame[column].dropna().unique()
    if len(distinct) != 1:
        raise ResearchError(
            f"expected one {column} in the dataset, found {len(distinct)}: "
            f"{', '.join(str(v) for v in distinct[:5])}"
        )
    return distinct[0]


def _as_datetime(value) -> datetime:
    """A naive IST ``datetime``, whatever the frame stored it as.

    Parquet round-trips these as pandas timestamps; the rest of the harness
    does arithmetic in minutes against the label horizon, and a mix of types
    would compare inconsistently.
    """
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if to_pydatetime is not None:
        return to_pydatetime()
    if isinstance(value, datetime):
        return value
    raise ResearchError(f"decision_time {value!r} is not a timestamp")


def load_panel(
    instrument: str,
    timeframe: str,
    label: str,
    features: Sequence[str] | None = None,
    root: Path | str = DEFAULT_ROOT,
) -> tuple[Panel, Usability]:
    """Read a stored dataset and hand back a panel over it.

    Reads only the columns the panel needs, which on a 58-column dataset asking
    for one label and three features is six columns rather than all of them.
    """
    fs = state_features(timeframe)
    names = tuple(features) if features is not None else fs.names
    _check_columns(label, names, timeframe)
    columns = [
        "instrument",
        "decision_time",
        "timeframe",
        label,
        f"{label}__status",
    ]
    for name in names:
        columns += [name, f"{name}__status"]
    frame = read_dataset(instrument, fs, root=root, columns=columns)
    return panel_from_frame(
        frame, label, names, instrument=instrument, timeframe=timeframe
    )


__all__ = [
    "Usability",
    "VALID",
    "label_horizon_minutes",
    "label_kind",
    "load_panel",
    "panel_from_frame",
]
