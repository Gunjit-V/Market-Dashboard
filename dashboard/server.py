"""
Live Dashboard Server
─────────────────────
Standalone FastAPI server (port 8050) that serves the live L2 dashboard
and provides a REST API to query tick_data from PostgreSQL.

Usage:
    cd d:\\code\\HereWeGoAgain
    python -m dashboard.server
"""

import sys
import os
import json
import asyncio
import threading
import select
import time as _time
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal
import psycopg2.extensions

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Ensure project root is on sys.path so `db.connection` resolves
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from db.connection import ConnectionManager

app = FastAPI(title="NIFTY Live Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = ConnectionManager()

DASHBOARD_DIR = Path(__file__).resolve().parent

# ── Simple In-Memory Cache ────────────────────────────────────────────────────
CACHE_TTL_SECONDS = 0.5  # Reduced TTL since we're event-driven now
_cache = {"data": None, "timestamp": 0}

# ── WebSocket Client Management ──────────────────────────────────────────────
_connected_clients: set[WebSocket] = set()
_tick_event = asyncio.Event()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _decimal(v):
    """Convert Decimal to float for JSON serialization."""
    if isinstance(v, Decimal):
        return float(v)
    return v


def _get_nifty_futures_instrument_id(conn) -> tuple:
    """Find the NIFTY Futures instrument (nearest expiry) and return (id, symbol)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, symbol
            FROM instruments
            WHERE instrument_type = 'FUTIDX'
              AND name = 'NIFTY'
              AND expiry >= CURRENT_DATE
            ORDER BY expiry ASC
            LIMIT 1
        """)
        row = cur.fetchone()
    if row:
        return row[0], row[1]
    return None, None


def _get_today_range():
    """Return (start, end) timestamps for today's market session."""
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=0)
    end = datetime.combine(today, datetime.min.time()).replace(hour=15, minute=35)
    return start, end


# ── Chart Data: Per-Minute Aggregation ─────────────────────────────────────────

def _get_chart_data(conn, instrument_id: int, start: datetime, end: datetime) -> list:
    """
    Aggregate tick_data into per-minute bars with L2 analytics:
      - LTP (last), spread (avg/max from best_5), imbalance, depth, cum delta, wall counts
    """
    with conn.cursor() as cur:
        cur.execute("""
            WITH minute_agg AS (
                SELECT
                    date_trunc('minute', timestamp) AS ts_minute,

                    -- Price
                    (array_agg(ltp ORDER BY timestamp DESC))[1] AS ltp,

                    -- Spread from best 5 bid/ask
                    AVG(
                        CASE
                            WHEN best_5_sell IS NOT NULL AND best_5_buy IS NOT NULL
                                 AND jsonb_array_length(best_5_sell) > 0
                                 AND jsonb_array_length(best_5_buy) > 0
                            THEN (best_5_sell->0->>'price')::numeric - (best_5_buy->0->>'price')::numeric
                            ELSE NULL
                        END
                    ) AS avg_spread,
                    MAX(
                        CASE
                            WHEN best_5_sell IS NOT NULL AND best_5_buy IS NOT NULL
                                 AND jsonb_array_length(best_5_sell) > 0
                                 AND jsonb_array_length(best_5_buy) > 0
                            THEN (best_5_sell->0->>'price')::numeric - (best_5_buy->0->>'price')::numeric
                            ELSE NULL
                        END
                    ) AS max_spread,

                    -- Imbalance
                    AVG(
                        CASE
                            WHEN (total_buy_qty + total_sell_qty) > 0
                            THEN (total_buy_qty - total_sell_qty)::numeric
                                 / (total_buy_qty + total_sell_qty)::numeric
                            ELSE 0
                        END
                    ) AS avg_imbalance,

                    -- Depth
                    AVG(total_buy_qty) AS avg_buy_depth,
                    AVG(total_sell_qty) AS avg_sell_depth,

                    -- Delta (buy - sell) change within this minute
                    SUM(total_buy_qty - total_sell_qty) AS minute_delta,

                    -- Count of ticks for reference
                    COUNT(*) AS tick_count

                FROM tick_data
                WHERE instrument_id = %s
                  AND timestamp >= %s
                  AND timestamp <= %s
                GROUP BY ts_minute
                ORDER BY ts_minute
            )
            SELECT * FROM minute_agg
        """, (instrument_id, start, end))

        rows = cur.fetchall()

    if not rows:
        return []

    # Build chart data with cumulative delta
    chart_data = []
    cum_delta = 0

    for row in rows:
        ts_minute, ltp, avg_spread, max_spread, avg_imbalance, \
            avg_buy_depth, avg_sell_depth, minute_delta, tick_count = row

        cum_delta += int(minute_delta or 0)

        chart_data.append({
            "ts": ts_minute.strftime("%H:%M"),
            "ltp": _decimal(ltp),
            "avg_spread": round(_decimal(avg_spread or 0), 2),
            "max_spread": round(_decimal(max_spread or 0), 1),
            "avg_imbalance": round(_decimal(avg_imbalance or 0), 4),
            "avg_buy_depth": int(avg_buy_depth or 0),
            "avg_sell_depth": int(avg_sell_depth or 0),
            "cum_delta": cum_delta,
            "buy_walls": 0,  # filled in by wall detection
            "sell_walls": 0,
        })

    return chart_data


