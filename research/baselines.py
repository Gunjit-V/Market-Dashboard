"""The trivial answers, as first-class objects.

Brief S4.1: *compare against the best naive baseline, never a convenient one*.
The rule exists because it was broken twice during Phase 2 -- once by a
subagent that had been warned about it in writing -- and the second break
turned a 22-point direction "edge" into a loss once the right comparison was
made.

A rule that lives in a reviewer's head gets skipped. So every baseline here is
a :class:`Predictor`, fitted and scored by exactly the same code path as a
model, and :mod:`research.report` refuses to present a model result without at
least one of them beside it.

The four the brief names
------------------------
* :class:`BestNaiveClassifier` -- ``max(always_up, always_down)``, chosen on
  the test window itself;
* :class:`TrainMeanRegressor` -- the training mean, repeated;
* :class:`Persistence` -- "it will be what it has just been": a feature carried
  forward unchanged;
* :class:`BiasCorrectedPersistence` -- the same, rescaled to the training
  window's level.

Why the classifier reads the test labels
----------------------------------------
Because that is its definition. On a down day "always predict down" scores 65%
and "always predict up" scores 35%; the honest baseline is the better of the
two, and which one is better is a property of the window being scored. A
baseline chosen from the training window instead would be beatable by luck, and
would restore exactly the error S4.1 forbids.

That access is explicit (``uses_test_labels = True``) and
:mod:`research.evaluate` accepts the declaration only from a baseline. Models
are handed a masked panel and cannot read the answers at all.

Why bias-corrected persistence is a separate baseline
-----------------------------------------------------
Multiplying a predictor by a positive constant cannot change its correlation --
the ranking is identical, term for term. Bias correction therefore only ever
moves the *level*, which is precisely why it belongs beside plain persistence:
the pair separates "the model got the level right" from "the model got the
order right", and a model that only does the former is beaten by a one-line
rescale of the trivial answer.
"""

from __future__ import annotations

from typing import Sequence

from research.errors import ResearchError
from research.panel import CLASSIFICATION, REGRESSION, Panel


class Predictor:
    """What the harness can evaluate: something that fits, then predicts.

    Subclasses set :attr:`kind` and implement :meth:`_fit` and
    :meth:`_predict`. The public :meth:`fit` / :meth:`predict` wrap them with
    the checks every predictor needs, so no subclass can forget them.
    """

    #: :data:`~research.panel.CLASSIFICATION` or
    #: :data:`~research.panel.REGRESSION`.
    kind: str = REGRESSION

    #: Baselines are the trivial answers a model must beat. The flag is what
    #: lets a report insist on having one.
    is_baseline: bool = False

    #: Whether :meth:`predict` reads the outcomes of the window it is scoring.
    #: True only for :class:`BestNaiveClassifier`; see the module docstring.
    uses_test_labels: bool = False

    def __init__(self, name: str) -> None:
        self.name = name
        self._fitted = False

    @property
    def params(self) -> dict[str, object]:
        """Everything that decides this predictor's output, for the report.

        A model with a random seed records it here, which is what makes brief
        gate 8 -- same data and seed, same numbers -- checkable from a report
        alone rather than by rerunning.
        """
        return {}

    @property
    def fitted(self) -> bool:
        return self._fitted

    def fit(self, train: Panel) -> "Predictor":
        """Learn from *train*, and return self so calls can chain."""
        if len(train) == 0:
            raise ResearchError(f"{self.name}: cannot fit on an empty panel")
        if train.kind != self.kind:
            raise ResearchError(
                f"{self.name} is a {self.kind} but the panel's label "
                f"{train.label!r} is a {train.kind}"
            )
        self._fit(train)
        self._fitted = True
        return self

    def predict(self, panel: Panel) -> tuple[float, ...]:
        """Predictions for every row of *panel*, in row order."""
        if not self._fitted:
            raise ResearchError(
                f"{self.name}: fit on a training window before predicting"
            )
        if len(panel) == 0:
            raise ResearchError(f"{self.name}: cannot predict on an empty panel")
        out = tuple(float(v) for v in self._predict(panel))
        if len(out) != len(panel):
            raise ResearchError(
                f"{self.name}: {len(out)} predictions for {len(panel)} rows"
            )
        return out

    def _fit(self, train: Panel) -> None:
        raise NotImplementedError

    def _predict(self, panel: Panel) -> Sequence[float]:
        raise NotImplementedError

    def __str__(self) -> str:
        return self.name


class ConstantRegressor(Predictor):
    """Predicts one number, always. The harness's smallest honest input.

    Exists for its own sake as well as for tests: brief S5 requires the
    harness to be able to evaluate a constant predictor and report it
    honestly, which means reporting that its correlation is *undefined* rather
    than zero.
    """

    kind = REGRESSION
    is_baseline = True

    def __init__(self, value: float, name: str | None = None) -> None:
        super().__init__(name or f"constant({value:g})")
        self.value = float(value)

    @property
    def params(self) -> dict[str, object]:
        return {"value": self.value}

    def _fit(self, train: Panel) -> None:
        return None

    def _predict(self, panel: Panel) -> Sequence[float]:
        return [self.value] * len(panel)


