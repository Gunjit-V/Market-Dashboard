/**
 * Candlestick chart using TradingView’s Lightweight Charts (https://github.com/tradingview/lightweight-charts).
 * Time axis is formatted in IST (Asia/Kolkata).
 */
import { useEffect, useRef, useState } from 'react'
import { createChart, TickMarkType } from 'lightweight-charts'
import type { OHLCVCandle } from '../api/types'

const IST_TIMEZONE = 'Asia/Kolkata'

/** Format a UTC Unix timestamp (seconds) as IST for the chart time axis. Keep labels short (≤8 chars) per library docs. */
function formatTickMarkIST(time: unknown, tickMarkType: TickMarkType): string {
  const sec = typeof time === 'number' ? time : 0
  const d = new Date(sec * 1000)
  const opts: Intl.DateTimeFormatOptions = { timeZone: IST_TIMEZONE }
  switch (tickMarkType) {
    case TickMarkType.Year:
      return d.toLocaleString('en-IN', { ...opts, year: 'numeric' })
    case TickMarkType.Month:
      return d.toLocaleString('en-IN', { ...opts, month: 'short' })
    case TickMarkType.DayOfMonth:
      return d.toLocaleString('en-IN', { ...opts, day: 'numeric' })
    case TickMarkType.Time:
      return d.toLocaleString('en-IN', { ...opts, hour: '2-digit', minute: '2-digit', hour12: false })
    case TickMarkType.TimeWithSeconds:
      return d.toLocaleString('en-IN', { ...opts, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
    default:
      return d.toLocaleString('en-IN', { ...opts, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
  }
}

// Hex colors so the chart library (canvas) can use them; it doesn't resolve CSS variables
const CHART_THEME = {
  background: '#111820',
  text: '#94a3b8',
  border: '#1e2936',
  up: '#22c55e',
  down: '#ef4444',
}

interface ChartCandle {
  time: number
  open: number
  high: number
  low: number
  close: number
}

function candleToChart(c: OHLCVCandle): ChartCandle {
  const t = new Date(c.timestamp).getTime()
  return {
    time: Math.floor(t / 1000),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  }
}

interface CandlestickChartProps {
  candles: OHLCVCandle[]
  height?: number
}

export default function CandlestickChart({ candles, height = 360 }: CandlestickChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ReturnType<typeof createChart> | null>(null)
  const seriesRef = useRef<{ setData: (data: ChartCandle[]) => void } | null>(null)
  const [chartError, setChartError] = useState<string | null>(null)

  // Create chart once when we have a container; resize observer
  useEffect(() => {
    if (!containerRef.current || candles.length === 0) return
    const container = containerRef.current
    setChartError(null)
    let chart: ReturnType<typeof createChart>
    try {
      chart = createChart(container, {
        layout: {
          background: { type: 'solid', color: CHART_THEME.background },
          textColor: CHART_THEME.text,
          fontFamily: 'DM Sans, system-ui, sans-serif',
          fontSize: 12,
        },
        grid: {
          vertLines: { color: CHART_THEME.border },
          horzLines: { color: CHART_THEME.border },
        },
        rightPriceScale: {
          borderColor: CHART_THEME.border,
          scaleMargins: { top: 0.1, bottom: 0.2 },
        },
        timeScale: {
          borderColor: CHART_THEME.border,
          timeVisible: true,
          secondsVisible: true,
          tickMarkFormatter: (time: unknown, tickMarkType: TickMarkType) => formatTickMarkIST(time, tickMarkType),
        },
        crosshair: {
          vertLine: { color: CHART_THEME.text },
          horzLine: { color: CHART_THEME.text },
        },
        width: container.clientWidth || 800,
        height,
      })

      const candlestickSeries = chart.addCandlestickSeries({
        upColor: CHART_THEME.up,
        downColor: CHART_THEME.down,
        borderDownColor: CHART_THEME.down,
        borderUpColor: CHART_THEME.up,
        wickDownColor: CHART_THEME.down,
        wickUpColor: CHART_THEME.up,
      })

      chartRef.current = chart
      seriesRef.current = candlestickSeries

      const data = candles.map(candleToChart)
      candlestickSeries.setData(data)
      chart.timeScale().fitContent()

      const resizeObserver = new ResizeObserver((entries) => {
        const entry = entries[0]
        if (entry?.contentRect?.width) chart.applyOptions({ width: entry.contentRect.width })
      })
      resizeObserver.observe(container)

      return () => {
        resizeObserver.disconnect()
        chart.remove()
        chartRef.current = null
        seriesRef.current = null
      }
    } catch (err) {
      setChartError(err instanceof Error ? err.message : 'Chart failed to load')
      return undefined
    }
  }, [height]) // re-run if height changes; candles applied in update effect

  // Update data when candles change (same chart instance)
  useEffect(() => {
    if (!seriesRef.current || !chartRef.current || candles.length === 0) return
    const data = candles.map(candleToChart)
    seriesRef.current.setData(data)
    chartRef.current.timeScale().fitContent()
  }, [candles])

  if (candles.length === 0) return null

  if (chartError) {
    return (
      <div className="candlestick-chart candlestick-chart-error" style={{ height: `${height}px`, width: '100%' }}>
        <span style={{ color: 'var(--text-muted)' }}>Chart: {chartError}</span>
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      className="candlestick-chart"
      style={{ height: `${height}px`, width: '100%', minWidth: 200 }}
    />
  )
}
