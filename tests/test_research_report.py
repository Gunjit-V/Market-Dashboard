"""Reports that refuse to flatter.

Three refusals are the subject here: a result without a baseline, a mixed
regression result summarised as a win, and a family of hypotheses reported
without saying how many were asked.
"""

from __future__ import annotations

from datetime import date

import pytest

from research.errors import ReportError
from research.metrics import (
    Correlation,
    accuracy,
    regression_metrics,
)
from research.panel import CLASSIFICATION, REGRESSION
from research.report import (
    BEATS,
    Comparison,
    HypothesisLedger,
    INCONCLUSIVE,
    LOSES,
    MIXED,
    Result,
    TIES,
    WalkForwardReport,
    WindowInfo,
)

WINDOW = WindowInfo(
    index=0,
    train_start=date(2026, 3, 2),
    train_end=date(2026, 3, 20),
    test_start=date(2026, 3, 24),
    test_end=date(2026, 3, 27),
    train_rows=1500,
    test_rows=375,
    gap_sessions=1,
)

TRUTH = [0.10, 0.20, 0.30, 0.40, 0.50]


def regression_result(name, predicted, is_baseline=False, window=WINDOW):
    return Result(
        predictor=name,
        kind=REGRESSION,
        is_baseline=is_baseline,
        window=window,
        regression=regression_metrics(TRUTH, predicted),
    )


def classification_result(name, rate, n=1000, is_baseline=False):
    correct = round(rate * n)
    truth = [1.0] * correct + [-1.0] * (n - correct)
    return Result(
        predictor=name,
        kind=CLASSIFICATION,
        is_baseline=is_baseline,
        window=WINDOW,
        accuracy=accuracy(truth, [1.0] * n),
    )


class TestResult:
    def test_a_regression_result_needs_both_metrics(self):
        with pytest.raises(ReportError) as exc:
            Result(predictor="m", kind=REGRESSION, is_baseline=False, window=WINDOW)
        assert "error and ranking" in str(exc.value)

    def test_a_classification_result_needs_an_accuracy(self):
        with pytest.raises(ReportError):
            Result(predictor="m", kind=CLASSIFICATION, is_baseline=False, window=WINDOW)

    def test_a_result_is_one_kind_or_the_other(self):
        with pytest.raises(ReportError):
            Result(
                predictor="m",
                kind=REGRESSION,
                is_baseline=False,
                window=WINDOW,
                accuracy=accuracy([1.0], [1.0]),
                regression=regression_metrics(TRUTH, TRUTH),
            )

    def test_describes_itself_with_the_interval_intact(self):
        described = regression_result("persistence", TRUTH, is_baseline=True).describe()
        assert described["regression"]["rmse"] == 0.0
        assert described["regression"]["r"] == pytest.approx(1.0)
        assert described["window"]["test_rows"] == 375


class TestComparisonRefusals:
    def test_a_result_without_a_baseline_cannot_be_presented(self):
        # Brief S4.1, made structural.
        with pytest.raises(ReportError) as exc:
            Comparison(model=regression_result("model", TRUTH), baselines=())
        assert "baseline" in str(exc.value)

    def test_a_model_cannot_stand_in_for_a_baseline(self):
        with pytest.raises(ReportError) as exc:
            Comparison(
                model=regression_result("model", TRUTH),
                baselines=(regression_result("another model", TRUTH),),
            )
        assert "not a baseline" in str(exc.value)

    def test_a_baseline_scored_on_another_window_is_refused(self):
        elsewhere = WindowInfo(
            index=1,
            train_start=date(2026, 4, 1),
            train_end=date(2026, 4, 10),
            test_start=date(2026, 4, 14),
            test_end=date(2026, 4, 17),
            train_rows=1500,
            test_rows=375,
            gap_sessions=1,
        )
        with pytest.raises(ReportError) as exc:
            Comparison(
                model=regression_result("model", TRUTH),
                baselines=(
                    regression_result("p", TRUTH, is_baseline=True, window=elsewhere),
                ),
            )
        assert "different window" in str(exc.value)

    def test_a_classification_baseline_cannot_judge_a_regression_model(self):
        with pytest.raises(ReportError):
            Comparison(
                model=regression_result("model", TRUTH),
                baselines=(classification_result("naive", 0.6, is_baseline=True),),
            )


