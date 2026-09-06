import type {
  ApiResponse,
  PaginatedResponse,
  Instrument,
  OHLCVCandle,
  DownloadLog,
  DownloadTriggerRequest,
  DownloadStatusData,
  SchedulerHealthData,
  ATMInfo,
  IVChainData,
  IVHistoryData,
  Strategy,
  BacktestRun,
  BacktestRequest,
  Trade,
  EquityPoint,
} from './types'

const API_BASE = import.meta.env.VITE_API_URL ?? '/api'
// Only needed if the API sets API_KEY server-side; unset by default for
// local dev, where the backend's require_api_key dependency is a no-op.
const API_KEY = import.meta.env.VITE_API_KEY as string | undefined

async function request<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
      ...options?.headers,
    },
  })
  const text = await res.text()
  if (!text.trim()) {
    throw new Error(
      res.ok
        ? 'Empty response from server'
        : `Backend unreachable (${res.status}). Is the API running on port 8000?`
    )
  }
  let json: unknown
  try {
    json = JSON.parse(text)
  } catch {
    throw new Error(
      res.ok
        ? 'Invalid JSON from server'
        : `Backend returned non-JSON (${res.status}). Check the API is running.`
    )
  }
  if (!res.ok) {
    const msg = typeof (json as { message?: string }).message === 'string'
      ? (json as { message: string }).message
      : res.statusText
    throw new Error(msg)
  }
  // FastAPI routes in this app catch their own exceptions and return
  // {status: "error", message: "..."} with HTTP 200, so a successful
  // fetch does not imply a successful response — check the envelope too.
  const envelope = json as { status?: string; message?: string }
  if (envelope.status === 'error') {
    throw new Error(envelope.message || 'Request failed')
  }
  return json as T
}

export const api = {
  health: () =>
    request<ApiResponse<{ api: string; database: string; postgres_version?: string }>>('/health'),

  schedulerHealth: () =>
    request<ApiResponse<SchedulerHealthData>>('/health/schedulers'),

  instruments: (params?: {
    instrument_type?: string
    exchange?: string
    search?: string
    is_active?: boolean
    page?: number
    page_size?: number
  }) => {
    const sp = new URLSearchParams()
    if (params?.instrument_type) sp.set('instrument_type', params.instrument_type)
    if (params?.exchange) sp.set('exchange', params.exchange)
    if (params?.search) sp.set('search', params.search)
    if (params?.is_active !== undefined) sp.set('is_active', String(params.is_active))
    if (params?.page) sp.set('page', String(params.page))
    if (params?.page_size) sp.set('page_size', String(params.page_size ?? 50))
    const q = sp.toString()
    return request<PaginatedResponse<Instrument>>(`/instruments${q ? `?${q}` : ''}`)
  },

  instrument: (symbol: string) =>
    request<ApiResponse<Instrument>>(`/instruments/${encodeURIComponent(symbol)}`),

  ohlcv: (
    symbol: string,
    params?: { from_date?: string; to_date?: string; page?: number; page_size?: number }
  ) => {
    const sp = new URLSearchParams()
    if (params?.from_date) sp.set('from_date', params.from_date)
    if (params?.to_date) sp.set('to_date', params.to_date)
    if (params?.page) sp.set('page', String(params.page))
    if (params?.page_size) sp.set('page_size', String(params.page_size ?? 500))
    const q = sp.toString()
    return request<PaginatedResponse<OHLCVCandle>>(
      `/ohlcv/${encodeURIComponent(symbol)}${q ? `?${q}` : ''}`
    )
  },

  ohlcvLatest: (symbol: string) =>
    request<ApiResponse<OHLCVCandle>>(`/ohlcv/${encodeURIComponent(symbol)}/latest`),

  downloadStatus: () =>
    request<ApiResponse<DownloadStatusData>>('/download/status'),

  downloadStatusSymbol: (symbol: string) =>
    request<ApiResponse<DownloadLog>>(`/download/status/${encodeURIComponent(symbol)}`),

  downloadLogs: (params?: { symbol?: string; status?: string; page?: number; page_size?: number }) => {
    const sp = new URLSearchParams()
    if (params?.symbol) sp.set('symbol', params.symbol)
    if (params?.status) sp.set('status', params.status)
    if (params?.page) sp.set('page', String(params.page))
    if (params?.page_size) sp.set('page_size', String(params.page_size ?? 50))
    const q = sp.toString()
    return request<PaginatedResponse<DownloadLog>>(`/download/logs${q ? `?${q}` : ''}`)
  },

  downloadTrigger: (body: DownloadTriggerRequest) =>
    request<ApiResponse<{ instrument_types: string[]; days: number }>>('/download/trigger', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ─── Volatility API ────────────────────────────────────────────────

  volatilityATM: () =>
    request<ApiResponse<ATMInfo>>('/volatility/atm'),

  volatilityChain: (expiry: string) =>
    request<ApiResponse<IVChainData>>(`/volatility/chain?expiry=${expiry}`),

  volatilityStrikes: (expiry: string) =>
    request<ApiResponse<{ expiry: string; strikes: number[] }>>(`/volatility/strikes?expiry=${expiry}`),

  volatilityIVHistory: (symbol: string) =>
    request<ApiResponse<IVHistoryData>>(`/volatility/iv-history?symbol=${encodeURIComponent(symbol)}`),

  // ─── Strategies / Backtesting ────────────────────────────────────────

  strategies: () => request<ApiResponse<Strategy[]>>('/strategies'),

  strategyBacktests: (strategyId: number) =>
    request<ApiResponse<BacktestRun[]>>(`/strategies/${strategyId}/backtests`),

  backtestEquityCurve: (runId: number) =>
    request<ApiResponse<EquityPoint[]>>(`/strategies/backtests/${runId}/equity-curve`),

  backtestTrades: (runId: number) =>
    request<ApiResponse<Trade[]>>(`/strategies/backtests/${runId}/trades`),

  runBacktest: (body: BacktestRequest) =>
    request<ApiResponse<null>>('/strategies/backtests/run', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}

