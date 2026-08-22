"""
Sync Instrument Master from Angel One OpenAPI to PostgreSQL.

Downloads the full ~145k instrument master, upserts it into the instruments
table, then activates the Nifty 50 Index, Nifty Futures, and Nifty options
within ATM ± OPTIONS_STRIKE_RANGE strikes at the nearest expiry, while
deactivating expired contracts.

Intended to run once daily before market open, since the ATM strike moves
with the market and the option activation set must be recomputed each day.
"""

import os
import sys
from pathlib import Path
import pandas as pd
import psycopg2.extras

# Ensure project root is on sys.path so `db.connection` resolves
project_root = str(Path(__file__).resolve().parents[1])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from db.connection import ConnectionManager
from downloader.ohlcv import authenticate

URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Nifty 50 spot quote, used to compute the ATM strike for option activation.
SPOT_EXCHANGE = "NSE"
SPOT_SYMBOL = "Nifty 50"
SPOT_TOKEN = "99926000"
STRIKE_STEP = 50
OPTIONS_STRIKE_RANGE = int(os.getenv("OPTIONS_STRIKE_RANGE", "10"))


# ── Download & clean ──────────────────────────────────────────────────────────

def get_instrument_master() -> pd.DataFrame:
    """Download the Angel One instrument master and return a cleaned DataFrame."""
    print(f"Downloading master from {URL}...")
    df = pd.read_json(URL)

    print("Cleaning data...")
    # Token as string (Angel One uses numeric tokens but DB stores varchar)
    df["token"] = df["token"].astype(str)

    # Strike parsing — Angel One provides strikes × 100 for NFO options
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce").astype(float)
    mask_options = df["instrumenttype"].isin(["OPTIDX", "OPTSTK"])
    df.loc[mask_options, "strike"] = df.loc[mask_options, "strike"] / 100.0

    # Expiry parsing
    parsed_expiry = pd.to_datetime(
        df["expiry"], format="%d%b%Y", errors="coerce"
    ).dt.date
    df["expiry"] = parsed_expiry.where(parsed_expiry.notna(), None)

    # Lot size
    df["lotsize"] = (
        pd.to_numeric(df["lotsize"], errors="coerce").fillna(0).astype(int)
    )

    # Fill NaNs with empty strings for text columns
    df["name"] = df["name"].fillna("")
    df["exch_seg"] = df["exch_seg"].fillna("")
    df["instrumenttype"] = df["instrumenttype"].fillna("")

    # Replace strike NaN with None
    df["strike"] = df["strike"].where(df["strike"].notna(), None)

    print(f"Total instruments fetched: {len(df)}")
    return df


# ── Optional CSV export ───────────────────────────────────────────────────────

def save_master_csv(df: pd.DataFrame) -> Path:
    """Save the full instrument master to ``data/instrument_master.csv``."""
    data_dir = Path(__file__).resolve().parents[1] / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = data_dir / "instrument_master.csv"
    df.to_csv(output_path, index=False)
    print(f"Master CSV saved to {output_path}")
    return output_path


# ── Upsert to database ───────────────────────────────────────────────────────

def upsert_instruments(conn, df: pd.DataFrame):
    """Bulk-upsert all instruments into the database."""
    records = []
    for row in df[
        [
            "symbol",
            "token",
            "name",
            "exch_seg",
            "instrumenttype",
            "expiry",
            "strike",
            "lotsize",
        ]
    ].itertuples(index=False):
        expiry_val = row.expiry if pd.notna(row.expiry) and row.expiry is not None else None
        strike_val = float(row.strike) if pd.notna(row.strike) and row.strike is not None else None
        lotsize_val = int(row.lotsize) if pd.notna(row.lotsize) else 0
        records.append(
            (
                row.symbol,
                str(row.token),
                row.name,
                row.exch_seg,
                row.instrumenttype,
                expiry_val,
                strike_val,
                lotsize_val,
            )
        )

    insert_query = """
        INSERT INTO instruments
            (symbol, token, name, exchange, instrument_type, expiry, strike, lot_size)
        VALUES %s
        ON CONFLICT (token, exchange) DO UPDATE SET
            symbol = EXCLUDED.symbol,
            name = EXCLUDED.name,
            instrument_type = EXCLUDED.instrument_type,
            expiry = EXCLUDED.expiry,
            strike = EXCLUDED.strike,
            lot_size = EXCLUDED.lot_size
    """

    print(f"Upserting {len(records)} instruments into the database...")
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur, insert_query, records, page_size=5000
        )
    conn.commit()
    print("Upsert complete.")