# ── Wall Event Detection ────────────────────────────────────────────────────────

WALL_QTY_THRESHOLD = 5000
MEGA_WALL_THRESHOLD = 5000

def _get_wall_events(conn, instrument_id: int, start: datetime, end: datetime) -> list:
    """
    Detect wall events: ticks where any single bid/ask level has qty >= threshold.
    Scans the best_5_buy and best_5_sell JSONB arrays.
    """
    with conn.cursor() as cur:
        # Query ticks with large qty in best 5 levels
        cur.execute("""
            SELECT timestamp, ltp, best_5_buy, best_5_sell
            FROM tick_data
            WHERE instrument_id = %s
              AND timestamp >= %s
              AND timestamp <= %s
              AND (
                  EXISTS (
                      SELECT 1 FROM jsonb_array_elements(best_5_buy) elem
                      WHERE (elem->>'quantity')::int >= %s
                  )
                  OR
                  EXISTS (
                      SELECT 1 FROM jsonb_array_elements(best_5_sell) elem
                      WHERE (elem->>'quantity')::int >= %s
                  )
              )
            ORDER BY timestamp
        """, (instrument_id, start, end, MEGA_WALL_THRESHOLD, MEGA_WALL_THRESHOLD))

        rows = cur.fetchall()

    events = []
    seen = set()  # Deduplicate by (minute, side, price)

    for ts, ltp, b5_buy, b5_sell in rows:
        minute_key = ts.strftime("%H:%M")

        # Check buy walls
        if b5_buy:
            levels = b5_buy if isinstance(b5_buy, list) else json.loads(b5_buy) if isinstance(b5_buy, str) else b5_buy
            if isinstance(levels, list):
                for level in levels:
                    qty = level.get("quantity", 0)
                    price = level.get("price", 0)
                    if qty >= MEGA_WALL_THRESHOLD:
                        key = (minute_key, "BUY", round(price, 1))
                        if key not in seen:
                            seen.add(key)
                            events.append({
                                "ts": minute_key,
                                "side": "BUY",
                                "price": round(price, 1),
                                "qty": qty,
                                "ltp": _decimal(ltp),
                            })

        # Check sell walls
        if b5_sell:
            levels = b5_sell if isinstance(b5_sell, list) else json.loads(b5_sell) if isinstance(b5_sell, str) else b5_sell
            if isinstance(levels, list):
                for level in levels:
                    qty = level.get("quantity", 0)
                    price = level.get("price", 0)
                    if qty >= MEGA_WALL_THRESHOLD:
                        key = (minute_key, "SELL", round(price, 1))
                        if key not in seen:
                            seen.add(key)
                            events.append({
                                "ts": minute_key,
                                "side": "SELL",
                                "price": round(price, 1),
                                "qty": qty,
                                "ltp": _decimal(ltp),
                            })

    # Sort by qty descending, take top 50
    events.sort(key=lambda e: e["qty"], reverse=True)
    return events[:50]