class TestRegressionVerdicts:
    def test_beating_on_both_axes_is_a_win(self):
        model = regression_result("model", [0.11, 0.21, 0.29, 0.41, 0.49])
        baseline = regression_result(
            "persistence", [0.15, 0.24, 0.26, 0.45, 0.44], is_baseline=True
        )
        comparison = Comparison(model=model, baselines=(baseline,))
        assert comparison.error_verdict == BEATS
        assert comparison.ranking_verdict == BEATS
        assert comparison.verdict == BEATS

    def test_better_error_with_worse_ranking_is_mixed_not_a_win(self):
        # The Phase 2 result this rule was written for: a 53-67% RMSE
        # improvement with a negative correlation.
        # Persistence ranks perfectly here and sits a flat 0.25 too high; the
        # model is nearly constant and ordered backwards.
        model = regression_result("model", [0.31, 0.30, 0.30, 0.29, 0.28])
        baseline = regression_result(
            "persistence", [0.35, 0.45, 0.55, 0.65, 0.75], is_baseline=True
        )
        comparison = Comparison(model=model, baselines=(baseline,))
        assert comparison.error_verdict == BEATS
        assert comparison.ranking_verdict == LOSES
        assert comparison.verdict == MIXED
        assert "mixed" in comparison.render()

    def test_a_model_with_no_ranking_never_reads_as_a_win(self):
        # A constant prediction can beat persistence on RMSE outright. It has
        # ordered nothing, so the verdict is mixed and the per-axis words say
        # exactly why.
        model = regression_result("model", [0.30] * 5)
        baseline = regression_result(
            "persistence", [0.35, 0.45, 0.55, 0.65, 0.75], is_baseline=True
        )
        comparison = Comparison(model=model, baselines=(baseline,))
        assert comparison.error_verdict == BEATS
        assert comparison.ranking_verdict == INCONCLUSIVE
        assert comparison.verdict == MIXED

    def test_losing_on_both_axes_is_a_loss(self):
        model = regression_result("model", [0.50, 0.40, 0.30, 0.20, 0.10])
        baseline = regression_result(
            "persistence", [0.11, 0.21, 0.29, 0.41, 0.49], is_baseline=True
        )
        assert Comparison(model=model, baselines=(baseline,)).verdict == LOSES

    def test_identical_predictions_tie(self):
        model = regression_result("model", [0.11, 0.21, 0.29, 0.41, 0.49])
        baseline = regression_result(
            "persistence", [0.11, 0.21, 0.29, 0.41, 0.49], is_baseline=True
        )
        assert Comparison(model=model, baselines=(baseline,)).verdict == TIES

    def test_a_difference_in_the_sixteenth_digit_is_not_a_difference(self):
        # Rescaling a predictor by a positive constant cannot change its
        # correlation, but in floating point it moves it by ~1e-16. Reported
        # as a loss, that would make bias-corrected persistence look worse at
        # ranking than the predictor it is a rescale of.
        predicted = [0.11, 0.21, 0.29, 0.41, 0.49]
        model = regression_result("model", [1.37 * value for value in predicted])
        baseline = regression_result("persistence", predicted, is_baseline=True)
        assert model.regression.correlation.r != baseline.regression.correlation.r
        assert Comparison(model=model, baselines=(baseline,)).ranking_verdict == TIES

    def test_the_hardest_baseline_is_the_one_to_beat(self):
        model = regression_result("model", [0.12, 0.22, 0.28, 0.42, 0.48])
        easy = regression_result("train-mean", [0.30] * 5, is_baseline=True)
        hard = regression_result(
            "persistence", [0.11, 0.21, 0.29, 0.41, 0.49], is_baseline=True
        )
        comparison = Comparison(model=model, baselines=(easy, hard))
        assert comparison.best_baseline_rmse == pytest.approx(hard.regression.rmse)
        assert comparison.best_baseline_r == pytest.approx(hard.regression.correlation.r)
        assert comparison.error_verdict == LOSES

    def test_a_baseline_with_no_ranking_leaves_the_model_against_zero(self):
        model = regression_result("model", [0.11, 0.21, 0.29, 0.41, 0.49])
        constant = regression_result("constant", [0.30] * 5, is_baseline=True)
        comparison = Comparison(model=model, baselines=(constant,))
        assert comparison.best_baseline_r is None
        assert comparison.ranking_verdict == BEATS

    def test_a_correlation_that_cannot_be_judged_is_not_a_win(self):
        model = Result(
            predictor="model",
            kind=REGRESSION,
            is_baseline=False,
            window=WINDOW,
            regression=regression_metrics(TRUTH, [0.30] * 5),
        )
        constant = regression_result("constant", [0.25] * 5, is_baseline=True)
        comparison = Comparison(model=model, baselines=(constant,))
        assert model.regression.correlation == Correlation(
            n=5, r=None, detail="the predictions are constant"
        )
        assert comparison.ranking_verdict == INCONCLUSIVE


class TestClassificationVerdicts:
    def test_beating_requires_the_whole_interval_above_the_naive_rate(self):
        model = classification_result("model", 0.60)
        naive = classification_result("best-naive", 0.50, is_baseline=True)
        comparison = Comparison(model=model, baselines=(naive,))
        assert comparison.best_baseline_rate == pytest.approx(0.50)
        assert comparison.verdict == BEATS

    def test_an_overlapping_interval_is_inconclusive_not_a_win(self):
        model = classification_result("model", 0.53)
        naive = classification_result("best-naive", 0.50, is_baseline=True)
        assert Comparison(model=model, baselines=(naive,)).verdict == INCONCLUSIVE

    def test_losing_to_the_best_naive_answer_is_a_loss(self):
        # The Phase 2 direction result: 59.3% against a best naive of 63.0%.
        model = classification_result("model", 0.593)
        naive = classification_result("best-naive", 0.630, is_baseline=True)
        comparison = Comparison(model=model, baselines=(naive,))
        assert comparison.verdict == LOSES
        assert "0.6300" in comparison.render()

    def test_the_render_puts_the_baseline_first(self):
        model = classification_result("model", 0.593)
        naive = classification_result("best-naive", 0.630, is_baseline=True)
        rendered = Comparison(model=model, baselines=(naive,)).render()
        assert rendered.index("baseline") < rendered.index("model")


