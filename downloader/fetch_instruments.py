import os
import json
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"


def fetch_instrument_master():
    """Fetch the instrument master list from Angel One and return as a DataFrame."""
    try:
        print("Downloading instrument master...")
        response = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
        response.raise_for_status()

        data = response.json()
        df = pd.DataFrame(data)

        print(f"Total instruments fetched: {len(df)}")
        print(f"Columns: {list(df.columns)}")
        return df

    except Exception as e:
        print(f"Error fetching instrument master: {e}")
        return None


def filter_nse_equities(df: pd.DataFrame) -> pd.DataFrame:
    """Filter only NSE equities from the instrument master."""
    nse_eq = df[
        (df["exch_seg"] == "NSE") &
        (df["instrumenttype"] == "") &
        (df["symbol"].str.endswith("-EQ"))
    ].copy()

    # Clean up symbol name by removing the -EQ suffix
    nse_eq["symbol"] = nse_eq["symbol"].str.replace("-EQ", "", regex=False)

    print(f"NSE Equities found: {len(nse_eq)}")
    return nse_eq


if __name__ == "__main__":
    df = fetch_instrument_master()

    if df is not None:
        # Save full master to CSV for reference
        df.to_csv("data/instrument_master.csv", index=False)
        print("Full instrument master saved to instrument_master.csv")

        # Filter and save NSE equities
        nse_eq = filter_nse_equities(df)
        nse_eq.to_csv("nse_equities.csv", index=False)
        print("NSE equities saved to nse_equities.csv")

        # Preview
        print("\nSample NSE Equities:")
        print(nse_eq[["symbol", "token", "name",
              "exch_seg", "instrumenttype"]].head(10))