# ── Wall Count per Minute (for chart_data enrichment) ──────────────────────────

WALL_COUNT_THRESHOLD = 2000

def _count_walls_per_minute(conn, instrument_id: int, start: datetime, end: datetime) -> dict:
    """
    Count buy/sell wall occurrences per minute (levels >= 2000 qty).
    Returns {minute_str: {"buy": count, "sell": count}}.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                to_char(timestamp, 'HH24:MI') AS ts_min,
                SUM(
                    (SELECT COUNT(*) FROM jsonb_array_elements(
                        CASE WHEN best_5_buy IS NOT NULL AND jsonb_typeof(best_5_buy) = 'array'
                             THEN best_5_buy ELSE '[]'::jsonb END
                    ) elem WHERE (elem->>'quantity')::int >= %s)
                ) AS buy_walls,
                SUM(
                    (SELECT COUNT(*) FROM jsonb_array_elements(
                        CASE WHEN best_5_sell IS NOT NULL AND jsonb_typeof(best_5_sell) = 'array'
                             THEN best_5_sell ELSE '[]'::jsonb END
                    ) elem WHERE (elem->>'quantity')::int >= %s)
                ) AS sell_walls
            FROM tick_data
            WHERE instrument_id = %s
              AND timestamp >= %s
              AND timestamp <= %s
            GROUP BY ts_min
            ORDER BY ts_min
        """, (WALL_COUNT_THRESHOLD, WALL_COUNT_THRESHOLD, instrument_id, start, end))

        rows = cur.fetchall()

    result = {}
    for ts_min, buy_w, sell_w in rows:
        result[ts_min] = {"buy": int(buy_w or 0), "sell": int(sell_w or 0)}
    return result


# ── Absorption Event Detection ─────────────────────────────────────────────────

ABSORPTION_WALL_QTY = 3000

