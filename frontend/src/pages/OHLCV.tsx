import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import DatePicker from 'react-datepicker'
import 'react-datepicker/dist/react-datepicker.css'
import { api } from '../api/client'
import type { OHLCVCandle } from '../api/types'
import CandlestickChart from '../components/CandlestickChart'
import './OHLCV.css'

/** Format as local datetime for API: YYYY-MM-DDTHH:mm:ss */
function formatDateTimeForApi(d: Date): string {
  const y = d.getFullYear()
  const M = String(d.getMonth() + 1).padStart(2, '0')
  const D = String(d.getDate()).padStart(2, '0')
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  const s = String(d.getSeconds()).padStart(2, '0')
  return `${y}-${M}-${D}T${h}:${m}:${s}`
}

export default function OHLCV() {
  const { symbol } = useParams<{ symbol: string }>()
  const [candles, setCandles] = useState<OHLCVCandle[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [fromDateTime, setFromDateTime] = useState<Date | null>(null)
  const [toDateTime, setToDateTime] = useState<Date | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fromStr = fromDateTime ? formatDateTimeForApi(fromDateTime) : ''
  const toStr = toDateTime ? formatDateTimeForApi(toDateTime) : ''

  useEffect(() => {
    if (!symbol) return
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .ohlcv(symbol, {
        from_date: fromStr || undefined,
        to_date: toStr || undefined,
        page,
        page_size: 200,
      })
      .then((res) => {
        if (cancelled) return
        if (res.status === 'error' && res.message) {
          setError(res.message)
          setCandles([])
          setTotal(0)
        } else {
          setError(null)
          setCandles(res.data ?? [])
          setTotal(res.meta?.total ?? 0)
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [symbol, fromStr, toStr, page])

  if (!symbol) return <div className="page"><div className="error-msg">Missing symbol</div></div>

  const totalPages = Math.ceil(total / 200)

  return (
    <div className="page">
      <h1>OHLCV — <span className="mono">{symbol}</span></h1>
      <div className="card" style={{ marginBottom: '1rem' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'center' }}>
          <label className="date-picker-label">
            <span style={{ color: 'var(--text-muted)', fontSize: '0.9rem' }}>From</span>
            <DatePicker
              selected={fromDateTime}
              onChange={(d) => { setFromDateTime(d ?? null); setPage(1); }}
              selectsStart
              startDate={fromDateTime}
              endDate={toDateTime}
              maxDate={toDateTime ?? undefined}
              showTimeSelect
              timeIntervals={15}
              timeCaption="Time"
              timeFormat="HH:mm"
              placeholderText="Date & time"
              className="input date-picker-input"
              dateFormat="yyyy-MM-dd HH:mm"
              showMonthDropdown
              showYearDropdown
              dropdownMode="select"
            />
          </label>
          <label className="date-picker-label">
            <span style={{ color: 'var(--text-muted)', fontSize: '0.9rem' }}>To</span>
            <DatePicker
              selected={toDateTime}
              onChange={(d) => { setToDateTime(d ?? null); setPage(1); }}
              selectsEnd
              startDate={fromDateTime}
              endDate={toDateTime}
              minDate={fromDateTime ?? undefined}
              showTimeSelect
              timeIntervals={15}
              timeCaption="Time"
              timeFormat="HH:mm"
              placeholderText="Date & time"
              className="input date-picker-input"
              dateFormat="yyyy-MM-dd HH:mm"
              showMonthDropdown
              showYearDropdown
              dropdownMode="select"
            />
          </label>
          <button className="btn" onClick={() => setPage(1)}>Apply</button>
        </div>
      </div>
      {error && <div className="error-msg">{error}</div>}
      {loading ? (
        <div className="loading">Loading…</div>
      ) : (
        <>
          {candles.length > 0 ? (
            <div className="card" style={{ marginBottom: '1rem', minHeight: 360 }}>
              <CandlestickChart candles={candles} height={360} />
            </div>
          ) : (
            <div className="card" style={{ marginBottom: '1rem', padding: '2rem', color: 'var(--text-muted)', textAlign: 'center' }}>
              No OHLCV data for this range. Try another date or check the symbol.
            </div>
          )}
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Timestamp</th>
                  <th>Open</th>
                  <th>High</th>
                  <th>Low</th>
                  <th>Close</th>
                  <th>Volume</th>
                </tr>
              </thead>
              <tbody>
                {candles.map((c, i) => (
                  <tr key={c.timestamp + i}>
                    <td className="mono">{new Date(c.timestamp).toLocaleString()}</td>
                    <td className="mono">{c.open.toFixed(2)}</td>
                    <td className="mono">{c.high.toFixed(2)}</td>
                    <td className="mono">{c.low.toFixed(2)}</td>
                    <td className="mono">{c.close.toFixed(2)}</td>
                    <td className="mono">{c.volume.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {total > 0 && (
            <div style={{ marginTop: '1rem', display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
              <span className="mono" style={{ color: 'var(--text-muted)' }}>
                {total} candles · page {page} of {totalPages} (page 1 = most recent)
              </span>
              <button className="btn" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Newer</button>
              <button className="btn" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Older</button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
