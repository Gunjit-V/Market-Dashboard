"""Sweep pacing and instrument priority.

Measured after hours on 2026-09-07 with the scheduler stopped:

* Throttling does not track call spacing. 10 calls at 0.34s spacing failed 1;
  the same 10 at 6.0s spacing failed 3. A 60s window at 3s spacing lost 5 of
  18. So pacing alone cannot avoid the limit.
* Retries do work. A full 49-instrument sweep reached 96% coverage with a
  single retry, versus 67% on first attempt.
* Failures cluster at the START of a sweep: positions 0-11 succeeded 6/12
  while positions 37-48 succeeded 11/12. Ordering critical instruments first
  would therefore expose them to the worst failure rate — priority must decide
  who gets *retried*, not who goes first.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("SmartApi", reason="Angel One SDK not installed")

from downloader.ohlcv import (
    MIN_INSTRUMENT_GAP,
    PRIORITY_TYPES,
    SWEEP_BUDGET_SECONDS,
    _instrument_gap,
    is_priority,
)


def test_indices_and_futures_are_priority_instruments():
    assert "AMXIDX" in PRIORITY_TYPES
    assert "FUTIDX" in PRIORITY_TYPES
    assert is_priority("AMXIDX") and is_priority("FUTIDX")


def test_the_option_chain_is_best_effort():
    # 42 of 49 active instruments are options; retrying all of them would
    # overrun the one-minute budget.
    assert not is_priority("OPTIDX")


def test_the_sweep_fits_inside_the_scheduler_tick():
    # The 1m job fires every 60s; an overrunning sweep causes the next tick to
    # be skipped outright.
    assert SWEEP_BUDGET_SECONDS < 60


@pytest.mark.parametrize("count", [7, 49, 100])
def test_pacing_spreads_instruments_across_the_budget(count):
    gap = _instrument_gap(count)
    assert gap >= MIN_INSTRUMENT_GAP
    # For counts that fit, the sweep should use the budget rather than burst.
    if count * MIN_INSTRUMENT_GAP < SWEEP_BUDGET_SECONDS:
        assert gap * count == pytest.approx(SWEEP_BUDGET_SECONDS)


def test_a_large_instrument_set_falls_back_to_the_minimum_gap():
    # 200 instruments cannot fit in the budget; pacing must not go below the
    # floor and turn into an unthrottled burst.
    assert _instrument_gap(500) == MIN_INSTRUMENT_GAP


def test_a_single_instrument_does_not_divide_by_zero():
    assert _instrument_gap(1) == MIN_INSTRUMENT_GAP
    assert _instrument_gap(0) == MIN_INSTRUMENT_GAP


def test_only_priority_instruments_are_queued_for_retry():
    from downloader.ohlcv import download_historical_data

    src = inspect.getsource(download_historical_data)
    assert "if is_priority(instrument_type):" in src
    assert "empty.append(instrument)" in src
    assert "Retrying" in src


def test_the_sweep_paces_between_instruments():
    from downloader.ohlcv import download_historical_data

    src = inspect.getsource(download_historical_data)
    assert "time.sleep(gap)" in src
    # The final instrument needs no trailing wait.
    assert "if idx < total:" in src


def test_five_minute_runs_are_queued_behind_one_minute():
    import scheduler.ohlcv_scheduler as sched

    src = inspect.getsource(sched.IntervalRunner.__init__)
    # A single worker is what serialises 1m and 5m; two workers let them
    # compete for the same per-account quota.
    assert "max_workers=1" in src


def test_derivative_backfill_window_matches_the_option_chains_life():
    from downloader.ohlcv import CHUNK_DAYS, DERIVATIVE_DAYS

    # Sized to the weekly Nifty option chain's actual listing life (~30 days
    # before expiry), not the platform default (90). Measured on 2026-09-07:
    # 90 days cost 756 API calls (~25 min) to backfill 42 fresh options; 30
    # days cuts that to 252 calls (~8 min). See downloader/ohlcv.py for the
    # accepted tradeoff on monthly index futures, which can list further out
    # than 30 days and so have their first-run history truncated too.
    assert DERIVATIVE_DAYS == 30
    assert DERIVATIVE_DAYS % CHUNK_DAYS == 0, (
        "should divide evenly into CHUNK_DAYS-sized pagination chunks"
    )
