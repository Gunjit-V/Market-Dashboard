"""The ingestion hook must be able to see problems without causing them.

The critical property tested here is backward compatibility: in the default
``report`` mode the downloader inserts exactly the rows it inserted before
Phase 1, in the same order, unmodified.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest

from marketdata.ingest import (
    MODE_ENV,
    QUARANTINE_ENV,
    REJECT,
    REPORT,
    candle_to_record,
    configured_mode,
    log_screen_result,
    quarantine_path,
    screen_candles,
    write_quarantine,
)

# Raw Angel One candles: [timestamp, open, high, low, close, volume]
GOOD = ["2026-09-03T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5, 1000]
GOOD_2 = ["2026-09-03T09:20:00+05:30", 100.5, 102.0, 100.0, 101.5, 1200]
BROKEN_HIGH = ["2026-09-03T09:25:00+05:30", 100.0, 98.0, 99.0, 99.5, 900]
NEGATIVE_VOLUME = ["2026-09-03T09:30:00+05:30", 100.0, 101.0, 99.0, 100.5, -1]


def normalize(value):
    """Same normalisation the downloader applies, without importing SmartApi."""
    from datetime import datetime, timedelta, timezone

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone(timedelta(hours=5, minutes=30)))
        parsed = parsed.replace(tzinfo=None)
    return parsed


# ── Mode resolution ──────────────────────────────────────────────────────────

def test_report_is_the_default_mode():
    assert configured_mode({}) == REPORT


def test_mode_comes_from_the_environment():
    assert configured_mode({MODE_ENV: "reject"}) == REJECT
    assert configured_mode({MODE_ENV: " REPORT "}) == REPORT


def test_an_unknown_mode_falls_back_to_report():
    assert configured_mode({MODE_ENV: "delete-everything"}) == REPORT


def test_an_unknown_mode_argument_is_rejected_loudly():
    with pytest.raises(ValueError, match="Unknown mode"):
        screen_candles([GOOD], 1, "5m", mode="repair")


# ── Backward compatibility ───────────────────────────────────────────────────

def test_report_mode_passes_every_candle_through_unchanged():
    candles = [GOOD, GOOD_2, BROKEN_HIGH, NEGATIVE_VOLUME]
    before = copy.deepcopy(candles)

    screened = screen_candles(candles, 1, "5m", mode=REPORT,
                              normalize_timestamp=normalize)

    assert list(screened.accepted) == candles, "insertion set must not change"
    assert screened.rejected == ()
    assert candles == before, "the raw candles must not be mutated"
    # ...while still reporting what is wrong with them.
    assert not screened.result.ok
    assert screened.result.invalid_indices == frozenset({2, 3})


def test_report_mode_preserves_row_identity_and_order():
    candles = [GOOD, GOOD_2]
    screened = screen_candles(candles, 1, "5m", mode=REPORT,
                              normalize_timestamp=normalize)
    assert screened.accepted[0] is GOOD
    assert screened.accepted[1] is GOOD_2


def test_clean_candles_produce_no_issues_in_either_mode():
    for mode in (REPORT, REJECT):
        screened = screen_candles([GOOD, GOOD_2], 1, "5m", mode=mode,
                                  normalize_timestamp=normalize)
        assert screened.result.issues == ()
        assert list(screened.accepted) == [GOOD, GOOD_2]


# ── Reject mode ──────────────────────────────────────────────────────────────

def test_reject_mode_withholds_only_the_invalid_rows():
    candles = [GOOD, GOOD_2, BROKEN_HIGH]
    before = copy.deepcopy(candles)

    screened = screen_candles(candles, 1, "5m", mode=REJECT,
                              normalize_timestamp=normalize)

    assert list(screened.accepted) == [GOOD, GOOD_2]
    assert list(screened.rejected) == [BROKEN_HIGH]
    assert screened.rejected[0] is BROKEN_HIGH  # handed back verbatim
    assert candles == before


def test_reject_mode_keeps_warning_only_rows():
    # A bar outside session hours is a warning, not a contract violation, so
    # it is still inserted — quarantining it would lose real data.
    out_of_session = ["2026-09-03T03:45:00+05:30", 100.0, 101.0, 99.0, 100.5, 10]
    screened = screen_candles([out_of_session], 1, "5m", mode=REJECT,
                              normalize_timestamp=normalize)
    assert list(screened.accepted) == [out_of_session]
    assert screened.rejected == ()
    assert screened.result.warnings


# ── Batch semantics ──────────────────────────────────────────────────────────

def test_a_partial_batch_is_never_gap_checked():
    # An ingestion chunk is expected to be partial; gaps mean nothing here.
    screened = screen_candles([GOOD, NEGATIVE_VOLUME], 1, "5m", mode=REPORT,
                              normalize_timestamp=normalize)
    assert screened.result.missing_timestamps == ()
    assert screened.result.expected_count is None


def test_timestamps_are_normalised_only_for_the_validator():
    # Without normalisation the feed's "+05:30" offset would be flagged.
    raw = screen_candles([GOOD], 1, "5m", mode=REPORT)
    normalised = screen_candles([GOOD], 1, "5m", mode=REPORT,
                                normalize_timestamp=normalize)
    assert "timezone_inconsistent" in raw.result.by_code()
    assert normalised.result.issues == ()


def test_an_unparseable_timestamp_is_reported_not_dropped():
    bad = ["not-a-timestamp", 100.0, 101.0, 99.0, 100.5, 10]
    screened = screen_candles([bad], 1, "5m", mode=REPORT,
                              normalize_timestamp=normalize)
    assert "malformed_timestamp" in screened.result.by_code()
    assert list(screened.accepted) == [bad]


def test_candle_to_record_maps_the_feed_positionally():
    assert candle_to_record(GOOD, 7) == {
        "instrument_id": 7,
        "timestamp": "2026-09-03T09:15:00+05:30",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000,
    }


def test_an_empty_batch_is_a_no_op():
    screened = screen_candles([], 1, "5m", mode=REJECT)
    assert screened.accepted == ()
    assert screened.rejected == ()
    assert screened.result.record_count == 0


# ── Quarantine ───────────────────────────────────────────────────────────────

def test_quarantine_writes_rejected_rows_verbatim_with_reasons(tmp_path):
    path = tmp_path / "quarantine.jsonl"
    screened = screen_candles([GOOD, BROKEN_HIGH], 1, "5m", mode=REJECT,
                              normalize_timestamp=normalize)

    written = write_quarantine(screened, instrument_id=1, timeframe="5m", path=path)

    assert written == 1
    entry = json.loads(path.read_text().strip())
    assert entry["row"] == BROKEN_HIGH
    assert entry["instrument_id"] == 1
    assert entry["timeframe"] == "5m"
    assert "ohlc_invariant" in entry["reasons"]


def test_quarantine_appends_across_batches(tmp_path):
    path = tmp_path / "nested" / "quarantine.jsonl"
    for _ in range(2):
        screened = screen_candles([BROKEN_HIGH], 1, "5m", mode=REJECT,
                                  normalize_timestamp=normalize)
        write_quarantine(screened, 1, "5m", path=path)
    assert len(path.read_text().strip().splitlines()) == 2


def test_quarantine_is_a_no_op_without_configuration(monkeypatch):
    monkeypatch.delenv(QUARANTINE_ENV, raising=False)
    screened = screen_candles([BROKEN_HIGH], 1, "5m", mode=REJECT,
                              normalize_timestamp=normalize)
    assert quarantine_path({}) is None
    assert write_quarantine(screened, 1, "5m") == 0


def test_quarantine_is_a_no_op_when_nothing_was_rejected(tmp_path):
    path = tmp_path / "quarantine.jsonl"
    screened = screen_candles([GOOD], 1, "5m", mode=REJECT,
                              normalize_timestamp=normalize)
    assert write_quarantine(screened, 1, "5m", path=path) == 0
    assert not path.exists()


# ── Logging ──────────────────────────────────────────────────────────────────

def test_clean_batches_log_nothing(caplog):
    screened = screen_candles([GOOD], 1, "5m", mode=REPORT,
                              normalize_timestamp=normalize)
    with caplog.at_level(logging.DEBUG, logger="marketdata.ingest"):
        log_screen_result(screened, context="ohlcv_5min instrument_id=1")
    assert caplog.records == []


def test_errors_are_logged_once_per_batch_with_context(caplog):
    screened = screen_candles([GOOD, BROKEN_HIGH], 1, "5m", mode=REPORT,
                              normalize_timestamp=normalize)
    with caplog.at_level(logging.WARNING, logger="marketdata.ingest"):
        log_screen_result(screened, context="ohlcv_5min instrument_id=1")

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "ohlcv_5min instrument_id=1" in message
    assert "ohlc_invariant=2" in message  # high < low AND high < open