# ── Activation / deactivation ─────────────────────────────────────────────────

def activate_nifty_instruments(conn):
    """Activate Nifty 50 Index and current Nifty Futures.

    Also deactivates any instruments whose expiry has passed.
    """
    with conn.cursor() as cur:
        # 1. Deactivate expired instruments
        cur.execute("""
            UPDATE instruments
            SET is_active = FALSE
            WHERE expiry < CURRENT_DATE
              AND is_active = TRUE
        """)
        deactivated = cur.rowcount
        print(f"Deactivated {deactivated} expired instruments.")

        # 2. Activate Nifty 50 Index (AMXIDX)
        cur.execute("""
            UPDATE instruments
            SET is_active = TRUE
            WHERE instrument_type = 'AMXIDX'
              AND (name = 'Nifty 50' OR symbol = 'Nifty 50')
        """)
        idx_activated = cur.rowcount
        print(f"Activated {idx_activated} Nifty 50 Index instrument(s).")

        # 3. Activate Nifty Futures (FUTIDX) with valid expiry
        cur.execute("""
            UPDATE instruments
            SET is_active = TRUE
            WHERE instrument_type = 'FUTIDX'
              AND name = 'NIFTY'
              AND expiry >= CURRENT_DATE
        """)
        fut_activated = cur.rowcount
        print(f"Activated {fut_activated} Nifty Futures instrument(s).")

    conn.commit()


def get_spot_price(smart_api) -> float | None:
    """Fetch the current Nifty 50 spot LTP via SmartAPI."""
    try:
        response = smart_api.ltpData(SPOT_EXCHANGE, SPOT_SYMBOL, SPOT_TOKEN)
        if response and response.get("status"):
            return float(response["data"]["ltp"])
        print(f"Failed to fetch Nifty 50 spot price: {response}")
        return None
    except Exception as e:
        print(f"Error fetching Nifty 50 spot price: {e}")
        return None


def activate_nifty_options(conn, smart_api):
    """Activate Nifty options within ATM ± OPTIONS_STRIKE_RANGE strikes.

    Deactivates all previously active options first, since the ATM strike
    (and therefore the relevant strike band) moves daily — yesterday's
    activated options are not necessarily still within range today.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE instruments
            SET is_active = FALSE
            WHERE instrument_type = 'OPTIDX' AND name = 'NIFTY' AND is_active = TRUE
        """)
        conn.commit()

    spot_price = get_spot_price(smart_api)
    if spot_price is None:
        print("Skipping Nifty option activation: no spot price available.")
        return

    atm_strike = round(spot_price / STRIKE_STEP) * STRIKE_STEP
    lower_strike = atm_strike - (OPTIONS_STRIKE_RANGE * STRIKE_STEP)
    upper_strike = atm_strike + (OPTIONS_STRIKE_RANGE * STRIKE_STEP)

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE instruments
            SET is_active = TRUE
            WHERE instrument_type = 'OPTIDX'
              AND name = 'NIFTY'
              AND expiry = (
                  SELECT MIN(expiry) FROM instruments
                  WHERE instrument_type = 'OPTIDX'
                    AND name = 'NIFTY'
                    AND expiry >= CURRENT_DATE
              )
              AND strike >= %s
              AND strike <= %s
        """, (lower_strike, upper_strike))
        opt_activated = cur.rowcount
    conn.commit()

    print(
        f"Activated {opt_activated} Nifty option instrument(s) "
        f"(ATM {atm_strike}, strikes {lower_strike}-{upper_strike})."
    )


# ── Main entry point ─────────────────────────────────────────────────────────

def sync_all(save_csv: bool = True):
    """Download the instrument master, upsert to DB, and activate Nifty instruments.

    Parameters
    ----------
    save_csv : bool
        If True, save the raw master data to ``data/instrument_master.csv``.
    """
    df = get_instrument_master()

    if save_csv:
        save_master_csv(df)

    manager = ConnectionManager()
    conn = manager.get_connection()

    try:
        upsert_instruments(conn, df)
        activate_nifty_instruments(conn)

        smart_api = authenticate()
        if smart_api is not None:
            activate_nifty_options(conn, smart_api)
        else:
            print("Skipping Nifty option activation: SmartAPI authentication failed.")

        print("\nInstrument sync complete!")
    except Exception as e:
        conn.rollback()
        print(f"Error during sync: {e}")
        raise
    finally:
        conn.close()
        print("Connection closed.")


if __name__ == "__main__":
    sync_all()