def _get_absorption_events(conn, instrument_id: int, start: datetime, end: datetime) -> list:
    """
    Detect absorption events: large walls (>= 3000 qty) that get consumed
    and price moves through them.

    Logic: Find ticks with large walls, then check if price moved beyond
    the wall price within the next few minutes.
    """
    with conn.cursor() as cur:
        # Get ticks with large walls
        cur.execute("""
            SELECT timestamp, ltp, best_5_buy, best_5_sell
            FROM tick_data
            WHERE instrument_id = %s
              AND timestamp >= %s
              AND timestamp <= %s
              AND (
                  EXISTS (
                      SELECT 1 FROM jsonb_array_elements(
                          CASE WHEN best_5_buy IS NOT NULL AND jsonb_typeof(best_5_buy) = 'array'
                               THEN best_5_buy ELSE '[]'::jsonb END
                      ) elem
                      WHERE (elem->>'quantity')::int >= %s
                  )
                  OR
                  EXISTS (
                      SELECT 1 FROM jsonb_array_elements(
                          CASE WHEN best_5_sell IS NOT NULL AND jsonb_typeof(best_5_sell) = 'array'
                               THEN best_5_sell ELSE '[]'::jsonb END
                      ) elem
                      WHERE (elem->>'quantity')::int >= %s
                  )
              )
            ORDER BY timestamp
        """, (instrument_id, start, end, ABSORPTION_WALL_QTY, ABSORPTION_WALL_QTY))

        wall_ticks = cur.fetchall()

    if not wall_ticks:
        return []

    # For each wall, check if price moved through it
    absorptions = []
    seen_absorptions = set()

    for ts, ltp, b5_buy, b5_sell in wall_ticks:
        minute_key = ts.strftime("%H:%M")

        # Check buy wall absorption (price drops below buy wall)
        if b5_buy:
            levels = b5_buy if isinstance(b5_buy, list) else []
            if isinstance(levels, list):
                for level in levels:
                    qty = level.get("quantity", 0)
                    price = level.get("price", 0)
                    if qty >= ABSORPTION_WALL_QTY and price > 0:
                        # Check if price later went below this buy wall
                        with manager.get_connection() as check_conn:
                            with check_conn.cursor() as cur2:
                                cur2.execute("""
                                    SELECT MIN(ltp)
                                    FROM tick_data
                                    WHERE instrument_id = %s
                                      AND timestamp > %s
                                      AND timestamp <= %s + INTERVAL '5 minutes'
                                """, (instrument_id, ts, ts))
                                result = cur2.fetchone()

                        if result and result[0]:
                            min_price_after = float(result[0])
                            if min_price_after < price:
                                move = round(min_price_after - price, 1)
                                key = (minute_key, "Buy Wall Absorbed", round(price, 1))
                                if key not in seen_absorptions:
                                    seen_absorptions.add(key)
                                    absorptions.append({
                                        "ts": minute_key,
                                        "type": "Buy Wall Absorbed",
                                        "price": round(_decimal(ltp), 1),
                                        "move": move,
                                    })

        # Check sell wall absorption (price rises above sell wall)
        if b5_sell:
            levels = b5_sell if isinstance(b5_sell, list) else []
            if isinstance(levels, list):
                for level in levels:
                    qty = level.get("quantity", 0)
                    price = level.get("price", 0)
                    if qty >= ABSORPTION_WALL_QTY and price > 0:
                        with manager.get_connection() as check_conn:
                            with check_conn.cursor() as cur2:
                                cur2.execute("""
                                    SELECT MAX(ltp)
                                    FROM tick_data
                                    WHERE instrument_id = %s
                                      AND timestamp > %s
                                      AND timestamp <= %s + INTERVAL '5 minutes'
                                """, (instrument_id, ts, ts))
                                result = cur2.fetchone()

                        if result and result[0]:
                            max_price_after = float(result[0])
                            if max_price_after > price:
                                move = round(max_price_after - price, 1)
                                key = (minute_key, "Sell Wall Absorbed", round(price, 1))
                                if key not in seen_absorptions:
                                    seen_absorptions.add(key)
                                    absorptions.append({
                                        "ts": minute_key,
                                        "type": "Sell Wall Absorbed",
                                        "price": round(_decimal(ltp), 1),
                                        "move": move,
                                    })

    return absorptions[:30]


# ── Summary Stats ──────────────────────────────────────────────────────────────

def _get_summary(chart_data: list, wall_events: list, absorption_events: list) -> dict:
    """Compute stat strip values from chart_data."""
    if not chart_data:
        return {
            "avg_spread": 0, "max_spread": 0,
            "buy_wall_count": 0, "sell_wall_count": 0,
            "absorption_count": 0, "avg_imbalance": 0,
            "imbalance_bias": "neutral",
        }

    spreads = [d["avg_spread"] for d in chart_data if d["avg_spread"] > 0]
    max_spreads = [d["max_spread"] for d in chart_data if d["max_spread"] > 0]
    imbalances = [d["avg_imbalance"] for d in chart_data]

    avg_spread = round(sum(spreads) / len(spreads), 2) if spreads else 0
    max_spread = max(max_spreads) if max_spreads else 0
    avg_imbalance = round(sum(imbalances) / len(imbalances), 4) if imbalances else 0

    buy_wall_count = sum(1 for e in wall_events if e["side"] == "BUY")
    sell_wall_count = sum(1 for e in wall_events if e["side"] == "SELL")

    total_buy_walls = sum(d["buy_walls"] for d in chart_data)
    total_sell_walls = sum(d["sell_walls"] for d in chart_data)

    if avg_imbalance > 0.02:
        bias = "buy-side bias"
    elif avg_imbalance < -0.02:
        bias = "sell-side bias"
    else:
        bias = "neutral"

    return {
        "avg_spread": avg_spread,
        "max_spread": max_spread,
        "buy_wall_count": total_buy_walls,
        "sell_wall_count": total_sell_walls,
        "absorption_count": len(absorption_events),
        "avg_imbalance": avg_imbalance,
        "imbalance_bias": bias,
    }


