import os
import json
import time
import math
import threading
from datetime import datetime, timedelta, time as clock_time, timezone
from queue import Queue, Empty
from dotenv import load_dotenv
import pyotp
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from SmartApi.smartExceptions import DataException
from db.connection import ConnectionManager
from scheduler.nse_calendar import IST, is_nse_trading_day

load_dotenv()

MARKET_OPEN = clock_time(9, 15)
MARKET_CLOSE = clock_time(15, 30)

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PASSWORD")
TOTP_TOKEN = os.getenv("ANGEL_TOTP_TOKEN")

# Number of ticks to batch before inserting into DB
BATCH_SIZE = 100

# WebSocket subscription modes
MODE_LTP = 1         # Last Traded Price only (lightest)
MODE_QUOTE = 2       # LTP + OHLC + Volume
MODE_SNAP_QUOTE = 3  # Full data: OHLC, volume, OI, avg price, best 5 bid/ask

# Angel One exchange type codes for WebSocket
EXCHANGE_TYPE = {
    "NSE": 1,
    "NFO": 2,
    "BSE": 3,
    "BFO": 4,
    "MCX": 5,
}

CORRELATION_ID = "tick_session_1"

# Spot token / strike config per index
INDEX_CONFIG = {
    "NIFTY": {
        "spot_token":  "99926000",
        "spot_symbol": "Nifty 50",
        "spot_exchange": "NSE",
        "strike_step": 50,
    },
    "BANKNIFTY": {
        "spot_token":  "99926009",
        "spot_symbol": "Nifty Bank",
        "spot_exchange": "NSE",
        "strike_step": 100,
    },
}


# ── Authentication ─────────────────────────────────────────────────────────────

def authenticate() -> dict | None:
    """
    Authenticate with Angel One SmartAPI.
    Returns dict with auth_token, refresh_token, feed_token or None on failure.
    """
    try:
        smartApi = SmartConnect(API_KEY)
        totp = pyotp.TOTP(TOTP_TOKEN).now()
        data = smartApi.generateSession(CLIENT_ID, PASSWORD, totp)

        if not data["status"]:
            print("Authentication failed:", data)
            return None

        print("Authentication successful!")
        return {
            "auth_token":    data["data"]["jwtToken"],
            "refresh_token": data["data"]["refreshToken"],
            "feed_token":    smartApi.getfeedToken(),
        }

    except Exception as e:
        print(f"Authentication error: {e}")
        return None


# ── Database Setup ─────────────────────────────────────────────────────────────

def create_tick_table_if_not_exists(conn):
    """
    Create the tick_data table with full SNAP_QUOTE schema if it doesn't exist.

    Columns:
        ltp, ltq                        — last traded price & quantity
        open, high, low, close          — day OHLC prices
        avg_trade_price                 — VWAP
        volume                          — cumulative day volume
        total_buy_qty, total_sell_qty   — pending order quantities
        open_interest                   — OI (derivatives only)
        best_5_buy, best_5_sell         — top 5 bid/ask levels as JSONB
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tick_data (
                    id                  BIGSERIAL PRIMARY KEY,
                    instrument_id       INTEGER NOT NULL REFERENCES instruments(id),
                    timestamp           TIMESTAMP NOT NULL,
                    ltp                 DECIMAL(12, 2) NOT NULL,
                    ltq                 INTEGER,
                    open                DECIMAL(12, 2),
                    high                DECIMAL(12, 2),
                    low                 DECIMAL(12, 2),
                    close               DECIMAL(12, 2),
                    avg_trade_price     DECIMAL(12, 2),
                    volume              BIGINT,
                    total_buy_qty       BIGINT,
                    total_sell_qty      BIGINT,
                    open_interest       BIGINT,
                    best_5_buy          JSONB,
                    best_5_sell         JSONB,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(instrument_id, timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_tick_data_instrument_timestamp
                ON tick_data(instrument_id, timestamp DESC);

                CREATE INDEX IF NOT EXISTS idx_tick_data_timestamp
                ON tick_data(timestamp DESC);
            """)
            conn.commit()
            print("tick_data table verified/created successfully.")
    except Exception as e:
        print(f"Error creating tick_data table: {e}")
        conn.rollback()


