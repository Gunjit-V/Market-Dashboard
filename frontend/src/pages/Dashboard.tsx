import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { DownloadStatusData, SchedulerHealthData } from '../api/types'

const SERVICE_LABELS: Record<string, string> = {
  tick_downloader: 'Tick downloader',
  ohlcv_scheduler: 'OHLCV scheduler',
  instrument_sync_scheduler: 'Instrument sync',
  paper_trading_scheduler: 'Paper trading',
}

const STATUS_COLOR: Record<string, string> = {
  ok: 'var(--accent)',
  stale: 'var(--error)',
  unknown: 'var(--text-muted)',
}

function formatMinutesAgo(minutes: number | null): string {
  if (minutes === null) return 'never'
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${Math.round(minutes)}m ago`
  const hours = minutes / 60
  if (hours < 24) return `${hours.toFixed(1)}h ago`
  return `${(hours / 24).toFixed(1)}d ago`
}

const SCHEDULER_POLL_MS = 60_000

export default function Dashboard() {
  const [health, setHealth] = useState<{ api: string; database: string; postgres_version?: string } | null>(null)
  const [downloadStatus, setDownloadStatus] = useState<DownloadStatusData | null>(null)
  const [schedulerHealth, setSchedulerHealth] = useState<SchedulerHealthData | null>(null)
  const [schedulerError, setSchedulerError] = useState<string | null>(null)
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

  useEffect(() => {
    let cancelled = false
    const load = () => {
      api
        .schedulerHealth()
        .then((res) => {
          if (cancelled) return
          setSchedulerHealth(res.data ?? null)
          setSchedulerError(null)
        })
        .catch((e) => {
          if (!cancelled) setSchedulerError(e instanceof Error ? e.message : 'Failed to load')
        })
    }
    load()
    const interval = setInterval(load, SCHEDULER_POLL_MS)
    return () => { cancelled = true; clearInterval(interval) }
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
        <h2 style={{ fontSize: '1.05rem', marginBottom: '0.75rem' }}>Background services</h2>
        {schedulerError && <div className="error-msg">{schedulerError}</div>}
        {schedulerHealth && (
          <>
            <div className="mono" style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.5rem' }}>
              Market {schedulerHealth.market_open ? 'open' : 'closed'} · checked {new Date(schedulerHealth.checked_at).toLocaleTimeString()}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '1rem' }}>
              {Object.entries(schedulerHealth.services).map(([key, svc]) => (
                <div key={key} className="card">
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.25rem' }}>
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>{SERVICE_LABELS[key] ?? key}</span>
                    <span
                      title={svc.status}
                      style={{
                        width: '0.6rem',
                        height: '0.6rem',
                        borderRadius: '50%',
                        background: STATUS_COLOR[svc.status] ?? 'var(--text-muted)',
                        display: 'inline-block',
                      }}
                    />
                  </div>
                  <div className="mono" style={{ fontSize: '1rem' }}>
                    {formatMinutesAgo(svc.minutes_ago)}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
      <div style={{ marginTop: '1.5rem' }}>
        <Link to="/instruments" className="btn btn-primary">Browse instruments</Link>
        <Link to="/download" className="btn" style={{ marginLeft: '0.5rem' }}>Download & logs</Link>
      </div>
    </div>
  )
}