class TestHypothesisLedger:
    def test_counts_what_was_tested(self):
        ledger = HypothesisLedger(tested=("fwd_rv_15m", "fwd_rv_30m", "fwd_rv_60m"))
        assert ledger.count == 3
        assert ledger.effective_tests == 3
        assert ledger.threshold == pytest.approx(0.05 / 3)

    def test_a_pre_registered_primary_keeps_the_unadjusted_threshold(self):
        ledger = HypothesisLedger(
            tested=("fwd_rv_15m", "fwd_rv_30m", "fwd_rv_60m"), primary="fwd_rv_60m"
        )
        assert ledger.threshold == pytest.approx(0.05)
        assert "pre-registered" in ledger.render()

    def test_independent_tests_can_be_fewer_than_the_columns_tested(self):
        # Nine labels, six independent quantities: fwd_dir_h is the sign of
        # fwd_ret_h. 1 - 0.95^6 = 0.265, the brief's 26%.
        ledger = HypothesisLedger(
            tested=tuple(f"label-{i}" for i in range(9)), independent=6
        )
        assert ledger.family_error_rate == pytest.approx(0.2649, abs=1e-4)
        assert "6 independent" in ledger.render()

    def test_a_primary_must_be_among_the_tested(self):
        with pytest.raises(ReportError):
            HypothesisLedger(tested=("a", "b"), primary="c")

    def test_an_empty_ledger_is_refused(self):
        with pytest.raises(ReportError):
            HypothesisLedger(tested=())

    def test_a_repeated_hypothesis_is_refused(self):
        with pytest.raises(ReportError):
            HypothesisLedger(tested=("a", "a"))

    def test_more_independent_tests_than_hypotheses_is_refused(self):
        with pytest.raises(ReportError):
            HypothesisLedger(tested=("a", "b"), independent=3)


class TestWalkForwardReport:
    def build(self, verdicts):
        comparisons = []
        for index, wins in enumerate(verdicts):
            window = WindowInfo(
                index=index,
                train_start=date(2026, 3, 2),
                train_end=date(2026, 3, 20),
                test_start=date(2026, 3, 24),
                test_end=date(2026, 3, 27),
                train_rows=1500,
                test_rows=1000,
                gap_sessions=1,
            )
            predicted = (
                [0.11, 0.21, 0.29, 0.41, 0.49] if wins else [0.50, 0.40, 0.30, 0.20, 0.10]
            )
            comparisons.append(
                Comparison(
                    model=regression_result("ridge", predicted, window=window),
                    baselines=(
                        regression_result(
                            "persistence",
                            [0.13, 0.23, 0.27, 0.43, 0.47],
                            is_baseline=True,
                            window=window,
                        ),
                    ),
                )
            )
        return WalkForwardReport(
            instrument="Nifty 50",
            timeframe="5m",
            label="fwd_rv_60m",
            model="ridge",
            comparisons=tuple(comparisons),
            hypotheses=HypothesisLedger(tested=("fwd_rv_60m",), primary="fwd_rv_60m"),
        )

    def test_counts_windows_won_rather_than_averaging_them(self):
        # Brief S4.4: an edge in one window out of five is noise, and an
        # average would hide which it was.
        report = self.build([True, False, False, True, False])
        consistency = report.consistency()
        assert consistency["windows"] == 5
        assert consistency["won_overall"] == 2
        assert consistency["won_on_error"] == 2
        assert consistency["won_on_ranking"] == 2
        assert consistency["verdicts"] == {BEATS: 2, LOSES: 3}

    def test_reports_every_window_not_just_a_summary(self):
        rendered = self.build([True, False, True]).render()
        for index in range(3):
            assert f"window #{index}" in rendered
        assert "consistency" in rendered

    def test_states_the_detectable_effect_size(self):
        # Brief S4.5: every claim states what its window could have seen.
        report = self.build([True, True])
        assert "power" in report.render()
        assert report.smallest_window == 1000

    def test_states_how_many_hypotheses_were_tested(self):
        assert "hypotheses: 1 tested" in self.build([True]).render()

    def test_needs_at_least_one_window(self):
        with pytest.raises(ReportError):
            WalkForwardReport(
                instrument="Nifty 50",
                timeframe="5m",
                label="fwd_rv_60m",
                model="ridge",
                comparisons=(),
                hypotheses=HypothesisLedger(tested=("fwd_rv_60m",)),
            )

    def test_describes_itself_as_data(self):
        described = self.build([True, False]).describe()
        assert described["windows"] == 2
        assert len(described["comparisons"]) == 2
        assert described["hypotheses"]["count"] == 1
        assert described["consistency"]["won_overall"] == 1
