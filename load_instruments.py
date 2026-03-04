import os
import pandas as pd
from dotenv import load_dotenv
from connect_db import ConnectionManager

load_dotenv()


def load_instruments_to_db(csv_path: str = "nse_equities.csv"):
    """Load NSE equities from CSV into the instruments table."""
    try:
        # Load the CSV
        df = pd.read_csv(csv_path)
        print(f"Loaded {len(df)} instruments from {csv_path}")

        manager = ConnectionManager()
        conn = None

        try:
            conn = manager.get_connection()
            cur = conn.cursor()

            inserted = 0
            skipped = 0

            for _, row in df.iterrows():
                try:
                    cur.execute("""
                        INSERT INTO instruments (symbol, token, name, exchange, instrument_type)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (token) DO NOTHING
                    """, (
                        row["symbol"],
                        str(row["token"]),
                        row["name"],
                        row["exch_seg"],
                        row["instrumenttype"] if row["instrumenttype"] else "EQ"
                    ))

                    if cur.rowcount == 1:
                        inserted += 1
                    else:
                        skipped += 1

                except Exception as e:
                    print(f"Error inserting {row['symbol']}: {e}")
                    conn.rollback()
                    continue

            conn.commit()
            print(f"Done! Inserted: {inserted}, Skipped (already exist): {skipped}")

        finally:
            if conn:
                conn.close()
                print("Connection closed.")

    except Exception as e:
        print(f"Error loading instruments: {e}")


if __name__ == "__main__":
    load_instruments_to_db()