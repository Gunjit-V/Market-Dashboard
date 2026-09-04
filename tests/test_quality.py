"""The quality report must derive every number from real validation output."""

from __future__ import annotations

from datetime import datetime, timedelta

from marketdata.quality import FAIL, PASS, WARNING, build_quality_report, summarize
from marketdata.validation import validate_ohlcv, validate_ticks
from tests.conftest import bar, bars, tick

OPEN = datetime(2026, 9, 3, 9, 15)
PERIOD_START = datetime(2026, 9, 3)
PERIOD_END = datetime(2026, 9, 4)


def _report(records_5m, records_1m=None, ticks=None, calendar=None, notes=()):
    results = {}
    timestamps = {}
    if ticks is not None:
        results["tick"] = validate_ticks(ticks, is_trading_day=calendar)
        timestamps["tick"] = [t["timestamp"] for t in ticks]
    if records_1m is not None:
        results["ohlcv_1m"] = validate_ohlcv(records_1m, "1m", is_trading_day=calendar)
        timestamps["ohlcv_1m"] = [r["timestamp"] for r in records_1m]
    if records_5m is not None:
        results["ohlcv_5m"] = validate_ohlcv(records_5m, "5m", is_trading_day=calendar)
        timestamps["ohlcv_5m"] = [r["timestamp"] for r in records_5m]
    return build_quality_report(
        "NIFTY", PERIOD_START, PERIOD_END, results, timestamps, notes=notes
    )


def test_clean_data_reports_pass(weekday_calendar):
    report = _report(bars(OPEN, 10), calendar=weekday_calendar)
    assert report.status == PASS
    section = report.section("ohlcv_5m")
    assert section.records == 10
    assert section.errors == 0
    assert section.warnings == 0
    assert section.unexpected_gaps == 0


def test_a_gap_downgrades_the_report_to_warning(weekday_calendar):
    records = [bar(OPEN), bar(OPEN + timedelta(minutes=10))]
    report = _report(records, calendar=weekday_calendar)
    assert report.status == WARNING
    assert report.section("ohlcv_5m").unexpected_gaps == 1


def test_a_broken_invariant_fails_the_report(weekday_calendar):
    records = [bar(OPEN, high=1.0)]
    report = _report(records, calendar=weekday_calendar)
    assert report.status == FAIL
    assert report.section("ohlcv_5m").invalid_ohlc >= 1


def test_the_worst_section_decides_the_overall_status(weekday_calendar):
    report = _report(
        records_5m=bars(OPEN, 5),                       # clean
        records_1m=[bar(OPEN, high=1.0)],               # broken
        ticks=[tick(datetime(2026, 9, 3, 10, 0))],      # clean
        calendar=weekday_calendar,
    )
    assert report.section("ohlcv_5m").status == PASS
    assert report.section("ohlcv_1m").status == FAIL
    assert report.status == FAIL


def test_counts_are_taken_from_the_validation_result(weekday_calendar):
    records = [
        bar(OPEN),
        bar(OPEN),                                   # duplicate
        bar(OPEN + timedelta(minutes=10), volume=-1),  # negative volume
        bar(OPEN + timedelta(minutes=5), high=1.0),    # out of order + invariant
    ]
    result = validate_ohlcv(records, "5m", is_trading_day=weekday_calendar)
    section = summarize(result, [r["timestamp"] for r in records])

    assert section.records == 4
    assert section.duplicates == result.count("duplicate_timestamp")
    assert section.out_of_order == result.count("out_of_order")
    assert section.invalid_ohlc == result.count("ohlc_invariant")
    assert section.volume_issues == result.count("negative_value")
    assert section.errors == len(result.errors)
    assert section.warnings == len(result.warnings)
    assert section.first_timestamp == OPEN
    assert section.last_timestamp == OPEN + timedelta(minutes=10)


def test_completeness_is_present_over_expected(weekday_calendar):
    # 09:15, [09:20 missing], 09:25, 09:30 → 3 of 4 expected bars.
    records = [
        bar(datetime(2026, 9, 3, 9, 15)),
        bar(datetime(2026, 9, 3, 9, 25)),
        bar(datetime(2026, 9, 3, 9, 30)),
    ]
    section = _report(records, calendar=weekday_calendar).section("ohlcv_5m")
    assert section.expected_records == 4
    assert section.unexpected_gaps == 1
    assert section.completeness_pct == 75.0


def test_completeness_is_unknown_when_no_expectation_was_computed():
    result = validate_ohlcv([], "5m")
    assert summarize(result).completeness_pct is None


def test_a_dataset_that_was_not_validated_is_absent_not_clean(weekday_calendar):
    report = _report(bars(OPEN, 3), calendar=weekday_calendar)
    assert report.section("tick") is None
    assert [s.dataset for s in report.sections] == ["ohlcv_5m"]


def test_sections_are_ordered_ticks_then_1m_then_5m(weekday_calendar):
    report = _report(
        records_5m=bars(OPEN, 2),
        records_1m=bars(OPEN, 2, bar_minutes=1),
        ticks=[tick(datetime(2026, 9, 3, 10, 0))],
        calendar=weekday_calendar,
    )
    assert [s.dataset for s in report.sections] == ["tick", "ohlcv_1m", "ohlcv_5m"]


def test_rendered_report_contains_the_required_lines(weekday_calendar):
    report = _report(
        records_5m=bars(OPEN, 2),
        records_1m=bars(OPEN, 2, bar_minutes=1),
        ticks=[tick(datetime(2026, 9, 3, 10, 0))],
        calendar=weekday_calendar,
    )
    text = report.render()
    assert "Instrument: NIFTY" in text
    assert "Period: 2026-09-03 00:00:00 → 2026-09-04 00:00:00" in text
    assert "Ticks" in text
    assert "1m OHLCV" in text
    assert "5m OHLCV" in text
    assert "Records         : 2" in text
    assert text.strip().endswith("Overall status: PASS")


def test_rendered_report_surfaces_notes(weekday_calendar):
    report = _report(
        bars(OPEN, 2), calendar=weekday_calendar, notes=("calendar is incomplete",)
    )
    assert "Note: calendar is incomplete" in report.render()


def test_a_report_with_no_datasets_says_so():
    report = build_quality_report("NIFTY", PERIOD_START, PERIOD_END, {})
    assert report.status == PASS
    assert "No datasets were validated." in report.render()


def test_report_serialises_to_json_safe_primitives(weekday_calendar):
    import json

    report = _report(
        records_5m=[bar(OPEN, high=1.0)],
        ticks=[tick(datetime(2026, 9, 3, 10, 0))],
        calendar=weekday_calendar,
    )
    payload = json.loads(json.dumps(report.as_dict()))
    assert payload["instrument"] == "NIFTY"
    assert payload["status"] == FAIL
    assert payload["period"]["start"] == "2026-09-03T00:00:00"
    assert {s["dataset"] for s in payload["sections"]} == {"tick", "ohlcv_5m"}
