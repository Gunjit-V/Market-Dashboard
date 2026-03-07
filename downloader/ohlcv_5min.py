import os
import time
from datetime import datetime, timedelta
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
                print("Authentication failed:", data)
                return None

            print("Authentication successful!")
            return smartApi

        except Exception as e:
            print(
                f"Authentication error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
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


def get_instruments(conn, instrument_types: list[str]) -> list:
    """Fetch instruments from the database filtered by instrument type."""
    placeholders = ",".join(["%s"] * len(instrument_types))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, symbol, token, exchange, instrument_type, expiry
            FROM instruments
            WHERE instrument_type IN ({placeholders})
            ORDER BY instrument_type, expiry, symbol
            """,
            instrument_types,
        )
        return cur.fetchall()


def get_last_downloaded_at(conn, instrument_id: int):
    """Get the last successful download timestamp for an instrument."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT last_downloaded_at
            FROM download_log
            WHERE instrument_id = %s
              AND status = 'success'
            ORDER BY last_run_at DESC
            LIMIT 1
            """,
            (instrument_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None


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
                f"    Exception fetching candle data (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
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
                """, (instrument_id, timestamp, open_, high, low, close, volume))

                if cur.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1

            except Exception as e:
                print(f"    Error inserting candle {candle}: {e}")
                conn.rollback()
                continue

    conn.commit()
    return inserted, skipped


def download_for_instrument(smartApi, conn, instrument: tuple, days: int) -> tuple[int, int]:
    """Download historical data for a single instrument incrementally."""
    instrument_id, symbol, token, exchange, instrument_type, expiry = instrument

    end_date = datetime.now()

    # Skip expired instruments
    if expiry and expiry < end_date.date():
        return 0, 0

    # Check last successful download — incremental logic
    last_downloaded_at = get_last_downloaded_at(conn, instrument_id)
    if last_downloaded_at:
        # Start from where we left off
        start_date = last_downloaded_at
    else:
        # First time download — fetch full history
        start_date = end_date - timedelta(days=days)

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
            # Track the latest candle timestamp
            last_candle_time = candles[-1][0]
            if isinstance(last_candle_time, str):
                last_candle_time = datetime.strptime(
                    last_candle_time, "%Y-%m-%dT%H:%M:%S%z")
                # Strip timezone info for PostgreSQL
                last_candle_time = last_candle_time.replace(tzinfo=None)

        current_start = current_end
        time.sleep(API_DELAY)

    return total_inserted, total_skipped, last_candle_time


def download_historical_data(instrument_types: list[str], days: int = DEFAULT_DAYS):
    """Download historical 5-minute data for all instruments of given types."""
    smartApi = authenticate()
    if not smartApi:
        return

    manager = ConnectionManager()
    conn = None

    try:
        conn = manager.get_connection()

        # Create the ohlcv_5min table if it doesn't exist
        create_ohlcv_5min_table_if_not_exists(conn)

        instruments = get_instruments(conn, instrument_types)
        total = len(instruments)
        print(f"\nFound {total} instruments to download: {instrument_types}")
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
                    error_message=str(e),
                )
                print(f"FAILED: {e}")
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
        print(f"Error: {e}")

    finally:
        if conn:
            conn.close()
            print("Connection closed.")


if __name__ == "__main__":
    download_historical_data(
        instrument_types=["AMXIDX", "FUTIDX", "OPTIDX"],
        days=DEFAULT_DAYS
    )