# ── API Endpoint ───────────────────────────────────────────────────────────────

@app.get("/api/dashboard-data")
def get_dashboard_data():
    """
    Single endpoint returning all dashboard data for today's session.
    Uses a 10-second in-memory cache to avoid expensive re-queries.
    """
    # Check cache first
    now = _time.time()
    if _cache["data"] is not None and (now - _cache["timestamp"]) < CACHE_TTL_SECONDS:
        return _cache["data"]

    conn = manager.get_connection()
    try:
        instrument_id, symbol = _get_nifty_futures_instrument_id(conn)
        if not instrument_id:
            return JSONResponse(
                status_code=404,
                content={"error": "NIFTY Futures instrument not found in DB"},
            )

        start, end = _get_today_range()

        # 1. Per-minute chart data
        chart_data = _get_chart_data(conn, instrument_id, start, end)

        # 2. Wall counts per minute → enrich chart_data
        wall_counts = _count_walls_per_minute(conn, instrument_id, start, end)
        for bar in chart_data:
            wc = wall_counts.get(bar["ts"], {"buy": 0, "sell": 0})
            bar["buy_walls"] = wc["buy"]
            bar["sell_walls"] = wc["sell"]

        # 3. Wall events (mega walls >= 5000)
        wall_events = _get_wall_events(conn, instrument_id, start, end)

        # 4. Absorption events
        absorption_events = _get_absorption_events(conn, instrument_id, start, end)

        # 5. Summary stats
        summary = _get_summary(chart_data, wall_events, absorption_events)

        # 6. Latest price info
        latest_price = None
        session_start_time = None
        session_end_time = None
        if chart_data:
            latest_price = chart_data[-1]["ltp"]
            session_start_time = chart_data[0]["ts"]
            session_end_time = chart_data[-1]["ts"]

        response = {
            "chart_data": chart_data,
            "wall_events": wall_events,
            "absorption_events": absorption_events,
            "summary": summary,
            "meta": {
                "symbol": symbol or "NIFTY FUT",
                "date": date.today().strftime("%d %b %Y").upper(),
                "latest_price": latest_price,
                "session_start": session_start_time,
                "session_end": session_end_time,
                "last_updated": datetime.now().strftime("%H:%M:%S"),
                "bar_count": len(chart_data),
            },
        }

        # Store in cache
        _cache["data"] = response
        _cache["timestamp"] = _time.time()

        return response

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)},
        )
    finally:
        conn.close()


# ── WebSocket Endpoint ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint — pushes dashboard data to connected clients."""
    await ws.accept()
    _connected_clients.add(ws)
    print(f"WebSocket client connected. Total: {len(_connected_clients)}")
    try:
        # Send current cached data immediately on connect
        if _cache["data"] is not None:
            await ws.send_json(_cache["data"])

        # Keep connection alive — listen for pings/close
        while True:
            # Wait for any message (client sends pings to keep alive)
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                # Send a ping to check if client is still alive
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        _connected_clients.discard(ws)
        print(f"WebSocket client disconnected. Total: {len(_connected_clients)}")


