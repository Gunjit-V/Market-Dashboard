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

