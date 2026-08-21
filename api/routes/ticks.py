from fastapi import APIRouter, Depends, Query
from typing import Optional
from datetime import datetime, timedelta
from api.dependencies import get_db
from api.models.schemas import Response, PaginatedResponse

router = APIRouter()


@router.get("/{symbol}/latest", response_model=Response)
def get_latest_tick(symbol: str, conn=Depends(get_db)):
    """Get the most recent tick for a given symbol."""
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
                SELECT timestamp, ltp, ltq, volume, best_5_buy, best_5_sell
                FROM tick_data
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
                message=f"No tick data found for '{symbol}'",
            )

        return Response(
            status="success",
            data={
                "timestamp": row[0],
                "ltp": float(row[1]),
                "ltq": row[2],
                "volume": row[3],
                "best_5_buy": row[4],
                "best_5_sell": row[5],
            }
        )

    except Exception as e:
        return Response(
            status="error",
            message=str(e),
        )


@router.get("/{symbol}", response_model=PaginatedResponse)
def get_tick_data(
    symbol: str,
    from_date: Optional[datetime] = Query(
        None, description="Start datetime e.g. 2026-01-01T09:15:00"),
    to_date: Optional[datetime] = Query(
        None, description="End datetime e.g. 2026-03-05T15:30:00"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=5000),
    conn=Depends(get_db),
):
    """
    Fetch tick-level data for a given symbol.

    Tick data represents individual price movements and volumes for an instrument.
    Returns paginated results ordered by timestamp ascending.
    """
    try:
        # Default to last 1 day if no date range provided
        if not to_date:
            to_date = datetime.now()
        if not from_date:
            from_date = to_date - timedelta(days=1)

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
                FROM tick_data
                WHERE instrument_id = %s
                  AND timestamp >= %s
                  AND timestamp <= %s
                """,
                (instrument_id, from_date, to_date)
            )
            total = cur.fetchone()[0]

            # Get paginated tick data
            offset = (page - 1) * page_size
            cur.execute(
                """
                SELECT timestamp, ltp, ltq, volume, best_5_buy, best_5_sell
                FROM tick_data
                WHERE instrument_id = %s
                  AND timestamp >= %s
                  AND timestamp <= %s
                ORDER BY timestamp ASC
                LIMIT %s OFFSET %s
                """,
                (instrument_id, from_date, to_date, page_size, offset)
            )
            rows = cur.fetchall()

        ticks = [
            {
                "timestamp": row[0],
                "ltp": float(row[1]),
                "ltq": row[2],
                "volume": row[3],
                "best_5_buy": row[4],
                "best_5_sell": row[5],
            }
            for row in rows
        ]

        return PaginatedResponse(
            status="success",
            data=ticks,
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


@router.get("/{symbol}/summary", response_model=Response)
def get_tick_summary(
    symbol: str,
    from_date: Optional[datetime] = Query(
        None, description="Start datetime e.g. 2026-01-01T09:15:00"),
    to_date: Optional[datetime] = Query(
        None, description="End datetime e.g. 2026-03-05T15:30:00"),
    conn=Depends(get_db),
):
    """
    Get summary statistics for tick data within a date range.

    Returns: min/max/avg price, total volume, tick count, etc.
    """
    try:
        # Default to last 1 day if no date range provided
        if not to_date:
            to_date = datetime.now()
        if not from_date:
            from_date = to_date - timedelta(days=1)

        with conn.cursor() as cur:
            # Resolve symbol to instrument_id
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

            # Get summary statistics
            cur.execute(
                """
                SELECT 
                    COUNT(*) as tick_count,
                    MIN(ltp) as min_price,
                    MAX(ltp) as max_price,
                    AVG(ltp) as avg_price,
                    SUM(volume) as total_volume,
                    MIN(timestamp) as earliest_tick,
                    MAX(timestamp) as latest_tick
                FROM tick_data
                WHERE instrument_id = %s
                  AND timestamp >= %s
                  AND timestamp <= %s
                """,
                (instrument_id, from_date, to_date)
            )
            row = cur.fetchone()

        if not row or row[0] == 0:
            return Response(
                status="error",
                message=f"No tick data found for '{symbol}' in the specified range",
            )

        return Response(
            status="success",
            data={
                "symbol": symbol,
                "tick_count": row[0],
                "min_price": float(row[1]),
                "max_price": float(row[2]),
                "avg_price": float(row[3]) if row[3] else None,
                "total_volume": row[4],
                "earliest_tick": row[5],
                "latest_tick": row[6],
                "from_date": from_date,
                "to_date": to_date,
            }
        )

    except Exception as e:
        return Response(
            status="error",
            message=str(e),
        )
