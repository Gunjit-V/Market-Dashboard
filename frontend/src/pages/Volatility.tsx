/*
 * pages/Volatility.tsx
 *
 * IV / RV Volatility Dashboard.
 *
 * Data flow:
 *   1. User picks a futures symbol (e.g. NIFTY30MAR26FUT)
 *   2. Fetches OHLCV from existing api.ohlcv() — your Postgres data
 *   3. Computes RV in-browser via rv.ts utilities
 *   4. IV panel shows placeholder until options_collector.py has run
 *
 * Add to App.tsx:
 *   import Volatility from './pages/Volatility'
 *   <Route path="/volatility" element={<Volatility />} />
 *
 * Add to Layout.tsx nav:
 *   { to: '/volatility', label: 'Volatility' }
 */

import { useEffect, useState, useCallback } from 'react'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend, ReferenceLine,
} from 'recharts'
import { api } from '../api/client'
import type { OHLCVCandle, Instrument } from '../api/types'
import {
  rollingRV, currentRV, allMethodsRV,
  fmtPct, rvLabel, rvDescription,
  type RVMethod,
} from '../utils/rv'
import './Volatility.css'

// ── Types local to this page ─────────────────────────────────────────────────

type RVWindow = 5 | 10 | 20

const RV_METHODS: RVMethod[] = ['close_close', 'parkinson', 'garman_klass', 'rogers_satchell']
const RV_WINDOWS: RVWindow[] = [5, 10, 20]

// Placeholder spot price — replace with live tick feed when available
const PLACEHOLDER_SPOT = 24832.0

// IV placeholder — remove once options_collector.py is running
const IV_PLACEHOLDER = null as null  // will be: { atm_iv: 0.142, pcr: 1.1, ... }

// ── Colour tokens (mirror your index.css) ───────────────────────────────────
const C = {
  bg:         '#0b0f14',
  card:       '#111820',
  border:     '#1e2936',
  text:       '#e2e8f0',
  muted:      '#94a3b8',
  accent:     '#22c55e',
  accentDim:  '#16a34a',
  warn:       '#eab308',
  error:      '#ef4444',
  blue:       '#3b82f6',
  purple:     '#a855f7',
}

// ── Sub-components ───────────────────────────────────────────────────────────

interface StatCardProps {
  label: string
  value: string
  sub?: string
  color?: string
  badge?: { text: string; kind: 'success' | 'warn' | 'error' | 'info' }
  loading?: boolean
}

function StatCard({ label, value, sub, color, badge, loading }: StatCardProps) {
  return (
    <div className="card vol-stat-card">
      <div className="vol-stat-label">{label}</div>
      {loading
        ? <div className="vol-stat-skeleton" />
        : <div className="vol-stat-value mono" style={{ color: color ?? C.text }}>{value}</div>
      }
      {sub && <div className="vol-stat-sub mono">{sub}</div>}
      {badge && <span className={`badge badge-${badge.kind} vol-stat-badge`}>{badge.text}</span>}
    </div>
  )
}

interface SectionHeadProps {
  title: string
  sub?: string
}
function SectionHead({ title, sub }: SectionHeadProps) {
  return (
    <div className="vol-section-head">
      <h2 className="vol-section-title">{title}</h2>
      {sub && <span className="vol-section-sub">{sub}</span>}
    </div>
  )
}

// ── Custom tooltip for recharts ───────────────────────────────────────────────
function RVTooltip({ active, payload, label }: {
  active?: boolean; payload?: { name: string; value: number; color: string }[]; label?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="vol-tooltip">
      <div className="vol-tooltip-ts">{label ? new Date(label).toLocaleString() : ''}</div>
      {payload.map((p) => (
        <div key={p.name} className="vol-tooltip-row">
          <span style={{ color: p.color }}>{p.name}</span>
          <span className="mono">{(p.value).toFixed(2)}%</span>
        </div>
      ))}
    </div>
  )
}

