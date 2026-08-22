import { Link, useLocation } from 'react-router-dom'

interface LayoutProps {
  children: React.ReactNode
}

const nav = [
  { to: '/', label: 'Dashboard' },
  { to: '/instruments', label: 'Instruments' },
  { to: '/download', label: 'Download' },
  { to: '/volatility', label: 'Volatility' },
  { to: '/strategies', label: 'Strategies' },
  { to: '/paper-trading', label: 'Paper Trading' },
]

export default function Layout({ children }: LayoutProps) {
  const location = useLocation()

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      <header
        style={{
          borderBottom: '1px solid var(--border)',
          background: 'var(--bg-card)',
          padding: '0.75rem 2rem',
        }}
      >
        <div
          style={{
            maxWidth: 1400,
            margin: '0 auto',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '2rem',
          }}
        >
          <Link
            to="/"
            style={{
              fontWeight: 700,
              fontSize: '1.1rem',
              color: 'var(--text)',
              textDecoration: 'none',
            }}
          >
            Indian Stock Market
          </Link>
          <nav style={{ display: 'flex', gap: '0.5rem' }}>
            {nav.map(({ to, label }) => (
              <Link
                key={to}
                to={to}
                style={{
                  padding: '0.5rem 0.75rem',
                  borderRadius: 'var(--radius)',
                  color: location.pathname === to ? 'var(--accent)' : 'var(--text-muted)',
                  textDecoration: 'none',
                  fontWeight: location.pathname === to ? 600 : 400,
                }}
              >
                {label}
              </Link>
            ))}
          </nav>
        </div>
      </header>
      <main style={{ flex: 1 }}>{children}</main>
    </div>
  )
}
