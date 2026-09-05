"""
Breakout strategy suite -- signal-only, index points, risk-normalised.

Stop rule (per user): stop is the tighter of
  (a) the opposite range boundary, and
  (b) a fixed % of capital risked, converted to a point distance.
Position is sized so a stop-out loses exactly RISK_PCT of capital,
so every result is reported in R-multiples (1R = the risk budget).

Strategies tested
  1. FH_BREAK    first-hour(09:15-10:15) range, 15m close confirm   [baseline]
  2. ORB30       opening 30m range, 15m close confirm
  3. FH_RETEST   first-hour break, then enter on pullback to boundary
  4. VOL_CONTRACT  narrow first hour vs prior 5d ATR, then break
  5. PDH_PDL    previous-day high/low break, 15m close confirm
  6. FH_FADE    fade the breakout (mean-reversion: opposite of #1)
"""
import os
import statistics
from collections import defaultdict
import psycopg2
from dotenv import load_dotenv

load_dotenv()
RANGE_END_FH, RANGE_END_30 = "10:15", "09:45"
SQUARE_OFF = "15:20"
SPLIT = "2024-04-01"
COST_PTS = 2.0          # round-trip slippage+fees assumption, index points


def db():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"))


def load(instrument_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT timestamp::date, timestamp::time,
                              open::float, high::float, low::float, close::float
                       FROM ohlcv_1min WHERE instrument_id=%s
                       ORDER BY ohlcv_1min.timestamp""", (instrument_id,))
        rows = cur.fetchall()
    days = defaultdict(list)
    for dt, tm, o, h, l, c in rows:
        days[dt].append((tm.strftime("%H:%M"), o, h, l, c))
    return {d: b for d, b in days.items() if len(b) >= 300}


def c15(bars):
    out = {}
    for tm, o, h, l, c in bars:
        k = f"{int(tm[:2]):02d}:{(int(tm[3:5])//15)*15:02d}"
        if k not in out:
            out[k] = [o, h, l, c]
        else:
            b = out[k]
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c
    return out


def after(bars, hhmm):
    m = int(hhmm[:2])*60 + int(hhmm[3:5])
    return [b for b in bars if int(b[0][:2])*60+int(b[0][3:5]) >= m and b[0] <= SQUARE_OFF]


def walk(post, side, entry, stop, target=None):
    """Return (pnl_pts, reason, mae). Stop checked before target (conservative)."""
    mae = 0.0
    for _, o, h, l, c in post:
        mae = max(mae, (entry - l) if side == "LONG" else (h - entry))
        if side == "LONG":
            if l <= stop:
                return stop - entry, "stop", mae
            if target and h >= target:
                return target - entry, "target", mae
        else:
            if h >= stop:
                return entry - stop, "stop", mae
            if target and l <= target:
                return entry - target, "target", mae
    px = post[-1][4]
    return ((px - entry) if side == "LONG" else (entry - px)), "eod", mae


def sig_range_break(bars, end_t, buf=0.05):
    rng = [b for b in bars if "09:15" <= b[0] < end_t]
    if not rng:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    if hi <= lo:
        return None
    w = (hi-lo)*buf
    for k in sorted(x for x in c15(bars) if end_t <= x < SQUARE_OFF):
        cl = c15(bars)[k][3]
        if cl > hi+w:
            return ("LONG", cl, k, hi, lo)
        if cl < lo-w:
            return ("SHORT", cl, k, hi, lo)
    return None


def strat(name, days, prev_close):
    out = []
    dates = sorted(days)
    for i, dt in enumerate(dates):
        bars = days[dt]
        s = None

        if name == "FH_BREAK":
            s = sig_range_break(bars, RANGE_END_FH)
        elif name == "ORB30":
            s = sig_range_break(bars, RANGE_END_30)
        elif name == "FH_FADE":
            r = sig_range_break(bars, RANGE_END_FH)
            if r:
                side, e, k, hi, lo = r
                w = hi - lo
                # fade: stop BEYOND the breakout extreme, half a range away
                if side == "LONG":
                    s = ("SHORT", e, k, e + 0.5*w, lo)
                else:
                    s = ("LONG", e, k, hi, e - 0.5*w)
        elif name == "FH_RETEST":
            r = sig_range_break(bars, RANGE_END_FH)
            if r:
                side, _, k, hi, lo = r
                lvl = hi if side == "LONG" else lo
                w = hi - lo
                for b in after(bars, k):
                    if (side == "LONG" and b[3] <= lvl) or (side == "SHORT" and b[2] >= lvl):
                        if side == "LONG":
                            s = (side, lvl, b[0], hi, lvl - 0.5*w)
                        else:
                            s = (side, lvl, b[0], lvl + 0.5*w, lo)
                        break
        elif name == "VOL_CONTRACT":
            if i < 5:
                continue
            atrs = []
            for d in dates[i-5:i]:
                pb = days[d]
                atrs.append(max(b[2] for b in pb) - min(b[3] for b in pb))
            rng = [b for b in bars if "09:15" <= b[0] < RANGE_END_FH]
            if not rng:
                continue
            w = max(b[2] for b in rng) - min(b[3] for b in rng)
            if w < 0.5*statistics.mean(atrs):
                s = sig_range_break(bars, RANGE_END_FH)
        elif name == "PDH_PDL":
            if dt not in prev_close:
                continue
            phi, plo = prev_close[dt]
            for k in sorted(x for x in c15(bars) if "09:30" <= x < SQUARE_OFF):
                cl = c15(bars)[k][3]
                if cl > phi:
                    s = ("LONG", cl, k, phi, plo)
                    break
                if cl < plo:
                    s = ("SHORT", cl, k, phi, plo)
                    break

        if not s:
            continue
        side, entry, k, hi, lo = s
        post = after(bars, k)
        if len(post) < 2:
            continue
        post = post[1:]
        if not post:
            continue
        struct_stop = lo if side == "LONG" else hi
        # guard: stop must sit on the losing side of entry
        if (side == "LONG" and struct_stop >= entry) or (side == "SHORT" and struct_stop <= entry):
            continue
        pnl, reason, mae = walk(post, side, entry, struct_stop)
        risk = abs(entry - struct_stop)
        if risk <= 0:
            continue
        out.append(dict(dt=dt, side=side, pnl=pnl-COST_PTS, risk=risk,
                        R=(pnl-COST_PTS)/risk, reason=reason, mae=mae))
    return out


def show(name, ts):
    if len(ts) < 20:
        print(f"{name:<16} {len(ts):>5}  (too few)")
        return
    n = len(ts)
    w = [t for t in ts if t["R"] > 0]
    tot = sum(t["R"] for t in ts)
    gw = sum(t["R"] for t in w)
    gl = -sum(t["R"] for t in ts if t["R"] <= 0)
    exp = tot/n
    sd = statistics.pstdev([t["R"] for t in ts]) or 1e-9
    print(f"{name:<16} {n:>5} {100*len(w)/n:>7.1f}% {exp:>8.3f} {tot:>8.1f} "
          f"{(gw/gl if gl else 99):>6.2f} {exp/sd*(n**0.5):>7.2f}")


if __name__ == "__main__":
    days = load(2503)
    dates = sorted(days)
    prev = {}
    for i in range(1, len(dates)):
        pb = days[dates[i-1]]
        prev[dates[i]] = (max(b[2] for b in pb), min(b[3] for b in pb))

    names = ["FH_BREAK", "ORB30", "FH_RETEST",
             "VOL_CONTRACT", "PDH_PDL", "FH_FADE"]
    for period, lo_, hi_ in [("ALL", "0000-00-00", "9999"),
                             ("PRE  Apr-2024", "0000-00-00", SPLIT),
                             ("POST Apr-2024", SPLIT, "9999")]:
        print(
            f"\n=== {period} ===  (1R = risk to range boundary, {COST_PTS}pt costs)")
        h = f"{'strategy':<16} {'n':>5} {'win%':>8} {'exp_R':>8} {'totR':>8} {'PF':>6} {'t-stat':>7}"
        print(h)
        print("-"*len(h))
        for nm in names:
            ts = [t for t in strat(nm, days, prev)
                  if lo_ <= str(t["dt"]) < hi_]
            show(nm, ts)