class TrainMeanRegressor(Predictor):
    """Predicts the training mean -- the baseline S4.1 names for regression.

    The training mean, not the test mean: the mean of the window being scored
    is not knowable when the prediction is made, and using it would be the
    same error as choosing the naive classifier from the test window for a
    model rather than for a baseline.
    """

    kind = REGRESSION
    is_baseline = True

    def __init__(self, name: str = "train-mean") -> None:
        super().__init__(name)
        self.mean: float | None = None

    @property
    def params(self) -> dict[str, object]:
        return {"mean": self.mean}

    def _fit(self, train: Panel) -> None:
        self.mean = sum(train.y) / len(train)

    def _predict(self, panel: Panel) -> Sequence[float]:
        return [self.mean] * len(panel)


class Persistence(Predictor):
    """"It will be what it has just been" -- a feature, used unchanged.

    The right baseline for a volatility forecast, and a hard one: Phase 2
    measured trailing five-session realized volatility at +0.61 correlation
    with subsequent realized volatility, above the +0.56 that implied
    volatility achieved.

    *column* names the feature to carry forward -- ``rv_short`` for
    ``fwd_rv_60m`` on the 5-minute set, where both cover 100 minutes of
    returns. Nothing here checks that the two spans match: that is a modelling
    decision, and it belongs in the study that chooses the pairing, recorded in
    :attr:`params` for the report.
    """

    kind = REGRESSION
    is_baseline = True

    def __init__(self, column: str, name: str | None = None) -> None:
        super().__init__(name or f"persistence({column})")
        self.column = column

    @property
    def params(self) -> dict[str, object]:
        return {"column": self.column}

    def _require(self, panel: Panel) -> tuple[float, ...]:
        if not panel.has_column(self.column):
            raise ResearchError(
                f"{self.name}: the panel has no {self.column!r} column"
            )
        return panel.column(self.column)

    def _fit(self, train: Panel) -> None:
        self._require(train)

    def _predict(self, panel: Panel) -> Sequence[float]:
        return self._require(panel)


class BiasCorrectedPersistence(Persistence):
    """Persistence rescaled so its training-window level matches the label's.

    The scale is ``mean(train label) / mean(train persistence)``, learned on
    the training window only. Realized volatility over 12 forward bars is
    systematically below a 20-bar trailing estimate in a calming market and
    above it in a turbulent one; correcting that removes a constant part of the
    error.

    It cannot improve the ranking. A positive scalar leaves Pearson's r
    unchanged, so this baseline and :class:`Persistence` always report the same
    correlation -- which makes the pair a direct measurement of how much of a
    model's RMSE improvement was level rather than order.
    """

    def __init__(self, column: str, name: str | None = None) -> None:
        super().__init__(column, name or f"bias-corrected-persistence({column})")
        self.scale: float | None = None

    @property
    def params(self) -> dict[str, object]:
        return {"column": self.column, "scale": self.scale}

    def _fit(self, train: Panel) -> None:
        carrier = self._require(train)
        mean_carrier = sum(carrier) / len(carrier)
        if mean_carrier <= 0.0:
            raise ResearchError(
                f"{self.name}: the mean of {self.column!r} over the training "
                f"window is {mean_carrier:g}; a multiplicative correction needs "
                f"a positive one"
            )
        self.scale = (sum(train.y) / len(train)) / mean_carrier

    def _predict(self, panel: Panel) -> Sequence[float]:
        return [self.scale * value for value in self._require(panel)]


class AlwaysPredict(Predictor):
    """One class, every row. The building block of the naive classifier."""

    kind = CLASSIFICATION
    is_baseline = True

    def __init__(self, value: float, name: str | None = None) -> None:
        super().__init__(name or f"always({value:+g})")
        self.value = float(value)

    @property
    def params(self) -> dict[str, object]:
        return {"value": self.value}

    def _fit(self, train: Panel) -> None:
        return None

    def _predict(self, panel: Panel) -> Sequence[float]:
        return [self.value] * len(panel)


class BestNaiveClassifier(Predictor):
    """``max(always_up, always_down, ...)`` scored on the test window itself.

    The baseline brief S4.1 makes mandatory. It evaluates one
    :class:`AlwaysPredict` per class observed in the window and keeps the best,
    so a model is measured against the best constant answer available rather
    than the most flattering one.

    The direction label is three-valued -- 0.181% of Nifty 50 five-minute bars
    close exactly unchanged, and Phase 2 kept that distinction rather than
    folding it into up or down -- so the candidates are whatever classes the
    window actually contains, not a hard-coded pair.

    Ties go to the smaller class value, purely so that two runs over the same
    data produce the same report (brief gate 8).
    """

    kind = CLASSIFICATION
    is_baseline = True
    uses_test_labels = True

    def __init__(self, name: str = "best-naive") -> None:
        super().__init__(name)
        self.chosen: float | None = None
        self.candidates: tuple[float, ...] = ()

    @property
    def params(self) -> dict[str, object]:
        return {"chosen_class": self.chosen, "candidates": list(self.candidates)}

    def _fit(self, train: Panel) -> None:
        # Nothing is learned here on purpose: the choice is made per test
        # window, which is the whole point of the baseline.
        return None

    def _predict(self, panel: Panel) -> Sequence[float]:
        outcomes = panel.y
        classes = sorted(set(outcomes))
        scores = {
            value: sum(1 for outcome in outcomes if outcome == value)
            for value in classes
        }
        best = max(classes, key=lambda value: (scores[value], -value))
        self.candidates = tuple(classes)
        self.chosen = best
        return [best] * len(panel)


__all__ = [
    "AlwaysPredict",
    "BestNaiveClassifier",
    "BiasCorrectedPersistence",
    "ConstantRegressor",
    "Persistence",
    "Predictor",
    "TrainMeanRegressor",
]
