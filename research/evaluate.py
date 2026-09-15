"""Running a predictor over a split, with the checks that make it mean something.

This is the only place a prediction is made, and it is where the brief's rules
stop being documentation:

* the split is applied **by session date**, and the row-level chronology is
  re-checked afterwards -- a split object can be constructed correctly and
  still be applied to a panel it does not describe (S4.3);
* the last training label's observation time must fall before the first test
  decision, which is the check the calendar gap exists to satisfy and the one
  that would catch a label horizon long enough to cross it (S4.3);
* a model is handed a **masked** panel and cannot read the test outcomes; only
  a declared baseline may (S4.1);
* a model and its baselines are fitted and scored by the same code, on the same
  rows, so the comparison cannot drift;
* every walk-forward window is evaluated the same way, and the report carries
  all of them (S4.4).

Nothing here fits a model -- Phase 3A deliberately ships no models. What it
ships is the yardstick, and the proof that the yardstick is straight is that
the only predictors it can currently evaluate are trivial ones, honestly
scored.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Sequence

from research.baselines import Predictor
from research.errors import LeakageError, ResearchError
from research.metrics import DEFAULT_CONFIDENCE, accuracy, regression_metrics
from research.panel import CLASSIFICATION, Panel
from research.report import (
    Comparison,
    HypothesisLedger,
    Result,
    WalkForwardReport,
    WindowInfo,
)
from research.splits import Split


def split_panel(panel: Panel, split: Split) -> tuple[Panel, Panel]:
    """The training and test rows of *panel* under *split*.

    Both blocks may legitimately be missing sessions -- a session with no
    usable rows contributes none -- but neither may be empty, because a window
    with nothing in it produces a metric over nothing.
    """
    train = panel.on_sessions(split.train)
    test = panel.on_sessions(split.test)
    if len(train) == 0:
        raise ResearchError(
            f"window #{split.index}: no usable rows in the training sessions "
            f"{split.train[0]}..{split.train[-1]}"
        )
    if len(test) == 0:
        raise ResearchError(
            f"window #{split.index}: no usable rows in the test sessions "
            f"{split.test[0]}..{split.test[-1]}"
        )
    return train, test


def assert_no_leakage(train: Panel, test: Panel) -> None:
    """Every training label was observed before the first test decision.

    Two checks, and the second is the one that matters. Ordering the decision
    times is necessary but not sufficient: a label reaches ``horizon_minutes``
    past its decision time, so training rows can be strictly earlier than every
    test row and still carry answers drawn from the test window. The Phase 2
    labels never cross a session boundary, so the one-session gap a
    :class:`~research.splits.Split` insists on always satisfies this -- and if
    a future label does cross one, this is where it stops rather than where it
    quietly inflates a result.
    """
    if len(train) == 0 or len(test) == 0:
        raise ResearchError("cannot check a window with no rows")
    if train.label != test.label:
        raise ResearchError(
            f"the training panel holds {train.label!r} and the test panel "
            f"{test.label!r}"
        )
    if train.last_decision >= test.first_decision:
        raise LeakageError(
            f"training runs to {train.last_decision} but testing starts at "
            f"{test.first_decision}; the split is not chronological"
        )
    observed = train.last_decision + timedelta(minutes=train.horizon_minutes)
    if observed >= test.first_decision:
        raise LeakageError(
            f"the last training label ({train.label}) is not observed until "
            f"{observed}, which is at or after the first test decision "
            f"{test.first_decision}; widen the gap"
        )


def _predictions(predictor: Predictor, test: Panel) -> tuple[float, ...]:
    """Predict, showing the answers only to a baseline that declares it.

    The declaration is checked here rather than trusted, because a model that
    sets the flag would be scoring itself against the answer sheet, and that is
    the single most convincing way to produce a wrong result.
    """
    if not predictor.uses_test_labels:
        return predictor.predict(test.masked())
    if not predictor.is_baseline:
        raise LeakageError(
            f"{predictor.name} is not a baseline but asks to read the test "
            f"labels; only the best-naive baseline of brief S4.1 may"
        )
    return predictor.predict(test)


def evaluate(
    predictor: Predictor,
    train: Panel,
    test: Panel,
    split: Split | None = None,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Result:
    """Fit on *train*, score on *test*, and record what was done.

    *split* is optional only so that a caller holding two panels can still get
    a result; the window's dates are recovered from the panels when it is
    absent. The leakage check runs either way.
    """
    assert_no_leakage(train, test)
    predictor.fit(train)
    predicted = _predictions(predictor, test)
    outcomes = test.y

    if split is not None:
        window = WindowInfo.from_split(split, len(train), len(test))
    else:
        window = WindowInfo(
            index=0,
            train_start=train.first_decision.date(),
            train_end=train.last_decision.date(),
            test_start=test.first_decision.date(),
            test_end=test.last_decision.date(),
            train_rows=len(train),
            test_rows=len(test),
            gap_sessions=0,
        )

    if test.kind == CLASSIFICATION:
        return Result(
            predictor=predictor.name,
            kind=test.kind,
            is_baseline=predictor.is_baseline,
            window=window,
            accuracy=accuracy(outcomes, predicted, confidence),
            params=dict(predictor.params),
        )
    return Result(
        predictor=predictor.name,
        kind=test.kind,
        is_baseline=predictor.is_baseline,
        window=window,
        regression=regression_metrics(outcomes, predicted, confidence),
        params=dict(predictor.params),
    )


def compare(
    model: Predictor,
    baselines: Sequence[Predictor],
    panel: Panel,
    split: Split,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Comparison:
    """One window: the model and its baselines, on identical rows.

    Passing no baselines is refused here as well as in
    :class:`~research.report.Comparison`, so the message names the window
    rather than arriving later without context.
    """
    if not baselines:
        raise ResearchError(
            f"window #{split.index}: evaluating {model.name} without a baseline "
            f"is refused; brief S4.1"
        )
    train, test = split_panel(panel, split)
    return Comparison(
        model=evaluate(model, train, test, split, confidence),
        baselines=tuple(
            evaluate(baseline, train, test, split, confidence)
            for baseline in baselines
        ),
    )


def walk_forward_study(
    model: Predictor,
    baselines: Sequence[Predictor],
    panel: Panel,
    splits: Sequence[Split],
    hypotheses: HypothesisLedger,
    confidence: float = DEFAULT_CONFIDENCE,
) -> WalkForwardReport:
    """Every window, reported together.

    The ledger is a required argument rather than an optional one. Brief S4.6
    asks how many hypotheses were tested, and a question that can be skipped by
    leaving out an argument is a question that will be skipped.
    """
    if not splits:
        raise ResearchError("a walk-forward study needs at least one window")
    return WalkForwardReport(
        instrument=panel.instrument,
        timeframe=panel.timeframe,
        label=panel.label,
        model=model.name,
        comparisons=tuple(
            compare(model, baselines, panel, split, confidence) for split in splits
        ),
        hypotheses=hypotheses,
    )


__all__ = [
    "assert_no_leakage",
    "compare",
    "evaluate",
    "split_panel",
    "walk_forward_study",
]
