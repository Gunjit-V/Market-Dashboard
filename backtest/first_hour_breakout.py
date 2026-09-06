"""
First-hour range breakout study (signal only, no option structure).

Range      : 09:15-10:15 high/low from 1-min bars.
Confirm    : a 15-min candle must CLOSE beyond the range boundary.
Entry      : close of the confirming 15-min candle.
Exit       : 15:20 (square-off) or stop at the opposite range boundary.
Measures   : follow-through vs reversal, MFE/MAE, and regime split.

No option pricing here on purpose -- this tests whether the direction call
has any edge at all. Everything is in index points.
"""
import os, argparse
from collections import namedtuple
import psycopg2
from dotenv import load_dotenv

load_dotenv()

RANGE_START, RANGE_END = "09:15", "10:15"
SQUARE_OFF = "15:20"
REGIME_SPLIT = "2024-04-01"   # user's stated regime change

Day = namedtuple("Day", "dt hi lo bars")


def db():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
    )


def load_days(conn, instrument_id):
    """One pass: pull every 1-min bar, bucket by date."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT timestamp::date, timestamp::time,
                   open::float, high::float, low::float, close::float
            FROM ohlcv_1min
            WHERE instrument_id = %s
            ORDER BY ohlcv_1min.timestamp
        """, (instrument_id,))
        rows = cur.fetchall()

    days = {}
    for dt, tm, o, h, l, c in rows:
        days.setdefault(dt, []).append((tm.strftime("%H:%M"), o, h, l, c))
    return days


def to_15min(bars):
    """Aggregate 1-min bars into 15-min candles keyed by bucket start."""
    out = {}
    for tm, o, h, l, c in bars:
        hh, mm = int(tm[:2]), int(tm[3:5])
        key = f"{hh:02d}:{(mm // 15) * 15:02d}"
        if key not in out:
            out[key] = [o, h, l, c]
        else:
            b = out[key]
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c
    return out


def run(instrument_id, buffer_pct=0.0):
    conn = db()
    try:
        days = load_days(conn, instrument_id)
    finally:
        conn.close()

    trades = []
    no_signal = 0

    for dt in sorted(days):
        bars = days[dt]
        if len(bars) < 300:            # skip truncated sessions
            continue

        rng = [b for b in bars if RANGE_START <= b[0] < RANGE_END]
        if not rng:
            continue
        hi = max(b[2] for b in rng)
        lo = min(b[3] for b in rng)
        if hi <= lo:
            continue

        buf = (hi - lo) * buffer_pct
        c15 = to_15min(bars)
        after = sorted(k for k in c15 if RANGE_END <= k < SQUARE_OFF)

        side = entry = entry_t = None
        for k in after:
            _, _, _, close = c15[k]
            if close > hi + buf:
                side, entry, entry_t = "LONG", close, k
                break
            if close < lo - buf:
                side, entry, entry_t = "SHORT", close, k
                break

        if side is None:
            no_signal += 1
            continue

        # Post-entry path from 1-min bars (strictly after the confirming candle)
        eh, em = int(entry_t[:2]), int(entry_t[3:5])
        end_min = eh * 60 + em + 15
        post = [b for b in bars
                if (int(b[0][:2]) * 60 + int(b[0][3:5])) >= end_min
                and b[0] <= SQUARE_OFF]
        if not post:
            continue

        stop = lo if side == "LONG" else hi
        exit_px, exit_reason = post[-1][4], "eod"
        mfe = mae = 0.0

        for _, o, h, l, c in post:
            up, dn = h - entry, entry - l
            if side == "LONG":
                mfe, mae = max(mfe, up), max(mae, dn)
                if l <= stop:
                    exit_px, exit_reason = stop, "stop"
                    break
            else:
                mfe, mae = max(mfe, dn), max(mae, up)
                if h >= stop:
                    exit_px, exit_reason = stop, "stop"
                    break

        pnl = (exit_px - entry) if side == "LONG" else (entry - exit_px)
        trades.append(dict(dt=dt, side=side, entry_t=entry_t, entry=entry,
                           exit=exit_px, reason=exit_reason, pnl=pnl,
                           mfe=mfe, mae=mae, rng=hi - lo))
    return trades, no_signal


def stats(label, ts):
    if not ts:
        print(f"{label:<22} no trades")
        return
    n = len(ts)
    wins = [t for t in ts if t["pnl"] > 0]
    pnl = [t["pnl"] for t in ts]
    tot = sum(pnl)
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in ts if t["pnl"] <= 0)
    stops = sum(1 for t in ts if t["reason"] == "stop")
    print(f"{label:<22} {n:>5} {100*len(wins)/n:>7.1f}% {tot:>9.0f} "
          f"{tot/n:>8.2f} {sum(t['mfe'] for t in ts)/n:>7.1f} "
          f"{sum(t['mae'] for t in ts)/n:>7.1f} {100*stops/n:>7.1f}% "
          f"{(gw/gl if gl else float('inf')):>7.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", type=int, default=2503)
    ap.add_argument("--buffer", type=float, default=0.0,
                    help="breakout buffer as fraction of range width")
    a = ap.parse_args()

    trades, no_sig = run(a.instrument, a.buffer)
    total = len(trades) + no_sig
    print(f"\nInstrument {a.instrument} | buffer={a.buffer:.0%} | "
          f"{total} sessions | {len(trades)} signals "
          f"({100*len(trades)/total:.0f}%) | {no_sig} no-breakout\n")
    hdr = f"{'':<22} {'n':>5} {'win%':>8} {'net_pts':>9} {'avg':>8} {'MFE':>7} {'MAE':>7} {'stop%':>8} {'PF':>7}"
    print(hdr); print("-" * len(hdr))
    stats("ALL", trades)
    stats("LONG", [t for t in trades if t["side"] == "LONG"])
    stats("SHORT", [t for t in trades if t["side"] == "SHORT"])
    print("-" * len(hdr))
    stats(f"pre  {REGIME_SPLIT}", [t for t in trades if str(t["dt"]) < REGIME_SPLIT])
    stats(f"post {REGIME_SPLIT}", [t for t in trades if str(t["dt"]) >= REGIME_SPLIT])
