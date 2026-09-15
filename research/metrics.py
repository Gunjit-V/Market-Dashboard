"""Metrics, each carrying the uncertainty that makes it readable.

A bare number is the problem this module exists to solve. "61.2% accurate"
and "RMSE 0.043" are both compatible with a worthless model, and Phase 2
produced several of each. So nothing here returns a float on its own:

* an accuracy comes with an interval and a row count, because a 61% accuracy
  over 50 rows and one over 50,000 rows are different claims;
* a correlation comes with an interval and may be **undefined** -- a constant
  predictor has no ranking, and saying its correlation is zero would be an
  invention;
* the sample-size arithmetic of the Phase 3 brief (S4.5) is a function here,
  so "what edge could this window even detect?" is answerable rather than
  assumed.

Everything is standard library. ``statistics.NormalDist`` supplies the normal
quantiles and ``math.lgamma`` the binomial tail, so every number in this file
can be checked by hand, and the tests do exactly that. Nothing here reads a
database, a network or a clock.

Two metrics, never one
----------------------
:func:`regression_metrics` returns RMSE *and* Pearson correlation together
because S4.2 of the brief is not advisory: on single sessions in Phase 2 a
volatility model beat persistence on RMSE by 53-67% while its correlation was
negative. It won by shrinking toward a sane level, not by ranking turbulent
periods above calm ones. Returning one without the other makes that
indistinguishable from a real result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from research.errors import MetricError

#: Interval width used everywhere unless a caller says otherwise.
DEFAULT_CONFIDENCE = 0.95

#: Significance level for the power arithmetic, two-sided.
DEFAULT_ALPHA = 0.05

#: Conventional power target. 80% means a genuine edge of the stated size is
#: missed one time in five, which is worth remembering before reading a single
#: negative result as settled.
DEFAULT_POWER = 0.80

_NORMAL = None


def _normal():
    """The standard normal, imported lazily to keep import cost off the path."""
    global _NORMAL
    if _NORMAL is None:
        from statistics import NormalDist

        _NORMAL = NormalDist()
    return _NORMAL


def z_for_confidence(confidence: float = DEFAULT_CONFIDENCE) -> float:
    """The two-sided normal quantile for *confidence*, e.g. 1.96 at 95%."""
    if not 0.0 < confidence < 1.0:
        raise MetricError(f"confidence must be in (0, 1), got {confidence!r}")
    return _normal().inv_cdf(0.5 + confidence / 2.0)


def _check_pair(y_true: Sequence[float], y_pred: Sequence[float]) -> int:
    if len(y_true) != len(y_pred):
        raise MetricError(
            f"length mismatch: {len(y_true)} true values, {len(y_pred)} predicted"
        )
    if not y_true:
        raise MetricError("no rows to score")
    return len(y_true)


@dataclass(frozen=True)
class Interval:
    """A confidence interval, and what it does or does not contain."""

    low: float
    high: float
    confidence: float = DEFAULT_CONFIDENCE

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise MetricError(f"interval is inverted: [{self.low}, {self.high}]")

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high

    def excludes(self, value: float) -> bool:
        return not self.contains(value)

    def is_above(self, value: float) -> bool:
        """The whole interval sits above *value* -- the only honest "better"."""
        return self.low > value

    def is_below(self, value: float) -> bool:
        return self.high < value

    @property
    def width(self) -> float:
        return self.high - self.low

    def __str__(self) -> str:
        return f"[{self.low:.4f}, {self.high:.4f}]"


def wilson_interval(
    correct: int,
    n: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Interval:
    """A confidence interval for a proportion, Wilson's form.

    Wilson rather than the textbook ``p +/- z*sqrt(pq/n)`` because the latter
    misbehaves exactly where this harness is most at risk of being believed:
    small windows and rates near 0 or 1, where it produces bounds outside
    [0, 1] and covers less than it claims. Wilson stays inside the unit
    interval at every n and is what the acceptance test checks by hand.
    """
    if n <= 0:
        raise MetricError("cannot form an interval over zero rows")
    if not 0 <= correct <= n:
        raise MetricError(f"correct={correct} is not within 0..{n}")
    z = z_for_confidence(confidence)
    p = correct / n
    denominator = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = (z / denominator) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return Interval(max(0.0, centre - half), min(1.0, centre + half), confidence)


@dataclass(frozen=True)
class Accuracy:
    """A classification score that cannot be quoted without its uncertainty."""

    n: int
    correct: int
    rate: float
    interval: Interval

    def beats(self, baseline_rate: float) -> bool:
        """True only when the whole interval sits above *baseline_rate*.

        Deliberately strict. A point estimate above the baseline is the normal
        appearance of noise: at n=50 a worthless model scores 55% or better a
        quarter of the time.
        """
        return self.interval.is_above(baseline_rate)

    def loses_to(self, baseline_rate: float) -> bool:
        return self.interval.is_below(baseline_rate)

    def indistinguishable_from(self, baseline_rate: float) -> bool:
        return self.interval.contains(baseline_rate)

    def __str__(self) -> str:
        return f"{self.rate:.4f} {self.interval} (n={self.n})"


def accuracy(
    y_true: Sequence[float],
    y_pred: Sequence[float],
    confidence: float = DEFAULT_CONFIDENCE,
) -> Accuracy:
    """Exact-match accuracy over class labels, with its Wilson interval.

    Comparison is exact rather than tolerant because the direction label is
    three-valued and integral (-1, 0, +1); a predictor that emits 0.999 has
    not predicted a class and should not be scored as though it had.
    """
    n = _check_pair(y_true, y_pred)
    correct = sum(1 for t, p in zip(y_true, y_pred) if float(t) == float(p))
    return Accuracy(
        n=n,
        correct=correct,
        rate=correct / n,
        interval=wilson_interval(correct, n, confidence),
    )


def _dispersion(values: Sequence[float], mean: float) -> float:
    """Sum of squared deviations, zero when the series really is constant.

    The test is ``min == max`` rather than ``variance == 0`` because the second
    is not true of a constant series in floating point: sixty copies of 0.12
    have a mean of 0.12000000000000002, deviations of -2.8e-17, and a variance
    of 5e-34 -- small, positive, and enough to produce a correlation of -2e-15
    complete with a confidence interval. That number is exactly the
    plausible-looking nothing the Phase 2 status model was built to keep out of
    a dataset, and it arrives here by a different door.
    """
    if min(values) == max(values):
        return 0.0
    return sum((value - mean) ** 2 for value in values)


@dataclass(frozen=True)
class Correlation:
    """Pearson's r, its interval, and -- when it has none -- why.

    ``r is None`` is a real outcome, not a failure. A constant predictor has no
    ordering to compare, so its rank agreement with anything is undefined.
    Returning 0.0 there would make "this predictor ranks nothing" and "this
    predictor ranks badly" the same number, which is the mistake the Phase 2
    feature-status model exists to prevent.
    """

    n: int
    r: float | None
    interval: Interval | None = None
    p_value: float | None = None
    detail: str | None = None

    @property
    def is_defined(self) -> bool:
        return self.r is not None

    def __str__(self) -> str:
        if self.r is None:
            return f"undefined ({self.detail})"
        interval = f" {self.interval}" if self.interval else ""
        return f"{self.r:+.4f}{interval} (n={self.n})"


def pearson(
    y_true: Sequence[float],
    y_pred: Sequence[float],
    confidence: float = DEFAULT_CONFIDENCE,
) -> Correlation:
    """Correlation between predictions and outcomes, with a Fisher-z interval.

    This is the *ranking* question: does the predictor order calm periods below
    turbulent ones? It is independent of level, and in particular multiplying
    every prediction by a positive constant cannot change it -- which is why
    bias-corrected persistence exists as a separate baseline for error and
    scores identically here (see ``research/baselines.py``).
    """
    n = _check_pair(y_true, y_pred)
    if n < 3:
        return Correlation(n=n, r=None, detail=f"needs at least 3 rows, got {n}")

    mean_true = sum(y_true) / n
    mean_pred = sum(y_pred) / n
    cov = sum((t - mean_true) * (p - mean_pred) for t, p in zip(y_true, y_pred))
    var_true = _dispersion(y_true, mean_true)
    var_pred = _dispersion(y_pred, mean_pred)

    if var_pred == 0.0 and var_true == 0.0:
        return Correlation(n=n, r=None, detail="both series are constant")
    if var_pred == 0.0:
        return Correlation(n=n, r=None, detail="the predictions are constant")
    if var_true == 0.0:
        return Correlation(n=n, r=None, detail="the outcomes are constant")

    r = cov / math.sqrt(var_true * var_pred)
    # Floating-point error can push a perfect correlation just outside [-1, 1],
    # and atanh would then raise rather than report the perfect fit.
    r = max(-1.0, min(1.0, r))

    if n < 4 or abs(r) == 1.0:
        # Fisher's transform needs n > 3, and is infinite at |r| = 1.
        return Correlation(n=n, r=r, detail="interval needs n > 3 and |r| < 1")

    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    half = z_for_confidence(confidence) * se
    interval = Interval(math.tanh(z - half), math.tanh(z + half), confidence)
    p_value = 2.0 * (1.0 - _normal().cdf(abs(z) / se))
    return Correlation(n=n, r=r, interval=interval, p_value=p_value)


def rmse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Root mean squared error -- the *level* question."""
    n = _check_pair(y_true, y_pred)
    return math.sqrt(sum((t - p) ** 2 for t, p in zip(y_true, y_pred)) / n)


