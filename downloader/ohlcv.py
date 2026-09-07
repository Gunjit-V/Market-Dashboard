import os
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import pyotp
from SmartApi import SmartConnect
from db.connection import ConnectionManager
from marketdata.ingest import (
    log_screen_result,
    screen_candles,
    write_quarantine,
)

load_dotenv()

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PASSWORD")
TOTP_TOKEN = os.getenv("ANGEL_TOTP_TOKEN")

CHUNK_DAYS = 5
API_DELAY = 2.0  # Delay between API calls to avoid rate limiting
DEFAULT_DAYS = 1000
MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0  # Start with 2 second backoff

# Derivative contracts (futures/options) are short-lived and never trade
# before they are listed, so paginating DEFAULT_DAYS back for them wastes
# API calls on empty chunks. Cap their first-run backfill window instead.
DERIVATIVE_TYPES = {"FUTIDX", "OPTIDX", "FUTSTK", "OPTSTK"}
DERIVATIVE_DAYS = 90

# ── Interval configuration ───────────────────────────────────────────────────
# Each entry maps a short label to the Angel One API interval name, the
# PostgreSQL table that stores the candles, and the bar width in minutes
# (used for the incremental cursor offset).

INTERVALS = {
    "1m": {"api_interval": "ONE_MINUTE",  "table": "ohlcv_1min", "minutes": 1},
    "5m": {"api_interval": "FIVE_MINUTE", "table": "ohlcv_5min", "minutes": 5},
}


def _interval_label(cfg: dict) -> str:
    """Reverse-lookup the short interval label for a config dict."""
    for label, candidate in INTERVALS.items():
        if candidate is cfg or candidate == cfg:
            return label
    raise ValueError(f"Unknown interval config {cfg!r}")


def _interval_config(interval: str) -> dict:
    """Return the config dict for *interval*, or raise on invalid input."""
    try:
        return INTERVALS[interval]
    except KeyError:
        valid = ", ".join(sorted(INTERVALS))
        raise ValueError(
            f"Unknown interval {interval!r}. Choose from: {valid}"
        )


# ── Authentication ────────────────────────────────────────────────────────────

def authenticate():
    """Authenticate with Angel One SmartAPI with retry logic."""
    backoff = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES):
        try:
            smartApi = SmartConnect(API_KEY)
            totp = pyotp.TOTP(TOTP_TOKEN).now()
            data = smartApi.generateSession(CLIENT_ID, PASSWORD, totp)

            if not data["status"]:
                # Do not write the complete authentication response to a
                # scheduled-task log: it can contain session credentials.
                print(
                    "Authentication failed: "
                    f"{data.get('errorcode', 'unknown error')} "
                    f"{data.get('message', '')}".strip()
                )
                return None

            print("Authentication successful!")
            return smartApi

        except Exception as e:
            print(
                "Authentication error "
                f"(attempt {attempt + 1}/{MAX_RETRIES}): {type(e).__name__}"
            )
            if attempt < MAX_RETRIES - 1:
                print(f"Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2
            else:
                print("Max authentication retries reached.")
                return None

    return None


# ── Instrument lookup ─────────────────────────────────────────────────────────

def get_instruments(
    conn,
    instrument_types: list[str] | None = None,
    names: list[str] | None = None,
    symbols: list[str] | None = None,
) -> list:
    """Fetch active instruments filtered by type, name, and/or symbol.

    Filters are combined with AND.  Within each filter the values are ORed.
    At least one filter must be provided; otherwise an empty list is returned.
    """
    filters = []
    params = []

    if instrument_types:
        placeholders = ",".join(["%s"] * len(instrument_types))
        filters.append(f"instrument_type IN ({placeholders})")
        params.extend(instrument_types)

    if names:
        name_clauses = " OR ".join(["name = %s"] * len(names))
        filters.append(f"({name_clauses})")
        params.extend(names)

    if symbols:
        placeholders = ",".join(["%s"] * len(symbols))
        filters.append(f"UPPER(symbol) IN ({placeholders})")
        params.extend(symbol.upper() for symbol in symbols)

    if not filters:
        return []

    filters.append("is_active = TRUE")
    where_clause = " AND ".join(filters)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, symbol, token, exchange, instrument_type, expiry
            FROM instruments
            WHERE {where_clause}
            ORDER BY instrument_type, expiry, symbol
            """,
            params,
        )
        return cur.fetchall()


# ── Incremental cursor ────────────────────────────────────────────────────────

def get_last_downloaded_at(conn, instrument_id: int, table: str):
    """Return the latest candle timestamp for *instrument_id* in *table*.

    Querying the data table directly is safer than ``download_log`` because
    the log is shared across intervals and a partially completed run resumes
    correctly from the actual data.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MAX(timestamp) FROM {table} WHERE instrument_id = %s",
            (instrument_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


# ── Timestamp normalisation ───────────────────────────────────────────────────

def normalize_timestamp(value):
    """Return a naive IST timestamp for PostgreSQL's timestamp column."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone(timedelta(hours=5, minutes=30)))
        parsed = parsed.replace(tzinfo=None)
    return parsed


# ── Download log ──────────────────────────────────────────────────────────────

def write_download_log(
    conn,
    instrument_id: int,
    status: str,
    last_downloaded_at=None,
    candles_inserted: int = 0,
    candles_skipped: int = 0,
    error_message: str = None,
):
    """Write a log entry to the download_log table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO download_log (
                instrument_id, last_downloaded_at, last_run_at,
                status, candles_inserted, candles_skipped, error_message
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                instrument_id,
                last_downloaded_at,
                datetime.now(),
                status,
                candles_inserted,
                candles_skipped,
                error_message,
            ),
        )
    conn.commit()


# ── API fetch with retry ─────────────────────────────────────────────────────

# Angel One signals throttling two different ways: a structured JSON body with
# errorcode AB1004, and a plain-text body ("Access denied because of exceeding
# access rate") that the SDK cannot parse, so it surfaces as a DataException.
# Both mean the same thing and both deserve the same backoff — without this,
# the text variant fell into the generic handler and was logged as an opaque
# "DataException", which reads like the data is unavailable rather than
# throttled.
# Angel One uses two throttle codes: AB1004 and AB1021 ("Too many requests").
# Only the first was handled; AB1021 fell through to the generic branch and
# returned no candles without retrying.
RATE_LIMIT_ERRORCODES = ("AB1004", "AB1021")
RATE_LIMIT_ERRORCODE = RATE_LIMIT_ERRORCODES[0]  # kept for compatibility
RATE_LIMIT_MARKERS = (
    "exceeding access rate",
    "access denied because of exceeding",
    "rate limit",
    "too many requests",
)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Whether *exc* is Angel One's unparseable rate-limit rejection."""
    text = str(exc).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)



