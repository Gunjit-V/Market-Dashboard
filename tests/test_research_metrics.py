"""Every metric against a value computed by hand.

Brief S5 acceptance 4. The constants below are derived in the comments beside
them, from the definition of the metric rather than from running the code, so
a change in implementation that changes an answer fails here rather than
passing quietly and moving the conclusion of a study.
"""

from __future__ import annotations

import math

import pytest

from research.errors import MetricError
from research.metrics import (
    Interval,
    accuracy,
    binomial_at_least,
    bonferroni_alpha,
    detectable_edge,
    family_error_rate,
    mean_error,
    pearson,
    probability_of_scoring,
    regression_metrics,
    rmse,
    rows_needed,
    sidak_alpha,
    wilson_interval,
    z_for_confidence,
)


class TestQuantiles:
    def test_the_95_percent_two_sided_quantile_is_1_96(self):
        assert z_for_confidence(0.95) == pytest.approx(1.959964, abs=1e-6)

    def test_the_99_percent_quantile_is_2_576(self):
        assert z_for_confidence(0.99) == pytest.approx(2.575829, abs=1e-6)

    @pytest.mark.parametrize("bad", [0.0, 1.0, -0.5, 1.5])
    def test_a_confidence_outside_the_unit_interval_is_refused(self, bad):
        with pytest.raises(MetricError):
            z_for_confidence(bad)


class TestWilsonInterval:
    def test_matches_the_published_value_for_seven_of_ten(self):
        # p = 0.7, z = 1.959964, n = 10:
        #   denominator = 1 + z^2/n              = 1.3841459
        #   centre      = (0.7 + z^2/20)/denom   = 0.6444950
        #   half        = (z/denom)*sqrt(0.021 + z^2/400) = 0.2477226
        interval = wilson_interval(7, 10)
        assert interval.low == pytest.approx(0.3967, abs=1e-4)
        assert interval.high == pytest.approx(0.8922, abs=1e-4)

    def test_stays_inside_the_unit_interval_at_zero_successes(self):
        # The textbook normal interval would give [0, 0] here, claiming
        # certainty from ten observations.
        interval = wilson_interval(0, 10)
        assert interval.low == pytest.approx(0.0, abs=1e-12)
        assert interval.high == pytest.approx(0.2775, abs=1e-4)

    def test_a_thousand_coin_flips_land_within_three_points_of_a_half(self):
        interval = wilson_interval(500, 1000)
        assert interval.low == pytest.approx(0.4691, abs=1e-4)
        assert interval.high == pytest.approx(0.5309, abs=1e-4)

    def test_narrows_as_the_sample_grows(self):
        assert wilson_interval(500, 1000).width < wilson_interval(50, 100).width

    def test_refuses_an_empty_sample(self):
        with pytest.raises(MetricError):
            wilson_interval(0, 0)

    def test_refuses_more_successes_than_trials(self):
        with pytest.raises(MetricError):
            wilson_interval(11, 10)


class TestInterval:
    def test_knows_what_it_contains(self):
        interval = Interval(0.4, 0.6)
        assert interval.contains(0.5)
        assert interval.excludes(0.61)
        assert not interval.is_above(0.5)
        assert interval.is_above(0.39)
        assert interval.is_below(0.61)

    def test_refuses_to_be_inverted(self):
        with pytest.raises(MetricError):
            Interval(0.6, 0.4)


class TestAccuracy:
    def test_counts_exact_matches(self):
        score = accuracy([1, 1, -1, 1], [1, -1, -1, 1])
        assert score.correct == 3
        assert score.n == 4
        assert score.rate == 0.75

    def test_a_near_miss_is_not_a_class(self):
        # A predictor emitting 0.999 has not predicted the class +1.
        assert accuracy([1.0, 1.0], [0.999, 1.0]).correct == 1

    def test_beating_a_baseline_means_the_whole_interval_is_above_it(self):
        # 600/1000 = 0.600, Wilson 95% = [0.5693, 0.6299].
        score = accuracy([1.0] * 600 + [-1.0] * 400, [1.0] * 1000)
        assert score.rate == 0.6
        assert score.beats(0.5)
        assert not score.beats(0.57)
        assert score.indistinguishable_from(0.58)

    def test_a_point_estimate_above_the_baseline_is_not_enough(self):
        # 28/50 = 56% -- above a half, and entirely ordinary noise: a
        # worthless model scores 55% or better a quarter of the time at n=50.
        score = accuracy([1.0] * 28 + [-1.0] * 22, [1.0] * 50)
        assert score.rate == 0.56
        assert not score.beats(0.5)
        assert score.indistinguishable_from(0.5)

    def test_refuses_mismatched_lengths(self):
        with pytest.raises(MetricError):
            accuracy([1, 1], [1])

    def test_refuses_an_empty_window(self):
        with pytest.raises(MetricError):
            accuracy([], [])


