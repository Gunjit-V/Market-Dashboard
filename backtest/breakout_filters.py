"""Test confirmation filters on top of the first-hour breakout signal."""
import sys, statistics
sys.path.insert(0, ".")
from first_hour_breakout import run as _r
import first_hour_breakout as F

def enriched(buffer_pct=0.05):
    """Re-run but keep range width + entry time for filtering."""
    trades, _ = _r(2503, buffer_pct)
    return trades

def report(label, ts, base_n):
    if not ts:
        print(f"{label:<30} {'--':>5}"); return
    n = len(ts); tot = sum(t["pnl"] for t in ts)
    w = sum(1 for t in ts if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in ts if t["pnl"] <= 0)
    gw = sum(t["pnl"] for t in ts if t["pnl"] > 0)
    print(f"{label:<30} {n:>5} {100*n/base_n:>6.0f}% {100*w/n:>7.1f}% "
          f"{tot:>9.0f} {tot/n:>8.2f} {(gw/gl if gl else 99):>7.2f}")

if __name__ == "__main__":
    ts = enriched(0.05)
    post = [t for t in ts if str(t["dt"]) >= "2024-04-01"]
    widths = sorted(t["rng"] for t in post)
    med = statistics.median(widths)
    base = len(post)

    hdr = f"{'filter (post-Apr-2024)':<30} {'n':>5} {'kept':>7} {'win%':>7} {'net_pts':>9} {'avg':>8} {'PF':>7}"
    print(hdr); print("-"*len(hdr))
    report("no filter", post, base)
    print("-"*len(hdr))
    # 1. Range width
    report(f"narrow range (<{med:.0f}pts)", [t for t in post if t["rng"] < med], base)
    report(f"wide range (>={med:.0f}pts)", [t for t in post if t["rng"] >= med], base)
    # 2. Entry timing
    report("early breakout (<=11:30)", [t for t in post if t["entry_t"] <= "11:30"], base)
    report("late breakout (>11:30)", [t for t in post if t["entry_t"] > "11:30"], base)
    report("first candle only (10:15)", [t for t in post if t["entry_t"] == "10:15"], base)
    # 3. Direction
    report("LONG only", [t for t in post if t["side"]=="LONG"], base)
    report("SHORT only", [t for t in post if t["side"]=="SHORT"], base)
    # 4. Combined
    report("narrow + early", [t for t in post if t["rng"]<med and t["entry_t"]<="11:30"], base)
    report("narrow + early + SHORT", [t for t in post if t["rng"]<med and t["entry_t"]<="11:30" and t["side"]=="SHORT"], base)