def fetch_candle_data(
    smartApi,
    token: str,
    exchange: str,
    from_date: str,
    to_date: str,
    api_interval: str,
) -> list:
    """Fetch candle data from Angel One API with retry logic for rate limiting."""
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": api_interval,
        "fromdate": from_date,
        "todate": to_date,
    }

    backoff = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES):
        try:
            response = smartApi.getCandleData(params)

            if response and response.get("status"):
                return response.get("data", [])
            elif response and response.get("errorcode") in RATE_LIMIT_ERRORCODES:
                # Rate limiting error, retry with exponential backoff
                if attempt < MAX_RETRIES - 1:
                    print(
                        f"    Rate limited. Retrying in {backoff}s... "
                        f"(Attempt {attempt + 1}/{MAX_RETRIES})"
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                else:
                    print("    Max retries reached for rate limiting.")
                    return []
            else:
                return []

        except Exception as e:
            # Log the message, not just the class. A bare "DataException" hides
            # that this is throttling and reads as though the data does not
            # exist, which is a materially different (and unfixable) problem.
            rate_limited = _is_rate_limit_error(e)
            if rate_limited:
                # The full SDK text is a JSON-parse complaint wrapping the real
                # cause; naming the cause is enough, and this fires often.
                print(
                    f"    Rate limited by Angel One "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
            else:
                print(
                    f"    Exception fetching candle data "
                    f"(attempt {attempt + 1}/{MAX_RETRIES}): "
                    f"{type(e).__name__}: {e}"
                )
            if attempt < MAX_RETRIES - 1:
                print(f"    Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2
            else:
                if rate_limited:
                    print("    Max retries reached for rate limiting.")
                return []

    return []


# ── Persist candles ───────────────────────────────────────────────────────────

def save_candles_to_db(
    conn, instrument_id: int, candles: list, table: str, interval: str = "5m"
) -> tuple[int, int]:
    """Save candle data to *table* (``ohlcv_1min`` or ``ohlcv_5min``).

    Candles are screened against the data contract first (see
    ``marketdata/ingest.py``).  In the default ``report`` mode this only
    logs — every candle that would have been inserted before Phase 1 is still
    inserted, unchanged.  Set ``MARKETDATA_VALIDATION_MODE=reject`` to withhold
    contract-violating candles instead, optionally writing them to
    ``MARKETDATA_QUARANTINE_FILE`` for inspection.  Nothing is ever repaired.
    """
    if not candles:
        return 0, 0

    # Validation must never be able to stop ingestion: if screening itself
    # fails, fall back to the pre-Phase-1 behaviour of inserting everything.
    try:
        screened = screen_candles(
            candles,
            instrument_id,
            interval,
            normalize_timestamp=normalize_timestamp,
        )
        log_screen_result(screened, context=f"{table} instrument_id={instrument_id}")
        if screened.rejected:
            write_quarantine(screened, instrument_id, interval)
            print(
                f"    Quarantined {len(screened.rejected)} invalid candle(s) "
                f"({screened.result.by_code()})"
            )
        candles = list(screened.accepted)
    except Exception as e:
        print(f"    Candle validation skipped ({type(e).__name__}); inserting as-is")

    inserted = 0
    skipped = 0

    with conn.cursor() as cur:
        for candle in candles:
            cur.execute("SAVEPOINT candle_insert")
            try:
                timestamp = candle[0]
                open_ = candle[1]
                high = candle[2]
                low = candle[3]
                close = candle[4]
                volume = candle[5]

                cur.execute(
                    f"""
                    INSERT INTO {table}
                        (instrument_id, timestamp, open, high, low, close, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instrument_id, timestamp) DO NOTHING
                    """,
                    (
                        instrument_id,
                        normalize_timestamp(timestamp),
                        open_,
                        high,
                        low,
                        close,
                        volume,
                    ),
                )

                if cur.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1

            except Exception as e:
                print(f"    Error inserting candle: {type(e).__name__}")
                cur.execute("ROLLBACK TO SAVEPOINT candle_insert")
                continue
            finally:
                cur.execute("RELEASE SAVEPOINT candle_insert")

    conn.commit()
    return inserted, skipped


# ── Per-instrument download ───────────────────────────────────────────────────

def download_for_instrument(
    smartApi, conn, instrument: tuple, days: int, cfg: dict
) -> tuple[int, int, datetime | None]:
    """Download historical data for a single instrument incrementally."""
    instrument_id, symbol, token, exchange, instrument_type, expiry = instrument
    table = cfg["table"]
    bar_minutes = cfg["minutes"]
    api_interval = cfg["api_interval"]

    end_date = datetime.now()
    # Never request candles beyond an instrument's expiry. Expired contracts
    # are intentionally retained here because their historical data is valid.
    if expiry:
        end_date = min(end_date, datetime.combine(expiry, datetime.max.time()))

    # Check the data table for the incremental cursor.
    last_downloaded_at = get_last_downloaded_at(conn, instrument_id, table)
    if last_downloaded_at:
        # Request a small overlap; ON CONFLICT makes the overlap safe and
        # protects against a cursor that lands on a partially returned bar.
        start_date = last_downloaded_at + timedelta(minutes=bar_minutes)
    else:
        # For expired contracts, the requested history must be relative to
        # expiry rather than today's date. Derivative contracts get a much
        # shorter window than `days`, since they cannot have data before
        # they were listed (see DERIVATIVE_DAYS above).
        effective_days = min(days, DERIVATIVE_DAYS) if instrument_type in DERIVATIVE_TYPES else days
        start_date = end_date - timedelta(days=effective_days)

    if start_date >= end_date:
        return 0, 0, last_downloaded_at

    total_inserted = 0
    total_skipped = 0
    last_candle_time = None

    current_start = start_date
    while current_start < end_date:
        current_end = min(current_start + timedelta(days=CHUNK_DAYS), end_date)

        from_str = current_start.strftime("%Y-%m-%d %H:%M")
        to_str = current_end.strftime("%Y-%m-%d %H:%M")

        candles = fetch_candle_data(
            smartApi, token, exchange, from_str, to_str, api_interval
        )

        if candles:
            inserted, skipped = save_candles_to_db(
                conn, instrument_id, candles, table, interval=_interval_label(cfg)
            )
            total_inserted += inserted
            total_skipped += skipped
            # Track the latest candle timestamp, independent of API ordering.
            last_candle_time = max(
                normalize_timestamp(candle[0]) for candle in candles
            )

        current_start = current_end
        time.sleep(API_DELAY)

    return total_inserted, total_skipped, last_candle_time


# ── 5-min backfill from 1-min data ───────────────────────────────────────────

def backfill_from_one_minute(conn) -> tuple[int, int]:
    """Build complete five-minute bars from existing one-minute data.

    This is useful when the upstream five-minute endpoint was unavailable.
    ``DO NOTHING`` preserves every existing five-minute row and makes the
    operation safe to repeat.
    """
    with conn.cursor() as cur:
        cur.execute("""
            WITH grouped AS (
                SELECT
                    instrument_id,
                    date_trunc('hour', timestamp)
                        + floor(extract(minute FROM timestamp) / 5)
                          * interval '5 minutes' AS bucket,
                    array_agg(open ORDER BY timestamp) AS opens,
                    MAX(high) AS high,
                    MIN(low) AS low,
                    array_agg(close ORDER BY timestamp DESC) AS closes,
                    SUM(volume)::BIGINT AS volume,
                    COUNT(*) AS minute_count
                FROM ohlcv_1min
                GROUP BY instrument_id, bucket
                HAVING COUNT(*) = 5
            ), inserted AS (
                INSERT INTO ohlcv_5min
                    (instrument_id, timestamp, open, high, low, close, volume)
                SELECT instrument_id, bucket, opens[1], high, low, closes[1], volume
                FROM grouped
                ON CONFLICT (instrument_id, timestamp) DO NOTHING
                RETURNING 1
            )
            SELECT COUNT(*) FROM inserted
        """)
        inserted = cur.fetchone()[0]
        conn.commit()

    return inserted, 0


# ── Main entry point ─────────────────────────────────────────────────────────

def download_historical_data(
    interval: str = "5m",
    instrument_types: list[str] | None = None,
    names: list[str] | None = None,
    symbols: list[str] | None = None,
    days: int = DEFAULT_DAYS,
    smart_api=None,
):
    """Download incremental candle data for selected instruments.

    Parameters
    ----------
    interval : str
        Candle interval — ``"1m"`` or ``"5m"``.
    instrument_types : list[str] | None
        Filter by ``instrument_type`` column (e.g. ``["AMXIDX", "FUTIDX"]``).
    names : list[str] | None
        Filter by exact ``name`` column (e.g. ``["Nifty 50", "NIFTY"]``).
    symbols : list[str] | None
        Filter by ``symbol`` column (case-insensitive).
    days : int
        How many days of history to fetch on the first run.
    smart_api : SmartConnect | None
        Reuse an existing authenticated session (useful for schedulers).
    """
    cfg = _interval_config(interval)

    smartApi = smart_api or authenticate()
    if not smartApi:
        return

    manager = ConnectionManager()
    conn = None

    try:
        conn = manager.get_connection()

        instruments = get_instruments(conn, instrument_types, names, symbols)
        total = len(instruments)
        print(
            f"\nFound {total} instruments to download: "
            f"interval={interval}, types={instrument_types}, names={names}"
        )
        print(
            f"Downloading up to {days} days of {interval} data "
            f"(incremental)...\n"
        )

        grand_total_inserted = 0
        grand_total_skipped = 0
        failed = []

        for idx, instrument in enumerate(instruments, start=1):
            instrument_id, symbol, token, exchange, instrument_type, expiry = instrument
            print(f"[{idx}/{total}] {symbol} ({instrument_type})", end=" → ")

            try:
                result = download_for_instrument(
                    smartApi, conn, instrument, days, cfg
                )
                inserted, skipped, last_candle_time = result

                grand_total_inserted += inserted
                grand_total_skipped += skipped

                if inserted == 0 and skipped == 0:
                    # No data returned from API
                    write_download_log(
                        conn, instrument_id,
                        status="no_data",
                    )
                    print("No data.")
                else:
                    # Successful download
                    write_download_log(
                        conn, instrument_id,
                        status="success",
                        last_downloaded_at=last_candle_time,
                        candles_inserted=inserted,
                        candles_skipped=skipped,
                    )
                    print(f"Inserted: {inserted}, Skipped: {skipped}")

            except Exception as e:
                write_download_log(
                    conn, instrument_id,
                    status="failed",
                    error_message=f"{type(e).__name__} while downloading candle data",
                )
                print(f"FAILED: {type(e).__name__}")
                failed.append(symbol)
                continue

        print(f"\n{'='*60}")
        print("Download Complete!")
        print(f"Total Inserted : {grand_total_inserted}")
        print(f"Total Skipped  : {grand_total_skipped}")
        print(f"Failed         : {len(failed)}")
        if failed:
            print(f"Failed symbols : {failed}")

    except Exception as e:
        print(f"Error: {type(e).__name__}")

    finally:
        if conn:
            conn.close()
            print("Connection closed.")


if __name__ == "__main__":
    iv = ["1m", "5m"]
    for interval in iv:
        download_historical_data(
            interval=interval,
            instrument_types=["AMXIDX", "FUTIDX", "OPTIDX"],
            names=["NIFTY", "BANKNIFTY", "SENSEX", "BANKEX"],
            days=DEFAULT_DAYS,
        )
