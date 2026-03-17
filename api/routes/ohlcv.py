from fastapi import APIRouter, Depends, Query
from typing import Optional
from datetime import datetime, timedelta
from api.dependencies import get_db
from api.models.schemas import Response, PaginatedResponse, OHLCVCandle

router = APIRouter()


@router.get("/{symbol}", response_model=PaginatedResponse)
def get_ohlcv(
    symbol: str,
    from_date: Optional[datetime] = Query(
        None, description="Start datetime e.g. 2026-01-01T09:15:00"),
    to_date: Optional[datetime] = Query(
        None, description="End datetime e.g. 2026-03-05T15:30:00"),
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=5000),
    conn=Depends(get_db),
):
    """Fetch 5-minute OHLCV data for a given symbol."""
    try:
        # Default to last 7 days if no date range provided
        if not to_date:
            to_date = datetime.now()
        if not from_date:
            from_date = to_date - timedelta(days=7)

        with conn.cursor() as cur:
            # Resolve symbol to instrument_id
            cur.execute(
                "SELECT id FROM instruments WHERE symbol = %s",
                (symbol.upper(),)
            )
            row = cur.fetchone()

            if not row:
                return PaginatedResponse(
                    status="error",
                    message=f"Instrument '{symbol}' not found",
                    data=[],
                )

            instrument_id = row[0]

            # Get total count
            cur.execute(
                """
                SELECT COUNT(*)
                FROM ohlcv_5min
                WHERE instrument_id = %s
                  AND timestamp >= %s
                  AND timestamp <= %s
                """,
                (instrument_id, from_date, to_date)
            )
            total = cur.fetchone()[0]

            # Get paginated OHLCV data
            offset = (page - 1) * page_size
            cur.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM ohlcv_5min
                WHERE instrument_id = %s
                  AND timestamp >= %s
                  AND timestamp <= %s
                ORDER BY timestamp ASC
                LIMIT %s OFFSET %s
                """,
                (instrument_id, from_date, to_date, page_size, offset)
            )
            rows = cur.fetchall()

        candles = [
            OHLCVCandle(
                timestamp=row[0],
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=int(row[5]),
            )
            for row in rows
        ]

        return PaginatedResponse(
            status="success",
            data=candles,
            meta={
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    except Exception as e:
        return PaginatedResponse(
            status="error",
            message=str(e),
            data=[],
        )


@router.get("/{symbol}/latest", response_model=Response)
def get_latest_candle(symbol: str, conn=Depends(get_db)):
    """Fetch the most recent 5-minute candle for a given symbol."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM instruments WHERE symbol = %s",
                (symbol.upper(),)
            )
            row = cur.fetchone()

            if not row:
                return Response(
                    status="error",
                    message=f"Instrument '{symbol}' not found",
                )

            instrument_id = row[0]

            cur.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM ohlcv_5min
                WHERE instrument_id = %s
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (instrument_id,)
            )
            row = cur.fetchone()

        if not row:
            return Response(
                status="error",
                message=f"No OHLCV data found for '{symbol}'",
            )

        return Response(
            status="success",
            data=OHLCVCandle(
                timestamp=row[0],
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=int(row[5]),
            )
        )

    except Exception as e:
        return Response(
            status="error",
            message=str(e),
        )
