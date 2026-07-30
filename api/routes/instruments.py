from fastapi import APIRouter, Depends, Query
from typing import Optional
from api.dependencies import get_db
from api.models.schemas import Response, PaginatedResponse, Instrument

router = APIRouter()


@router.get("", response_model=PaginatedResponse)
def list_instruments(
    instrument_type: Optional[str] = Query(
        None, description="Filter by instrument type e.g. EQ, FUTIDX, OPTIDX"),
    exchange: Optional[str] = Query(
        None, description="Filter by exchange e.g. NSE, NFO"),
    search: Optional[str] = Query(
        None, description="Search by symbol or name"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    conn=Depends(get_db),
):
    """List all instruments with optional filters and pagination."""
    try:
        filters = []
        params = []

        if instrument_type:
            filters.append("instrument_type = %s")
            params.append(instrument_type)

        if exchange:
            filters.append("exchange = %s")
            params.append(exchange)

        if search:
            filters.append("(symbol ILIKE %s OR name ILIKE %s)")
            params.append(f"%{search}%")
            params.append(f"%{search}%")

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        with conn.cursor() as cur:
            # Get total count
            cur.execute(
                f"SELECT COUNT(*) FROM instruments {where_clause}",
                params
            )
            total = cur.fetchone()[0]

            # Get paginated results
            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT id, symbol, token, name, exchange, instrument_type,
                       expiry, strike, lot_size, created_at
                FROM instruments
                {where_clause}
                ORDER BY instrument_type, symbol
                LIMIT %s OFFSET %s
                """,
                params + [page_size, offset]
            )
            rows = cur.fetchall()

        instruments = [
            Instrument(
                id=row[0],
                symbol=row[1],
                token=row[2],
                name=row[3],
                exchange=row[4],
                instrument_type=row[5],
                expiry=row[6],
                strike=row[7],
                lot_size=row[8],
                created_at=row[9],
            )
            for row in rows
        ]

        return PaginatedResponse(
            status="success",
            data=instruments,
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


@router.get("/{symbol}", response_model=Response)
def get_instrument(symbol: str, conn=Depends(get_db)):
    """Get a single instrument by symbol."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, token, name, exchange, instrument_type,
                       expiry, strike, lot_size, created_at
                FROM instruments
                WHERE UPPER(symbol) = UPPER(%s)
                """,
                (symbol,)
            )
            row = cur.fetchone()

        if not row:
            return Response(
                status="error",
                message=f"Instrument '{symbol}' not found",
            )

        return Response(
            status="success",
            data=Instrument(
                id=row[0],
                symbol=row[1],
                token=row[2],
                name=row[3],
                exchange=row[4],
                instrument_type=row[5],
                expiry=row[6],
                strike=row[7],
                lot_size=row[8],
                created_at=row[9],
            )
        )

    except Exception as e:
        return Response(
            status="error",
            message=str(e),
        )
