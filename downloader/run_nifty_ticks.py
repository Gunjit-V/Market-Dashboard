"""
Run tick collection for exactly 2 instruments:
  1. Nifty 50 Index   (AMXIDX)
  2. Nifty 50 current-month Futures (FUTIDX, nearest expiry)
"""

import time
import threading
from queue import Queue
from db.connection import ConnectionManager
from downloader.tick_downloader import (
    authenticate,
    create_tick_table_if_not_exists,
    build_token_list,
    build_token_map,
    db_worker,
    on_open,
    on_data,
    on_error,
    on_close,
    API_KEY,
    CLIENT_ID,
    MODE_SNAP_QUOTE,
    CORRELATION_ID,
)
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


def get_nifty_instruments(conn):
    """
    Fetch exactly 2 instruments from the DB:
      - Nifty 50 Index  (instrument_type = 'AMXIDX', name = 'Nifty 50')
      - Nifty 50 current-month Futures (instrument_type = 'FUTIDX',
        name = 'NIFTY', nearest expiry)
    Returns list of (id, symbol, token, exchange, instrument_type, expiry).
    """
    instruments = []

    with conn.cursor() as cur:
        # 1. Nifty 50 Index
        cur.execute("""
            SELECT id, symbol, token, exchange, instrument_type, expiry
            FROM instruments
            WHERE instrument_type = 'AMXIDX'
              AND (name ILIKE '%Nifty 50%' OR symbol ILIKE '%Nifty 50%')
            LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            instruments.append(row)
            print(f"  ✓ Index:   {row[1]} — Token: {row[2]}")
        else:
            print("  ✗ Nifty 50 Index not found in instruments table!")

        # 2. Nifty 50 current-month Futures (nearest expiry ≥ today)
        cur.execute("""
            SELECT id, symbol, token, exchange, instrument_type, expiry
            FROM instruments
            WHERE instrument_type = 'FUTIDX'
              AND name = 'NIFTY'
              AND expiry >= CURRENT_DATE
            ORDER BY expiry ASC
            LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            instruments.append(row)
            print(
                f"  ✓ Futures: {row[1]} — Token: {row[2]} — Expiry: {row[5]}")
        else:
            print("  ✗ Nifty 50 current-month Futures not found in instruments table!")

    return instruments


if __name__ == "__main__":
    print("Starting Tick Collection: Nifty 50 Index + Current Month Futures\n")

    # Step 1: Authenticate
    session = authenticate()
    if not session:
        exit()

    manager = ConnectionManager()
    conn = manager.get_connection()

    try:
        # Step 2: Ensure tick_data table exists
        create_tick_table_if_not_exists(conn)

        # Step 3: Fetch just the 2 instruments
        print("Fetching instruments from DB...")
        instruments = get_nifty_instruments(conn)
        if len(instruments) < 2:
            print(
                f"\nOnly found {len(instruments)}/2 instruments. Check your instruments table.")
            if not instruments:
                exit()
            print("Continuing with available instruments...\n")
    finally:
        conn.close()

    # Step 4: Build lookup structures
    token_list = build_token_list(instruments)
    token_map = build_token_map(instruments)

    # Step 5: Shared state
    state = {
        "running": True,
        "total_inserted": 0,
        "total_skipped": 0,
    }
    tick_queue = Queue()

    # Step 6: Start DB worker thread
    db_conn = manager.get_connection()
    worker_thread = threading.Thread(
        target=db_worker,
        args=(db_conn, tick_queue, token_map, state),
        daemon=True,
    )
    worker_thread.start()

    # Step 7: Set up WebSocket
    sws = SmartWebSocketV2(
        session["auth_token"],
        API_KEY,
        CLIENT_ID,
        session["feed_token"],
    )
    sws.on_open = lambda wsapp: on_open(
        wsapp, sws, token_list, MODE_SNAP_QUOTE)
    sws.on_data = lambda wsapp, message: on_data(wsapp, message, tick_queue)
    sws.on_error = on_error
    sws.on_close = lambda wsapp: on_close(wsapp, state)

    # Step 8: Connect
    ws_thread = threading.Thread(target=sws.connect, daemon=True)
    ws_thread.start()

    print(
        f"\nTick collection running for {len(instruments)} instruments. Press Ctrl+C to stop.\n")

    # Step 9: Keep alive
    try:
        while state["running"]:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping tick collection...")
        state["running"] = False

        try:
            sws.close_connection()
        except Exception:
            pass

        worker_thread.join(timeout=10)
        db_conn.close()

        print(f"\nCollection stopped.")
        print(f"Total inserted : {state['total_inserted']}")
        print(f"Total skipped  : {state['total_skipped']}")
