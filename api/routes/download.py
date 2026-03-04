from fastapi import APIRouter, Depends, Query, BackgroundTasks
from typing import Optional
from api.dependencies import get_db
from api.models.schemas import Response, PaginatedResponse, DownloadLog, DownloadTriggerRequest

router = APIRouter()


@router.get("/status", response_model=Response)
def get_download_status(conn=Depends(get_db)):
    """Get overall download pipeline status."""
    try:
        with conn.cursor() as cur:
            # Total instruments
            cur.execute("SELECT COUNT(*) FROM instruments")
            total_instruments = cur.fetchone()[0]

            # Total candles
            cur.execute("SELECT COUNT(*) FROM ohlcv_1min")
            total_candles = cur.fetchone()[0]

            # Latest download run
            cur.execute(
                """
                SELECT last_run_at, status
                FROM download_log
                ORDER BY last_run_at DESC
                LIMIT 1
                """
            )
            latest_run = cur.fetchone()

            # Count by status
            cur.execute(
                """
                SELECT status, COUNT(*)
                FROM download_log
                GROUP BY status
                """
            )
            status_counts = {row[0]: row[1] for row in cur.fetchall()}

        return Response(
            status="success",
            data={
                "total_instruments": total_instruments,
                "total_candles": total_candles,
                "latest_run_at": latest_run[0] if latest_run else None,
                "latest_run_status": latest_run[1] if latest_run else None,
                "download_counts_by_status": status_counts,
            }
        )

    except Exception as e:
        return Response(
            status="error",
            message=str(e),
        )


@router.get("/status/{symbol}", response_model=Response)
def get_symbol_download_status(symbol: str, conn=Depends(get_db)):
    """Get the latest download log entry for a specific symbol."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT dl.id, dl.instrument_id, i.symbol,
                       dl.last_downloaded_at, dl.last_run_at,
                       dl.status, dl.candles_inserted,
                       dl.candles_skipped, dl.error_message
                FROM download_log dl
                JOIN instruments i ON i.id = dl.instrument_id
                WHERE i.symbol = %s
                ORDER BY dl.last_run_at DESC
                LIMIT 1
                """,
                (symbol.upper(),)
            )
            row = cur.fetchone()

        if not row:
            return Response(
                status="error",
                message=f"No download logs found for '{symbol}'",
            )

        return Response(
            status="success",
            data=DownloadLog(
                id=row[0],
                instrument_id=row[1],
                symbol=row[2],
                last_downloaded_at=row[3],
                last_run_at=row[4],
                status=row[5],
                candles_inserted=row[6],
                candles_skipped=row[7],
                error_message=row[8],
            )
        )

    except Exception as e:
        return Response(
            status="error",
            message=str(e),
        )


@router.get("/logs", response_model=PaginatedResponse)
def get_download_logs(
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    status: Optional[str] = Query(
        None, description="Filter by status: success, failed, no_data"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    conn=Depends(get_db),
):
    """Get paginated download logs with optional filters."""
    try:
        filters = []
        params = []

        if symbol:
            filters.append("i.symbol = %s")
            params.append(symbol.upper())

        if status:
            filters.append("dl.status = %s")
            params.append(status)

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        with conn.cursor() as cur:
            # Get total count
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM download_log dl
                JOIN instruments i ON i.id = dl.instrument_id
                {where_clause}
                """,
                params
            )
            total = cur.fetchone()[0]

            # Get paginated logs
            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT dl.id, dl.instrument_id, i.symbol,
                       dl.last_downloaded_at, dl.last_run_at,
                       dl.status, dl.candles_inserted,
                       dl.candles_skipped, dl.error_message
                FROM download_log dl
                JOIN instruments i ON i.id = dl.instrument_id
                {where_clause}
                ORDER BY dl.last_run_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [page_size, offset]
            )
            rows = cur.fetchall()

        logs = [
            DownloadLog(
                id=row[0],
                instrument_id=row[1],
                symbol=row[2],
                last_downloaded_at=row[3],
                last_run_at=row[4],
                status=row[5],
                candles_inserted=row[6],
                candles_skipped=row[7],
                error_message=row[8],
            )
            for row in rows
        ]

        return PaginatedResponse(
            status="success",
            data=logs,
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


def run_download(instrument_types: list, days: int):
    """Background task to trigger the downloader."""
    import sys
    import os
    sys.path.append(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    from downloader import download_historical_data
    download_historical_data(instrument_types=instrument_types, days=days)


@router.post("/trigger", response_model=Response)
def trigger_download(
    request: DownloadTriggerRequest,
    background_tasks: BackgroundTasks,
):
    """Manually trigger a download run in the background."""
    try:
        background_tasks.add_task(
            run_download,
            instrument_types=request.instrument_types,
            days=request.days,
        )

        return Response(
            status="success",
            message=f"Download triggered for {request.instrument_types} — last {request.days} day(s)",
            data={
                "instrument_types": request.instrument_types,
                "days": request.days,
            }
        )

    except Exception as e:
        return Response(
            status="error",
            message=str(e),
        )
