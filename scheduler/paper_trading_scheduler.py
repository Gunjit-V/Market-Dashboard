"""Run paper-trading strategy evaluations after each 5-minute OHLCV bar.

Mirrors scheduler.ohlcv_scheduler's structure: a stdlib-only loop gated on
market hours and trading days via scheduler.nse_calendar. Runs slightly
after each 5-min boundary (PAPER_DELAY_SECONDS past the delay the OHLCV
scheduler itself uses) so this always evaluates against that bar's data,
not a stale one.

Strategies evaluated:
  - rv_breakout on each symbol in PAPER_RV_SYMBOLS (default: Nifty 50).
  - vrp_reversion on each NIFTY option within PAPER_VRP_STRIKE_RANGE
    strikes of the current ATM at the nearest expiry (both CE and PE),
    recomputed each run since ATM moves — mirrors how
    downloader.sync_instruments picks the option activation band.

Configure with environment variables:

    PAPER_RV_SYMBOLS=Nifty 50
    PAPER_VRP_UNDERLYING=NIFTY25AUG26FUT
    PAPER_VRP_STRIKE_RANGE=2
    PAPER_DELAY_SECONDS=30
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, time as clock_time
from zoneinfo import ZoneInfo

from backtest.paper_trading import evaluate_rv_breakout_paper, evaluate_vrp_reversion_paper
from db.connection import ConnectionManager
from scheduler.nse_calendar import is_nse_trading_day

LOGGER = logging.getLogger("paper_trading_scheduler")
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = clock_time(9, 15)
MARKET_CLOSE = clock_time(15, 30)
PAPER_DELAY_SECONDS = int(os.getenv("PAPER_DELAY_SECONDS", "30"))


def _csv(name: str, default: str = "") -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


RV_SYMBOLS = _csv("PAPER_RV_SYMBOLS", "Nifty 50")
VRP_UNDERLYING = os.getenv("PAPER_VRP_UNDERLYING", "NIFTY25AUG26FUT")
VRP_STRIKE_RANGE = int(os.getenv("PAPER_VRP_STRIKE_RANGE", "2"))


def in_market_hours(moment: datetime) -> bool:
    current = moment.timetz().replace(tzinfo=None)
    return MARKET_OPEN <= current < MARKET_CLOSE


def _current_atm_options(conn, name: str = "NIFTY", strike_range: int = VRP_STRIKE_RANGE) -> list[str]:
    """Return option symbols within `strike_range` strikes of the current
    active-instrument midpoint at the nearest expiry. Uses the active-set
    already maintained by sync_instruments (ATM +/- OPTIONS_STRIKE_RANGE)
    rather than re-deriving ATM from a live spot quote, so this needs no
    SmartAPI session of its own.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, strike FROM instruments
            WHERE instrument_type = 'OPTIDX' AND name = %s AND is_active
              AND expiry = (
                  SELECT MIN(expiry) FROM instruments
                  WHERE instrument_type = 'OPTIDX' AND name = %s AND is_active
              )
            ORDER BY strike
            """,
            (name, name),
        )
        rows = cur.fetchall()
    if not rows:
        return []
    strikes = sorted({float(r[1]) for r in rows})
    mid_strike = strikes[len(strikes) // 2]
    step = strikes[1] - strikes[0] if len(strikes) > 1 else 50.0
    lo, hi = mid_strike - strike_range * step, mid_strike + strike_range * step
    return [symbol for symbol, strike in rows if lo <= float(strike) <= hi]


def _run_cycle() -> None:
    for symbol in RV_SYMBOLS:
        try:
            result = evaluate_rv_breakout_paper(symbol=symbol)
            LOGGER.info("rv_breakout[%s]: %s", symbol, result)
        except Exception:
            LOGGER.exception("rv_breakout evaluation failed for %s", symbol)

    conn = ConnectionManager().get_connection()
    try:
        option_symbols = _current_atm_options(conn)
    except Exception:
        LOGGER.exception("Failed to resolve ATM option band")
        option_symbols = []
    finally:
        conn.close()

    for opt_symbol in option_symbols:
        try:
            result = evaluate_vrp_reversion_paper(
                option_symbol=opt_symbol, underlying_symbol=VRP_UNDERLYING
            )
            LOGGER.info("vrp_reversion[%s]: %s", opt_symbol, result)
        except Exception:
            LOGGER.exception("vrp_reversion evaluation failed for %s", opt_symbol)


def run_forever() -> None:
    LOGGER.info(
        "Paper trading scheduler started; rv_symbols=%s vrp_underlying=%s strike_range=%s",
        RV_SYMBOLS, VRP_UNDERLYING, VRP_STRIKE_RANGE,
    )
    last_tick: tuple[date, int] | None = None

    while True:
        now = datetime.now(IST)
        tick = (now.date(), now.hour * 60 + now.minute)

        # 5-min boundaries aligned with the 09:15 session open, after a
        # delay long enough for the OHLCV scheduler's own bar-delay plus
        # its download/insert time to have completed.
        on_boundary = (now.hour * 60 + now.minute - 9 * 60 - 15) % 5 == 0
        if (
            tick != last_tick
            and now.second >= PAPER_DELAY_SECONDS
            and is_nse_trading_day(now.date())
            and in_market_hours(now)
            and on_boundary
        ):
            _run_cycle()
            last_tick = tick

        time.sleep(2.0)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("PAPER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
