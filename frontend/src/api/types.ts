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
  latest_run_at?: string
  latest_run_status?: string
  download_counts_by_status?: Record<string, number>
}

// ── Volatility types — add these to your existing types.ts ──────────────────

export type RVMethod = 'close_close' | 'parkinson' | 'garman_klass' | 'rogers_satchell'

export type RVWindow = 5 | 10 | 20

export interface RVPoint {
  timestamp: string   // ISO string, end of rolling window
  rv: number          // annualised RV as a decimal (e.g. 0.142 = 14.2%)
  method: RVMethod
  window_days: number
  candle_count: number
}

export interface IVPoint {
  timestamp: string
  strike: number
  expiry: string
  option_type: 'CE' | 'PE'
  iv: number           // annualised IV as decimal
  delta: number
  underlying_price: number
}

export interface IVSurface {
  timestamp: string
  atm_iv: number
  skew: number         // OTM put IV − ATM IV
  term_structure: {
    expiry: string
    dte: number
    atm_iv: number
  }[]
}

// What the /volatility/rv endpoint should return
export interface RVResponse {
  symbol: string
  method: RVMethod
  window_days: number
  current_rv: number
  history: RVPoint[]
  candles_used: number
  from_date: string
  to_date: string
}

// What the /volatility/iv endpoint should return (once options data is live)
export interface IVResponse {
  symbol: string           // underlying, e.g. "NIFTY"
  timestamp: string
  atm_iv: number
  iv_rank: number | null   // 0–100, null if < 52 weeks of history
  iv_percentile: number | null
  pcr: number | null
  max_pain: number | null
  history: IVPoint[]
}

export interface VolatilityComparison {
  timestamp: string
  rv: number
  iv: number | null
  vrp: number | null       // iv − rv  (Volatility Risk Premium)
}