// ── IV Placeholder Panel ──────────────────────────────────────────────────────
function IVPlaceholderPanel() {
  return (
    <div className="card vol-iv-placeholder">
      <div className="vol-iv-placeholder-icon">⌛</div>
      <div className="vol-iv-placeholder-title">Options data not yet available</div>
      <p className="vol-iv-placeholder-body">
        Implied Volatility requires live option chain snapshots from{' '}
        <code>options_collector.py</code>. Once it has run for at least one
        market session, IV will appear here automatically.
      </p>
      <div className="vol-iv-placeholder-checklist">
        {[
          ['✓', C.accent,  'Options collector code built'],
          ['✓', C.accent,  'Schema ready (option_chain_snapshot)'],
          ['✓', C.accent,  'IV / Greeks computation implemented'],
          ['○', C.muted,   'Run options_collector.py for ≥1 session'],
          ['○', C.muted,   'Accumulate 10–15 days for baselines'],
        ].map(([icon, color, text], i) => (
          <div key={i} className="vol-iv-checklist-row">
            <span style={{ color: color as string }}>{icon}</span>
            <span style={{ color: C.muted }}>{text}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function Volatility() {
  // Controls
  const [symbol, setSymbol]       = useState('NIFTY30MAR26FUT')
  const [inputSymbol, setInput]   = useState('NIFTY30MAR26FUT')
  const [window_, setWindow]      = useState<RVWindow>(10)
  const [method, setMethod]       = useState<RVMethod>('garman_klass')
  const [showAll, setShowAll]     = useState(false)   // overlay all 4 methods on chart
  const [instruments, setInstruments] = useState<Instrument[]>([])

  // Data
  const [candles, setCandles]     = useState<OHLCVCandle[]>([])
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState<string | null>(null)
  const [spot, setSpot]           = useState<number>(PLACEHOLDER_SPOT)

  // Load instruments list for the selector
  useEffect(() => {
    api.instruments({ instrument_type: 'FUTIDX', page_size: 50 })
      .then(r => setInstruments(r.data ?? []))
      .catch(() => {})
  }, [])

  // Load candles for the selected symbol
  const loadCandles = useCallback(() => {
    if (!symbol) return
    setLoading(true)
    setError(null)
    // Fetch last ~30 days of 5-min candles (page_size 2250 = 30 × 75 candles)
    api.ohlcv(symbol, { page_size: 2250 })
      .then(r => {
        const data = r.data ?? []
        setCandles(data)
        if (data.length > 0) setSpot(data[data.length - 1].close)
      })
      .catch(e => setError(e instanceof Error ? e.message : 'Failed to load'))
      .finally(() => setLoading(false))
  }, [symbol])

  useEffect(() => { loadCandles() }, [loadCandles])

  // ── Computed values ─────────────────────────────────────────────────────

  const rvCurrent = currentRV(candles, method, window_)
  const rvHistory = rollingRV(candles, method, window_)
  const allRV     = allMethodsRV(candles, window_)

  // Build chart series — either one method or all four overlaid
  const chartData = showAll
    ? buildMultiMethodSeries(candles, window_)
    : rvHistory.map(p => ({
        ts: new Date(p.timestamp).toLocaleDateString('en-IN', { month: 'short', day: 'numeric' }),
        [rvLabel(method)]: p.rv_pct,
      }))

  const vrp = IV_PLACEHOLDER != null && rvCurrent != null
    ? (IV_PLACEHOLDER as number) - rvCurrent
    : null

  // ── Helpers ──────────────────────────────────────────────────────────────

  const rvColor = (rv: number | null) => {
    if (rv == null) return C.muted
    if (rv < 0.10) return C.accent
    if (rv < 0.20) return C.warn
    return C.error
  }

  const rvRegime = (rv: number | null): string => {
    if (rv == null) return '—'
    if (rv < 0.10) return 'Low'
    if (rv < 0.15) return 'Normal'
    if (rv < 0.25) return 'Elevated'
    return 'High'
  }

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <div className="page">
      {/* ── Header ── */}
      <div className="vol-header">
        <div>
          <h1 className="vol-title">Volatility Dashboard</h1>
          <p className="vol-subtitle">
            Realized &amp; Implied Volatility · 5-min OHLCV · Nifty Futures
          </p>
        </div>
        <div className="vol-header-badge">
          <span style={{ color: C.warn }}>◆</span>
          <span style={{ color: C.muted }}>Spot (placeholder)</span>
          <span className="mono" style={{ color: C.text, fontSize: '1.1rem', fontWeight: 700 }}>
            {spot.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
          </span>
        </div>
      </div>

      {/* ── Controls ── */}
      <div className="card vol-controls">
        <div className="vol-controls-row">
          {/* Symbol selector */}
          <div className="vol-control-group">
            <label className="vol-control-label">Symbol</label>
            {instruments.length > 0
              ? (
                <select
                  className="input vol-select"
                  value={symbol}
                  onChange={e => setSymbol(e.target.value)}
                >
                  {instruments.map(i => (
                    <option key={i.id} value={i.symbol}>{i.symbol}</option>
                  ))}
                </select>
              )
              : (
                <div style={{ display: 'flex', gap: '0.5rem' }}>
                  <input
                    className="input"
                    value={inputSymbol}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && setSymbol(inputSymbol)}
                    placeholder="e.g. NIFTY30MAR26FUT"
                    style={{ width: 200 }}
                  />
                  <button className="btn btn-primary" onClick={() => setSymbol(inputSymbol)}>
                    Load
                  </button>
                </div>
              )
            }
          </div>

          {/* Window selector */}
          <div className="vol-control-group">
            <label className="vol-control-label">Rolling Window</label>
            <div className="vol-pill-group">
              {RV_WINDOWS.map(w => (
                <button
                  key={w}
                  className={`vol-pill ${window_ === w ? 'vol-pill-active' : ''}`}
                  onClick={() => setWindow(w)}
                >
                  {w}D
                </button>
              ))}
            </div>
          </div>

          {/* Method selector */}
          <div className="vol-control-group">
            <label className="vol-control-label">RV Method</label>
            <select
              className="input vol-select"
              value={method}
              onChange={e => setMethod(e.target.value as RVMethod)}
              disabled={showAll}
            >
              {RV_METHODS.map(m => (
                <option key={m} value={m}>{rvLabel(m)}</option>
              ))}
            </select>
          </div>

          {/* Overlay toggle */}
          <div className="vol-control-group" style={{ alignSelf: 'flex-end' }}>
            <label className="vol-toggle-label">
              <input
                type="checkbox"
                checked={showAll}
                onChange={e => setShowAll(e.target.checked)}
                className="vol-toggle-input"
              />
              <span className="vol-toggle-track">
                <span className="vol-toggle-thumb" />
              </span>
              <span style={{ color: C.muted, fontSize: '0.85rem' }}>Overlay all methods</span>
            </label>
          </div>
        </div>

        {!showAll && (
          <div className="vol-method-desc">
            {rvDescription(method)}
          </div>
        )}
      </div>

      {error && <div className="error-msg" style={{ marginBottom: '1rem' }}>{error}</div>}

      {/* ── Stat cards ── */}
      <div className="vol-stats-grid">
        <StatCard
          label={`RV ${window_}D · ${rvLabel(method)}`}
          value={fmtPct(rvCurrent)}
          sub={rvCurrent ? `${rvRegime(rvCurrent)} volatility regime` : 'Insufficient data'}
          color={rvColor(rvCurrent)}
          badge={rvCurrent ? { text: rvRegime(rvCurrent), kind: rvCurrent < 0.15 ? 'success' : rvCurrent < 0.25 ? 'warn' : 'error' } : undefined}
          loading={loading}
        />
        <StatCard
          label="IV ATM (Implied Vol)"
          value="—"
          sub="Awaiting options data"
          color={C.muted}
          badge={{ text: 'Pending', kind: 'warn' }}
          loading={false}
        />
        <StatCard
          label="VRP (IV − RV)"
          value={vrp != null ? fmtPct(vrp) : '—'}
          sub={vrp != null
            ? vrp > 0 ? 'Options expensive vs realised' : 'Options cheap vs realised'
            : 'Available once IV is live'
          }
          color={vrp != null ? (vrp > 0 ? C.warn : C.accent) : C.muted}
          loading={loading}
        />
        <StatCard
          label="Candles loaded"
          value={loading ? '…' : candles.length.toLocaleString()}
          sub={candles.length > 0
            ? `${new Date(candles[0].timestamp).toLocaleDateString()} → ${new Date(candles[candles.length - 1].timestamp).toLocaleDateString()}`
            : undefined
          }
          loading={loading}
        />
      </div>

      {/* ── RV Chart ── */}
      <div className="vol-section">
        <SectionHead
          title="Realized Volatility — Rolling History"
          sub={showAll ? 'All 4 estimators overlaid' : `${window_}D rolling · ${rvLabel(method)}`}
        />
        <div className="card vol-chart-card">
          {loading ? (
            <div className="vol-chart-loading">Loading candle data…</div>
          ) : chartData.length === 0 ? (
            <div className="vol-chart-empty">
              {candles.length === 0
                ? `No OHLCV data found for ${symbol}. Check the symbol or download data first.`
                : `Not enough candles to compute ${window_}D rolling RV (need ${window_ * 75}, have ${candles.length}).`
              }
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={chartData} margin={{ top: 8, right: 24, left: 0, bottom: 0 }}>
                <CartesianGrid stroke={C.border} strokeDasharray="3 3" />
                <XAxis
                  dataKey="ts"
                  tick={{ fill: C.muted, fontSize: 11 }}
                  axisLine={{ stroke: C.border }}
                  tickLine={false}
                  interval="preserveStartEnd"
                />
                <YAxis
                  tick={{ fill: C.muted, fontSize: 11 }}
                  axisLine={{ stroke: C.border }}
                  tickLine={false}
                  tickFormatter={v => `${v.toFixed(1)}%`}
                  width={52}
                />
                <Tooltip content={<RVTooltip />} />
                {showAll && <Legend wrapperStyle={{ color: C.muted, fontSize: 12 }} />}
                {/* Reference bands */}
                <ReferenceLine y={10} stroke={C.accent}  strokeDasharray="4 4" strokeOpacity={0.4} label={{ value: '10%', fill: C.accent,  fontSize: 10 }} />
                <ReferenceLine y={20} stroke={C.warn}    strokeDasharray="4 4" strokeOpacity={0.4} label={{ value: '20%', fill: C.warn,    fontSize: 10 }} />
                <ReferenceLine y={30} stroke={C.error}   strokeDasharray="4 4" strokeOpacity={0.4} label={{ value: '30%', fill: C.error,   fontSize: 10 }} />
                {showAll ? (
                  <>
                    <Line dataKey="Close-to-Close"    stroke={C.blue}   dot={false} strokeWidth={1.5} />
                    <Line dataKey="Parkinson"         stroke={C.accent} dot={false} strokeWidth={1.5} />
                    <Line dataKey="Garman-Klass"      stroke={C.warn}   dot={false} strokeWidth={2} />
                    <Line dataKey="Rogers-Satchell"   stroke={C.purple} dot={false} strokeWidth={1.5} />
                  </>
                ) : (
                  <Line
                    dataKey={rvLabel(method)}
                    stroke={C.accent}
                    dot={false}
                    strokeWidth={2}
                    activeDot={{ r: 4, fill: C.accent }}
                  />
                )}
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      {/* ── Method comparison table ── */}
      {!loading && candles.length > 0 && (
        <div className="vol-section">
          <SectionHead
            title="RV Method Comparison"
            sub={`${window_}D window · current values`}
          />
          <div className="card">
            <table className="vol-method-table">
              <thead>
                <tr>
                  <th>Method</th>
                  <th>RV (annualised)</th>
                  <th>Regime</th>
                  <th>Description</th>
                </tr>
              </thead>
              <tbody>
                {RV_METHODS.map(m => {
                  const rv = allRV[m]
                  return (
                    <tr
                      key={m}
                      className={m === method ? 'vol-method-row-active' : ''}
                      onClick={() => { setMethod(m); setShowAll(false) }}
                    >
                      <td>
                        <span className="mono" style={{ color: C.text }}>{rvLabel(m)}</span>
                        {m === method && (
                          <span className="badge badge-success" style={{ marginLeft: 8, fontSize: '0.7rem' }}>
                            active
                          </span>
                        )}
                      </td>
                      <td className="mono" style={{ color: rvColor(rv), fontWeight: 600 }}>
                        {fmtPct(rv)}
                      </td>
                      <td>
                        {rv != null && (
                          <span className={`badge badge-${rv < 0.15 ? 'success' : rv < 0.25 ? 'warn' : 'error'}`}>
                            {rvRegime(rv)}
                          </span>
                        )}
                      </td>
                      <td style={{ color: C.muted, fontSize: '0.85rem' }}>
                        {rvDescription(m)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── IV Panel ── */}
      <div className="vol-section">
        <SectionHead
          title="Implied Volatility"
          sub="IV · IV Rank · IV Skew · PCR · Max Pain"
        />
        <IVPlaceholderPanel />
      </div>
    </div>
  )
}
