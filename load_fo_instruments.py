import os
import pandas as pd
import pyotp
from datetime import datetime
from SmartApi import SmartConnect
from dotenv import load_dotenv
from connect_db import ConnectionManager

load_dotenv()

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PASSWORD")
TOTP_TOKEN = os.getenv("ANGEL_TOTP_TOKEN")

NIFTY_SPOT_TOKEN = "99926000"
ATM_RANGE = 2000
STRIKE_STEP = 50  # Nifty strikes are in multiples of 50


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


def get_nifty_spot_price(smartApi) -> float:
    """Fetch current Nifty 50 spot price from the API."""
    try:
        response = smartApi.ltpData("NSE", "Nifty 50", NIFTY_SPOT_TOKEN)
        if response and response.get("status"):
            ltp = response["data"]["ltp"]
            print(f"Current Nifty Spot Price: {ltp}")
            return float(ltp)
        else:
            print("Failed to fetch Nifty spot price:", response)
            return None
    except Exception as e:
        print(f"Error fetching spot price: {e}")
        return None


def get_atm_strike(spot_price: float) -> float:
    """Round spot price to nearest Nifty strike."""
    return round(spot_price / STRIKE_STEP) * STRIKE_STEP


def filter_nifty_futures(df: pd.DataFrame) -> pd.DataFrame:
    """Filter all active Nifty Futures (FUTIDX) from the master."""
    today = datetime.now().date()

    futures = df[
        (df["instrumenttype"] == "FUTIDX") &
        (df["name"] == "NIFTY") &
        (df["exch_seg"] == "NFO") &
        (df["expiry"].dt.date >= today)
    ].copy()

    print(f"Nifty Futures found: {len(futures)}")
    return futures


def load_master(csv_path: str = "instrument_master.csv") -> pd.DataFrame:
    """Load the instrument master CSV."""
    df = pd.read_csv(csv_path, low_memory=False)
    df["token"] = df["token"].astype(str)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["expiry"] = pd.to_datetime(
        df["expiry"], format="%d%b%Y", errors="coerce")
    df["lotsize"] = pd.to_numeric(df["lotsize"], errors="coerce")
    return df


def filter_nifty_options(df: pd.DataFrame, atm_strike: float) -> pd.DataFrame:
    """Filter Nifty Options (OPTIDX) within ATM ± 2000 range."""
    today = datetime.now().date()

    # Strikes in master CSV are multiplied by 100
    lower = (atm_strike - ATM_RANGE) * 100
    upper = (atm_strike + ATM_RANGE) * 100

    options = df[
        (df["instrumenttype"] == "OPTIDX") &
        (df["name"] == "NIFTY") &
        (df["exch_seg"] == "NFO") &
        (df["expiry"].dt.date >= today) &
        (df["strike"] >= lower) &
        (df["strike"] <= upper)
    ].copy()

    # Convert strike back to actual value
    options["strike"] = options["strike"] / 100

    print(f"Nifty Options found (ATM ± {ATM_RANGE}): {len(options)}")
    return options


def load_fo_instruments_to_db(df: pd.DataFrame):
    """Load F&O instruments into the instruments table."""
    manager = ConnectionManager()
    conn = None

    try:
        conn = manager.get_connection()
        inserted = 0
        skipped = 0

        with conn.cursor() as cur:
            for _, row in df.iterrows():
                try:
                    cur.execute("""
                        INSERT INTO instruments 
                            (symbol, token, name, exchange, instrument_type, expiry, strike, lot_size)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (token) DO NOTHING
                    """, (
                        row["symbol"],
                        str(row["token"]),
                        row["name"],
                        row["exch_seg"],
                        row["instrumenttype"],
                        row["expiry"].date() if pd.notna(
                            row["expiry"]) else None,
                        float(row["strike"]) if pd.notna(
                            row["strike"]) else None,
                        int(row["lotsize"]) if pd.notna(
                            row["lotsize"]) else None,
                    ))

                    if cur.rowcount == 1:
                        inserted += 1
                    else:
                        skipped += 1

                except Exception as e:
                    print(f"  Error inserting {row['symbol']}: {e}")
                    conn.rollback()
                    continue

        conn.commit()
        print(
            f"Done! Inserted: {inserted}, Skipped (already exist): {skipped}")

    finally:
        if conn:
            conn.close()
            print("Connection closed.")


if __name__ == "__main__":
    # Step 1: Authenticate
    smartApi = authenticate()
    if not smartApi:
        exit()

    # Step 2: Get current Nifty spot price
    spot_price = get_nifty_spot_price(smartApi)
    if not spot_price:
        exit()

    # Step 3: Calculate ATM strike
    atm_strike = get_atm_strike(spot_price)
    print(
        f"ATM Strike: {atm_strike}, Range: {atm_strike - ATM_RANGE} to {atm_strike + ATM_RANGE}")

    # Step 4: Load instrument master
    df = load_master()

    # Step 5: Filter futures and options
    futures = filter_nifty_futures(df)
    options = filter_nifty_options(df, atm_strike)

    # Step 6: Combine and load to DB
    fo_instruments = pd.concat([futures, options], ignore_index=True)
    print(f"\nTotal F&O instruments to load: {len(fo_instruments)}")
    load_fo_instruments_to_db(fo_instruments)