class TestRmseAndBias:
    def test_rmse_of_a_known_error_sequence(self):
        # errors 0, -1, -2 -> sqrt((0 + 1 + 4)/3) = sqrt(5/3) = 1.2909944
        assert rmse([1, 2, 3], [1, 3, 5]) == pytest.approx(1.2909944, abs=1e-7)

    def test_rmse_is_zero_for_a_perfect_prediction(self):
        assert rmse([0.1, 0.2], [0.1, 0.2]) == 0.0

    def test_mean_error_is_signed(self):
        # predictions are 0, +1, +2 above the outcomes -> +1.0
        assert mean_error([1, 2, 3], [1, 3, 5]) == pytest.approx(1.0)
        assert mean_error([1, 3, 5], [1, 2, 3]) == pytest.approx(-1.0)


class TestPearson:
    def test_matches_a_hand_computed_correlation(self):
        # x = 1..5, y = 2,4,5,4,5. Deviations: dx = -2,-1,0,1,2;
        # dy = -2,0,1,0,1. cov = 6, var_x = 10, var_y = 6.
        # r = 6/sqrt(60) = 0.7745967
        result = pearson([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
        assert result.r == pytest.approx(0.7745967, abs=1e-7)

    def test_carries_a_fisher_interval_and_a_p_value(self):
        # z = atanh(0.7745967) = 1.031725, se = 1/sqrt(n-3) = 0.7071068,
        # half-width = 1.959964*se = 1.385899
        # -> [tanh(-0.354174), tanh(2.417624)] = [-0.3401, 0.9842]
        result = pearson([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
        assert result.interval.low == pytest.approx(-0.3401, abs=1e-4)
        assert result.interval.high == pytest.approx(0.9842, abs=1e-4)
        assert result.p_value == pytest.approx(0.1445, abs=1e-4)

    def test_a_perfect_line_is_exactly_one(self):
        assert pearson([1, 2, 3], [10, 20, 30]).r == pytest.approx(1.0)

    def test_an_inverted_predictor_is_negative(self):
        assert pearson([1, 2, 3], [3, 2, 1]).r == pytest.approx(-1.0)

    def test_a_positive_rescale_cannot_change_it(self):
        # The property bias-corrected persistence relies on: correcting the
        # level leaves the ranking untouched, term for term.
        truth = [0.10, 0.14, 0.09, 0.17]
        predicted = [0.12, 0.15, 0.11, 0.16]
        scaled = [1.37 * value for value in predicted]
        assert pearson(truth, scaled).r == pytest.approx(pearson(truth, predicted).r)

    def test_a_constant_predictor_has_no_correlation_at_all(self):
        result = pearson([1, 2, 3, 4], [7, 7, 7, 7])
        assert result.r is None
        assert not result.is_defined
        assert "constant" in result.detail
        assert "undefined" in str(result)

    def test_a_constant_the_mean_cannot_represent_exactly_is_still_constant(self):
        # Sixty copies of 0.12 have a floating-point variance of 5e-34, which
        # is enough for the textbook formula to return a correlation of -2e-15
        # with a confidence interval around it. A constant predictor ranks
        # nothing, whatever the arithmetic says.
        result = pearson([0.1 + i / 1000 for i in range(60)], [0.12] * 60)
        assert result.r is None
        assert result.interval is None
        assert "predictions are constant" in result.detail

    def test_a_constant_outcome_is_reported_separately(self):
        result = pearson([5, 5, 5, 5], [1, 2, 3, 4])
        assert result.r is None
        assert "outcomes" in result.detail

    def test_two_rows_cannot_support_a_correlation(self):
        result = pearson([1, 2], [1, 2])
        assert result.r is None
        assert "3 rows" in result.detail

    def test_three_rows_give_a_correlation_but_no_interval(self):
        result = pearson([1, 2, 3], [1, 3, 2])
        assert result.is_defined
        assert result.interval is None


class TestRegressionMetrics:
    def test_reports_error_and_ranking_together(self):
        metrics = regression_metrics([1, 2, 3], [1, 3, 5])
        assert metrics.rmse == pytest.approx(1.2909944, abs=1e-7)
        assert metrics.correlation.r == pytest.approx(1.0)
        assert metrics.mean_error == pytest.approx(1.0)
        assert metrics.n == 3

    def test_a_shrinking_predictor_can_win_on_error_and_lose_on_ranking(self):
        # The Phase 2 finding this pair of metrics exists to expose: the
        # constant is closer on average and orders nothing at all, while the
        # inverted predictor ranks backwards.
        truth = [0.10, 0.20, 0.30, 0.40]
        constant = [0.25, 0.25, 0.25, 0.25]
        inverted = [0.40, 0.30, 0.20, 0.10]
        assert rmse(truth, constant) < rmse(truth, inverted)
        assert regression_metrics(truth, constant).correlation.r is None
        assert regression_metrics(truth, inverted).correlation.r == pytest.approx(-1.0)


class TestPowerArithmetic:
    def test_rows_needed_for_a_one_point_edge(self):
        # (1.959964 + 0.841621)^2 * 0.25 / 0.0001 = 19622.2 -> 19623
        assert rows_needed(0.01) == 19623

    def test_matches_the_briefs_table_to_within_a_fraction_of_a_percent(self):
        # The brief quotes 782 / 4,898 / 19,598 / 78,398 using rounded
        # quantiles; these are the same figures with the exact ones.
        for edge, quoted in ((0.05, 782), (0.02, 4898), (0.01, 19598), (0.005, 78398)):
            assert rows_needed(edge) == pytest.approx(quoted, rel=0.004)

    def test_quadruples_when_the_edge_halves(self):
        assert rows_needed(0.01) == pytest.approx(rows_needed(0.02) * 4, rel=1e-3)

    def test_detectable_edge_inverts_rows_needed(self):
        assert detectable_edge(rows_needed(0.01)) == pytest.approx(0.01, rel=1e-3)

    def test_a_fifty_row_window_can_only_see_an_enormous_edge(self):
        # 19.8 percentage points -- which is why no conclusion is drawn from a
        # window that size (brief S4.5).
        assert detectable_edge(50) == pytest.approx(0.1981, abs=1e-4)

    def test_refuses_an_impossible_edge(self):
        with pytest.raises(MetricError):
            rows_needed(0.0)
        with pytest.raises(MetricError):
            rows_needed(1.5)

    def test_refuses_an_empty_window(self):
        with pytest.raises(MetricError):
            detectable_edge(0)


class TestBinomialTail:
    def test_ten_heads_in_ten_flips(self):
        assert binomial_at_least(10, 0.5, 10) == pytest.approx(1 / 1024)

    def test_at_least_two_of_three(self):
        # (3 + 1)/8 = 0.5
        assert binomial_at_least(3, 0.5, 2) == pytest.approx(0.5)

    def test_the_whole_range_is_certain(self):
        assert binomial_at_least(10, 0.5, 0) == 1.0
        assert binomial_at_least(10, 0.5, 11) == 0.0

    def test_a_worthless_model_scores_55_percent_a_quarter_of_the_time(self):
        # The brief's figure, exactly: 50 rows, no edge at all.
        assert probability_of_scoring(0.50, 50, 0.55) == pytest.approx(0.24, abs=0.005)

    def test_a_genuine_edge_looks_like_a_loss_two_times_in_five(self):
        # A real 51% edge over 50 rows scores below a half 38.8% of the time.
        looks_like_a_loss = 1 - probability_of_scoring(0.51, 50, 0.50)
        assert looks_like_a_loss == pytest.approx(0.388, abs=0.005)

    def test_refuses_a_threshold_outside_the_unit_interval(self):
        with pytest.raises(MetricError):
            probability_of_scoring(0.5, 50, 1.5)


class TestMultiplicity:
    def test_six_independent_tests_produce_a_false_positive_a_quarter_of_the_time(self):
        # 1 - 0.95^6 = 0.2649 -- the brief's 26%. The nine labels hold six
        # independent quantities, because fwd_dir_h is the sign of fwd_ret_h.
        assert family_error_rate(0.05, 6) == pytest.approx(0.2649, abs=1e-4)

    def test_counting_all_nine_labels_would_give_a_different_answer(self):
        assert family_error_rate(0.05, 9) == pytest.approx(0.3698, abs=1e-4)

    def test_one_test_is_just_alpha(self):
        assert family_error_rate(0.05, 1) == pytest.approx(0.05)

    def test_bonferroni_divides_evenly(self):
        assert bonferroni_alpha(0.05, 6) == pytest.approx(0.05 / 6)

    def test_sidak_holds_the_family_rate_at_alpha_exactly(self):
        adjusted = sidak_alpha(0.05, 6)
        assert family_error_rate(adjusted, 6) == pytest.approx(0.05)

    def test_sidak_is_slightly_less_conservative_than_bonferroni(self):
        assert sidak_alpha(0.05, 6) > bonferroni_alpha(0.05, 6)

    @pytest.mark.parametrize("tests", [0, -1])
    def test_a_family_holds_at_least_one_test(self, tests):
        with pytest.raises(MetricError):
            family_error_rate(0.05, tests)


class TestNoSilentNumbers:
    def test_nothing_returns_a_score_for_no_rows(self):
        for call in (
            lambda: rmse([], []),
            lambda: mean_error([], []),
            lambda: pearson([], []),
            lambda: regression_metrics([], []),
        ):
            with pytest.raises(MetricError):
                call()

    def test_mismatched_lengths_are_refused_everywhere(self):
        for call in (
            lambda: rmse([1, 2], [1]),
            lambda: pearson([1, 2], [1]),
            lambda: regression_metrics([1, 2], [1]),
        ):
            with pytest.raises(MetricError):
                call()

    def test_a_correlation_of_one_is_reported_without_an_infinite_interval(self):
        result = pearson([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
        assert result.r == pytest.approx(1.0)
        assert result.interval is None
        assert not math.isinf(result.r)
