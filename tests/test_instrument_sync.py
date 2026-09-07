"""Instrument sync retry behaviour.

On 2026-09-07 a transient SSL hostname-verification failure at 08:45 aborted
the daily sync. Because the scheduler marked the day complete before running,
it never retried, and because sync_all() deactivates the previous option set
before computing the new one, the system was left with 7 active instruments
instead of 49 — the 42 Nifty options stayed off for the whole session.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

pytest.importorskip("pandas", reason="sync_instruments requires pandas")
pytest.importorskip("psycopg2", reason="db.connection requires psycopg2")

ROOT = Path(__file__).resolve().parents[1]


def test_the_day_is_marked_done_only_after_a_successful_run():
    import scheduler.instrument_sync_scheduler as m

    src = inspect.getsource(m.run_forever)
    # The bug: last_run_date was assigned before _run_sync(), so a failure
    # still counted as "done for today".
    assert "if _run_sync():" in src
    assert "last_success_date = today" in src


def test_run_sync_reports_success_or_failure():
    import scheduler.instrument_sync_scheduler as m

    assert m._run_sync.__annotations__.get("return") == "bool"
    src = inspect.getsource(m._run_sync)
    assert "return True" in src and "return False" in src


def test_failures_are_retried_a_bounded_number_of_times():
    import scheduler.instrument_sync_scheduler as m

    assert m.RETRY_SECONDS > 0
    assert m.MAX_DAILY_ATTEMPTS > 1
    src = inspect.getsource(m.run_forever)
    assert "next_attempt_at" in src
    # Retries must stop eventually rather than hammering the upstream host.
    assert "attempts < MAX_DAILY_ATTEMPTS" in src


def test_the_attempt_counter_resets_each_day():
    import scheduler.instrument_sync_scheduler as m

    src = inspect.getsource(m.run_forever)
    assert "attempt_date != today" in src
    assert "attempts = 0" in src


def test_the_master_download_retries_with_backoff():
    import downloader.sync_instruments as s

    assert s.MASTER_MAX_RETRIES > 1
    assert s.MASTER_INITIAL_BACKOFF > 0
    src = inspect.getsource(s.get_instrument_master)
    assert "backoff *= 2" in src
    # The final attempt must re-raise, not swallow the error and return
    # an empty frame that would wipe the instruments table.
    assert "raise" in src


def test_the_download_still_fails_loudly_when_exhausted():
    """A silent failure here would upsert nothing and deactivate everything."""
    import downloader.sync_instruments as s

    tree = ast.parse(inspect.getsource(s.get_instrument_master).lstrip())
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert raises, "exhausted retries must propagate"


def test_sync_all_downloads_before_touching_the_database():
    """Ordering matters: a download failure must not leave the DB half-updated."""
    import downloader.sync_instruments as s

    src = inspect.getsource(s.sync_all)
    download_at = src.index("get_instrument_master()")
    connect_at = src.index("get_connection()")
    assert download_at < connect_at
