// Mirrors FastAPI schemas

export interface Meta {
  total: number
  page: number
  page_size: number
}

export interface ApiResponse<T = unknown> {
  status: string
  message?: string
  data?: T
}

export interface PaginatedResponse<T = unknown> {
  status: string
  message?: string
  data?: T[]
  meta?: Meta
}

export interface Instrument {
  id: number
  symbol: string
  token: string
  name?: string
  exchange: string
  instrument_type?: string
  expiry?: string
  strike?: number
  lot_size?: number
  created_at?: string
}

export interface OHLCVCandle {
  timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface DownloadLog {
  id: number
  instrument_id: number
  symbol?: string
  last_downloaded_at?: string
  last_run_at: string
  status: string
  candles_inserted?: number
  candles_skipped?: number
  error_message?: string
}

export interface DownloadTriggerRequest {
  instrument_types: string[]
  days?: number
}

export interface DownloadStatusData {
  total_instruments: number
  total_candles: number
  total_candles_1min?: number
  total_candles_5min?: number
  latest_5min_candle_at?: string
  latest_run_at?: string
  latest_run_status?: string
  download_counts_by_status?: Record<string, number>
  instruments_by_type?: Record<string, number>
  candles_by_type?: Record<string, number>
  instruments_with_no_data?: number
}

// ─── Volatility API types ────────────────────────────────────────────────────

export interface ATMInfo {
  underlying: number
  futures_symbol: string
  atm_strike: number
  expiries: string[]
  strikes: number[]
}

export interface IVChainRow {
  strike: number
  ce_iv: number | null
  pe_iv: number | null
  ce_close: number | null
  pe_close: number | null
  ce_volume: number | null
  pe_volume: number | null
  ce_symbol: string | null
  pe_symbol: string | null
}

export interface IVChainData {
  underlying: number
  expiry: string
  time_to_expiry_years: number
  chain: IVChainRow[]
}

export interface IVHistoryPoint {
  timestamp: string
  iv_pct: number
  option_close: number
  underlying: number
}

export interface IVHistoryData {
  symbol: string
  strike: number
  expiry: string
  option_type: string
  series: IVHistoryPoint[]
}

// ─── Strategies / Backtesting / Paper Trading ─────────────────────────────────

export interface Strategy {
  id: number
  name: string
  description?: string
  params: Record<string, unknown>
  is_active: boolean
  created_at?: string
}

export interface BacktestRun {
  id: number
  strategy_id: number
  params: Record<string, unknown>
  from_date: string
  to_date: string
  starting_capital: number
  ending_capital: number | null
  total_trades: number
  winning_trades: number
  losing_trades: number
  total_pnl: number | null
  max_drawdown_pct: number | null
  sharpe_ratio: number | null
  win_rate_pct: number | null
  status: 'running' | 'completed' | 'failed'
  error_message?: string | null
  started_at: string
  completed_at?: string | null
}

export interface BacktestRequest {
  strategy: 'rv_breakout' | 'vrp_reversion'
  symbol?: string
  option_symbol?: string
  underlying_symbol?: string
  from_date?: string
  to_date?: string
  starting_capital?: number
  capital_per_trade?: number
  params?: Record<string, unknown>
}

export interface Trade {
  id: number
  backtest_run_id: number | null
  strategy_id: number
  is_paper: boolean
  instrument_id: number
  symbol: string
  side: 'LONG' | 'SHORT'
  signal_reason?: string
  entry_time: string
  entry_price: number
  quantity: number
  exit_time: string | null
  exit_price: number | null
  exit_reason: string | null
  pnl: number | null
  pnl_pct: number | null
  status: 'open' | 'closed'
  metadata?: Record<string, unknown>
}

export interface EquityPoint {
  timestamp: string
  equity: number
  cash: number
  open_positions_value: number
  drawdown_pct?: number | null
}

export interface SignalRow {
  id: number
  strategy_id: number
  strategy_name: string
  instrument_id: number | null
  symbol: string | null
  timestamp: string
  signal_type: 'entry_long' | 'entry_short' | 'exit' | 'hold'
  reason?: string
  metrics?: Record<string, number>
  acted_on: boolean
}

export interface PaperTradingSummary {
  strategy_id: number
  strategy_name: string
  starting_capital: number
  current_equity: number
  total_pnl: number
  total_pnl_pct: number
  open_trades: number
  closed_trades: number
  winning_trades: number
  losing_trades: number
  win_rate_pct: number
  max_drawdown_pct: number
}

