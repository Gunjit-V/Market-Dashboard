from datetime import datetime, time as clock_time

from fastapi import APIRouter, Depends

from api.dependencies import get_db
from api.models.schemas import Response
from scheduler.nse_calendar import IST, is_nse_trading_day

router = APIRouter()

MARKET_OPEN = clock_time(9, 15)
MARKET_CLOSE = clock_time(15, 30)

# How stale a service's last-seen timestamp can be, during market hours,
# before it's flagged red. Generous relative to each service's own cadence
# so a normal scheduling jitter doesn't false-positive.
STALE_THRESHOLD_MINUTES = {
    "tick_downloader": 10,
    "ohlcv_scheduler": 15,
    "instrument_sync_scheduler": 24 * 60,  # runs once/day before market open
    "paper_trading_scheduler": 15,
}

SERVICE_QUERIES = {
    "tick_downloader": "SELECT MAX(timestamp) FROM tick_data",
    "ohlcv_scheduler": "SELECT MAX(last_run_at) FROM download_log",
    "instrument_sync_scheduler": "SELECT MAX(updated_at) FROM instruments",
    "paper_trading_scheduler": (
        "SELECT MAX(timestamp) FROM equity_curve WHERE is_paper = TRUE"
    ),
}


@router.get("", response_model=Response)
def health_check(conn=Depends(get_db)):
    """Check API and database connectivity."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            version = cur.fetchone()[0]

        return Response(
            status="success",
            message="API is healthy",
            data={
                "api": "running",
                "database": "connected",
                "postgres_version": version,
            }
        )

    except Exception as e:
        return Response(
            status="error",
            message=f"Database connection failed: {str(e)}",
            data={
                "api": "running",
                "database": "disconnected",
            }
        )


@router.get("/schedulers", response_model=Response)
def scheduler_health(conn=Depends(get_db)):
    """Report last-seen timestamps for each background scheduler/downloader,
    so a silent death (e.g. a container exiting cleanly and never restarting)
    is visible instead of discovered by accident."""
    now = datetime.now(IST)
    trading_day = is_nse_trading_day(now)
    market_open = trading_day and MARKET_OPEN <= now.time() <= MARKET_CLOSE

    services = {}
    with conn.cursor() as cur:
        for name, query in SERVICE_QUERIES.items():
            try:
                cur.execute(query)
                last_seen = cur.fetchone()[0]
            except Exception:
                last_seen = None

            if last_seen is None:
                status = "unknown"
                minutes_ago = None
            else:
                last_seen_ist = last_seen.replace(tzinfo=IST) if last_seen.tzinfo is None else last_seen.astimezone(IST)
                minutes_ago = (now - last_seen_ist).total_seconds() / 60
                threshold = STALE_THRESHOLD_MINUTES[name]
                if market_open and minutes_ago > threshold:
                    status = "stale"
                else:
                    status = "ok"

            services[name] = {
                "last_seen": last_seen.isoformat() if last_seen else None,
                "minutes_ago": round(minutes_ago, 1) if minutes_ago is not None else None,
                "status": status,
            }

    overall = "error" if any(s["status"] == "stale" for s in services.values()) else "success"
    return Response(
        status=overall,
        message="Scheduler heartbeat",
        data={
            "market_open": market_open,
            "checked_at": now.isoformat(),
            "services": services,
        },
    )
