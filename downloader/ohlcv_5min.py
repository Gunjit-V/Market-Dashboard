import os
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import pyotp
from SmartApi import SmartConnect
from db.connection import ConnectionManager

load_dotenv()

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PASSWORD")
TOTP_TOKEN = os.getenv("ANGEL_TOTP_TOKEN")

CHUNK_DAYS = 5
API_DELAY = 2.0  # Increased delay to avoid rate limiting
DEFAULT_DAYS = 30
MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0  # Start with 2 second backoff


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


def create_ohlcv_5min_table_if_not_exists(conn):
    """Create the ohlcv_5min table if it doesn't exist."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ohlcv_5min (
                    id                  BIGSERIAL PRIMARY KEY,
                    instrument_id       INTEGER NOT NULL REFERENCES instruments(id),
                    timestamp           TIMESTAMP NOT NULL,
                    open                DECIMAL(12, 4) NOT NULL,
                    high                DECIMAL(12, 4) NOT NULL,
                    low                 DECIMAL(12, 4) NOT NULL,
                    close               DECIMAL(12, 4) NOT NULL,
                    volume              BIGINT NOT NULL,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(instrument_id, timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_ohlcv_5min_instrument_timestamp
                ON ohlcv_5min(instrument_id, timestamp DESC);

                CREATE INDEX IF NOT EXISTS idx_ohlcv_5min_timestamp
                ON ohlcv_5min(timestamp DESC);
            """)
        conn.commit()
        print("ohlcv_5min table created or verified.")
    except Exception as e:
        print(f"Error creating ohlcv_5min table: {e}")
        conn.rollback()


def get_instruments(
    conn,
    instrument_types: list[str] | None = None,
    symbols: list[str] | None = None,
) -> list:
    """Fetch instruments filtered by type and, optionally, exact symbols."""
    filters = []
    params = []

    if instrument_types:
        placeholders = ",".join(["%s"] * len(instrument_types))
        filters.append(f"instrument_type IN ({placeholders})")
        params.extend(instrument_types)

    if symbols:
        placeholders = ",".join(["%s"] * len(symbols))
        filters.append(f"UPPER(symbol) IN ({placeholders})")
        params.extend(symbol.upper() for symbol in symbols)

    if not filters:
        return []

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


def get_last_downloaded_at(conn, instrument_id: int):
    """Get the latest persisted five-minute candle for an instrument.

    ``download_log`` is shared by the one- and five-minute downloaders, so it
    is not a safe cursor for this pipeline. The data table is the source of
    truth and also lets a partially completed run resume correctly.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(timestamp)
            FROM ohlcv_5min
            WHERE instrument_id = %s
            """,
            (instrument_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None


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
            )
        )
    conn.commit()


def fetch_candle_data(smartApi, token: str, exchange: str, from_date: str, to_date: str) -> list:
    """Fetch 5-minute candle data from Angel One API with retry logic for rate limiting."""
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": "FIVE_MINUTE",
        "fromdate": from_date,
        "todate": to_date,
    }

    backoff = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES):
        try:
            response = smartApi.getCandleData(params)

            if response and response.get("status"):
                return response.get("data", [])
            elif response and response.get("errorcode") == "AB1004":
                # Rate limiting error, retry with exponential backoff
                if attempt < MAX_RETRIES - 1:
                    print(
                        f"    Rate limited. Retrying in {backoff}s... (Attempt {attempt + 1}/{MAX_RETRIES})")
                    time.sleep(backoff)
                    backoff *= 2  # Exponential backoff
                    continue
                else:
                    print(f"    Max retries reached for rate limiting.")
                    return []
            else:
                return []

        except Exception as e:
            print(
                "    Exception fetching candle data "
                f"(attempt {attempt + 1}/{MAX_RETRIES}): {type(e).__name__}"
            )
            if attempt < MAX_RETRIES - 1:
                print(f"    Retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2
            else:
                return []

    return []


def save_candles_to_db(conn, instrument_id: int, candles: list) -> tuple[int, int]:
    """Save candle data to the ohlcv_5min table."""
    if not candles:
        return 0, 0

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

                cur.execute("""
                    INSERT INTO ohlcv_5min (instrument_id, timestamp, open, high, low, close, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instrument_id, timestamp) DO NOTHING
                """, (instrument_id, normalize_timestamp(timestamp), open_, high, low, close, volume))

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


def download_for_instrument(smartApi, conn, instrument: tuple, days: int) -> tuple[int, int]:
    """Download historical data for a single instrument incrementally."""
    instrument_id, symbol, token, exchange, instrument_type, expiry = instrument

    end_date = datetime.now()
    # Never request candles beyond an instrument's expiry. Expired contracts
    # are intentionally retained here because their historical data is valid.
    if expiry:
        end_date = min(end_date, datetime.combine(expiry, datetime.max.time()))

    # Check the five-minute table for the incremental cursor.
    last_downloaded_at = get_last_downloaded_at(conn, instrument_id)
    if last_downloaded_at:
        # Request a small overlap; ON CONFLICT makes the overlap safe and
        # protects against a cursor that lands on a partially returned bar.
        start_date = last_downloaded_at + timedelta(minutes=5)
    else:
        # For expired contracts, the requested history must be relative to
        # expiry rather than today's date.
        start_date = end_date - timedelta(days=days)

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
            smartApi, token, exchange, from_str, to_str)

        if candles:
            inserted, skipped = save_candles_to_db(
                conn, instrument_id, candles)
            total_inserted += inserted
            total_skipped += skipped
            # Track the latest candle timestamp, independent of API ordering.
            last_candle_time = max(
                normalize_timestamp(candle[0]) for candle in candles
            )

        current_start = current_end
        time.sleep(API_DELAY)

    return total_inserted, total_skipped, last_candle_time


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


def download_historical_data(
    instrument_types: list[str] | None,
    days: int = DEFAULT_DAYS,
    symbols: list[str] | None = None,
    smart_api=None,
):
    """Download incremental five-minute data for selected instruments.

    ``smart_api`` lets a long-running scheduler reuse one authenticated
    session instead of creating a new Angel One login every five minutes.
    """
    smartApi = smart_api or authenticate()
    if not smartApi:
        return

    manager = ConnectionManager()
    conn = None

    try:
        conn = manager.get_connection()

        # Create the ohlcv_5min table if it doesn't exist
        create_ohlcv_5min_table_if_not_exists(conn)

        instruments = get_instruments(conn, instrument_types, symbols)
        total = len(instruments)
        print(
            f"\nFound {total} instruments to download: "
            f"types={instrument_types}, symbols={symbols}"
        )
        print(
            f"Downloading up to {days} days of 5-minute data (incremental)...\n")

        grand_total_inserted = 0
        grand_total_skipped = 0
        failed = []

        for idx, instrument in enumerate(instruments, start=1):
            instrument_id, symbol, token, exchange, instrument_type, expiry = instrument
            print(f"[{idx}/{total}] {symbol} ({instrument_type})", end=" → ")

            try:
                result = download_for_instrument(
                    smartApi, conn, instrument, days)
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
        print(f"Download Complete!")
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
    download_historical_data(
        instrument_types=["AMXIDX", "FUTIDX", "OPTIDX"],
        days=DEFAULT_DAYS
    )