async def _build_dashboard_data() -> dict | None:
    """Build dashboard data (runs in thread pool to avoid blocking asyncio)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _build_dashboard_data_sync)


def _build_dashboard_data_sync() -> dict | None:
    """Synchronous version of dashboard data builder."""
    conn = manager.get_connection()
    try:
        instrument_id, symbol = _get_nifty_futures_instrument_id(conn)
        if not instrument_id:
            return None

        start, end = _get_today_range()
        chart_data = _get_chart_data(conn, instrument_id, start, end)
        wall_counts = _count_walls_per_minute(conn, instrument_id, start, end)
        for bar in chart_data:
            wc = wall_counts.get(bar["ts"], {"buy": 0, "sell": 0})
            bar["buy_walls"] = wc["buy"]
            bar["sell_walls"] = wc["sell"]

        wall_events = _get_wall_events(conn, instrument_id, start, end)
        absorption_events = _get_absorption_events(conn, instrument_id, start, end)
        summary = _get_summary(chart_data, wall_events, absorption_events)

        latest_price = None
        session_start_time = None
        session_end_time = None
        if chart_data:
            latest_price = chart_data[-1]["ltp"]
            session_start_time = chart_data[0]["ts"]
            session_end_time = chart_data[-1]["ts"]

        response = {
            "chart_data": chart_data,
            "wall_events": wall_events,
            "absorption_events": absorption_events,
            "summary": summary,
            "meta": {
                "symbol": symbol or "NIFTY FUT",
                "date": date.today().strftime("%d %b %Y").upper(),
                "latest_price": latest_price,
                "session_start": session_start_time,
                "session_end": session_end_time,
                "last_updated": datetime.now().strftime("%H:%M:%S"),
                "bar_count": len(chart_data),
            },
        }

        _cache["data"] = response
        _cache["timestamp"] = _time.time()
        return response

    except Exception as e:
        print(f"Error building dashboard data: {e}")
        return None
    finally:
        conn.close()


def _pg_listen_thread(loop):
    """Runs in a background thread to listen for PostgreSQL NOTIFY events using select."""
    print("PostgreSQL LISTEN thread started")
    while True:
        try:
            conn = manager.get_connection()
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute("LISTEN tick_update;")
            
            while True:
                if select.select([conn], [], [], 5.0) == ([conn], [], []):
                    conn.poll()
                    while conn.notifies:
                        # Clear all pending notifications
                        conn.notifies.pop(0)
                        
                    # Signal the broadcast loop that an update arrived
                    loop.call_soon_threadsafe(_tick_event.set)
        except Exception as e:
            print(f"PG listen thread error: {e}. Reconnecting in 5s...")
            _time.sleep(5)
        finally:
            try:
                conn.close()
            except Exception:
                pass


async def _ws_broadcast_loop():
    """
    Background task: waits for `_tick_event` signals (triggered by PG NOTIFY)
    and pushes fresh data to all connected WebSocket clients immediately.
    """
    loop = asyncio.get_event_loop()
    threading.Thread(target=_pg_listen_thread, args=(loop,), daemon=True).start()
    
    print("WebSocket broadcast loop started (event-driven via Postgres NOTIFY)")
    while True:
        await _tick_event.wait()
        _tick_event.clear()
        
        # Debounce: wait briefly to allow a batch of ticks to finish inserting
        await asyncio.sleep(0.5)
        
        # If no clients, don't waste time querying the DB
        if not _connected_clients:
            continue

        data = await _build_dashboard_data()
        if data is None:
            continue

        # Broadcast to all connected clients
        dead_clients = set()
        for client in _connected_clients.copy():
            try:
                await client.send_json(data)
            except Exception:
                dead_clients.add(client)

        # Clean up dead connections
        for client in dead_clients:
            _connected_clients.discard(client)

        if dead_clients:
            print(f"Removed {len(dead_clients)} dead WebSocket client(s). Active: {len(_connected_clients)}")


@app.on_event("startup")
async def startup_event():
    """Start the WebSocket broadcast loop on server startup."""
    asyncio.create_task(_ws_broadcast_loop())


# ── Serve Dashboard HTML ───────────────────────────────────────────────────────

@app.get("/")
def serve_dashboard():
    """Serve the live dashboard HTML."""
    html_path = DASHBOARD_DIR / "live_dashboard.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    return JSONResponse(
        status_code=404,
        content={"error": "live_dashboard.html not found"},
    )


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("Starting Live Dashboard Server on http://localhost:8050")
    print("Open http://localhost:8050 in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8050, log_level="info")
