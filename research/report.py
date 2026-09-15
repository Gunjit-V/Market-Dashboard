"""Results that cannot be presented without the comparison that reads them.

Brief S11: *report the baseline first*. Every impressive number in Phase 2's
exploratory work -- a 22-point direction edge, a 67% RMSE improvement --
evaporated under a correct baseline or a second metric. The objects here are
built so that the evaporation happens before the number is written down rather
than after.

Three refusals are structural:

**A comparison without a baseline does not exist.** :class:`Comparison`
raises if constructed with none, so "the model scored 0.61" has nowhere to live
in this package unless the trivial answer is beside it.

**A mixed result is never a win.** When a model improves RMSE and degrades
correlation, the verdict is ``mixed``, and it is reported that way in the same
sentence as the numbers (brief S4.2). A single summary number for regression
was rejected for exactly this reason.

**A single window is not evidence.** :class:`WalkForwardReport` reports how
many windows the model won, not just an average over them (brief S4.4), the
edge the windows were large enough to detect (S4.5) and how many hypotheses
the family holds (S4.6).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from research.errors import ReportError
from research.metrics import (
    Accuracy,
    DEFAULT_ALPHA,
    RegressionMetrics,
    bonferroni_alpha,
    detectable_edge,
    family_error_rate,
)
from research.panel import CLASSIFICATION, REGRESSION
from research.splits import Split

#: Verdict vocabulary. ``mixed`` exists so that error and ranking cannot be
#: collapsed into one word; ``inconclusive`` so that "not distinguishable from
#: the trivial answer" is not written down as a loss, which would overstate the
#: evidence in the other direction.
BEATS = "beats"
LOSES = "loses"
MIXED = "mixed"
TIES = "ties"
INCONCLUSIVE = "inconclusive"

#: Two metric values closer than this are the same number, and the verdict is
#: ``ties``. Without it, arithmetic that provably cannot change a metric still
#: moves it in the sixteenth digit -- rescaling persistence by a positive
#: constant leaves Pearson's r identical in principle and differing by 1e-16 in
#: floating point, and a bare ``<`` would report that as the model losing on
#: ranking. A real difference between two forecasts is many orders of magnitude
#: larger than this.
METRIC_TOLERANCE = 1e-12


def _indistinguishable(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=METRIC_TOLERANCE, abs_tol=1e-15)


@dataclass(frozen=True)
class WindowInfo:
    """Which rows a result was computed over."""

    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    train_rows: int
    test_rows: int
    gap_sessions: int

    @classmethod
    def from_split(cls, split: Split, train_rows: int, test_rows: int) -> "WindowInfo":
        return cls(
            index=split.index,
            train_start=split.train[0],
            train_end=split.train[-1],
            test_start=split.test[0],
            test_end=split.test[-1],
            train_rows=train_rows,
            test_rows=test_rows,
            gap_sessions=split.gap_sessions,
        )

    def describe(self) -> dict[str, object]:
        return {
            "index": self.index,
            "train": [self.train_start.isoformat(), self.train_end.isoformat()],
            "test": [self.test_start.isoformat(), self.test_end.isoformat()],
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "gap_sessions": self.gap_sessions,
        }


@dataclass(frozen=True)
class Result:
    """One predictor, scored on one window.

    Exactly one of *accuracy* and *regression* is set, decided by the label's
    kind. A classification result carries no RMSE and a regression result
    carries no accuracy, rather than carrying a zero.
    """

    predictor: str
    kind: str
    is_baseline: bool
    window: WindowInfo
    accuracy: Accuracy | None = None
    regression: RegressionMetrics | None = None
    params: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind == CLASSIFICATION and self.accuracy is None:
            raise ReportError(
                f"{self.predictor}: a classification result needs an accuracy"
            )
        if self.kind == REGRESSION and self.regression is None:
            raise ReportError(
                f"{self.predictor}: a regression result needs error and ranking"
            )
        if self.accuracy is not None and self.regression is not None:
            raise ReportError(
                f"{self.predictor}: a result is one kind or the other, not both"
            )

    @property
    def n(self) -> int:
        return self.window.test_rows

    def headline(self) -> str:
        if self.accuracy is not None:
            return f"{self.accuracy}"
        return f"{self.regression}"

    def describe(self) -> dict[str, object]:
        out: dict[str, object] = {
            "predictor": self.predictor,
            "kind": self.kind,
            "is_baseline": self.is_baseline,
            "window": self.window.describe(),
            "params": dict(self.params),
        }
        if self.accuracy is not None:
            out["accuracy"] = {
                "n": self.accuracy.n,
                "correct": self.accuracy.correct,
                "rate": self.accuracy.rate,
                "ci": [self.accuracy.interval.low, self.accuracy.interval.high],
                "confidence": self.accuracy.interval.confidence,
            }
        if self.regression is not None:
            correlation = self.regression.correlation
            out["regression"] = {
                "n": self.regression.n,
                "rmse": self.regression.rmse,
                "mean_error": self.regression.mean_error,
                "r": correlation.r,
                "r_ci": (
                    None
                    if correlation.interval is None
                    else [correlation.interval.low, correlation.interval.high]
                ),
                "r_p_value": correlation.p_value,
                "r_detail": correlation.detail,
            }
        return out


@dataclass(frozen=True)
class Comparison:
    """A model's result on one window, with the baselines it must beat.

    The constructor is the enforcement point: no baselines, no comparison.
    """

    model: Result
    baselines: tuple[Result, ...]

    def __post_init__(self) -> None:
        if not self.baselines:
            raise ReportError(
                f"{self.model.predictor} on window {self.model.window.index}: a "
                f"result needs at least one baseline beside it. Brief S4.1 -- "
                f"the comparison is the result."
            )
        for baseline in self.baselines:
            if not baseline.is_baseline:
                raise ReportError(
                    f"{baseline.predictor} is not a baseline and cannot stand in "
                    f"for one"
                )
            if baseline.kind != self.model.kind:
                raise ReportError(
                    f"{baseline.predictor} is a {baseline.kind} baseline for a "
                    f"{self.model.kind} model"
                )
            if baseline.window != self.model.window:
                raise ReportError(
                    f"{baseline.predictor} was scored on a different window than "
                    f"{self.model.predictor}"
                )

    # -- classification ---------------------------------------------------

    @property
    def best_baseline_rate(self) -> float:
        """The hardest accuracy to beat among the baselines."""
        rates = [b.accuracy.rate for b in self.baselines if b.accuracy is not None]
        if not rates:
            raise ReportError("no classification baseline to compare against")
        return max(rates)

    # -- regression -------------------------------------------------------

    @property
    def best_baseline_rmse(self) -> float:
        errors = [b.regression.rmse for b in self.baselines if b.regression]
        if not errors:
            raise ReportError("no regression baseline to compare against")
        return min(errors)

    @property
    def best_baseline_r(self) -> float | None:
        """The best baseline correlation, or ``None`` if none is defined.

        A constant baseline has no ranking at all, so there is nothing to beat
        on that axis -- reported as such rather than treated as zero, which
        would hand the model a win it did not earn.
        """
        defined = [
            b.regression.correlation.r
            for b in self.baselines
            if b.regression and b.regression.correlation.is_defined
        ]
        return max(defined) if defined else None

    # -- verdicts ---------------------------------------------------------

    @property
    def error_verdict(self) -> str:
        if self.model.regression is None:
            raise ReportError("error verdict asked of a classification comparison")
        model_rmse = self.model.regression.rmse
        best = self.best_baseline_rmse
        if _indistinguishable(model_rmse, best):
            return TIES
        return BEATS if model_rmse < best else LOSES

    @property
    def ranking_verdict(self) -> str:
        if self.model.regression is None:
            raise ReportError("ranking verdict asked of a classification comparison")
        model_r = self.model.regression.correlation.r
        best = self.best_baseline_r
        if model_r is None:
            return INCONCLUSIVE
        if best is None:
            # No baseline ranks anything. The model's own interval against zero
            # is then the only available statement.
            interval = self.model.regression.correlation.interval
            if interval is None:
                return INCONCLUSIVE
            return BEATS if interval.is_above(0.0) else INCONCLUSIVE
        if _indistinguishable(model_r, best):
            return TIES
        return BEATS if model_r > best else LOSES

    @property
    def verdict(self) -> str:
        """One word, and never a flattering one.

        Regression: ``beats`` only when the model wins on error *and* ranking
        (or wins one and exactly ties the other). Any disagreement between the
        axes, including an axis that cannot be judged, is ``mixed`` -- brief
        S4.2, which exists because a Phase 2 volatility model beat persistence
        on RMSE by 53-67% while ranking the periods backwards.

        Classification: ``beats`` only when the whole confidence interval sits
        above the best naive rate. A point estimate above it is the ordinary
        appearance of noise at these sample sizes.
        """
        if self.model.kind == CLASSIFICATION:
            accuracy = self.model.accuracy
            rate = self.best_baseline_rate
            if accuracy.beats(rate):
                return BEATS
            if accuracy.rate < rate:
                return LOSES
            return INCONCLUSIVE

        axes = {self.error_verdict, self.ranking_verdict}
        if axes <= {BEATS, TIES} and BEATS in axes:
            return BEATS
        if axes <= {LOSES, TIES} and LOSES in axes:
            return LOSES
        if axes == {TIES}:
            return TIES
        if axes == {INCONCLUSIVE}:
            return INCONCLUSIVE
        # The axes disagree -- one improved and the other degraded, or one of
        # them cannot be judged at all. A constant predictor that beats
        # persistence on RMSE while having no ranking lands here, which is the
        # Phase 2 result that looked like a 67% improvement and was not one.
        return MIXED

    def render(self) -> str:
        """The baseline first, then the model, then the verdict."""
        lines = [f"window #{self.model.window.index} "
                 f"{self.model.window.test_start}..{self.model.window.test_end} "
                 f"(n={self.model.n})"]
        for baseline in self.baselines:
            lines.append(f"  baseline  {baseline.predictor:<38} {baseline.headline()}")
        lines.append(f"  model     {self.model.predictor:<38} {self.model.headline()}")
        if self.model.kind == REGRESSION:
            lines.append(
                f"  verdict   {self.verdict} (error {self.error_verdict}, "
                f"ranking {self.ranking_verdict})"
            )
        else:
            lines.append(
                f"  verdict   {self.verdict} against a best naive of "
                f"{self.best_baseline_rate:.4f}"
            )
        return "\n".join(lines)

    def describe(self) -> dict[str, object]:
        out = {
            "model": self.model.describe(),
            "baselines": [b.describe() for b in self.baselines],
            "verdict": self.verdict,
        }
        if self.model.kind == REGRESSION:
            out["error_verdict"] = self.error_verdict
            out["ranking_verdict"] = self.ranking_verdict
            out["best_baseline_rmse"] = self.best_baseline_rmse
            out["best_baseline_r"] = self.best_baseline_r
        else:
            out["best_baseline_rate"] = self.best_baseline_rate
        return out


@dataclass(frozen=True)
class HypothesisLedger:
    """How many questions were asked, and which one was asked first.

    Brief S4.6. Testing nine labels with no signal at all produces at least one
    "significant" result about a quarter of the time, and that does not improve
    with more data -- it is a property of how many things were tested.

    *independent* is asked for separately because the nine registered labels are
    not nine independent questions: ``fwd_dir_h`` is the sign of ``fwd_ret_h``,
    leaving six independent quantities and a family error rate of 0.265 rather
    than 0.370.

    Either declare a *primary* hypothesis in advance, in which case its
    threshold stays at *alpha*, or accept the Bonferroni-adjusted one.
    """

    tested: tuple[str, ...]
    primary: str | None = None
    alpha: float = DEFAULT_ALPHA
    independent: int | None = None

    def __post_init__(self) -> None:
        if not self.tested:
            raise ReportError("a ledger records at least one hypothesis")
        if len(set(self.tested)) != len(self.tested):
            raise ReportError("a hypothesis is listed twice in the ledger")
        if self.primary is not None and self.primary not in self.tested:
            raise ReportError(
                f"the primary hypothesis {self.primary!r} is not among the "
                f"{len(self.tested)} tested"
            )
        if self.independent is not None and not 1 <= self.independent <= len(
            self.tested
        ):
            raise ReportError(
                f"independent={self.independent} must be between 1 and "
                f"{len(self.tested)}"
            )

    @property
    def count(self) -> int:
        return len(self.tested)

    @property
    def effective_tests(self) -> int:
        return self.independent if self.independent is not None else self.count

    @property
    def threshold(self) -> float:
        """The level a result in this family must clear."""
        if self.primary is not None:
            return self.alpha
        return bonferroni_alpha(self.alpha, self.effective_tests)

    @property
    def family_error_rate(self) -> float:
        return family_error_rate(self.alpha, self.effective_tests)

    def render(self) -> str:
        if self.primary is not None:
            rule = (
                f"primary hypothesis pre-registered ({self.primary}); "
                f"threshold {self.threshold:.4f}"
            )
        else:
            rule = (
                f"no primary pre-registered; Bonferroni threshold "
                f"{self.threshold:.4f}"
            )
        return (
            f"hypotheses: {self.count} tested, {self.effective_tests} independent; "
            f"at alpha={self.alpha:g} one would look significant "
            f"{self.family_error_rate:.1%} of the time with no signal. {rule}"
        )

    def describe(self) -> dict[str, object]:
        return {
            "tested": list(self.tested),
            "count": self.count,
            "primary": self.primary,
            "alpha": self.alpha,
            "independent": self.effective_tests,
            "threshold": self.threshold,
            "family_error_rate": self.family_error_rate,
        }


@dataclass(frozen=True)
class WalkForwardReport:
    """Every window of one study, and what they agree on.

    The consistency figures are the deliverable, not the average: brief S4.4
    asks how many windows the model won, because an edge that shows up in one
    window and nowhere else is noise wearing a result's clothes.
    """

    instrument: str
    timeframe: str
    label: str
    model: str
    comparisons: tuple[Comparison, ...]
    hypotheses: HypothesisLedger

    def __post_init__(self) -> None:
        if not self.comparisons:
            raise ReportError("a walk-forward report needs at least one window")
        kinds = {c.model.kind for c in self.comparisons}
        if len(kinds) != 1:
            raise ReportError(f"windows disagree about the label's kind: {kinds}")

    @property
    def kind(self) -> str:
        return self.comparisons[0].model.kind

    @property
    def windows(self) -> int:
        return len(self.comparisons)

    @property
    def rows(self) -> int:
        return sum(c.model.n for c in self.comparisons)

    @property
    def smallest_window(self) -> int:
        return min(c.model.n for c in self.comparisons)

    def verdicts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for comparison in self.comparisons:
            counts[comparison.verdict] = counts.get(comparison.verdict, 0) + 1
        return counts

    def consistency(self) -> dict[str, object]:
        """Windows won, per axis -- never collapsed into one number."""
        out: dict[str, object] = {
            "windows": self.windows,
            "verdicts": self.verdicts(),
        }
        if self.kind == REGRESSION:
            out["won_on_error"] = sum(
                1 for c in self.comparisons if c.error_verdict == BEATS
            )
            out["won_on_ranking"] = sum(
                1 for c in self.comparisons if c.ranking_verdict == BEATS
            )
            # "overall" rather than "both": a window the model wins on error
            # and exactly ties on ranking counts as a win, and calling that
            # "won on both" would misdescribe a zero in the ranking column.
            out["won_overall"] = sum(1 for c in self.comparisons if c.verdict == BEATS)
        else:
            out["beat_best_naive"] = sum(
                1 for c in self.comparisons if c.verdict == BEATS
            )
            out["lost_to_best_naive"] = sum(
                1 for c in self.comparisons if c.verdict == LOSES
            )
        return out

    def power_statement(self) -> str:
        """What the smallest window could have detected, in the brief's terms."""
        if self.kind == CLASSIFICATION:
            # Computed at a 50% reference rate, where the variance of a
            # proportion is largest. Against a naive rate further from a half
            # the detectable edge is smaller, so this is the conservative
            # statement rather than the flattering one.
            edge = detectable_edge(self.smallest_window)
            return (
                f"power: the smallest window holds {self.smallest_window} rows, "
                f"which at 80% power can detect an edge of {edge * 100:.2f}pp "
                f"over a 50% reference rate; anything smaller is invisible here"
            )
        return (
            f"power: {self.rows} rows across {self.windows} windows, smallest "
            f"{self.smallest_window}; correlation intervals above are the "
            f"per-window statement of what that supports"
        )

    def render(self) -> str:
        """The whole study as text: baselines first, per window, then totals."""
        header = (
            f"{self.model} on {self.label} "
            f"({self.instrument}, {self.timeframe}) -- {self.windows} "
            f"walk-forward windows, {self.rows} test rows"
        )
        lines = [header, "=" * len(header), ""]
        for comparison in self.comparisons:
            lines.append(comparison.render())
            lines.append("")
        consistency = self.consistency()
        lines.append("consistency")
        for key, value in consistency.items():
            lines.append(f"  {key}: {value}")
        lines.append("")
        lines.append(self.power_statement())
        lines.append(self.hypotheses.render())
        return "\n".join(lines)

    def describe(self) -> dict[str, object]:
        return {
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "label": self.label,
            "model": self.model,
            "kind": self.kind,
            "windows": self.windows,
            "rows": self.rows,
            "comparisons": [c.describe() for c in self.comparisons],
            "consistency": self.consistency(),
            "power": self.power_statement(),
            "hypotheses": self.hypotheses.describe(),
        }


__all__ = [
    "BEATS",
    "METRIC_TOLERANCE",
    "Comparison",
    "HypothesisLedger",
    "INCONCLUSIVE",
    "LOSES",
    "MIXED",
    "Result",
    "TIES",
    "WalkForwardReport",
    "WindowInfo",
]
