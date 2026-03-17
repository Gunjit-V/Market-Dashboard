"""
Volatility API — IV chain, ATM detection, strike list.

Provides endpoints for the IV/RV Dashboard frontend page.
"""

from fastapi import APIRouter, Depends, Query
from typing import Optional
from datetime import date, datetime, timedelta
from api.dependencies import get_db
from api.models.schemas import Response
from api.utils.iv import implied_volatility, time_to_expiry_years, RISK_FREE_RATE

router = APIRouter()


@router.get("/atm")
def get_atm_info(conn=Depends(get_db)):
    """
    Detect the current ATM strike and nearest expiry.
    Uses the latest FUTIDX close price as the underlying.
    """
    try:
        with conn.cursor() as cur:
            # Get latest futures close as underlying price
            cur.execute("""
                SELECT i.symbol, o.close, i.expiry
                FROM ohlcv_5min o
                JOIN instruments i ON i.id = o.instrument_id
                WHERE i.instrument_type = 'FUTIDX'
                ORDER BY o.timestamp DESC
                LIMIT 1
            """)
            fut_row = cur.fetchone()
            if not fut_row:
                return Response(status="error", message="No futures data found")

            underlying = float(fut_row[1])
            # Round to nearest 50 (Nifty strike step)
            atm_strike = round(underlying / 50) * 50

            # Get nearest expiry with option data
            cur.execute("""
                SELECT DISTINCT i.expiry
                FROM instruments i
                JOIN ohlcv_5min o ON o.instrument_id = i.id
                WHERE i.instrument_type = 'OPTIDX'
                  AND i.expiry >= CURRENT_DATE
                ORDER BY i.expiry
                LIMIT 5
            """)
            expiries = [r[0].isoformat() for r in cur.fetchall()]

            # Get all available strikes for nearest expiry
            if expiries:
                cur.execute("""
                    SELECT DISTINCT i.strike
                    FROM instruments i
                    JOIN ohlcv_5min o ON o.instrument_id = i.id
                    WHERE i.instrument_type = 'OPTIDX'
                      AND i.expiry = %s
                    ORDER BY i.strike
                """, (expiries[0],))
                strikes = [float(r[0]) for r in cur.fetchall()]
            else:
                strikes = []

        return Response(
            status="success",
            data={
                "underlying": underlying,
                "futures_symbol": fut_row[0],
                "atm_strike": atm_strike,
                "expiries": expiries,
                "strikes": strikes,
            }
        )
    except Exception as e:
        return Response(status="error", message=str(e))


@router.get("/chain")
def get_iv_chain(
    expiry: str = Query(..., description="Expiry date YYYY-MM-DD"),
    conn=Depends(get_db),
):
    """
    Compute IV for all strikes at a given expiry.
    Returns the IV smile data for charting.
    """
    try:
        expiry_date = date.fromisoformat(expiry)
        T = time_to_expiry_years(expiry_date)

        with conn.cursor() as cur:
            # Get underlying price from latest futures close
            cur.execute("""
                SELECT o.close FROM ohlcv_5min o
                JOIN instruments i ON i.id = o.instrument_id
                WHERE i.instrument_type = 'FUTIDX'
                ORDER BY o.timestamp DESC LIMIT 1
            """)
            fut_row = cur.fetchone()
            if not fut_row:
                return Response(status="error", message="No futures data")
            S = float(fut_row[0])

            # Get all OPTIDX instruments at this expiry with their latest close
            cur.execute("""
                SELECT DISTINCT ON (i.id)
                    i.id, i.symbol, i.strike, i.expiry,
                    o.close as option_close, o.volume, o.timestamp
                FROM instruments i
                JOIN ohlcv_5min o ON o.instrument_id = i.id
                WHERE i.instrument_type = 'OPTIDX'
                  AND i.expiry = %s
                ORDER BY i.id, o.timestamp DESC
            """, (expiry_date,))
            rows = cur.fetchall()

        # Compute IV for each option
        chain = {}
        for row in rows:
            inst_id, symbol, strike, exp, option_close, volume, ts = row
            strike = float(strike)
            option_close = float(option_close)

            # Detect CE/PE from symbol
            opt_type = "CE" if "CE" in symbol else "PE"

            iv = implied_volatility(
                option_price=option_close,
                S=S, K=strike, T=T,
                r=RISK_FREE_RATE,
                option_type=opt_type,
            )

            if strike not in chain:
                chain[strike] = {
                    "strike": strike,
                    "ce_iv": None, "pe_iv": None,
                    "ce_close": None, "pe_close": None,
                    "ce_volume": None, "pe_volume": None,
                    "ce_symbol": None, "pe_symbol": None,
                }

            key_prefix = "ce" if opt_type == "CE" else "pe"
            chain[strike][f"{key_prefix}_iv"] = round(
                iv * 100, 2) if iv else None
            chain[strike][f"{key_prefix}_close"] = option_close
            chain[strike][f"{key_prefix}_volume"] = int(volume)
            chain[strike][f"{key_prefix}_symbol"] = symbol

        # Sort by strike
        result = sorted(chain.values(), key=lambda x: x["strike"])

        return Response(
            status="success",
            data={
                "underlying": S,
                "expiry": expiry,
                "time_to_expiry_years": round(T, 6),
                "chain": result,
            }
        )
    except Exception as e:
        return Response(status="error", message=str(e))


