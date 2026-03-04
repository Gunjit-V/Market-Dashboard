from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime, date


# ─── Base Response Envelope ───────────────────────────────────────────────────

class Meta(BaseModel):
    total: int
    page: int
    page_size: int


class Response(BaseModel):
    status: str
    message: Optional[str] = None
    data: Optional[Any] = None


class PaginatedResponse(BaseModel):
    status: str
    message: Optional[str] = None
    data: Optional[Any] = None
    meta: Optional[Meta] = None


# ─── Instruments ──────────────────────────────────────────────────────────────

class Instrument(BaseModel):
    id: int
    symbol: str
    token: str
    name: Optional[str] = None
    exchange: str
    instrument_type: Optional[str] = None
    expiry: Optional[date] = None
    strike: Optional[float] = None
    lot_size: Optional[int] = None
    created_at: Optional[datetime] = None


# ─── OHLCV ────────────────────────────────────────────────────────────────────

class OHLCVCandle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


# ─── Download Log ─────────────────────────────────────────────────────────────

class DownloadLog(BaseModel):
    id: int
    instrument_id: int
    symbol: Optional[str] = None
    last_downloaded_at: Optional[datetime] = None
    last_run_at: datetime
    status: str
    candles_inserted: Optional[int] = None
    candles_skipped: Optional[int] = None
    error_message: Optional[str] = None


class DownloadTriggerRequest(BaseModel):
    instrument_types: List[str]
    days: int = 1
