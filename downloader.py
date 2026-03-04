import os
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pyotp
from SmartApi import SmartConnect
from connect_db import ConnectionManager

load_dotenv()

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PASSWORD")
TOTP_TOKEN = os.getenv("ANGEL_TOTP_TOKEN")

CHUNK_DAYS = 5
API_DELAY = 0.5


def authenticate():
    """Authenticate with Angel One SmartAPI."""
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
        print(f"Authentication error: {e}")
        return None


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


def fetch_candle_data(smartApi, token: str, exchange: str, from_date: str, to_date: str) -> list:
    """Fetch 1-minute candle data from Angel One API."""
    try:
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": "ONE_MINUTE",
            "fromdate": from_date,
            "todate": to_date,
        }
        response = smartApi.getCandleData(params)

        if response and response.get("status"):
            return response.get("data", [])
        else:
            return []

    except Exception as e:
        print(f"    Exception fetching candle data: {e}")
        return []


def save_candles_to_db(conn, instrument_id: int, candles: list) -> tuple[int, int]:
    """Save candle data to the ohlcv_1min table."""
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
                    INSERT INTO ohlcv_1min (instrument_id, timestamp, open, high, low, close, volume)
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
    """Download historical data for a single instrument."""
    instrument_id, symbol, token, exchange, instrument_type, expiry = instrument

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    # For expired instruments, don't bother downloading
    if expiry and expiry < end_date.date():
        return 0, 0

    total_inserted = 0
    total_skipped = 0

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

        current_start = current_end
        time.sleep(API_DELAY)

    return total_inserted, total_skipped


def download_historical_data(instrument_types: list[str], days: int = 30):
    """Download historical 1-minute data for all instruments of given types."""
    smartApi = authenticate()
    if not smartApi:
        return

    manager = ConnectionManager()
    conn = None

    try:
        conn = manager.get_connection()

        instruments = get_instruments(conn, instrument_types)
        total = len(instruments)
        print(f"\nFound {total} instruments to download: {instrument_types}")
        print(f"Downloading {days} days of 1-minute data...\n")

        grand_total_inserted = 0
        grand_total_skipped = 0
        failed = []

        for idx, instrument in enumerate(instruments, start=1):
            instrument_id, symbol, token, exchange, instrument_type, expiry = instrument
            print(
                f"[{idx}/{total}] {symbol} ({instrument_type}, {exchange})", end=" → ")

            try:
                inserted, skipped = download_for_instrument(
                    smartApi, conn, instrument, days
                )
                grand_total_inserted += inserted
                grand_total_skipped += skipped
                print(f"Inserted: {inserted}, Skipped: {skipped}")

            except Exception as e:
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
    # Download Nifty Index + Futures + Options
    download_historical_data(
        instrument_types=["AMXIDX", "FUTIDX", "OPTIDX"],
        days=30
    )