@router.get("/strikes")
def get_strikes_with_data(
    expiry: str = Query(..., description="Expiry date YYYY-MM-DD"),
    conn=Depends(get_db),
):
    """Get all strikes that have OHLCV data for a given expiry."""
    try:
        expiry_date = date.fromisoformat(expiry)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT i.strike
                FROM instruments i
                JOIN ohlcv_5min o ON o.instrument_id = i.id
                WHERE i.instrument_type = 'OPTIDX'
                  AND i.expiry = %s
                ORDER BY i.strike
            """, (expiry_date,))
            strikes = [float(r[0]) for r in cur.fetchall()]

        return Response(status="success", data={"expiry": expiry, "strikes": strikes})
    except Exception as e:
        return Response(status="error", message=str(e))


@router.get("/iv-history")
def get_iv_history(
    symbol: str = Query(...,
                        description="Option symbol e.g. NIFTY10MAR2624800CE"),
    conn=Depends(get_db),
):
    """
    Get historical IV time series for a specific option.
    Computes IV from each 5-min candle close price.
    """
    try:
        with conn.cursor() as cur:
            # Get option instrument details
            cur.execute("""
                SELECT id, strike, expiry FROM instruments
                WHERE symbol = %s AND instrument_type = 'OPTIDX'
            """, (symbol.upper(),))
            inst = cur.fetchone()
            if not inst:
                return Response(status="error", message=f"Option '{symbol}' not found")

            inst_id, strike, expiry = inst
            strike = float(strike)
            opt_type = "CE" if "CE" in symbol.upper() else "PE"

            # Get option OHLCV
            cur.execute("""
                SELECT timestamp, close FROM ohlcv_5min
                WHERE instrument_id = %s ORDER BY timestamp
            """, (inst_id,))
            option_candles = cur.fetchall()

            # Get futures OHLCV (underlying) — align timestamps
            cur.execute("""
                SELECT o.timestamp, o.close FROM ohlcv_5min o
                JOIN instruments i ON i.id = o.instrument_id
                WHERE i.instrument_type = 'FUTIDX'
                ORDER BY o.timestamp
            """)
            fut_candles = {r[0]: float(r[1]) for r in cur.fetchall()}

        # Compute IV at each timestamp
        iv_series = []
        for ts, opt_close in option_candles:
            opt_close = float(opt_close)
            # Find corresponding futures close
            S = fut_candles.get(ts)
            if not S or opt_close <= 0:
                continue

            T = time_to_expiry_years(expiry, ts)
            if T <= 0:
                continue

            iv = implied_volatility(
                option_price=opt_close, S=S, K=strike, T=T,
                r=RISK_FREE_RATE, option_type=opt_type,
            )
            if iv is not None:
                iv_series.append({
                    "timestamp": ts.isoformat(),
                    "iv_pct": round(iv * 100, 2),
                    "option_close": opt_close,
                    "underlying": S,
                })

        return Response(
            status="success",
            data={
                "symbol": symbol,
                "strike": strike,
                "expiry": expiry.isoformat(),
                "option_type": opt_type,
                "series": iv_series,
            }
        )
    except Exception as e:
        return Response(status="error", message=str(e))