# ── Spot Price / ATM Helpers ───────────────────────────────────────────────────

def get_spot_price(smart_api, index_name: str) -> float | None:
    """
    Fetch the current spot price for an index using the Angel One REST API.
    Returns the LTP in rupees, or None on failure.
    """
    cfg = INDEX_CONFIG.get(index_name)
    if not cfg:
        print(
            f"Unknown index '{index_name}'. Supported: {list(INDEX_CONFIG.keys())}")
        return None

    try:
        response = smart_api.ltpData(
            cfg["spot_exchange"], cfg["spot_symbol"], cfg["spot_token"]
        )
        if response and response.get("status"):
            ltp = float(response["data"]["ltp"])
            print(f"Current {index_name} spot price: {ltp}")
            return ltp
        else:
            print(f"Failed to fetch {index_name} spot price: {response}")
            return None
    except Exception as e:
        print(f"Error fetching {index_name} spot price: {e}")
        return None


def get_atm_strike(spot_price: float, strike_step: int) -> float:
    """Round spot price to the nearest strike multiple."""
    return round(spot_price / strike_step) * strike_step


def get_option_instruments(
    conn,
    index_name: str,
    atm_strike: float,
    strike_step: int,
    num_strikes: int = 5,
) -> list:
    """
    Fetch ATM ± num_strikes option instruments (both CE and PE) for the
    nearest expiry from the instruments table.

    Returns list of (id, symbol, token, exchange, instrument_type, expiry).
    """
    lower_strike = atm_strike - (num_strikes * strike_step)
    upper_strike = atm_strike + (num_strikes * strike_step)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, symbol, token, exchange, instrument_type, expiry
            FROM instruments
            WHERE instrument_type = 'OPTIDX'
              AND name = %s
              AND expiry = (
                  SELECT MIN(expiry) FROM instruments
                  WHERE instrument_type = 'OPTIDX'
                    AND name = %s
                    AND expiry >= CURRENT_DATE
              )
              AND strike >= %s
              AND strike <= %s
            ORDER BY strike ASC, symbol ASC
        """, (index_name, index_name, lower_strike, upper_strike))
        rows = cur.fetchall()

    ce = [r for r in rows if "CE" in r[1]]
    pe = [r for r in rows if "PE" in r[1]]
    print(
        f"Options found: {len(rows)} total "
        f"({len(ce)} CE + {len(pe)} PE), "
        f"strikes {lower_strike}–{upper_strike}, "
        f"expiry {rows[0][5] if rows else 'N/A'}"
    )
    return rows


# ── Instrument Fetching ────────────────────────────────────────────────────────

def get_instruments(conn, instrument_types: list[str], limit: int = None) -> list:
    """
    Fetch instruments filtered by instrument type.
    Returns list of (id, symbol, token, exchange, instrument_type, expiry).
    """
    placeholders = ",".join(["%s"] * len(instrument_types))
    query = f"""
        SELECT id, symbol, token, exchange, instrument_type, expiry
        FROM instruments
        WHERE instrument_type IN ({placeholders})
          AND is_active = TRUE
        ORDER BY instrument_type, symbol
    """
    if limit:
        query += f" LIMIT {limit}"

    with conn.cursor() as cur:
        cur.execute(query, instrument_types)
        return cur.fetchall()


def build_token_list(instruments: list) -> list:
    """
    Build subscription token list grouped by exchange type.

    SmartWebSocketV2 format:
        [{"exchangeType": 1, "tokens": ["token1", "token2"]}, ...]
    """
    exchange_tokens = {}
    for inst in instruments:
        exchange = inst[3]
        token = str(inst[2])
        exch_type = EXCHANGE_TYPE.get(exchange)
        if exch_type is None:
            continue
        exchange_tokens.setdefault(exch_type, []).append(token)

    return [
        {"exchangeType": exch_type, "tokens": tokens}
        for exch_type, tokens in exchange_tokens.items()
    ]


def build_token_map(instruments: list) -> dict:
    """
    Build a token → instrument_id map for fast lookup in tick callbacks.
    """
    return {str(inst[2]): inst[0] for inst in instruments}


# ── Tick Parsing ───────────────────────────────────────────────────────────────

def _paise_to_rupees(value) -> float | None:
    """Convert paise integer to rupees float. Returns None if value is missing."""
    if value is None:
        return None
    try:
        return float(value) / 100
    except (TypeError, ValueError):
        return None


def _parse_best5(raw_list: list) -> list | None:
    """
    Parse best 5 bid or ask levels from SNAP_QUOTE response.
    Returns a list of {price, quantity, orders} dicts with rupee prices.
    """
    if not raw_list:
        return None
    try:
        result = []
        for entry in raw_list:
            price = _paise_to_rupees(entry.get("price"))
            if price is not None:
                result.append({
                    "price":    price,
                    "quantity": entry.get("quantity"),
                    "orders":   entry.get("num_orders") or entry.get("orders"),
                })
        return result if result else None
    except Exception:
        return None


# Angel One publishes feed clocks as epoch integers in IST terms. Interpreting
# them with a bare datetime.fromtimestamp() would bind the value to whatever
# timezone the *collector process* happens to run in — IST on the native host,
# but UTC inside the tick-downloader container (docker-compose.yml sets TZ only
# for the scheduler services). Converting through an explicit IST offset makes
# the stored naive timestamp mean the same thing wherever the collector runs.
IST_TZ = timezone(timedelta(hours=5, minutes=30))


def _epoch_to_ist(value, unit_divisor: float) -> datetime | None:
    """Convert an epoch integer to a naive IST datetime.

    Returns None for missing/zero/unparseable values. Zero matters: Angel One
    sends last_traded_timestamp = 0 for an instrument that has not traded yet,
    and that must not be mistaken for midnight 1970.
    """
    if value is None:
        return None
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None

    try:
        aware = datetime.fromtimestamp(epoch / unit_divisor, tz=IST_TZ)
    except (OverflowError, OSError, ValueError):
        return None
    return aware.replace(tzinfo=None)


def resolve_tick_timestamp(raw: dict) -> tuple[datetime, str]:
    """Pick the best available event time for a tick, and say where it came from.

    Preference order:

    1. ``exchange_timestamp`` (milliseconds) — the exchange's own feed clock.
       Present in every subscription mode, populated on every packet, and
       measured at ~1.8s behind local receipt, so it is not a delayed field.
    2. ``last_traded_timestamp`` (seconds) — SNAP_QUOTE only, and 0 until the
       instrument actually trades. Accurate when present, but far too sparse
       to be the primary source.
    3. The collector's wall clock — a last resort. It is processing time, not
       event time, so the caller records that fact rather than silently
       passing it off as a market timestamp.
    """
    exchange_ts = _epoch_to_ist(raw.get("exchange_timestamp"), 1000.0)
    if exchange_ts is not None:
        return exchange_ts, "exchange"

    traded_ts = _epoch_to_ist(raw.get("last_traded_timestamp"), 1.0)
    if traded_ts is not None:
        return traded_ts, "last_traded"

    return datetime.now(IST_TZ).replace(tzinfo=None), "received"


def parse_tick(raw: dict, token_map: dict) -> dict | None:
    """
    Parse a raw SmartWebSocketV2 SNAP_QUOTE tick into our internal format.

    All prices from the API are in paise — divide by 100 to get rupees.

    SNAP_QUOTE fields used:
        token                       - instrument token
        sequence_number             - per-packet ordering token from the feed
        last_traded_price           - LTP in paise
        last_traded_quantity        - last traded quantity
        average_traded_price        - VWAP in paise
        volume_trade_for_the_day    - cumulative day volume
        total_buy_quantity          - total pending buy quantity
        total_sell_quantity         - total pending sell quantity
        open_price_of_the_day       - day open in paise
        high_price_of_the_day       - day high in paise
        low_price_of_the_day        - day low in paise
        closed_price                - previous close in paise
        open_interest               - OI (derivatives only)
        exchange_timestamp          - exchange feed clock, epoch MILLISECONDS
        last_traded_timestamp       - last trade time, epoch SECONDS (0 until
                                      the instrument trades; SNAP_QUOTE only)
        best_5_buy_data             - list of top 5 bid levels
        best_5_sell_data            - list of top 5 ask levels
    """
    try:
        token = str(raw.get("token", ""))
        if token not in token_map:
            return None

        ltp = _paise_to_rupees(raw.get("last_traded_price"))
        if not ltp or ltp <= 0:
            return None  # Skip invalid ticks

        # Prefer the exchange's own clock over the collector's wall clock.
        timestamp, time_source = resolve_tick_timestamp(raw)

        return {
            "instrument_id":   token_map[token],
            "timestamp":       timestamp,
            # The exchange's own per-packet ordering token. Part of the
            # uniqueness key (migration 001) so two genuine snapshots inside
            # the same one-second exchange timestamp are both preserved.
            "sequence_number": raw.get("sequence_number") or 0,
            "ltp":             ltp,
            "ltq":             raw.get("last_traded_quantity"),
            "open":            _paise_to_rupees(raw.get("open_price_of_the_day")),
            "high":            _paise_to_rupees(raw.get("high_price_of_the_day")),
            "low":             _paise_to_rupees(raw.get("low_price_of_the_day")),
            "close":           _paise_to_rupees(raw.get("closed_price")),
            "avg_trade_price": _paise_to_rupees(
                # The SDK emits "average_traded_price"; the older spelling is
                # accepted too so nothing breaks if the vendor renames it back.
                raw.get("average_traded_price", raw.get("average_trade_price"))
            ),
            "volume":          raw.get("volume_trade_for_the_day"),
            "total_buy_qty":   raw.get("total_buy_quantity"),
            "total_sell_qty":  raw.get("total_sell_quantity"),
            "open_interest":   raw.get("open_interest"),
            "best_5_buy":      _parse_best5(raw.get("best_5_buy_data", [])),
            "best_5_sell":     _parse_best5(raw.get("best_5_sell_data", [])),
            # Not persisted (no column); used by the collector to report how
            # many ticks fell back to processing time.
            "time_source":     time_source,
        }

    except Exception as e:
        print(f"  Error parsing tick: {e}")
        return None


# ── Database Insertion ─────────────────────────────────────────────────────────

def save_ticks_to_db(conn, ticks: list) -> tuple[int, int]:
    """
    Batch insert tick records into tick_data table.
    Returns (inserted_count, skipped_count).
    """
    if not ticks:
        return 0, 0

    inserted = 0
    skipped = 0

    with conn.cursor() as cur:
        for tick in ticks:
            try:
                best_5_buy = tick.get("best_5_buy")
                best_5_sell = tick.get("best_5_sell")

                cur.execute("""
                    INSERT INTO tick_data (
                        instrument_id, timestamp, sequence_number, ltp, ltq,
                        open, high, low, close,
                        avg_trade_price, volume,
                        total_buy_qty, total_sell_qty,
                        open_interest, best_5_buy, best_5_sell
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instrument_id, timestamp, sequence_number) DO NOTHING
                """, (
                    tick.get("instrument_id"),
                    tick.get("timestamp"),
                    tick.get("sequence_number", 0),
                    tick.get("ltp"),
                    tick.get("ltq"),
                    tick.get("open"),
                    tick.get("high"),
                    tick.get("low"),
                    tick.get("close"),
                    tick.get("avg_trade_price"),
                    tick.get("volume"),
                    tick.get("total_buy_qty"),
                    tick.get("total_sell_qty"),
                    tick.get("open_interest"),
                    json.dumps(best_5_buy) if best_5_buy else None,
                    json.dumps(best_5_sell) if best_5_sell else None,
                ))

                if cur.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1

            except Exception as e:
                print(f"  Error inserting tick: {e}")
                conn.rollback()
                continue

        if inserted > 0:
            cur.execute("NOTIFY tick_update;")

    conn.commit()
    return inserted, skipped


# ── WebSocket Callbacks ────────────────────────────────────────────────────────

def on_open(wsapp, sws, token_list: list, mode: int):
    """Subscribe to instruments when WebSocket connection opens."""
    print("WebSocket connection opened.")
    sws.subscribe(CORRELATION_ID, mode, token_list)
    total = sum(len(t["tokens"]) for t in token_list)
    print(f"Subscribed to {total} instruments (SNAP_QUOTE mode).")


def on_data(wsapp, message, tick_queue: Queue):
    """Push incoming tick messages onto the queue."""
    if isinstance(message, dict):
        tick_queue.put(message)
    elif isinstance(message, list):
        for tick in message:
            tick_queue.put(tick)


def on_error(wsapp, error, state: dict):
    """Log WebSocket errors.

    SmartWebSocketV2._on_error calls close_connection() (killing the socket)
    once MAX_RETRY_ATTEMPT is exhausted, but — unlike a normal disconnect —
    never calls on_close afterward. It also invokes self.on_error(...)
    directly at that point rather than through its usual (wsapp, error)
    dispatch, passing "Max retry attempt reached" as the first positional
    arg — i.e. in our `wsapp` slot, not `error`. Without checking both
    slots, state["running"] stays True forever: the main thread's
    `while state["running"]: sleep(1)` loop spins on a dead socket for the
    rest of the day, and run_forever() never gets a chance to reconnect.
    """
    print(f"WebSocket error: {error}")
    if "Max retry attempt reached" in (str(wsapp), str(error)):
        print("Max retry attempts reached; ending session so run_forever() can reconnect.")
        state["running"] = False


def on_close(wsapp, state: dict):
    """Mark collector as stopped when WebSocket closes."""
    print("WebSocket connection closed.")
    state["running"] = False


# ── DB Worker Thread ───────────────────────────────────────────────────────────

def db_worker(conn, tick_queue: Queue, token_map: dict, state: dict):
    """
    Background thread: drains tick queue and batch-inserts into DB.
    Runs until state["running"] is False and queue is empty.
    """
    tick_buffer = []

    while state["running"] or not tick_queue.empty():
        try:
            raw = tick_queue.get(timeout=1)
            parsed = parse_tick(raw, token_map)
            if parsed:
                tick_buffer.append(parsed)

            if len(tick_buffer) >= BATCH_SIZE:
                fallbacks = sum(
                    1 for t in tick_buffer if t.get("time_source") == "received"
                )
                inserted, skipped = save_ticks_to_db(conn, tick_buffer)
                state["total_inserted"] += inserted
                state["total_skipped"] += skipped
                state["total_time_fallback"] += fallbacks
                print(
                    f"  Batch inserted: {inserted} | "
                    f"Skipped: {skipped} | "
                    f"Total: {state['total_inserted']}"
                    + (f" | Clock fallback: {fallbacks}" if fallbacks else "")
                )
                tick_buffer = []

        except Empty:
            # Flush partial buffer on timeout
            if tick_buffer:
                inserted, skipped = save_ticks_to_db(conn, tick_buffer)
                state["total_inserted"] += inserted
                state["total_skipped"] += skipped
                tick_buffer = []
            continue

        except Exception as e:
            print(f"  DB worker error: {e}")
            continue

    # Final flush on shutdown
    if tick_buffer:
        inserted, skipped = save_ticks_to_db(conn, tick_buffer)
        state["total_inserted"] += inserted
        state["total_skipped"] += skipped
        print(f"  Final flush: {inserted} inserted, {skipped} skipped.")


# ── Main Collection Function ───────────────────────────────────────────────────

def start_realtime_tick_collection(
    instrument_types: list[str],
    mode: int = MODE_SNAP_QUOTE,
    limit: int = None,
    subscribe_options: bool = False,
    index_name: str = "NIFTY",
    num_strikes: int = 5,
):
    """
    Start real-time tick data collection for specified instrument types.

    Args:
        instrument_types: e.g. ["AMXIDX", "FUTIDX"]
        mode: 1=LTP, 2=Quote, 3=SnapQuote (default — maximum data)
        limit: Cap on instruments. Angel One allows max 1000 per WebSocket session.
        subscribe_options: If True, also subscribe to ATM ± num_strikes options.
        index_name: Index to derive ATM from ("NIFTY" or "BANKNIFTY").
        num_strikes: Number of strikes above and below ATM to subscribe.
    """
    # Step 1: Authenticate
    session = authenticate()
    if not session:
        return

    manager = ConnectionManager()
    conn = manager.get_connection()

    try:
        # Step 2: Ensure tick_data table exists
        create_tick_table_if_not_exists(conn)

        # Step 3: Fetch base instruments (index, futures, etc.)
        instruments = get_instruments(conn, instrument_types, limit=limit)
        if not instruments:
            print(f"No instruments found for types: {instrument_types}")
            return

        # Step 4: Optionally add ATM ± N option instruments
        if subscribe_options:
            cfg = INDEX_CONFIG.get(index_name)
            if not cfg:
                print(f"Unknown index '{index_name}'. Skipping options.")
            else:
                # Get spot price via REST API
                smart_api = SmartConnect(API_KEY)
                totp = pyotp.TOTP(TOTP_TOKEN).now()
                smart_api.generateSession(CLIENT_ID, PASSWORD, totp)

                spot_price = get_spot_price(smart_api, index_name)
                if spot_price:
                    atm = get_atm_strike(spot_price, cfg["strike_step"])
                    print(f"ATM strike: {atm}")

                    opt_instruments = get_option_instruments(
                        conn, index_name, atm, cfg["strike_step"], num_strikes
                    )
                    if opt_instruments:
                        instruments = list(instruments) + list(opt_instruments)
                    else:
                        print(
                            f"Warning: No OPTIDX instruments found for {index_name}. "
                            f"Run load_fo_instruments.py first."
                        )
                else:
                    print("Could not determine spot price. Skipping options.")

        # Step 5: Enforce 1000 token limit
        if len(instruments) > 1000:
            print(
                f"Warning: {len(instruments)} instruments exceed the 1000 token "
                f"WebSocket limit. Truncating to 1000."
            )
            instruments = instruments[:1000]

        print(f"\nSubscribing to {len(instruments)} instruments:")
        for inst in instruments[:10]:
            print(f"  {inst[1]} ({inst[4]}) — Token: {inst[2]}")
        if len(instruments) > 10:
            print(f"  ... and {len(instruments) - 10} more")

    finally:
        conn.close()

    # Step 5: Build lookup structures
    token_list = build_token_list(instruments)
    token_map = build_token_map(instruments)

    # Step 6: Shared state dict — replaces class instance variables
    state = {
        "running":        True,
        "total_inserted": 0,
        "total_skipped":  0,
        # Ticks with no usable exchange clock, which fell back to wall time.
        "total_time_fallback": 0,
    }
    tick_queue = Queue()

    # Step 7: Start DB worker thread
    db_conn = manager.get_connection()
    worker_thread = threading.Thread(
        target=db_worker,
        args=(db_conn, tick_queue, token_map, state),
        daemon=True,
    )
    worker_thread.start()

    # Step 8: Set up SmartWebSocketV2 with functional callbacks
    sws = SmartWebSocketV2(
        session["auth_token"],
        API_KEY,
        CLIENT_ID,
        session["feed_token"],
    )
    sws.on_open = lambda wsapp: on_open(wsapp, sws, token_list, mode)
    sws.on_data = lambda wsapp, message: on_data(wsapp, message, tick_queue)
    sws.on_error = lambda wsapp, error: on_error(wsapp, error, state)
    sws.on_close = lambda wsapp: on_close(wsapp, state)

    # Step 9: Run WebSocket in background thread
    ws_thread = threading.Thread(target=sws.connect, daemon=True)
    ws_thread.start()

    print("\nTick collection running. Press Ctrl+C to stop.\n")

    # Step 10: Keep the main thread alive until the session ends.
    #
    # The feed keeps publishing snapshots after 15:30 — unchanged LTP, zero
    # volume, zero last-traded-quantity — because the exchange is closed, not
    # because anything traded. Left running, the collector stored those for
    # hours (sessions bled to 18:53, and once until the following Monday).
    # Now that every snapshot carries a distinct sequence_number they would no
    # longer be deduplicated away either, so end the session at the close and
    # let run_forever() sleep until the next open.
    try:
        while state["running"]:
            if not in_market_hours(datetime.now(IST)):
                print("\nMarket closed. Ending session.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping tick collection...")
    finally:
        # One shutdown path for every exit reason (close, Ctrl+C, socket
        # error), so the socket is always closed and the queue always drained.
        # This previously ran only on Ctrl+C, which is why a normal session end
        # left ticks in the queue to be flushed — and stamped — hours later.
        state["running"] = False

        try:
            sws.close_connection()
        except Exception:
            pass

        worker_thread.join(timeout=10)
        db_conn.close()

        print("\nCollection stopped.")
        print(f"Total inserted : {state['total_inserted']}")
        print(f"Total skipped  : {state['total_skipped']}")
        print(f"Clock fallback : {state['total_time_fallback']}")


# ── Query Functions ────────────────────────────────────────────────────────────

def get_latest_tick(conn, symbol: str) -> dict | None:
    """Get the most recent tick for a given symbol."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.timestamp, t.ltp, t.ltq,
                       t.open, t.high, t.low, t.close,
                       t.avg_trade_price, t.volume,
                       t.total_buy_qty, t.total_sell_qty,
                       t.open_interest, t.best_5_buy, t.best_5_sell
                FROM tick_data t
                JOIN instruments i ON i.id = t.instrument_id
                WHERE i.symbol = %s
                ORDER BY t.timestamp DESC
                LIMIT 1
            """, (symbol.upper(),))
            row = cur.fetchone()

        if not row:
            return None

        return {
            "timestamp":       row[0],
            "ltp":             float(row[1]),
            "ltq":             row[2],
            "open":            float(row[3]) if row[3] else None,
            "high":            float(row[4]) if row[4] else None,
            "low":             float(row[5]) if row[5] else None,
            "close":           float(row[6]) if row[6] else None,
            "avg_trade_price": float(row[7]) if row[7] else None,
            "volume":          row[8],
            "total_buy_qty":   row[9],
            "total_sell_qty":  row[10],
            "open_interest":   row[11],
            "best_5_buy":      row[12],
            "best_5_sell":     row[13],
        }

    except Exception as e:
        print(f"Error fetching latest tick: {e}")
        return None


def get_tick_data_range(
    conn,
    symbol: str,
    from_date: datetime = None,
    to_date: datetime = None,
    limit: int = 1000,
) -> list:
    """Get tick data for a symbol within a date range."""
    try:
        if not to_date:
            to_date = datetime.now()
        if not from_date:
            from_date = to_date - timedelta(days=1)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.timestamp, t.ltp, t.ltq,
                       t.open, t.high, t.low, t.close,
                       t.avg_trade_price, t.volume,
                       t.total_buy_qty, t.total_sell_qty,
                       t.open_interest
                FROM tick_data t
                JOIN instruments i ON i.id = t.instrument_id
                WHERE i.symbol = %s
                  AND t.timestamp >= %s
                  AND t.timestamp <= %s
                ORDER BY t.timestamp ASC
                LIMIT %s
            """, (symbol.upper(), from_date, to_date, limit))
            rows = cur.fetchall()

        return [
            {
                "timestamp":       row[0],
                "ltp":             float(row[1]),
                "ltq":             row[2],
                "open":            float(row[3]) if row[3] else None,
                "high":            float(row[4]) if row[4] else None,
                "low":             float(row[5]) if row[5] else None,
                "close":           float(row[6]) if row[6] else None,
                "avg_trade_price": float(row[7]) if row[7] else None,
                "volume":          row[8],
                "total_buy_qty":   row[9],
                "total_sell_qty":  row[10],
                "open_interest":   row[11],
            }
            for row in rows
        ]

    except Exception as e:
        print(f"Error fetching tick data range: {e}")
        return []


