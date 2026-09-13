"""CLI: generate a versioned historical feature dataset.

    python -m features.build --instrument "Nifty 50" --timeframe 5m
    python -m features.build --instrument "Nifty 50" --from 2026-01-01 --to 2026-09-10
    python -m features.build --instrument "Nifty 50" --append
    python -m features.build --instrument "Nifty 50" --timeframe 1m --dry-run

``--append`` rebuilds from the last session already stored rather than from
the start, which turns a daily refresh from tens of minutes into seconds. It
restarts *at* that session rather than after it: a dataset written while a
session was still running holds only part of it, and appending strictly
after would leave the remainder missing for good.

Exit codes follow ``marketdata.report`` so this is usable as a scheduled check:
0 the build succeeded and its source data was clean, 1 it succeeded with
warnings, 2 the source data carried errors, 3 a usage problem.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from features.dataset import BuildStats, build_dataset, observed_sessions
from features.quality import FAIL, PASS, WARNING
from features.registry import feature_set_version, label_set, state_features
from features.store import (
    DEFAULT_ROOT,
    append_dataset,
    list_datasets,
    paths_for,
    resume_from,
    write_dataset,
)

EXIT_OK = 0
EXIT_WARNING = 1
EXIT_FAIL = 2
EXIT_USAGE = 3


def parse_day(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not an ISO date (YYYY-MM-DD)"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m features.build",
        description="Generate a versioned, point-in-time-safe feature dataset.",
    )
    parser.add_argument("--instrument", default="Nifty 50")
    parser.add_argument("--timeframe", default="5m", choices=("1m", "5m"))
    parser.add_argument("--from", dest="start", type=parse_day, default=None,
                        help="ISO start date; defaults to the earliest session")
    parser.add_argument("--to", dest="end", type=parse_day, default=None,
                        help="ISO end date; defaults to the latest session")
    parser.add_argument("--out", default=str(DEFAULT_ROOT),
                        help=f"output directory (default {DEFAULT_ROOT})")
    parser.add_argument("--append", action="store_true",
                        help="build only sessions from the last stored one "
                             "onward and merge into the existing dataset")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be built, write nothing")
    parser.add_argument("--no-validate", action="store_true",
                        help="skip per-session source validation")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--list", action="store_true",
                        help="list existing datasets and exit")
    return parser


def _bounds(conn, instrument: str, timeframe: str) -> tuple[date, date] | None:
    table = {"1m": "ohlcv_1min", "5m": "ohlcv_5min"}[timeframe]
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT min(o.timestamp)::date, max(o.timestamp)::date
                FROM {table} o JOIN instruments i ON i.id = o.instrument_id
                WHERE UPPER(i.symbol) = UPPER(%s)""",
            (instrument,),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return row[0], row[1]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        rows = list_datasets(args.out)
        if not rows:
            print(f"No datasets under {args.out}")
            return EXIT_OK
        for row in rows:
            print(
                f"{row['stem']:<44} {row['rows'] or 0:>8} rows  "
                f"{row['sessions'] or 0:>4} sessions  {row['source_verdict']}"
            )
        return EXIT_OK

    from db.connection import ConnectionManager

    conn = ConnectionManager().get_connection()

    bounds = _bounds(conn, args.instrument, args.timeframe)
    if bounds is None:
        print(f"No {args.timeframe} data for {args.instrument!r}", file=sys.stderr)
        return EXIT_USAGE
    start = args.start or bounds[0]
    end = args.end or bounds[1]
    if end < start:
        print("--to must not be before --from", file=sys.stderr)
        return EXIT_USAGE

    fs = state_features(args.timeframe)
    labels = label_set(args.timeframe)
    target = paths_for(args.instrument, fs, args.out)

    # An incremental run restarts at the last stored session. With no dataset
    # yet -- or an explicit --from -- it is simply a full build, so there is no
    # separate code path to keep in step.
    resume = None
    if args.append and args.start is None:
        resume = resume_from(args.instrument, fs, args.out)
        if resume is not None:
            start = max(start, resume)

    if not args.quiet:
        print(f"instrument   {args.instrument}  ({args.timeframe})")
        print(f"range        {start} .. {end}")
        print(f"features     {len(fs)}  version {feature_set_version(fs)}")
        print(f"labels       {len(labels)}  version {feature_set_version(labels)}")
        print(f"output       {target.parquet}")
        if resume is not None:
            print(f"mode         append -- rebuilding from {resume} onward")
        elif args.append:
            print("mode         append requested, no existing dataset: full build")

    if args.dry_run:
        sessions = observed_sessions(conn, args.instrument, start, end, args.timeframe)
        from features.spec import BARS_PER_SESSION

        print(f"\nwould build  {len(sessions)} sessions, "
              f"~{len(sessions) * BARS_PER_SESSION[args.timeframe]:,} rows "
              f"({len(fs) * 2 + len(labels) * 2 + 4} columns)")
        print("nothing written (--dry-run)")
        return EXIT_OK

    stats = BuildStats()

    def progress(day, running: BuildStats) -> None:
        if not args.quiet and running.sessions % 25 == 0:
            print(f"  ... {running.sessions:>4} sessions, {running.rows:>7,} rows"
                  f"  (at {day})")

    if not args.quiet:
        print("\nbuilding ...")
    rows = build_dataset(
        conn,
        args.instrument,
        start,
        end,
        args.timeframe,
        fs=fs,
        labels=labels,
        stats=stats,
        validate=not args.no_validate,
        progress=progress,
    )
    if resume is not None:
        written = append_dataset(
            rows, args.instrument, fs, labels, stats, resume, end, root=args.out
        )
    else:
        written = write_dataset(
            rows, args.instrument, fs, labels, stats, start, end, root=args.out
        )

    verdict = _report(stats, written, args.quiet)
    return {PASS: EXIT_OK, WARNING: EXIT_WARNING, FAIL: EXIT_FAIL}[verdict]


def _report(stats: BuildStats, written, quiet: bool) -> str:
    from features.quality import worst_verdict

    verdict = worst_verdict(stats.source_quality)
    if quiet:
        return verdict

    size = Path(written.parquet).stat().st_size / (1024 * 1024)
    print(f"\nwrote        {written.parquet}  ({size:.1f} MB)")
    print(f"             {written.manifest}")
    print(f"rows         {stats.rows:,} across {stats.sessions} sessions")

    valid = sum(n for k, n in stats.status_counts.items() if k.endswith(":valid"))
    total = sum(stats.status_counts.values())
    if total:
        print(f"features     {valid:,}/{total:,} valid ({100 * valid / total:.1f}%)")
    lvalid = sum(
        n for k, n in stats.label_status_counts.items() if k.endswith(":valid")
    )
    ltotal = sum(stats.label_status_counts.values())
    if ltotal:
        print(f"labels       {lvalid:,}/{ltotal:,} valid ({100 * lvalid / ltotal:.1f}%)")
    print(f"source data  {verdict}")
    return verdict


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
