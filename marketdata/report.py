"""Build a data-quality report for stored market data, and a CLI to print it.

Usage
-----
    python -m marketdata.report --instrument "Nifty 50" \
        --from 2026-01-01 --to 2026-09-01 [--timeframes 1m,5m] [--json]

Every number the report shows is computed from rows actually read out of
PostgreSQL through :func:`marketdata.access.get_market_data` and passed through
the validators in :mod:`marketdata.validation`.  Nothing is estimated.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Iterable, Sequence

from marketdata.access import InstrumentNotFound, get_market_data, resolve_instrument_id
from marketdata.quality import DataQualityReport, build_quality_report
from marketdata.sessions import MarketSession, REGULAR_SESSION, TradingDayFn
from marketdata.validation import ValidationResult, validate_ohlcv, validate_ticks
from scheduler.nse_calendar import NSE_HOLIDAYS_2026

DATASET_NAMES = {"tick": "tick", "1m": "ohlcv_1m", "5m": "ohlcv_5m"}
DEFAULT_TIMEFRAMES = ("1m", "5m")

#: Reading every tick of a long period is pointless for a quality summary and
#: can be enormous; the report says so explicitly when it truncates.
DEFAULT_MAX_TICKS = 500_000

#: Years for which scheduler.nse_calendar ships a curated holiday list. Outside
#: these, only weekends are known to be non-trading, so gap counts are
#: over-stated — the report carries a note rather than hiding the caveat.
_CALENDAR_YEARS = frozenset(day.year for day in NSE_HOLIDAYS_2026)


def _calendar_note(start: datetime, end: datetime) -> str | None:
    uncovered = sorted(
        {year for year in range(start.year, end.year + 1)} - _CALENDAR_YEARS
    )
    if not uncovered:
        return None
    years = ", ".join(str(year) for year in uncovered)
    return (
        f"The bundled NSE holiday calendar has no entries for {years}; only "
        "weekends are excluded there, so exchange holidays in those years are "
        "counted as unexpected gaps. Extend it via the NSE_HOLIDAYS "
        "environment variable (see scheduler/nse_calendar.py)."
    )


def assess_instrument(
    conn,
    instrument: str | int,
    start: datetime,
    end: datetime,
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
    *,
    session: MarketSession = REGULAR_SESSION,
    is_trading_day: TradingDayFn | None = None,
    max_ticks: int = DEFAULT_MAX_TICKS,
    generated_at: datetime | None = None,
) -> DataQualityReport:
    """Validate every requested dataset for one instrument over ``[start, end)``.

    Returns a :class:`~marketdata.quality.DataQualityReport`. Datasets that were
    not requested are simply absent from the report — never reported as clean.
    """
    resolve_instrument_id(conn, instrument)  # fail fast on an unknown symbol

    results: dict[str, ValidationResult] = {}
    timestamps: dict[str, list[datetime]] = {}
    notes: list[str] = []

    for timeframe in timeframes:
        key = timeframe.lower()
        if key in ("tick", "ticks"):
            ticks = get_market_data(
                conn, instrument, start, end, "tick", limit=max_ticks + 1
            )
            if len(ticks) > max_ticks:
                ticks = ticks[:max_ticks]
                notes.append(
                    f"Tick validation truncated to the first {max_ticks:,} rows "
                    "of the period; counts below cover only that slice."
                )
            records = [t.as_dict() for t in ticks]
            results["tick"] = validate_ticks(
                records, session=session, is_trading_day=is_trading_day
            )
            timestamps["tick"] = [t.timestamp for t in ticks]
        elif key in DATASET_NAMES:
            bars = get_market_data(conn, instrument, start, end, key)
            records = [b.as_dict() for b in bars]
            results[DATASET_NAMES[key]] = validate_ohlcv(
                records, key, session=session, is_trading_day=is_trading_day
            )
            timestamps[DATASET_NAMES[key]] = [b.timestamp for b in bars]
        else:
            raise ValueError(
                f"Unknown timeframe {timeframe!r}. "
                f"Choose from: {', '.join(sorted(DATASET_NAMES))}"
            )

    note = _calendar_note(start, end)
    if note:
        notes.append(note)

    return build_quality_report(
        instrument=str(instrument),
        start=start,
        end=end,
        results=results,
        timestamps=timestamps,
        generated_at=generated_at or datetime.now(),
        notes=notes,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_moment(text: str) -> datetime:
    """Accept ``YYYY-MM-DD`` or a full ISO-8601 naive timestamp."""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not an ISO date or datetime (e.g. 2026-01-01 or "
            "2026-01-01T09:15:00)"
        ) from None


def _csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m marketdata.report",
        description="Data-quality report for stored market data.",
    )
    parser.add_argument(
        "--instrument", "--symbol", required=True, dest="instrument",
        help="Symbol as stored in instruments.symbol (e.g. 'Nifty 50').",
    )
    parser.add_argument(
        "--from", dest="start", type=_parse_moment, required=True,
        help="Start of the period, inclusive (ISO date or datetime, IST).",
    )
    parser.add_argument(
        "--to", dest="end", type=_parse_moment, required=True,
        help="End of the period, exclusive (ISO date or datetime, IST).",
    )
    parser.add_argument(
        "--timeframes", type=_csv, default=list(DEFAULT_TIMEFRAMES),
        help="Comma-separated subset of tick,1m,5m (default: 1m,5m).",
    )
    parser.add_argument(
        "--max-ticks", type=int, default=DEFAULT_MAX_TICKS,
        help=f"Cap on ticks read for validation (default {DEFAULT_MAX_TICKS}).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser


def _default_connect():
    """Open a database connection using the project's connection manager.

    Imported lazily so ``--help`` (and the CLI tests) do not need psycopg2.
    """
    from db.connection import ConnectionManager

    return ConnectionManager().get_connection()


def main(argv: Iterable[str] | None = None, connect=None) -> int:
    """Return 0 on PASS, 1 on WARNING, 2 on FAIL, 3 on a usage/data error.

    ``connect`` is a zero-argument callable returning an open DB connection;
    it exists so the CLI can be exercised without a live database.
    """
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    conn = (connect or _default_connect)()
    try:
        report = assess_instrument(
            conn,
            args.instrument,
            args.start,
            args.end,
            args.timeframes,
            max_ticks=args.max_ticks,
        )
    except (InstrumentNotFound, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(report.render())

    return {"PASS": 0, "WARNING": 1, "FAIL": 2}[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
