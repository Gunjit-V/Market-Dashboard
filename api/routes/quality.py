"""Read-only data-quality endpoints.

Exposes the Phase 1 validation layer (``marketdata``) over HTTP so the same
numbers the CLI prints are available to the dashboard. These endpoints only
read: nothing here writes, repairs or deletes market data.
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_db
from api.models.schemas import Response
from marketdata.access import InstrumentNotFound
from marketdata.report import DEFAULT_MAX_TICKS, assess_instrument

router = APIRouter()

# Keep the default window small: the report reads every row in range.
DEFAULT_LOOKBACK_DAYS = 7


@router.get("/{symbol}", response_model=Response)
def get_data_quality(
    symbol: str,
    from_date: Optional[datetime] = Query(
        None, description="Start datetime, inclusive, IST e.g. 2026-01-01T09:15:00"),
    to_date: Optional[datetime] = Query(
        None, description="End datetime, exclusive, IST e.g. 2026-03-05T15:30:00"),
    timeframes: str = Query(
        "1m,5m", description="Comma-separated subset of tick,1m,5m"),
    max_ticks: int = Query(
        DEFAULT_MAX_TICKS, ge=1, le=DEFAULT_MAX_TICKS,
        description="Cap on ticks read when 'tick' is requested"),
    conn=Depends(get_db),
):
    """Validate stored data for *symbol* and return a data-quality report.

    Overall status is ``PASS``, ``WARNING`` or ``FAIL``; see
    ``docs/validation.md`` for what each check means.
    """
    try:
        if not to_date:
            to_date = datetime.now()
        if not from_date:
            from_date = to_date - timedelta(days=DEFAULT_LOOKBACK_DAYS)

        selected = [part.strip() for part in timeframes.split(",") if part.strip()]
        if not selected:
            return Response(status="error", message="No timeframes requested")

        report = assess_instrument(
            conn, symbol, from_date, to_date, selected, max_ticks=max_ticks
        )

        return Response(
            status="success",
            message=f"Data quality: {report.status}",
            data=report.as_dict(),
        )

    except InstrumentNotFound:
        return Response(status="error", message=f"Instrument '{symbol}' not found")
    except ValueError as exc:
        return Response(status="error", message=str(exc))
    except Exception as exc:
        return Response(status="error", message=str(exc))
