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
