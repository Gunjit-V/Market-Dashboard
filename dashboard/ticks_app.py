import psycopg2.extras
import psycopg2.extensions
from db.connection import ConnectionManager
import sys
import os
import json
import asyncio
import threading
import select
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import date

# Ensure project root is on sys.path
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = ConnectionManager()
DASHBOARD_DIR = Path(__file__).resolve().parent

# Global state for WebSocket clients
_clients: set[WebSocket] = set()
_loop = None


def _listen_pg():
    """Background thread to listen to Postgres NOTIFY."""
    print("Started Postgres LISTEN thread on 'new_tick' channel")
    while True:
        try:
            conn = manager.get_connection()
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute("LISTEN new_tick;")

            # Listen indefinitely
            while True:
                if select.select([conn], [], [], 5) == ([], [], []):
                    pass
                else:
                    conn.poll()
                    while conn.notifies:
                        notify = conn.notifies.pop(0)
                        payload = notify.payload
                        if _loop and _clients:
                            asyncio.run_coroutine_threadsafe(
                                _broadcast(payload), _loop
                            )
        except Exception as e:
            print(f"PG Listen Error: {e}. Reconnecting...")
            import time
            time.sleep(5)
        finally:
            try:
                conn.close()
            except Exception:
                pass


async def _broadcast(payload: str):
    """Broadcast JSON string to all clients."""
    dead = set()
    for ws in _clients.copy():
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _clients.discard(ws)


@app.on_event("startup")
async def startup():
    global _loop
    _loop = asyncio.get_running_loop()
    threading.Thread(target=_listen_pg, daemon=True).start()


@app.get("/")
def get_index():
    path = DASHBOARD_DIR / "ticks_app.html"
    return FileResponse(str(path))


@app.get("/api/instruments")
def get_instruments():
    """Fetch all unique instrument IDs available today."""
    conn = manager.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT instrument_id FROM tick_data WHERE timestamp >= CURRENT_DATE")
        rows = cur.fetchall()
        return [r[0] for r in rows if r[0] is not None]
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


@app.get("/api/history/{instrument_id}")
def get_history(instrument_id: int):
    """Fetch today's historical ticks for the given instrument, downsampled to fit chart."""
    conn = manager.get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT timestamp, ltp, total_buy_qty, total_sell_qty, best_5_buy, best_5_sell, instrument_id
            FROM tick_data
            WHERE instrument_id = %s AND timestamp >= CURRENT_DATE
            ORDER BY timestamp ASC
        """, (instrument_id,))
        rows = cur.fetchall()

        if not rows:
            return []

        # Downsample to maximum 1500 points so the UI doesn't lag out
        if len(rows) > 1500:
            step = len(rows) // 1500
            rows = rows[::step]

        data = []
        for r in rows:
            data.append({
                "timestamp": r["timestamp"].isoformat(),
                "ltp": float(r["ltp"]) if r["ltp"] else 0,
                "total_buy_qty": r["total_buy_qty"],
                "total_sell_qty": r["total_sell_qty"],
                "best_5_buy": r["best_5_buy"],
                "best_5_sell": r["best_5_sell"],
                "instrument_id": r["instrument_id"]
            })
        return data
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            # Keep connection alive
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)

if __name__ == "__main__":
    import uvicorn
    print("Starting tick dashboard on http://localhost:8060")
    uvicorn.run(app, host="0.0.0.0", port=8060)
