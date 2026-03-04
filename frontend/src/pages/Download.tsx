import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { DownloadStatusData, DownloadLog } from '../api/types'

export default function Download() {
  const [status, setStatus] = useState<DownloadStatusData | null>(null)
  const [logs, setLogs] = useState<DownloadLog[]>([])
  const [logPage, setLogPage] = useState(1)
  const [logTotal, setLogTotal] = useState(0)
  const [logSymbol, setLogSymbol] = useState('')
  const [logStatusFilter, setLogStatusFilter] = useState('')
  const [triggerTypes, setTriggerTypes] = useState('EQ')
  const [triggerDays, setTriggerDays] = useState(1)
  const [triggering, setTriggering] = useState(false)
  const [triggerMsg, setTriggerMsg] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const loadStatus = () => {
    api.downloadStatus().then((res) => setStatus(res.data ?? null)).catch(() => {})
  }

  const loadLogs = () => {
    api
      .downloadLogs({
        symbol: logSymbol || undefined,
        status: logStatusFilter || undefined,
        page: logPage,
        page_size: 30,
      })
      .then((res) => {
        setLogs(res.data ?? [])
        setLogTotal(res.meta?.total ?? 0)
      })
      .catch(() => {})
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api.downloadStatus()
      .then((res) => { if (!cancelled) setStatus(res.data ?? null) })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    api
      .downloadLogs({
        symbol: logSymbol || undefined,
        status: logStatusFilter || undefined,
        page: logPage,
        page_size: 30,
      })
      .then((res) => {
        setLogs(res.data ?? [])
        setLogTotal(res.meta?.total ?? 0)
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load'))
  }, [logPage, logSymbol, logStatusFilter])


  const handleTrigger = () => {
    const types = triggerTypes.split(/[\s,]+/).filter(Boolean)
    if (!types.length) return
    setTriggering(true)
    setTriggerMsg(null)
    api
      .downloadTrigger({ instrument_types: types, days: triggerDays })
      .then((res) => {
        setTriggerMsg(res.message ?? 'Triggered')
        loadStatus()
      })
      .catch((e) => setTriggerMsg(e instanceof Error ? e.message : 'Trigger failed'))
      .finally(() => setTriggering(false))
  }

  const logTotalPages = Math.ceil(logTotal / 30)

  return (
    <div className="page">
      <h1>Download</h1>
      {loading ? (
        <div className="loading">Loading…</div>
      ) : (
        <>
          <div className="card" style={{ marginBottom: '1rem' }}>
            <h2 style={{ fontSize: '1.1rem', margin: '0 0 0.75rem' }}>Status</h2>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: '1rem' }}>
              <div>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Instruments</div>
                <div className="mono">{status?.total_instruments ?? 0}</div>
              </div>
              <div>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Candles</div>
                <div className="mono">{status?.total_candles?.toLocaleString() ?? 0}</div>
              </div>
              <div>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Latest run</div>
                <div className="mono" style={{ fontSize: '0.9rem' }}>
                  {status?.latest_run_at ? new Date(status.latest_run_at).toLocaleString() : '—'}
                </div>
                {status?.latest_run_status && (
                  <span className={`badge badge-${status.latest_run_status === 'success' ? 'success' : 'error'}`} style={{ marginTop: '0.25rem' }}>
                    {status.latest_run_status}
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="card" style={{ marginBottom: '1rem' }}>
            <h2 style={{ fontSize: '1.1rem', margin: '0 0 0.75rem' }}>Trigger download</h2>
            <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginBottom: '0.75rem' }}>
              Comma-separated instrument types (e.g. EQ, FUTIDX). Runs in background.
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'center' }}>
              <input
                className="input"
                placeholder="EQ, FUTIDX"
                value={triggerTypes}
                onChange={(e) => setTriggerTypes(e.target.value)}
                style={{ width: '200px' }}
              />
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <span style={{ color: 'var(--text-muted)', fontSize: '0.9rem' }}>Days</span>
                <input
                  type="number"
                  min={1}
                  className="input"
                  value={triggerDays}
                  onChange={(e) => setTriggerDays(parseInt(e.target.value, 10) || 1)}
                  style={{ width: '70px' }}
                />
              </label>
              <button className="btn btn-primary" disabled={triggering} onClick={handleTrigger}>
                {triggering ? 'Triggering…' : 'Trigger'}
              </button>
            </div>
            {triggerMsg && (
              <p style={{ marginTop: '0.75rem', color: 'var(--accent)', fontSize: '0.9rem' }}>{triggerMsg}</p>
            )}
          </div>

          <div className="card">
            <h2 style={{ fontSize: '1.1rem', margin: '0 0 0.75rem' }}>Download logs</h2>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', marginBottom: '1rem' }}>
              <input
                className="input"
                placeholder="Filter by symbol"
                value={logSymbol}
                onChange={(e) => { setLogSymbol(e.target.value); setLogPage(1); }}
                style={{ width: '140px' }}
              />
              <select
                className="input"
                value={logStatusFilter}
                onChange={(e) => { setLogStatusFilter(e.target.value); setLogPage(1); }}
                style={{ width: '120px' }}
              >
                <option value="">All status</option>
                <option value="success">success</option>
                <option value="failed">failed</option>
                <option value="no_data">no_data</option>
              </select>
            </div>
            {error && <div className="error-msg">{error}</div>}
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Last run</th>
                    <th>Status</th>
                    <th>Inserted</th>
                    <th>Skipped</th>
                    <th>Error</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.map((log) => (
                    <tr key={log.id}>
                      <td className="mono">{log.symbol ?? '—'}</td>
                      <td className="mono">{new Date(log.last_run_at).toLocaleString()}</td>
                      <td>
                        <span className={`badge badge-${log.status === 'success' ? 'success' : log.status === 'failed' ? 'error' : 'warn'}`}>
                          {log.status}
                        </span>
                      </td>
                      <td className="mono">{log.candles_inserted ?? '—'}</td>
                      <td className="mono">{log.candles_skipped ?? '—'}</td>
                      <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }} title={log.error_message ?? ''}>
                        {log.error_message ?? '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {logTotal > 0 && (
              <div style={{ marginTop: '1rem', display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
                <span className="mono" style={{ color: 'var(--text-muted)' }}>
                  {logTotal} logs · page {logPage} of {logTotalPages}
                </span>
                <button className="btn" disabled={logPage <= 1} onClick={() => setLogPage((p) => p - 1)}>Previous</button>
                <button className="btn" disabled={logPage >= logTotalPages} onClick={() => setLogPage((p) => p + 1)}>Next</button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}