def in_market_hours(moment: datetime) -> bool:
    """Whether *moment* falls inside a live NSE session.

    Mirrors scheduler.ohlcv_scheduler.in_market_hours: the close is exclusive,
    so 15:30:00 is already outside the session.
    """
    if not is_nse_trading_day(moment.date()):
        return False
    clock = moment.timetz().replace(tzinfo=None)
    return MARKET_OPEN <= clock < MARKET_CLOSE



def _seconds_until_next_session() -> float:
    """Return seconds to sleep until the next trading-day market open.

    start_realtime_tick_collection() connects immediately and has no
    market-hours awareness of its own, so under a container restart policy
    like `unless-stopped` it would otherwise crash-loop (or sit connected
    to a dead session) outside 09:15-15:30 IST on a trading day.
    """
    now = datetime.now(IST)
    candidate = now

    while True:
        candidate_date = candidate.date()
        if is_nse_trading_day(candidate_date):
            open_dt = datetime.combine(candidate_date, MARKET_OPEN, tzinfo=IST)
            close_dt = datetime.combine(candidate_date, MARKET_CLOSE, tzinfo=IST)
            if now < open_dt:
                return (open_dt - now).total_seconds()
            if now <= close_dt:
                return 0.0
        candidate = datetime.combine(
            candidate_date + timedelta(days=1), clock_time(0, 0), tzinfo=IST
        )


def run_forever() -> None:
    """Run one tick-collection session per trading day, waiting between them.

    Suitable for a long-lived container: sleeps until the next 09:15 IST
    trading-day open, runs collection for that session (the WebSocket loop
    inside start_realtime_tick_collection blocks until the session ends),
    then waits for the next one — instead of the process exiting after a
    single day's session.
    """
    while True:
        wait_seconds = _seconds_until_next_session()
        if wait_seconds > 0:
            print(f"Market closed. Sleeping {wait_seconds / 60:.1f} min until next open...")
            time.sleep(wait_seconds)

        print("Starting Real-Time Tick Data Collection (SNAP_QUOTE — maximum data)...\n")
        start_realtime_tick_collection(
            instrument_types=["AMXIDX", "FUTIDX"],
            mode=MODE_SNAP_QUOTE,
            subscribe_options=True,
            index_name="NIFTY",
            num_strikes=5,
        )
        print("Session ended.")
        # Avoid a tight loop if the session ends immediately (e.g. auth
        # failure) instead of at market close.
        time.sleep(30)


if __name__ == "__main__":
    run_forever()
