"""
Real-time LTP Monitor
Polls PostgreSQL every 1 second for the latest LTP per instrument
and plots a live line chart for both Nifty 50 Index and Nifty 50 Futures.
"""

import sys
import os
import time
from datetime import datetime

from db.connection import ConnectionManager
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.animation import FuncAnimation

# ── Database ──────────────────────────────────────────────────────────────────
manager = ConnectionManager()


def get_instrument_names(conn) -> dict:
    """
    Fetch the instrument_id → display name mapping for the two Nifty instruments.
    Returns {instrument_id: label} dict.
    """
    labels = {}
    with conn.cursor() as cur:
        # Nifty 50 Index
        cur.execute("""
            SELECT id, symbol FROM instruments
            WHERE instrument_type = 'AMXIDX'
              AND (name ILIKE '%Nifty 50%' OR symbol ILIKE '%Nifty 50%')
            LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            labels[row[0]] = f"Nifty 50 Index ({row[1]})"

        # Nifty 50 Futures (nearest expiry)
        cur.execute("""
            SELECT id, symbol, expiry FROM instruments
            WHERE instrument_type = 'FUTIDX'
              AND name = 'NIFTY'
              AND expiry >= CURRENT_DATE
            ORDER BY expiry ASC
            LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            expiry_str = row[2].strftime("%d-%b") if row[2] else ""
            labels[row[0]] = f"Nifty 50 Futures ({row[1]} {expiry_str})"

    return labels


def fetch_latest_ltp(conn, instrument_ids: list) -> list[tuple]:
    """
    For each instrument_id, get the latest LTP row ordered by id DESC.
    Returns list of (instrument_id, timestamp, ltp).
    """
    if not instrument_ids:
        return []

    results = []
    with conn.cursor() as cur:
        for iid in instrument_ids:
            cur.execute("""
                SELECT instrument_id, timestamp, ltp
                FROM tick_data
                WHERE instrument_id = %s
                ORDER BY id DESC
                LIMIT 1
            """, (iid,))
            row = cur.fetchone()
            if row:
                results.append(row)
    return results


def main():
    print("Connecting to PostgreSQL...")
    conn = manager.get_connection()
    print("Connected!\n")

    # Discover instruments
    labels = get_instrument_names(conn)
    if not labels:
        print("No Nifty instruments found in the instruments table!")
        conn.close()
        sys.exit(1)

    instrument_ids = list(labels.keys())
    print("Monitoring instruments:")
    for iid, name in labels.items():
        print(f"  • {name}  (id={iid})")
    print()

    # Data storage: {instrument_id: {"times": [], "prices": []}}
    series = {iid: {"times": [], "prices": []} for iid in instrument_ids}

    # ── Chart setup ────────────────────────────────────────────────────────────
    plt.style.use("dark_background")
    fig, axes = plt.subplots(
        len(instrument_ids), 1,
        figsize=(14, 5 * len(instrument_ids)),
        sharex=True,
    )
    if len(instrument_ids) == 1:
        axes = [axes]

    # Colour palette
    colors = ["#00e5ff", "#ff6e40"]
    lines = {}

    for idx, iid in enumerate(instrument_ids):
        ax = axes[idx]
        color = colors[idx % len(colors)]
        line, = ax.plot([], [], color=color, linewidth=1.5, label=labels[iid])
        lines[iid] = (ax, line)

        ax.set_ylabel("LTP (₹)", fontsize=11, color="#aaaaaa")
        ax.legend(loc="upper left", fontsize=10,
                  facecolor="#1e1e1e", edgecolor="#444")
        ax.grid(True, alpha=0.2, linestyle="--")
        ax.tick_params(colors="#888888")

    axes[-1].set_xlabel("Time", fontsize=11, color="#aaaaaa")
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    fig.suptitle(
        "Real-Time LTP Monitor",
        fontsize=16, fontweight="bold", color="white", y=0.98,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.95])

    # ── Animation update ───────────────────────────────────────────────────────
    def update(frame):
        try:
            rows = fetch_latest_ltp(conn, instrument_ids)
        except Exception as e:
            print(f"  DB error: {e}")
            return list(lines.values())

        for instrument_id, ts, ltp in rows:
            s = series[instrument_id]
            # Only append if this is a new timestamp
            if not s["times"] or ts != s["times"][-1]:
                s["times"].append(ts)
                s["prices"].append(float(ltp))

            ax, line = lines[instrument_id]
            line.set_data(s["times"], s["prices"])
            ax.relim()
            ax.autoscale_view()

            # Show current price in title
            ax.set_title(
                f"{labels[instrument_id]}  —  ₹{float(ltp):,.2f}",
                fontsize=12, color=colors[instrument_ids.index(instrument_id) % len(colors)],
                pad=8,
            )

        fig.canvas.draw_idle()
        return [line for _, line in lines.values()]

    # Poll every 1000 ms (1 second)
    ani = FuncAnimation(fig, update, interval=5000, cache_frame_data=False)

    print("Chart window opened. Close the window or press Ctrl+C to stop.\n")

    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
        print("Connection closed. Monitor stopped.")


if __name__ == "__main__":
    main()
