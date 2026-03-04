import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { Instrument } from '../api/types'
import type { Meta } from '../api/types'

export default function Instruments() {
  const [list, setList] = useState<Instrument[]>([])
  const [meta, setMeta] = useState<Meta | null>(null)
  const [instrumentType, setInstrumentType] = useState('')
  const [exchange, setExchange] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .instruments({
        instrument_type: instrumentType || undefined,
        exchange: exchange || undefined,
        search: search || undefined,
        page,
        page_size: 50,
      })
      .then((res) => {
        if (cancelled) return
        setList(res.data ?? [])
        setMeta(res.meta ?? null)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [instrumentType, exchange, search, page])

  const totalPages = meta ? Math.ceil(meta.total / meta.page_size) : 0

  return (
    <div className="page">
      <h1>Instruments</h1>
      <div className="card" style={{ marginBottom: '1rem' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem', alignItems: 'center' }}>
          <input
            className="input"
            placeholder="Search symbol or name"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(1); }}
            style={{ width: '180px' }}
          />
          <input
            className="input"
            placeholder="Instrument type (e.g. EQ, FUTIDX)"
            value={instrumentType}
            onChange={(e) => { setInstrumentType(e.target.value); setPage(1); }}
            style={{ width: '180px' }}
          />
          <input
            className="input"
            placeholder="Exchange (e.g. NSE, NFO)"
            value={exchange}
            onChange={(e) => { setExchange(e.target.value); setPage(1); }}
            style={{ width: '120px' }}
          />
          <button className="btn" onClick={() => setPage(1)}>Apply</button>
        </div>
      </div>
      {error && <div className="error-msg">{error}</div>}
      {loading ? (
        <div className="loading">Loading…</div>
      ) : (
        <>
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Token</th>
                  <th>Name</th>
                  <th>Exchange</th>
                  <th>Type</th>
                  <th>Expiry</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {list.map((i) => (
                  <tr key={i.id}>
                    <td className="mono">{i.symbol}</td>
                    <td className="mono">{i.token}</td>
                    <td>{i.name ?? '—'}</td>
                    <td>{i.exchange}</td>
                    <td>{i.instrument_type ?? '—'}</td>
                    <td className="mono">{i.expiry ?? '—'}</td>
                    <td>
                      <Link to={`/ohlcv/${i.symbol}`} className="btn" style={{ padding: '0.35rem 0.6rem', fontSize: '0.8rem' }}>
                        OHLCV
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {meta && meta.total > 0 && (
            <div style={{ marginTop: '1rem', display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
              <span className="mono" style={{ color: 'var(--text-muted)' }}>
                {meta.total} total · page {meta.page} of {totalPages}
              </span>
              <button className="btn" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                Previous
              </button>
              <button className="btn" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
