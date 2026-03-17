/**
 * rv.ts — Realized Volatility computation utilities
 *
 * All methods take OHLCVCandle[] (already in your types.ts) and return
 * an annualised decimal (e.g. 0.142 = 14.2%).
 *
 * Candles should be 5-minute bars from ohlcv_1min/ohlcv_5min.
 * NSE session = 09:15–15:30 = 375 minutes = 75 five-min candles per day.
 */

import type { OHLCVCandle } from '../api/types'

// ── Constants ────────────────────────────────────────────────────────────────

const CANDLES_PER_DAY = 75          // 5-min candles in one NSE session
const TRADING_DAYS    = 252
export const ANNUALIZATION = Math.sqrt(CANDLES_PER_DAY * TRADING_DAYS) // ≈ 137.5

export type RVMethod = 'close_close' | 'parkinson' | 'garman_klass' | 'rogers_satchell'

// ── Individual estimators ────────────────────────────────────────────────────

/**
 * Close-to-Close (Yang-Zhang simplified)
 * Most common. Works on any price series but noisier than OHLC methods.
 */
export function closeToClose(candles: OHLCVCandle[]): number {
  if (candles.length < 2) return NaN
  const logReturns = []
  for (let i = 1; i < candles.length; i++) {
    const r = Math.log(candles[i].close / candles[i - 1].close)
    logReturns.push(r)
  }
  return stdDev(logReturns) * ANNUALIZATION
}

/**
 * Parkinson (1980) — uses High-Low range.
 * ~5× more efficient than close-to-close. Ignores drift.
 */
export function parkinson(candles: OHLCVCandle[]): number {
  if (candles.length < 1) return NaN
  const k = 1 / (4 * Math.log(2))
  const sum = candles.reduce((acc, c) => {
    if (c.high <= 0 || c.low <= 0) return acc
    return acc + Math.pow(Math.log(c.high / c.low), 2)
  }, 0)
  return Math.sqrt(k * sum / candles.length) * ANNUALIZATION
}

/**
 * Garman-Klass (1980) — uses Open/High/Low/Close.
 * More efficient than Parkinson. Best general-purpose OHLC estimator.
 */
export function garmanKlass(candles: OHLCVCandle[]): number {
  if (candles.length < 1) return NaN
  const ln2 = Math.log(2)
  const sum = candles.reduce((acc, c) => {
    if (c.high <= 0 || c.low <= 0 || c.open <= 0 || c.close <= 0) return acc
    const hl = 0.5 * Math.pow(Math.log(c.high / c.low), 2)
    const co = (2 * ln2 - 1) * Math.pow(Math.log(c.close / c.open), 2)
    return acc + (hl - co)
  }, 0)
  return Math.sqrt(sum / candles.length) * ANNUALIZATION
}

/**
 * Rogers-Satchell (1991) — handles non-zero drift better than Garman-Klass.
 * Better for trending instruments like Nifty.
 */
export function rogersSatchell(candles: OHLCVCandle[]): number {
  if (candles.length < 1) return NaN
  const sum = candles.reduce((acc, c) => {
    if (c.high <= 0 || c.low <= 0 || c.open <= 0 || c.close <= 0) return acc
    const u = Math.log(c.high / c.open)
    const d = Math.log(c.low  / c.open)
    const f = Math.log(c.close / c.open)
    return acc + u * (u - f) + d * (d - f)
  }, 0)
  return Math.sqrt(sum / candles.length) * ANNUALIZATION
}

// ── Dispatcher ───────────────────────────────────────────────────────────────

export function computeRV(candles: OHLCVCandle[], method: RVMethod): number {
  switch (method) {
    case 'close_close':      return closeToClose(candles)
    case 'parkinson':        return parkinson(candles)
    case 'garman_klass':     return garmanKlass(candles)
    case 'rogers_satchell':  return rogersSatchell(candles)
  }
}

// ── Rolling window series ─────────────────────────────────────────────────────

export interface RVDataPoint {
  timestamp: string
  rv: number
  rv_pct: number       // rv * 100, for display
}

/**
 * Compute a rolling RV series.
 *
 * @param candles   All 5-min candles (sorted ascending)
 * @param method    Which estimator to use
 * @param windowDays  Rolling window in trading days (default 10)
 * @returns array of {timestamp, rv} — one point per candle after warmup
 */
export function rollingRV(
  candles: OHLCVCandle[],
  method: RVMethod = 'garman_klass',
  windowDays = 10
): RVDataPoint[] {
  const windowSize = windowDays * CANDLES_PER_DAY
  if (candles.length < windowSize) return []

  const result: RVDataPoint[] = []
  for (let i = windowSize; i <= candles.length; i++) {
    const window = candles.slice(i - windowSize, i)
    const rv = computeRV(window, method)
    if (isFinite(rv)) {
      result.push({
        timestamp: candles[i - 1].timestamp,
        rv,
        rv_pct: parseFloat((rv * 100).toFixed(2)),
      })
    }
  }
  return result
}

/**
 * Compute a single RV over all candles provided (for the summary stat card).
 */
export function currentRV(
  candles: OHLCVCandle[],
  method: RVMethod = 'garman_klass',
  windowDays = 10
): number | null {
  const windowSize = windowDays * CANDLES_PER_DAY
  if (candles.length < windowSize) return null
  const window = candles.slice(-windowSize)
  const rv = computeRV(window, method)
  return isFinite(rv) ? rv : null
}

/**
 * Compute RV for all four methods simultaneously — for the comparison table.
 */
export function allMethodsRV(
  candles: OHLCVCandle[],
  windowDays = 10
): Record<RVMethod, number | null> {
  const methods: RVMethod[] = ['close_close', 'parkinson', 'garman_klass', 'rogers_satchell']
  const result = {} as Record<RVMethod, number | null>
  for (const m of methods) {
    result[m] = currentRV(candles, m, windowDays)
  }
  return result
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function stdDev(values: number[]): number {
  if (values.length === 0) return 0
  const mean = values.reduce((a, b) => a + b, 0) / values.length
  const variance = values.reduce((a, b) => a + Math.pow(b - mean, 2), 0) / values.length
  return Math.sqrt(variance)
}

export function fmtPct(v: number | null | undefined, decimals = 2): string {
  if (v == null || !isFinite(v)) return '—'
  return `${(v * 100).toFixed(decimals)}%`
}

export function rvLabel(method: RVMethod): string {
  return {
    close_close:     'Close-to-Close',
    parkinson:       'Parkinson',
    garman_klass:    'Garman-Klass',
    rogers_satchell: 'Rogers-Satchell',
  }[method]
}

export function rvDescription(method: RVMethod): string {
  return {
    close_close:     'Uses closing prices only. Most common but noisiest.',
    parkinson:       'Uses High-Low range. 5× more efficient than C-C.',
    garman_klass:    'Uses OHLC. Best general-purpose estimator.',
    rogers_satchell: 'Uses OHLC with drift correction. Best for trending markets.',
  }[method]
}
