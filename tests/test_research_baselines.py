"""The trivial answers, and the two properties that make them useful.

The best-naive classifier must pick the *best* constant answer on the window it
is scoring -- brief S4.1, the rule Phase 2 broke twice -- and bias-corrected
persistence must move the level without touching the ranking, which is what
makes the pair of baselines a direct measurement of how a model wins.
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
from research.errors import ResearchError
from research.metrics import accuracy, pearson
from research.panel import CLASSIFICATION
from tests.featurelib import trading_days
from tests.researchlib import CARRIER, direction_panel, panel_of, volatility_panel

DAYS = trading_days(date(2026, 3, 2), 8)
TIMES = [datetime(2026, 3, 2, 9, 20) + timedelta(minutes=5 * i) for i in range(4)]


class TestPredictorContract:
    def test_predicting_before_fitting_is_refused(self):
        panel = volatility_panel(DAYS, per_session=2)
        with pytest.raises(ResearchError) as exc:
            TrainMeanRegressor().predict(panel)
        assert "fit" in str(exc.value)

    def test_fitting_on_an_empty_panel_is_refused(self):
        panel = volatility_panel(DAYS, per_session=2)
        with pytest.raises(ResearchError):
            TrainMeanRegressor().fit(panel.select([]))

    def test_a_regressor_refuses_a_classification_panel(self):
        panel = direction_panel(DAYS, per_session=2)
        with pytest.raises(ResearchError) as exc:
            TrainMeanRegressor().fit(panel)
        assert "classification" in str(exc.value)

    def test_a_classifier_refuses_a_regression_panel(self):
        panel = volatility_panel(DAYS, per_session=2)
        with pytest.raises(ResearchError):
            BestNaiveClassifier().fit(panel)

    def test_fit_returns_the_predictor_so_calls_can_chain(self):
        panel = volatility_panel(DAYS, per_session=2)
        predictor = TrainMeanRegressor()
        assert predictor.fit(panel) is predictor
        assert predictor.fitted

    def test_every_baseline_declares_itself_one(self):
        for predictor in (
            ConstantRegressor(0.1),
            TrainMeanRegressor(),
            Persistence(CARRIER),
            BiasCorrectedPersistence(CARRIER),
            AlwaysPredict(1.0),
            BestNaiveClassifier(),
        ):
            assert predictor.is_baseline

    def test_only_the_best_naive_classifier_reads_the_test_labels(self):
        readers = [
            predictor.name
            for predictor in (
                ConstantRegressor(0.1),
                TrainMeanRegressor(),
                Persistence(CARRIER),
                BiasCorrectedPersistence(CARRIER),
                AlwaysPredict(1.0),
                BestNaiveClassifier(),
            )
            if predictor.uses_test_labels
        ]
        assert readers == ["best-naive"]

    def test_a_bare_predictor_has_nothing_to_predict_with(self):
        # The base class is a contract, not a usable predictor.
        with pytest.raises(NotImplementedError):
            Predictor("bare")._fit(volatility_panel(DAYS, per_session=2))


class TestConstantRegressor:
    def test_predicts_its_number_everywhere(self):
        panel = volatility_panel(DAYS, per_session=3)
        predicted = ConstantRegressor(0.42).fit(panel).predict(panel)
        assert set(predicted) == {0.42}
        assert len(predicted) == len(panel)

    def test_has_no_ranking_at_all(self):
        # Brief S5: the harness must report a constant predictor honestly,
        # which means "undefined", not "zero".
        panel = volatility_panel(DAYS, per_session=3)
        predicted = ConstantRegressor(0.42).fit(panel).predict(panel)
        assert pearson(panel.y, predicted).r is None


class TestTrainMeanRegressor:
    def test_predicts_the_training_mean(self):
        train = panel_of(TIMES, [1, 2, 3, 4], [0.10, 0.20, 0.30, 0.40])
        predictor = TrainMeanRegressor().fit(train)
        assert predictor.mean == pytest.approx(0.25)
        assert predictor.predict(train) == pytest.approx((0.25,) * 4)

    def test_does_not_learn_the_test_window(self):
        # The mean of the window being scored is not knowable at decision time.
        train = panel_of(TIMES, [1, 2, 3, 4], [0.10, 0.20, 0.30, 0.40])
        later = [t + timedelta(days=7) for t in TIMES]
        test = panel_of(later, [1, 2, 3, 4], [9.0, 9.0, 9.0, 9.0])
        predicted = TrainMeanRegressor().fit(train).predict(test)
        assert predicted == pytest.approx((0.25,) * 4)

    def test_records_what_it_learned(self):
        train = panel_of(TIMES, [1, 2, 3, 4], [0.10, 0.20, 0.30, 0.40])
        assert TrainMeanRegressor().fit(train).params == {"mean": pytest.approx(0.25)}


class TestPersistence:
    def test_carries_the_column_forward_unchanged(self):
        panel = panel_of(TIMES, [0.11, 0.13, 0.09, 0.15], [0.10, 0.14, 0.08, 0.16])
        predicted = Persistence(CARRIER).fit(panel).predict(panel)
        assert predicted == pytest.approx((0.11, 0.13, 0.09, 0.15))

    def test_names_the_column_it_reads(self):
        assert Persistence(CARRIER).params == {"column": CARRIER}
        assert CARRIER in Persistence(CARRIER).name

    def test_a_missing_column_is_refused_at_fit_time(self):
        panel = panel_of(TIMES, [0.11, 0.13, 0.09, 0.15], [0.1, 0.1, 0.1, 0.1])
        with pytest.raises(ResearchError) as exc:
            Persistence("rv_baseline").fit(panel)
        assert "rv_baseline" in str(exc.value)


class TestBiasCorrectedPersistence:
    def test_scales_to_the_training_level(self):
        # carrier mean 0.10, label mean 0.20 -> scale 2.0
        train = panel_of(TIMES, [0.05, 0.10, 0.10, 0.15], [0.10, 0.20, 0.20, 0.30])
        predictor = BiasCorrectedPersistence(CARRIER).fit(train)
        assert predictor.scale == pytest.approx(2.0)
        assert predictor.predict(train) == pytest.approx((0.10, 0.20, 0.20, 0.30))

    def test_cannot_change_the_ranking(self):
        # A positive rescale leaves Pearson's r identical, term for term --
        # which is why this baseline and plain persistence always report the
        # same correlation and different errors.
        panel = volatility_panel(DAYS, per_session=6, noise=0.01)
        plain = Persistence(CARRIER).fit(panel).predict(panel)
        corrected = BiasCorrectedPersistence(CARRIER).fit(panel).predict(panel)
        assert pearson(panel.y, corrected).r == pytest.approx(
            pearson(panel.y, plain).r
        )
        assert corrected != plain

    def test_learns_the_scale_from_training_only(self):
        train = panel_of(TIMES, [0.05, 0.10, 0.10, 0.15], [0.10, 0.20, 0.20, 0.30])
        later = [t + timedelta(days=7) for t in TIMES]
        test = panel_of(later, [0.20, 0.20, 0.20, 0.20], [9.0, 9.0, 9.0, 9.0])
        predicted = BiasCorrectedPersistence(CARRIER).fit(train).predict(test)
        assert predicted == pytest.approx((0.40,) * 4)

    def test_refuses_a_non_positive_carrier_mean(self):
        train = panel_of(TIMES, [0.0, 0.0, 0.0, 0.0], [0.1, 0.2, 0.3, 0.4])
        with pytest.raises(ResearchError) as exc:
            BiasCorrectedPersistence(CARRIER).fit(train)
        assert "positive" in str(exc.value)

    def test_records_the_scale_it_learned(self):
        train = panel_of(TIMES, [0.05, 0.10, 0.10, 0.15], [0.10, 0.20, 0.20, 0.30])
        params = BiasCorrectedPersistence(CARRIER).fit(train).params
        assert params == {"column": CARRIER, "scale": pytest.approx(2.0)}


class TestBestNaiveClassifier:
    def test_picks_the_majority_class_of_the_window_it_scores(self):
        # 65% down: "always down" scores 0.65 and "always up" 0.35. The
        # baseline must find the former.
        panel = direction_panel(DAYS, per_session=10, down_fraction=0.65)
        predictor = BestNaiveClassifier().fit(panel)
        predicted = predictor.predict(panel)
        assert set(predicted) == {-1.0}
        assert predictor.chosen == -1.0
        assert accuracy(panel.y, predicted).rate == pytest.approx(0.65)

    def test_switches_class_when_the_window_switches(self):
        up = direction_panel(DAYS, per_session=10, down_fraction=0.2)
        predictor = BestNaiveClassifier().fit(up)
        assert set(predictor.predict(up)) == {1.0}

    def test_beats_the_convenient_naive_answer_by_construction(self):
        panel = direction_panel(DAYS, per_session=10, down_fraction=0.65)
        convenient = AlwaysPredict(1.0).fit(panel).predict(panel)
        best = BestNaiveClassifier().fit(panel).predict(panel)
        assert accuracy(panel.y, best).rate > accuracy(panel.y, convenient).rate

    def test_considers_the_three_valued_label(self):
        # 0.181% of Nifty 50 five-minute bars close exactly unchanged, and
        # Phase 2 kept the distinction rather than folding it away.
        times = [datetime(2026, 3, 2, 9, 20) + timedelta(minutes=5 * i) for i in range(5)]
        panel = panel_of(
            times, [1, 2, 3, 4, 5], [0.0, 0.0, 0.0, 1.0, -1.0],
            kind=CLASSIFICATION, label="fwd_dir_30m", horizon_minutes=30,
        )
        predictor = BestNaiveClassifier().fit(panel)
        assert set(predictor.predict(panel)) == {0.0}
        assert predictor.candidates == (-1.0, 0.0, 1.0)

    def test_breaks_a_tie_the_same_way_every_time(self):
        times = [datetime(2026, 3, 2, 9, 20) + timedelta(minutes=5 * i) for i in range(4)]
        panel = panel_of(
            times, [1, 2, 3, 4], [1.0, 1.0, -1.0, -1.0],
            kind=CLASSIFICATION, label="fwd_dir_30m", horizon_minutes=30,
        )
        first = BestNaiveClassifier().fit(panel).predict(panel)
        second = BestNaiveClassifier().fit(panel).predict(panel)
        assert first == second == (-1.0,) * 4

    def test_learns_nothing_from_training(self):
        train = direction_panel(DAYS[:4], per_session=10, down_fraction=0.9)
        later = trading_days(date(2026, 4, 1), 4)
        test = direction_panel(later, per_session=10, down_fraction=0.1)
        predictor = BestNaiveClassifier().fit(train)
        assert set(predictor.predict(test)) == {1.0}


class TestAlwaysPredict:
    def test_predicts_one_class_forever(self):
        panel = direction_panel(DAYS, per_session=4)
        assert set(AlwaysPredict(-1.0).fit(panel).predict(panel)) == {-1.0}

    def test_names_itself_by_the_class_it_picks(self):
        assert AlwaysPredict(1.0).name == "always(+1)"
        assert AlwaysPredict(-1.0).name == "always(-1)"
