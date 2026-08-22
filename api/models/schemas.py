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


# ─── Strategies / Backtesting / Paper Trading ─────────────────────────────────

class Strategy(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    params: dict
    is_active: bool
    created_at: Optional[datetime] = None


class BacktestRun(BaseModel):
    id: int
    strategy_id: int
    strategy_name: Optional[str] = None
    params: dict
    from_date: datetime
    to_date: datetime
    starting_capital: float
    ending_capital: Optional[float] = None
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    win_rate_pct: Optional[float] = None
    status: str
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class BacktestRequest(BaseModel):
    strategy: str  # 'rv_breakout' | 'vrp_reversion'
    symbol: Optional[str] = None            # for rv_breakout
    option_symbol: Optional[str] = None     # for vrp_reversion
    underlying_symbol: Optional[str] = None  # for vrp_reversion
    from_date: Optional[datetime] = None
    to_date: Optional[datetime] = None
    starting_capital: float = 500_000.0
    capital_per_trade: Optional[float] = None
    params: Optional[dict] = None  # strategy-specific overrides (windows, thresholds)


class Trade(BaseModel):
    id: int
    backtest_run_id: Optional[int] = None
    strategy_id: int
    strategy_name: Optional[str] = None
    is_paper: bool
    instrument_id: int
    symbol: Optional[str] = None
    side: str
    signal_reason: Optional[str] = None
    entry_time: datetime
    entry_price: float
    quantity: int
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    status: str
    metadata: Optional[dict] = None


class EquityPoint(BaseModel):
    timestamp: datetime
    equity: float
    cash: float
    open_positions_value: float
    drawdown_pct: Optional[float] = None


class SignalRow(BaseModel):
    id: int
    strategy_id: int
    strategy_name: Optional[str] = None
    instrument_id: Optional[int] = None
    symbol: Optional[str] = None
    timestamp: datetime
    signal_type: str
    reason: Optional[str] = None
    metrics: Optional[dict] = None
    acted_on: bool


class PaperTradingSummary(BaseModel):
    strategy_id: int
    strategy_name: str
    starting_capital: float
    current_equity: float
    total_pnl: float
    total_pnl_pct: float
    open_trades: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    max_drawdown_pct: float
