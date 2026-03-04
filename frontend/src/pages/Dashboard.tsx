import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { DownloadStatusData } from '../api/types'

export default function Dashboard() {
  const [health, setHealth] = useState<{ api: string; database: string; postgres_version?: string } | null>(null)
  const [downloadStatus, setDownloadStatus] = useState<DownloadStatusData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([api.health(), api.downloadStatus()])
      .then(([hRes, dRes]) => {
        if (cancelled) return
        setHealth(hRes.data ?? null)
        setDownloadStatus(dRes.data ?? null)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [])

  if (loading) return <div className="page"><div className="loading">Loading…</div></div>
  if (error) return <div className="page"><div className="error-msg">{error}</div></div>

  return (
    <div className="page">
      <h1>Dashboard</h1>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '1rem' }}>
        <div className="card">
          <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: '0.25rem' }}>API</div>
          <div className="mono" style={{ color: health?.api === 'running' ? 'var(--accent)' : 'var(--error)' }}>
            {health?.api ?? '—'}
          </div>
        </div>
        <div className="card">
          <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: '0.25rem' }}>Database</div>
          <div className="mono" style={{ color: health?.database === 'connected' ? 'var(--accent)' : 'var(--error)' }}>
            {health?.database ?? '—'}
          </div>
          {health?.postgres_version && (
            <div className="mono" style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>
              {health.postgres_version}
            </div>
          )}
        </div>
        <div className="card">
          <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: '0.25rem' }}>Instruments</div>
          <div className="mono" style={{ fontSize: '1.25rem' }}>{downloadStatus?.total_instruments ?? 0}</div>
        </div>
        <div className="card">
          <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: '0.25rem' }}>OHLCV candles</div>
          <div className="mono" style={{ fontSize: '1.25rem' }}>{downloadStatus?.total_candles?.toLocaleString() ?? 0}</div>
        </div>
        <div className="card">
          <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: '0.25rem' }}>Latest run</div>
          <div className="mono" style={{ fontSize: '0.9rem' }}>
            {downloadStatus?.latest_run_at
              ? new Date(downloadStatus.latest_run_at).toLocaleString()
              : '—'}
          </div>
          {downloadStatus?.latest_run_status && (
            <span className={`badge badge-${downloadStatus.latest_run_status === 'success' ? 'success' : 'error'}`} style={{ marginTop: '0.5rem' }}>
              {downloadStatus.latest_run_status}
            </span>
          )}
        </div>
      </div>
      <div style={{ marginTop: '1.5rem' }}>
        <Link to="/instruments" className="btn btn-primary">Browse instruments</Link>
        <Link to="/download" className="btn" style={{ marginLeft: '0.5rem' }}>Download & logs</Link>
      </div>
    </div>
  )
}
