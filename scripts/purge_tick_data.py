"""Archive tick_data to Parquet, then purge it.

Why this exists
---------------
Ticks collected before the timestamp fix (see docs/point-in-time-data.md 5.1)
carry ``datetime.now()`` rather than an exchange clock in ~98.7% of rows. The
original event times were never stored, so those rows cannot be repaired and
are not usable as precise event times. This script archives them to a
compressed columnar file and then frees the space in PostgreSQL.

Safety
------
* **Dry-run by default.** Nothing is written or deleted without ``--execute``.
* **Archive before delete.** The delete step refuses to run unless the archive
  was written and verified to hold the same number of rows.
* **Batched deletes** so a multi-million-row purge does not hold one long
  transaction or lock the table against the live collector.
* ``--keep-from`` retains recent days; omit it to purge everything.

Usage
-----
    # See what would happen (default)
    python scripts/purge_tick_data.py

    # Archive + purge everything, for real
    python scripts/purge_tick_data.py --execute

    # Keep 2026-09-01 onward
    python scripts/purge_tick_data.py --keep-from 2026-09-01 --execute

    # Archive only; leave the table untouched
    python scripts/purge_tick_data.py --archive-only --execute
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

# Ensure the project root is importable when run as a plain script.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.connection import ConnectionManager  # noqa: E402

DEFAULT_ARCHIVE_DIR = PROJECT_ROOT / "data" / "archive"

# Rows are streamed in chunks so a 4.8 GB table never lands in memory at once.
FETCH_BATCH = 100_000
DELETE_BATCH = 50_000


# The regular NSE session. Rows stamped outside it are snapshots the feed
# kept publishing after the close (unchanged LTP, zero volume) that the
# collector stored before it learned to end its session at 15:30.
SESSION_OPEN = "09:15"
SESSION_CLOSE = "15:30"


def _where_clause(
    keep_from: date | None, out_of_session_only: bool = False
) -> tuple[str, list]:
    """SQL predicate selecting the rows to archive and purge."""
    clauses: list[str] = []
    params: list = []

    if keep_from is not None:
        clauses.append("timestamp < %s")
        params.append(keep_from)

    if out_of_session_only:
        clauses.append(
            "(timestamp::time < %s::time OR timestamp::time >= %s::time)"
        )
        params.extend([SESSION_OPEN, SESSION_CLOSE])

    if not clauses:
        return "TRUE", []
    return " AND ".join(clauses), params


def summarize(conn, keep_from: date | None, out_of_session_only: bool = False) -> dict:
    """Report what would be purged, without touching anything."""
    where, params = _where_clause(keep_from, out_of_session_only)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), min(timestamp)::date, max(timestamp)::date, "
            "count(DISTINCT timestamp::date) "
            f"FROM tick_data WHERE {where}",
            params,
        )
        doomed, first, last, days = cur.fetchone()

        cur.execute("SELECT count(*) FROM tick_data")
        total = cur.fetchone()[0]

        cur.execute("SELECT pg_size_pretty(pg_total_relation_size('tick_data'))")
        size = cur.fetchone()[0]

        cur.execute(
            "SELECT count(*) FILTER (WHERE date_part('microseconds', timestamp) = 0) "
            f"FROM tick_data WHERE {where}",
            params,
        )
        feed_clock = cur.fetchone()[0]

    return {
        "total_rows": total,
        "rows_to_purge": doomed,
        "rows_retained": total - doomed,
        "first_day": first,
        "last_day": last,
        "distinct_days": days,
        "table_size": size,
        "feed_clock_rows": feed_clock,
    }


def _parquet_schema():
    import pyarrow as pa

    return pa.schema([
        ("id", pa.int64()),
        ("instrument_id", pa.int32()),
        ("timestamp", pa.timestamp("us")),
        ("ltp", pa.float64()),
        ("ltq", pa.int64()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("avg_trade_price", pa.float64()),
        ("volume", pa.int64()),
        ("total_buy_qty", pa.int64()),
        ("total_sell_qty", pa.int64()),
        ("open_interest", pa.int64()),
        ("best_5_buy", pa.string()),
        ("best_5_sell", pa.string()),
        ("created_at", pa.timestamp("us")),
    ])


def _write_batch(writer, schema, rows: list) -> int:
    """Append one batch, converting psycopg2 Decimals to plain floats."""
    import pyarrow as pa

    columns = list(zip(*rows))
    arrays = []
    for field, column in zip(schema, columns):
        values = [float(v) if isinstance(v, Decimal) else v for v in column]
        arrays.append(pa.array(values, type=field.type))
    writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
    return len(rows)


def archive(conn, keep_from: date | None, out_path: Path,
            out_of_session_only: bool = False) -> int:
    """Stream the doomed rows into a Snappy-compressed Parquet file.

    Returns the number of rows written. The JSONB order-book columns are cast
    to text: they round-trip exactly, and Parquet compresses the repeated
    structure well without needing a nested schema.
    """
    import pyarrow.parquet as pq

    where, params = _where_clause(keep_from, out_of_session_only)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    schema = _parquet_schema()

    written = 0
    writer = None
    # A named (server-side) cursor keeps the result set on the server rather
    # than materialising millions of rows in the client.
    with conn.cursor(name="tick_archive_cursor") as cur:
        cur.itersize = FETCH_BATCH
        cur.execute(
            "SELECT id, instrument_id, timestamp, ltp, ltq, "
            "open, high, low, close, avg_trade_price, volume, "
            "total_buy_qty, total_sell_qty, open_interest, "
            "best_5_buy::text, best_5_sell::text, created_at "
            f"FROM tick_data WHERE {where} ORDER BY id",
            params,
        )
        try:
            writer = pq.ParquetWriter(out_path, schema, compression="snappy")
            batch: list = []
            for row in cur:
                batch.append(row)
                if len(batch) >= FETCH_BATCH:
                    written += _write_batch(writer, schema, batch)
                    print(f"  archived {written:,} rows...", flush=True)
                    batch = []
            if batch:
                written += _write_batch(writer, schema, batch)
        finally:
            if writer is not None:
                writer.close()

    return written


def verify(path: Path, expected_rows: int) -> bool:
    """Confirm the archive is readable and complete before anything is deleted."""
    import pyarrow.parquet as pq

    parquet_file = pq.ParquetFile(path)
    actual = parquet_file.metadata.num_rows
    print(f"  archive rows : {actual:,} (expected {expected_rows:,})")
    print(f"  archive size : {path.stat().st_size / 2 ** 20:.1f} MiB")
    if actual != expected_rows:
        print("  MISMATCH - refusing to delete.", file=sys.stderr)
        return False
    # Touch the first row group to prove the file actually decodes.
    if parquet_file.metadata.num_row_groups:
        parquet_file.read_row_group(0, columns=["timestamp", "ltp"])
    return True


def purge(conn, keep_from: date | None, out_of_session_only: bool = False) -> int:
    """Delete the archived rows in batches, committing as it goes."""
    where, params = _where_clause(keep_from, out_of_session_only)
    deleted = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tick_data WHERE id IN ("
                f"SELECT id FROM tick_data WHERE {where} "
                f"ORDER BY id LIMIT {DELETE_BATCH})",
                params,
            )
            batch = cur.rowcount
        conn.commit()
        if not batch:
            break
        deleted += batch
        print(f"  deleted {deleted:,} rows...", flush=True)
    return deleted


def reclaim(conn) -> None:
    """VACUUM FULL to return the freed pages to the filesystem.

    Plain VACUUM only marks pages reusable; after deleting millions of rows the
    file stays large. VACUUM FULL rewrites the table, so it takes an exclusive
    lock - run it while the collector is stopped.
    """
    old_isolation = conn.isolation_level
    conn.set_isolation_level(0)  # VACUUM cannot run inside a transaction
    try:
        with conn.cursor() as cur:
            print("  VACUUM FULL tick_data (exclusive lock, please wait)...")
            cur.execute("VACUUM FULL ANALYZE tick_data")
    finally:
        conn.set_isolation_level(old_isolation)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/purge_tick_data.py",
        description="Archive tick_data to Parquet, then purge it.",
    )
    parser.add_argument(
        "--keep-from", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
        help="Retain ticks on/after this date. Omit to purge every row.",
    )
    parser.add_argument(
        "--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR,
        help=f"Where to write the Parquet file (default: {DEFAULT_ARCHIVE_DIR}).",
    )
    parser.add_argument(
        "--out-of-session-only", action="store_true",
        help=("Restrict to ticks stamped outside 09:15-15:30 — the post-close "
              "snapshots stored before the collector learned to stop at the "
              "close. Combine with --keep-from, or use alone to clean every day."),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually archive and delete. Without this, nothing is changed.",
    )
    parser.add_argument(
        "--archive-only", action="store_true",
        help="Write the archive but do not delete anything.",
    )
    parser.add_argument(
        "--skip-vacuum", action="store_true",
        help="Skip VACUUM FULL. Space stays allocated until you vacuum later.",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    conn = ConnectionManager().get_connection()
    try:
        stats = summarize(conn, args.keep_from, args.out_of_session_only)
        purge_count = stats["rows_to_purge"]

        print("=" * 62)
        print("tick_data purge")
        print("=" * 62)
        print(f"  table size now : {stats['table_size']}")
        print(f"  total rows     : {stats['total_rows']:,}")
        print(f"  to purge       : {purge_count:,} across "
              f"{stats['distinct_days']} day(s) "
              f"({stats['first_day']} -> {stats['last_day']})")
        print(f"  to retain      : {stats['rows_retained']:,}")
        pct = 100 * stats["feed_clock_rows"] / max(purge_count, 1)
        print(f"  of the purged, real feed-clock rows: "
              f"{stats['feed_clock_rows']:,} ({pct:.1f}%)")

        if purge_count == 0:
            print("\nNothing to purge.")
            return 0

        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        scope = "all" if args.keep_from is None else f"before-{args.keep_from}"
        if args.out_of_session_only:
            scope = f"out-of-session-{scope}"
        out_path = args.archive_dir / f"tick_data_{scope}_{stamp}.parquet"
        print(f"\n  archive target : {out_path}")

        if not args.execute:
            print("\nDRY RUN - nothing written or deleted.")
            print("Re-run with --execute to proceed.")
            return 0

        print("\nArchiving...")
        written = archive(conn, args.keep_from, out_path, args.out_of_session_only)
        print(f"Archived {written:,} rows.")

        print("\nVerifying archive...")
        if not verify(out_path, purge_count):
            return 2

        if args.archive_only:
            print("\n--archive-only: table left untouched.")
            return 0

        print("\nPurging...")
        deleted = purge(conn, args.keep_from, args.out_of_session_only)
        print(f"Deleted {deleted:,} rows.")

        if not args.skip_vacuum:
            print("\nReclaiming space...")
            reclaim(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT pg_size_pretty(pg_total_relation_size('tick_data'))")
            print(f"\n  table size now : {cur.fetchone()[0]}")
            cur.execute("SELECT count(*) FROM tick_data")
            print(f"  rows remaining : {cur.fetchone()[0]:,}")
        print(f"  archive        : {out_path}")
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
