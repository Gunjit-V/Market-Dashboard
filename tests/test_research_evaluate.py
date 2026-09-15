"""The harness end to end, on panels no database was needed to build.

Brief S5 acceptance 1 and 5: given a labelled dataset, the harness produces
baseline scores with confidence intervals, and it does so with no database and
no network. Acceptance 2's other half lives here too -- a split that looks
chronological by date but leaks through the label horizon is caught at the row
level, not at the calendar level.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from research.baselines import (
    AlwaysPredict,
    BestNaiveClassifier,
    BiasCorrectedPersistence,
    ConstantRegressor,
    Persistence,
    Predictor,
    TrainMeanRegressor,
)
from research.errors import LeakageError, ResearchError
from research.evaluate import (
    assert_no_leakage,
    compare,
    evaluate,
    split_panel,
    walk_forward_study,
)
from research.panel import REGRESSION
from research.report import BEATS, HypothesisLedger, LOSES, TIES
from research.splits import Split, chronological_split, walk_forward
from tests.featurelib import trading_days
from tests.researchlib import CARRIER, direction_panel, panel_of, volatility_panel

DAYS = trading_days(date(2026, 3, 2), 30)
LEDGER = HypothesisLedger(tested=("fwd_rv_60m",), primary="fwd_rv_60m")


@pytest.fixture(scope="module")
def panel():
    return volatility_panel(DAYS, per_session=12)


@pytest.fixture(scope="module")
def classes():
    return direction_panel(DAYS, per_session=12, down_fraction=0.65)


class TestSplitApplication:
    def test_rows_are_divided_by_session(self, panel):
        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        assert train.sessions == split.train
        assert test.sessions == split.test
        assert len(train) + len(test) == len(panel) - 12  # the gap session

    def test_an_empty_training_block_is_an_error(self, panel):
        elsewhere = trading_days(date(2027, 1, 4), 12)
        split = Split(train=tuple(elsewhere[:5]), test=tuple(elsewhere[6:10]))
        with pytest.raises(ResearchError) as exc:
            split_panel(panel, split)
        assert "no usable rows" in str(exc.value)

    def test_an_empty_test_block_is_an_error(self, panel):
        later = trading_days(date(2027, 1, 4), 6)
        split = Split(train=tuple(panel.sessions[:5]), test=tuple(later))
        with pytest.raises(ResearchError):
            split_panel(panel, split)


class TestLeakageChecks:
    def test_accepts_a_properly_gapped_window(self, panel):
        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        assert_no_leakage(train, test)

    def test_rejects_training_that_runs_past_the_test_start(self, panel):
        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        with pytest.raises(LeakageError) as exc:
            assert_no_leakage(test, train)
        assert "not chronological" in str(exc.value)

    def test_rejects_a_label_window_that_reaches_into_the_test_block(self):
        # The check the calendar gap cannot make on its own: these two blocks
        # are in the right order, minutes apart, and the training label is not
        # observed until after testing has begun.
        early = [datetime(2026, 3, 2, 9, 20) + timedelta(minutes=5 * i) for i in range(4)]
        late = [datetime(2026, 3, 2, 10, 0) + timedelta(minutes=5 * i) for i in range(4)]
        train = panel_of(early, [0.1] * 4, [0.1] * 4)
        test = panel_of(late, [0.1] * 4, [0.1] * 4)
        with pytest.raises(LeakageError) as exc:
            assert_no_leakage(train, test)
        assert "not observed until" in str(exc.value)

    def test_a_shorter_horizon_clears_the_same_boundary(self):
        early = [datetime(2026, 3, 2, 9, 20) + timedelta(minutes=5 * i) for i in range(4)]
        late = [datetime(2026, 3, 2, 10, 0) + timedelta(minutes=5 * i) for i in range(4)]
        train = panel_of(early, [0.1] * 4, [0.1] * 4, horizon_minutes=15)
        test = panel_of(late, [0.1] * 4, [0.1] * 4, horizon_minutes=15)
        assert_no_leakage(train, test)

    def test_two_different_labels_cannot_be_compared(self):
        early = [datetime(2026, 3, 2, 9, 20) + timedelta(minutes=5 * i) for i in range(4)]
        late = [datetime(2026, 3, 9, 9, 20) + timedelta(minutes=5 * i) for i in range(4)]
        train = panel_of(early, [0.1] * 4, [0.1] * 4, label="fwd_rv_60m")
        test = panel_of(late, [0.1] * 4, [0.1] * 4, label="fwd_rv_30m")
        with pytest.raises(ResearchError):
            assert_no_leakage(train, test)


class TestModelsCannotSeeTheAnswers:
    def test_a_model_is_handed_a_masked_panel(self, panel):
        class Peeker(Predictor):
            kind = REGRESSION

            def _fit(self, train):
                return None

            def _predict(self, window):
                return window.y  # the answers, if it could get them

        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        with pytest.raises(LeakageError):
            evaluate(Peeker("peeker"), train, test, split)

    def test_a_model_declaring_it_reads_test_labels_is_refused(self, panel):
        class Cheat(Predictor):
            kind = REGRESSION
            uses_test_labels = True

            def _fit(self, train):
                return None

            def _predict(self, window):
                return window.y

        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        with pytest.raises(LeakageError) as exc:
            evaluate(Cheat("cheat"), train, test, split)
        assert "not a baseline" in str(exc.value)

    def test_the_best_naive_baseline_may_read_them(self, classes):
        split = chronological_split(classes.sessions, test_sessions=5)
        train, test = split_panel(classes, split)
        result = evaluate(BestNaiveClassifier(), train, test, split)
        assert result.accuracy.n == len(test)
        assert result.params["chosen_class"] in (-1.0, 1.0)


class TestEvaluate:
    def test_a_baseline_is_scored_with_an_interval(self, classes):
        # Brief S5 acceptance 1, in one line.
        split = chronological_split(classes.sessions, test_sessions=5)
        train, test = split_panel(classes, split)
        result = evaluate(BestNaiveClassifier(), train, test, split)
        assert result.accuracy.interval.low < result.accuracy.rate
        assert result.accuracy.rate < result.accuracy.interval.high
        assert result.accuracy.interval.confidence == 0.95

    def test_a_regression_baseline_carries_both_metrics(self, panel):
        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        result = evaluate(Persistence(CARRIER), train, test, split)
        assert result.regression.rmse > 0
        assert result.regression.correlation.is_defined
        assert result.regression.correlation.interval is not None

    def test_a_constant_predictor_is_reported_honestly(self, panel):
        # The brief's own acceptance: the harness must be able to evaluate a
        # constant predictor without inventing a ranking for it.
        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        result = evaluate(ConstantRegressor(0.12), train, test, split)
        assert result.regression.correlation.r is None
        assert "constant" in result.regression.correlation.detail

    def test_the_window_records_what_was_used(self, panel):
        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        result = evaluate(TrainMeanRegressor(), train, test, split)
        assert result.window.train_rows == len(train)
        assert result.window.test_rows == len(test)
        assert result.window.gap_sessions == 1
        assert result.window.test_start == split.test[0]

    def test_the_predictors_parameters_are_recorded(self, panel):
        split = chronological_split(panel.sessions, test_sessions=5)
        train, test = split_panel(panel, split)
        result = evaluate(BiasCorrectedPersistence(CARRIER), train, test, split)
        assert result.params["column"] == CARRIER
        assert result.params["scale"] > 0


class TestCompare:
    def test_scores_a_candidate_and_its_baselines_on_identical_rows(self, panel):
        split = chronological_split(panel.sessions, test_sessions=5)
        comparison = compare(
            BiasCorrectedPersistence(CARRIER),
            [TrainMeanRegressor(), Persistence(CARRIER)],
            panel,
            split,
        )
        windows = {result.window for result in comparison.baselines}
        assert windows == {comparison.model.window}
        assert len(comparison.baselines) == 2

    def test_refuses_to_run_without_a_baseline(self, panel):
        split = chronological_split(panel.sessions, test_sessions=5)
        with pytest.raises(ResearchError) as exc:
            compare(Persistence(CARRIER), [], panel, split)
        assert "without a baseline" in str(exc.value)

    def test_bias_correction_wins_on_level_and_ties_on_ranking(self, panel):
        # The pair exists to separate the two axes, and on a synthetic panel
        # where the carrier really does predict the target, it does exactly
        # that: lower error, identical correlation.
        split = chronological_split(panel.sessions, test_sessions=5)
        comparison = compare(
            BiasCorrectedPersistence(CARRIER), [Persistence(CARRIER)], panel, split
        )
        assert comparison.error_verdict == BEATS
        assert comparison.ranking_verdict == TIES
        assert comparison.model.regression.correlation.r == pytest.approx(
            comparison.baselines[0].regression.correlation.r
        )
        assert comparison.verdict == BEATS

    def test_the_convenient_naive_answer_loses_to_the_best_one(self, classes):
        # Brief S4.1's worked example: on a window that is 65% down, "always
        # up" scores 35% and the best naive answer scores 65%.
        split = chronological_split(classes.sessions, test_sessions=5)
        comparison = compare(
            AlwaysPredict(1.0, name="always-up"),
            [BestNaiveClassifier()],
            classes,
            split,
        )
        assert comparison.verdict == LOSES
        assert comparison.model.accuracy.rate < comparison.best_baseline_rate


class TestWalkForwardStudy:
    def test_runs_every_window_and_reports_them_all(self, panel):
        splits = walk_forward(panel.sessions, train_sessions=10, test_sessions=5)
        report = walk_forward_study(
            BiasCorrectedPersistence(CARRIER),
            [TrainMeanRegressor(), Persistence(CARRIER)],
            panel,
            splits,
            LEDGER,
        )
        assert report.windows == len(splits)
        assert report.rows == sum(c.model.n for c in report.comparisons)
        assert all(len(c.baselines) == 2 for c in report.comparisons)

    def test_the_consistency_figure_counts_windows_not_an_average(self, panel):
        splits = walk_forward(panel.sessions, train_sessions=10, test_sessions=5)
        report = walk_forward_study(
            BiasCorrectedPersistence(CARRIER),
            [Persistence(CARRIER)],
            panel,
            splits,
            LEDGER,
        )
        consistency = report.consistency()
        assert consistency["windows"] == len(splits)
        assert consistency["won_on_error"] == len(splits)

    def test_a_predictor_with_no_signal_does_not_win(self, panel):
        # "unrelated" is a seeded uniform draw with nothing to do with the
        # target; the harness should say so rather than find something.
        splits = walk_forward(panel.sessions, train_sessions=10, test_sessions=5)
        report = walk_forward_study(
            Persistence("unrelated"),
            [TrainMeanRegressor(), Persistence(CARRIER)],
            panel,
            splits,
            LEDGER,
        )
        assert report.consistency()["won_overall"] == 0

    def test_needs_at_least_one_window(self, panel):
        with pytest.raises(ResearchError):
            walk_forward_study(
                Persistence(CARRIER), [TrainMeanRegressor()], panel, [], LEDGER
            )

    def test_the_render_names_the_label_and_every_window(self, panel):
        splits = walk_forward(panel.sessions, train_sessions=10, test_sessions=5)
        rendered = walk_forward_study(
            Persistence(CARRIER), [TrainMeanRegressor()], panel, splits, LEDGER
        ).render()
        assert "fwd_rv_60m" in rendered
        assert rendered.count("window #") == len(splits)
        assert "hypotheses: 1 tested" in rendered
        assert "power:" in rendered


class TestReproducibility:
    def test_the_same_panel_and_splits_produce_the_same_report(self, panel):
        # Brief gate 8. Nothing in the harness reads a clock or an unseeded
        # random source, so two runs must agree exactly -- not approximately.
        splits = walk_forward(panel.sessions, train_sessions=10, test_sessions=5)

        def run():
            return walk_forward_study(
                BiasCorrectedPersistence(CARRIER),
                [TrainMeanRegressor(), Persistence(CARRIER)],
                panel,
                splits,
                LEDGER,
            )

        assert run().describe() == run().describe()
        assert run().render() == run().render()

    def test_rebuilding_the_fixture_gives_the_same_numbers(self):
        first = volatility_panel(DAYS[:10], per_session=6)
        second = volatility_panel(DAYS[:10], per_session=6)
        assert first == second
