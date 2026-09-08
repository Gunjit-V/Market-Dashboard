"""Source-data quality carried from Phase 1 validation into feature states.

These tests build real ValidationResults through marketdata.validation rather
than hand-rolling them, so the bridge is exercised against the actual Phase 1
output shape and cannot drift from it silently.
"""

from __future__ import annotations

import json
from datetime import datetime

from marketdata.validation import validate_ohlcv

from features.quality import (
    FAIL,
    PASS,
    WARNING,
    SourceQuality,
    affects_decision_time,
    clean_window,
    summarize_sources,
    verdict_for,
    window_quality,
    worst_verdict,
)
from tests.conftest import bar, bars

START = datetime(2026, 9, 3, 9, 15)
END = datetime(2026, 9, 3, 15, 30)


def validated(records, **kwargs):
    """Validate *records* and bridge the result, as the pipeline would."""
    result = validate_ohlcv(records, timeframe="5m", **kwargs)
    return window_quality(result, "Nifty 50", START, END, records)


class TestVerdict:
    def test_clean_bars_pass(self):
        assert validated(bars(START, 5)).verdict == PASS

    def test_broken_ohlc_invariant_fails(self):
        # high below open cannot have happened.
        records = bars(START, 3)
        records[1] = bar(records[1]["timestamp"], open_=100.0, high=95.0)
        assert validated(records).verdict == FAIL

    def test_gap_warns_rather_than_fails(self, weekday_calendar):
        # An absent in-session bar is information, not damage.
        records = bars(START, 3) + bars(START.replace(hour=10), 2)
        quality = validated(records, is_trading_day=weekday_calendar)
        assert quality.verdict == WARNING
        assert quality.missing_timestamps

    def test_verdict_for_matches_the_result(self):
        result = validate_ohlcv(bars(START, 5), timeframe="5m")
        assert verdict_for(result) == PASS


class TestSourceQuality:
    def test_counts_are_carried_through(self):
        quality = validated(bars(START, 5))
        assert quality.record_count == 5
        assert quality.error_count == 0
        assert quality.dataset == "ohlcv_5m"
        assert quality.instrument == "Nifty 50"

    def test_invalid_timestamps_are_recovered(self):
        records = bars(START, 3)
        broken = records[1]["timestamp"]
        records[1] = bar(broken, open_=100.0, high=95.0)
        assert validated(records).invalid_timestamps == (broken,)

    def test_invalid_timestamps_empty_without_records(self):
        # Counts still work; only the per-bar detail needs the batch.
        result = validate_ohlcv(bars(START, 3), timeframe="5m")
        quality = window_quality(result, "Nifty 50", START, END)
        assert quality.invalid_timestamps == ()
        assert quality.record_count == 3

    def test_ok_reflects_errors_only(self):
        assert validated(bars(START, 5)).ok

    def test_warnings_do_not_make_a_window_unusable(self, weekday_calendar):
        records = bars(START, 3) + bars(START.replace(hour=10), 2)
        quality = validated(records, is_trading_day=weekday_calendar)
        assert quality.verdict == WARNING
        assert quality.usable

    def test_failed_window_is_not_usable(self):
        records = bars(START, 3)
        records[1] = bar(records[1]["timestamp"], open_=100.0, high=95.0)
        assert not validated(records).usable

    def test_clean_window_helper(self):
        quality = clean_window("ohlcv_5m", "Nifty 50", START, END, record_count=75)
        assert quality.verdict == PASS
        assert quality.ok and quality.usable

    def test_issue_counts_default_to_empty_mapping(self):
        assert clean_window("ohlcv_5m", "Nifty 50", START, END).issue_counts == {}

    def test_as_dict_is_json_serialisable(self):
        loaded = json.loads(json.dumps(validated(bars(START, 5)).as_dict()))
        assert loaded["verdict"] == PASS
        assert loaded["start"] == "2026-09-03T09:15:00"


class TestAffectsDecisionTime:
    def quality_with_bad_bar_at(self, moment: datetime) -> SourceQuality:
        return SourceQuality(
            dataset="ohlcv_5m",
            instrument="Nifty 50",
            start=START,
            end=END,
            verdict=FAIL,
            record_count=10,
            error_count=1,
            invalid_timestamps=(moment,),
        )

    def test_bad_bar_before_the_decision_affects_it(self):
        quality = self.quality_with_bad_bar_at(datetime(2026, 9, 3, 10, 0))
        assert affects_decision_time(quality, datetime(2026, 9, 3, 11, 30))

    def test_bad_bar_after_the_decision_does_not(self):
        # A window-level FAIL does not condemn states built before the bad bar.
        quality = self.quality_with_bad_bar_at(datetime(2026, 9, 3, 14, 0))
        assert not affects_decision_time(quality, datetime(2026, 9, 3, 11, 30))

    def test_bad_bar_at_the_decision_time_affects_it(self):
        moment = datetime(2026, 9, 3, 11, 30)
        assert affects_decision_time(self.quality_with_bad_bar_at(moment), moment)

    def test_bad_bar_before_the_lookback_does_not(self):
        quality = self.quality_with_bad_bar_at(datetime(2026, 9, 3, 9, 20))
        assert not affects_decision_time(
            quality,
            datetime(2026, 9, 3, 11, 30),
            lookback_start=datetime(2026, 9, 3, 11, 0),
        )

    def test_bad_bar_inside_the_lookback_affects_it(self):
        quality = self.quality_with_bad_bar_at(datetime(2026, 9, 3, 11, 15))
        assert affects_decision_time(
            quality,
            datetime(2026, 9, 3, 11, 30),
            lookback_start=datetime(2026, 9, 3, 11, 0),
        )

    def test_clean_window_never_affects_anything(self):
        quality = clean_window("ohlcv_5m", "Nifty 50", START, END)
        assert not affects_decision_time(quality, datetime(2026, 9, 3, 11, 30))


class TestAggregation:
    def make(self, verdict: str, errors: int = 0, warnings: int = 0) -> SourceQuality:
        return SourceQuality(
            dataset="ohlcv_5m",
            instrument="Nifty 50",
            start=START,
            end=END,
            verdict=verdict,
            record_count=75,
            error_count=errors,
            warning_count=warnings,
        )

    def test_worst_verdict_prefers_fail(self):
        assert worst_verdict([self.make(PASS), self.make(FAIL)]) == FAIL

    def test_worst_verdict_prefers_warning_over_pass(self):
        assert worst_verdict([self.make(PASS), self.make(WARNING)]) == WARNING

    def test_worst_verdict_of_all_clean_is_pass(self):
        assert worst_verdict([self.make(PASS), self.make(PASS)]) == PASS

    def test_worst_verdict_of_nothing_is_pass(self):
        assert worst_verdict([]) == PASS

    def test_one_bad_window_is_not_averaged_away(self):
        # Nineteen clean windows must not dilute one broken one.
        qualities = [self.make(PASS) for _ in range(19)] + [self.make(FAIL, errors=1)]
        assert summarize_sources(qualities)["verdict"] == FAIL

    def test_summary_totals_counts_across_windows(self):
        summary = summarize_sources(
            [self.make(WARNING, warnings=2), self.make(FAIL, errors=3, warnings=1)]
        )
        assert summary["window_count"] == 2
        assert summary["error_count"] == 3
        assert summary["warning_count"] == 3

    def test_summary_is_json_serialisable(self):
        summary = summarize_sources([self.make(PASS)])
        assert json.loads(json.dumps(summary))["verdict"] == PASS
