"""Retention window arithmetic and configuration.

The cutoff is the one piece of this service that can quietly destroy data if
it is off by a day, so it is tested directly rather than through the scheduler
loop.
"""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("psycopg2", reason="db.connection requires psycopg2")
pytest.importorskip("pyarrow", reason="archive path requires pyarrow")

from scheduler.retention_scheduler import _retention_days, _run_time, cutoff_date


def test_cutoff_keeps_exactly_n_days_including_today():
    # 7 days ending 2026-09-14 means keep 09-08 .. 09-14 inclusive.
    assert cutoff_date(date(2026, 9, 14), 7) == date(2026, 9, 8)
    kept = (date(2026, 9, 14) - date(2026, 9, 8)).days + 1
    assert kept == 7


def test_cutoff_of_one_day_keeps_only_today():
    assert cutoff_date(date(2026, 9, 14), 1) == date(2026, 9, 14)


def test_cutoff_spans_month_boundaries():
    assert cutoff_date(date(2026, 9, 3), 7) == date(2026, 8, 28)


def test_today_is_never_purged():
    # Whatever the window, the cutoff must never exclude today's own session,
    # which is still accumulating rows.
    for days in range(1, 60):
        assert cutoff_date(date(2026, 9, 14), days) <= date(2026, 9, 14)


def test_retention_days_defaults_to_seven(monkeypatch):
    monkeypatch.delenv("TICK_RETENTION_DAYS", raising=False)
    assert _retention_days() == 7


def test_retention_days_reads_the_environment(monkeypatch):
    monkeypatch.setenv("TICK_RETENTION_DAYS", "14")
    assert _retention_days() == 14


@pytest.mark.parametrize("value", ["0", "-5"])
def test_a_non_positive_window_is_clamped_to_one_day(monkeypatch, value):
    # A window of 0 would purge today's live session.
    monkeypatch.setenv("TICK_RETENTION_DAYS", value)
    assert _retention_days() == 1


def test_a_malformed_window_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("TICK_RETENTION_DAYS", "seven")
    assert _retention_days() == 7


def test_run_time_defaults_to_after_market_close(monkeypatch):
    monkeypatch.delenv("RETENTION_RUN_TIME", raising=False)
    run_at = _run_time()
    assert (run_at.hour, run_at.minute) == (16, 30)
    # Must be after the 15:30 close so it never races the collector.
    assert run_at.hour * 60 + run_at.minute > 15 * 60 + 30


def test_run_time_reads_the_environment(monkeypatch):
    monkeypatch.setenv("RETENTION_RUN_TIME", "18:45")
    assert (_run_time().hour, _run_time().minute) == (18, 45)


def test_a_malformed_run_time_falls_back(monkeypatch):
    monkeypatch.setenv("RETENTION_RUN_TIME", "not-a-time")
    assert (_run_time().hour, _run_time().minute) == (16, 30)


# ── Clock-source marker (see docs/point-in-time-data.md 5.1) ─────────────────

def test_exchange_clock_rows_are_identified_by_sequence_number():
    """Timestamp precision cannot identify the clock source; seq can.

    Two heuristics were tried and both failed on real data:
      - whole-second (microseconds = 0) misclassifies genuine millisecond
        exchange timestamps like 13:53:39.025000 as fallbacks;
      - millisecond alignment misclassifies datetime.now() values, because
        Windows clock granularity puts ~59% of them on a ms boundary.
    """
    import inspect

    import purge_tick_data as pm

    src = inspect.getsource(pm.summarize)
    assert "sequence_number > 0" in src
    # The discredited precision heuristics must not come back.
    assert "microseconds" not in src.split("feed_clock")[0].split("cur.execute")[-1]
