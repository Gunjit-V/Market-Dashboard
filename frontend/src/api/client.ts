import type {
  ApiResponse,
  PaginatedResponse,
  Instrument,
  OHLCVCandle,
  DownloadLog,
  DownloadTriggerRequest,
  DownloadStatusData,
  RVResponse,
  IVResponse,
  RVMethod,
  RVWindow,
} from './types'

const API_BASE = import.meta.env.VITE_API_URL ?? '/api'

async function request<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
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
  return json as T
}

export const api = {
  health: () =>
    request<ApiResponse<{ api: string; database: string; postgres_version?: string }>>('/health'),

  instruments: (params?: {
    instrument_type?: string
    exchange?: string
    search?: string
    page?: number
    page_size?: number
  }) => {
    const sp = new URLSearchParams()
    if (params?.instrument_type) sp.set('instrument_type', params.instrument_type)
    if (params?.exchange) sp.set('exchange', params.exchange)
    if (params?.search) sp.set('search', params.search)
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
}

// ── Volatility API calls — added from integration guide
// These map to the backend endpoints you need to add in FastAPI.
// Endpoint signatures are documented below each call.

export const volatilityApi = {
  rv: (
    symbol: string,
    params?: {
      method?: RVMethod
      window_days?: RVWindow
      from_date?: string
      to_date?: string
    }
  ) => {
    const sp = new URLSearchParams()
    if (params?.method)      sp.set('method',      params.method)
    if (params?.window_days) sp.set('window_days', String(params.window_days))
    if (params?.from_date)   sp.set('from_date',   params.from_date)
    if (params?.to_date)     sp.set('to_date',     params.to_date)
    const q = sp.toString()
    // reuse your existing request() helper
    return request<{ status: string; data: RVResponse }>(
      `/volatility/rv/${encodeURIComponent(symbol)}${q ? `?${q}` : ''}`
    )
  },

  iv: (
    underlying: string,
    params?: { from_date?: string; to_date?: string }
  ) => {
    const sp = new URLSearchParams()
    if (params?.from_date) sp.set('from_date', params.from_date)
    if (params?.to_date)   sp.set('to_date',   params.to_date)
    const q = sp.toString()
    return request<{ status: string; data: IVResponse }>(
      `/volatility/iv/${encodeURIComponent(underlying)}${q ? `?${q}` : ''}`
    )
  },
}