def mean_error(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Signed bias, ``mean(pred - true)``.

    Reported alongside RMSE because it is what separates the two ways of
    beating a baseline: a predictor can improve RMSE purely by removing a
    constant offset, which changes nothing about its ranking.
    """
    n = _check_pair(y_true, y_pred)
    return sum(p - t for t, p in zip(y_true, y_pred)) / n


@dataclass(frozen=True)
class RegressionMetrics:
    """Error and ranking together, never one without the other."""

    n: int
    rmse: float
    correlation: Correlation
    mean_error: float

    def __str__(self) -> str:
        return (
            f"rmse={self.rmse:.6f} r={self.correlation} bias={self.mean_error:+.6f}"
        )


def regression_metrics(
    y_true: Sequence[float],
    y_pred: Sequence[float],
    confidence: float = DEFAULT_CONFIDENCE,
) -> RegressionMetrics:
    """Both metrics, computed once, returned as one object."""
    n = _check_pair(y_true, y_pred)
    return RegressionMetrics(
        n=n,
        rmse=rmse(y_true, y_pred),
        correlation=pearson(y_true, y_pred, confidence),
        mean_error=mean_error(y_true, y_pred),
    )


# --------------------------------------------------------------------------
# Sample-size arithmetic -- brief S4.5
# --------------------------------------------------------------------------


def rows_needed(
    edge: float,
    baseline: float = 0.5,
    power: float = DEFAULT_POWER,
    alpha: float = DEFAULT_ALPHA,
) -> int:
    """Rows required to detect *edge* over *baseline* at *power*.

    The standard normal approximation for one proportion against a fixed
    reference::

        n = (z(1 - alpha/2) + z(power))^2 * p(1 - p) / edge^2

    With p = 0.5 and the usual 5%/80% this gives 19,623 rows for a
    one-percentage-point edge -- about 261 five-minute sessions.

    The Phase 3 brief's table quotes 782 / 4,898 / 19,598 / 78,398 rows for
    edges of 5 / 2 / 1 / 0.5 percentage points; this returns 785 / 4,906 /
    19,623 / 78,489. The brief's figures are what the same formula gives with
    the quantiles rounded to 1.96 and 0.84, less two rows each. The gap is
    under 0.4% and changes no decision the number is used for, so the exact
    quantiles are kept here rather than reproducing the rounding.
    """
    if not 0.0 < edge < 1.0:
        raise MetricError(f"edge must be in (0, 1), got {edge!r}")
    if not 0.0 < baseline < 1.0:
        raise MetricError(f"baseline must be in (0, 1), got {baseline!r}")
    z_alpha = z_for_confidence(1.0 - alpha)
    z_power = _normal().inv_cdf(power)
    return math.ceil((z_alpha + z_power) ** 2 * baseline * (1 - baseline) / edge**2)


def detectable_edge(
    n: int,
    baseline: float = 0.5,
    power: float = DEFAULT_POWER,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """The smallest edge *n* rows could detect -- :func:`rows_needed` inverted.

    Every accuracy claim in Phase 3 is required to state this (brief S4.5), so
    that "the model did not beat the baseline" can be read as either "there is
    no edge" or "this window could never have seen one".
    """
    if n <= 0:
        raise MetricError("cannot compute a detectable edge over zero rows")
    z_alpha = z_for_confidence(1.0 - alpha)
    z_power = _normal().inv_cdf(power)
    return (z_alpha + z_power) * math.sqrt(baseline * (1 - baseline) / n)


def _log_choose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


#: Above this, the exact binomial sum costs more than it is worth here.
_MAX_EXACT_N = 200_000


def binomial_at_least(n: int, p: float, k: int) -> float:
    """``P(X >= k)`` for ``X ~ Binomial(n, p)``, summed exactly in log space.

    Exact rather than normal-approximated because the numbers it is used for
    are small-n statements about how often noise looks like a result, and the
    approximation is at its worst there.
    """
    if n <= 0:
        raise MetricError("n must be positive")
    if n > _MAX_EXACT_N:
        raise MetricError(f"n={n} is beyond the exact tail's range")
    if not 0.0 <= p <= 1.0:
        raise MetricError(f"p must be in [0, 1], got {p!r}")
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p == 0.0:
        return 0.0
    if p == 1.0:
        return 1.0
    log_p, log_q = math.log(p), math.log1p(-p)
    # Sum the shorter tail: the terms of the other one underflow to zero and
    # would lose the small probabilities this function exists to report.
    if k - 1 < n - k + 1:
        below = sum(
            math.exp(_log_choose(n, i) + i * log_p + (n - i) * log_q)
            for i in range(0, k)
        )
        return max(0.0, min(1.0, 1.0 - below))
    total = sum(
        math.exp(_log_choose(n, i) + i * log_p + (n - i) * log_q)
        for i in range(k, n + 1)
    )
    return max(0.0, min(1.0, total))


def probability_of_scoring(true_rate: float, n: int, threshold: float) -> float:
    """How often a predictor with edge *true_rate* scores at least *threshold*.

    Two readings of the same function, both quoted in the brief:

    * ``probability_of_scoring(0.50, 50, 0.55)`` -- a worthless model scores 55%
      or better roughly a quarter of the time over 50 rows;
    * ``1 - probability_of_scoring(0.51, 50, 0.50)`` -- a genuine 51% edge looks
      like a loss about 39% of the time over the same 50 rows.
    """
    if n <= 0:
        raise MetricError("n must be positive")
    if not 0.0 <= threshold <= 1.0:
        raise MetricError(f"threshold must be in [0, 1], got {threshold!r}")
    return binomial_at_least(n, true_rate, math.ceil(threshold * n))


# --------------------------------------------------------------------------
# Multiplicity -- brief S4.6
# --------------------------------------------------------------------------


def family_error_rate(alpha: float, tests: int) -> float:
    """``P(at least one false positive)`` across *tests* independent tests.

    The brief's 26% comes from here. The 9 registered labels are not 9
    independent questions: ``fwd_dir_h`` is the sign of ``fwd_ret_h``, so the
    family holds 6 independent quantities, and ``family_error_rate(0.05, 6)``
    is 0.265. Counting all 9 would give 0.370 -- the number is sensitive to
    how independence is counted, which is why the ledger in
    ``research/report.py`` asks for that count explicitly.
    """
    if not 0.0 < alpha < 1.0:
        raise MetricError(f"alpha must be in (0, 1), got {alpha!r}")
    if tests < 1:
        raise MetricError("a family holds at least one test")
    return 1.0 - (1.0 - alpha) ** tests


def bonferroni_alpha(alpha: float, tests: int) -> float:
    """*alpha* split evenly across *tests* -- conservative and unarguable."""
    if not 0.0 < alpha < 1.0:
        raise MetricError(f"alpha must be in (0, 1), got {alpha!r}")
    if tests < 1:
        raise MetricError("a family holds at least one test")
    return alpha / tests


def sidak_alpha(alpha: float, tests: int) -> float:
    """The per-test level that holds the family rate at exactly *alpha*."""
    if not 0.0 < alpha < 1.0:
        raise MetricError(f"alpha must be in (0, 1), got {alpha!r}")
    if tests < 1:
        raise MetricError("a family holds at least one test")
    return 1.0 - (1.0 - alpha) ** (1.0 / tests)


__all__ = [
    "Accuracy",
    "Correlation",
    "DEFAULT_ALPHA",
    "DEFAULT_CONFIDENCE",
    "DEFAULT_POWER",
    "Interval",
    "RegressionMetrics",
    "accuracy",
    "binomial_at_least",
    "bonferroni_alpha",
    "detectable_edge",
    "family_error_rate",
    "mean_error",
    "pearson",
    "probability_of_scoring",
    "regression_metrics",
    "rmse",
    "rows_needed",
    "sidak_alpha",
    "wilson_interval",
    "z_for_confidence",
]
