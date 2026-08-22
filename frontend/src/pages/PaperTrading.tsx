/*
 * pages/PaperTrading.tsx
 *
 * Live paper-trading dashboard. Reads state written by
 * scheduler.paper_trading_scheduler (trades, equity curve, and signal log
 * with is_paper=TRUE) — this page is read-only, no order execution happens
 * anywhere in this app.
 */

import { useCallback, useEffect, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  ReferenceLine,
} from "recharts";
import { api } from "../api/client";
import type { PaperTradingSummary, Trade, EquityPoint, SignalRow } from "../api/types";
import "./PaperTrading.css";

const C = {
  card: "#111820",
  border: "#1e2936",
  text: "#e2e8f0",
  muted: "#94a3b8",
  accent: "#22c55e",
  warn: "#eab308",
  error: "#ef4444",
  blue: "#3b82f6",
};

function EquityTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { value: number }[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="paper-tooltip">
      <div className="paper-tooltip-ts">{label ? new Date(label).toLocaleString("en-IN") : ""}</div>
      <div className="mono">₹{payload[0].value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</div>
    </div>
  );
}

export default function PaperTrading() {
  const [summaries, setSummaries] = useState<PaperTradingSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedStrategyId, setSelectedStrategyId] = useState<number | null>(null);

  const [equityCurve, setEquityCurve] = useState<EquityPoint[]>([]);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [closedTrades, setClosedTrades] = useState<Trade[]>([]);
  const [signals, setSignals] = useState<SignalRow[]>([]);

  const loadSummaries = useCallback(() => {
    api
      .paperTradingSummary()
      .then((r) => {
        const list = r.data ?? [];
        setSummaries(list);
        if (list.length > 0 && selectedStrategyId == null) {
          setSelectedStrategyId(list[0].strategy_id);
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadSummaries();
    // Refresh every 60s — new bars land every 5 min during market hours, but
    // a shorter poll keeps the page feeling live without being wasteful.
    const id = setInterval(loadSummaries, 60_000);
    return () => clearInterval(id);
  }, [loadSummaries]);

  useEffect(() => {
    if (selectedStrategyId == null) return;
    const load = () => {
      Promise.all([
        api.paperEquityCurve(selectedStrategyId),
        api.paperTrades({ strategy_id: selectedStrategyId, status: "open" }),
        api.paperTrades({ strategy_id: selectedStrategyId, status: "closed", limit: 100 }),
        api.paperSignals({ strategy_id: selectedStrategyId, limit: 50 }),
      ])
        .then(([eq, open, closed, sig]) => {
          setEquityCurve(eq.data ?? []);
          setOpenTrades(open.data ?? []);
          setClosedTrades(closed.data ?? []);
          setSignals(sig.data ?? []);
        })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, [selectedStrategyId]);

  const selectedSummary = summaries.find((s) => s.strategy_id === selectedStrategyId) ?? null;
  const equityChartData = equityCurve.map((p) => ({ ts: p.timestamp, Equity: p.equity }));

  if (loading) {
    return (
      <div className="page">
        <div className="loading">Loading paper trading data…</div>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="paper-header">
        <div>
          <h1 className="paper-title">Paper Trading</h1>
          <p className="paper-subtitle">
            Live signal-driven virtual positions · no real orders are placed
          </p>
        </div>
      </div>

      {error && <div className="error-msg">{error}</div>}

      {summaries.length === 0 ? (
        <div className="card" style={{ padding: "2rem", textAlign: "center", color: C.muted }}>
          No paper trading activity yet. The paper-trading-scheduler evaluates
          strategies once per 5-minute bar during market hours — check back
          after the next session opens.
        </div>
      ) : (
        <>
          {/* ── Strategy selector ── */}
          <div className="paper-strategy-tabs">
            {summaries.map((s) => (
              <button
                key={s.strategy_id}
                className={`paper-strategy-tab ${selectedStrategyId === s.strategy_id ? "paper-strategy-tab-active" : ""}`}
                onClick={() => setSelectedStrategyId(s.strategy_id)}
              >
                <span className="mono">{s.strategy_name}</span>
                <span
                  className="mono paper-strategy-tab-pnl"
                  style={{ color: s.total_pnl >= 0 ? C.accent : C.error }}
                >
                  {s.total_pnl >= 0 ? "+" : ""}
                  {s.total_pnl_pct.toFixed(2)}%
                </span>
              </button>
            ))}
          </div>

          {selectedSummary && (
            <>
              <div className="paper-stats-grid">
                <StatCard
                  label="Current Equity"
                  value={`₹${selectedSummary.current_equity.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`}
                  sub={`from ₹${selectedSummary.starting_capital.toLocaleString("en-IN")}`}
                />
                <StatCard
                  label="Total PnL"
                  value={`${selectedSummary.total_pnl >= 0 ? "+" : ""}₹${selectedSummary.total_pnl.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`}
                  color={selectedSummary.total_pnl >= 0 ? C.accent : C.error}
                  sub={`${selectedSummary.total_pnl_pct.toFixed(2)}%`}
                />
                <StatCard
                  label="Win Rate"
                  value={`${selectedSummary.win_rate_pct.toFixed(1)}%`}
                  sub={`${selectedSummary.winning_trades}W / ${selectedSummary.losing_trades}L`}
                />
                <StatCard
                  label="Max Drawdown"
                  value={`${selectedSummary.max_drawdown_pct.toFixed(2)}%`}
                  color={C.warn}
                />
                <StatCard label="Open Positions" value={String(selectedSummary.open_trades)} color={selectedSummary.open_trades > 0 ? C.blue : C.muted} />
              </div>

              <div className="paper-section">
                <div className="paper-section-title">Equity Curve</div>
                <div className="card paper-chart-card">
                  {equityChartData.length === 0 ? (
                    <div className="paper-chart-empty">No equity history yet.</div>
                  ) : (
                    <ResponsiveContainer width="100%" height={260}>
                      <LineChart data={equityChartData} margin={{ top: 8, right: 24, left: 0, bottom: 0 }}>
                        <CartesianGrid stroke={C.border} strokeDasharray="3 3" />
                        <XAxis
                          dataKey="ts"
                          tick={{ fill: C.muted, fontSize: 11 }}
                          axisLine={{ stroke: C.border }}
                          tickLine={false}
                          tickFormatter={(v) => new Date(v).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}
                          interval="preserveStartEnd"
                        />
                        <YAxis
                          tick={{ fill: C.muted, fontSize: 11 }}
                          axisLine={{ stroke: C.border }}
                          tickLine={false}
                          tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}k`}
                          width={56}
                          domain={["auto", "auto"]}
                        />
                        <Tooltip content={<EquityTooltip />} />
                        <ReferenceLine y={selectedSummary.starting_capital} stroke={C.muted} strokeDasharray="4 4" strokeOpacity={0.5} />
                        <Line dataKey="Equity" stroke={C.blue} dot={false} strokeWidth={2} />
                      </LineChart>
                    </ResponsiveContainer>
                  )}
                </div>
              </div>

              {openTrades.length > 0 && (
                <div className="paper-section">
                  <div className="paper-section-title">Open Positions ({openTrades.length})</div>
                  <div className="card" style={{ overflow: "auto" }}>
                    <table className="paper-table">
                      <thead>
                        <tr>
                          <th>Side</th>
                          <th>Symbol</th>
                          <th>Entry</th>
                          <th>Entry Price</th>
                          <th>Qty</th>
                          <th>Reason</th>
                        </tr>
                      </thead>
                      <tbody>
                        {openTrades.map((t) => (
                          <tr key={t.id}>
                            <td><span className={`badge ${t.side === "LONG" ? "badge-success" : "badge-error"}`}>{t.side}</span></td>
                            <td className="mono">{t.symbol}</td>
                            <td className="mono">{new Date(t.entry_time).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
                            <td className="mono">₹{t.entry_price.toFixed(2)}</td>
                            <td className="mono">{t.quantity}</td>
                            <td style={{ color: C.muted, fontSize: "0.8rem" }}>{t.signal_reason ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              <div className="paper-section">
                <div className="paper-section-title">Closed Trades ({closedTrades.length})</div>
                <div className="card" style={{ overflow: "auto" }}>
                  <table className="paper-table">
                    <thead>
                      <tr>
                        <th>Side</th>
                        <th>Symbol</th>
                        <th>Entry</th>
                        <th>Exit</th>
                        <th>Exit Reason</th>
                        <th>PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {closedTrades.map((t) => (
                        <tr key={t.id}>
                          <td><span className={`badge ${t.side === "LONG" ? "badge-success" : "badge-error"}`}>{t.side}</span></td>
                          <td className="mono">{t.symbol}</td>
                          <td className="mono">₹{t.entry_price.toFixed(2)}</td>
                          <td className="mono">{t.exit_price != null ? `₹${t.exit_price.toFixed(2)}` : "—"}</td>
                          <td style={{ color: C.muted, fontSize: "0.8rem" }}>{t.exit_reason ?? "—"}</td>
                          <td className="mono" style={{ color: t.pnl != null ? (t.pnl >= 0 ? C.accent : C.error) : C.muted, fontWeight: 600 }}>
                            {t.pnl != null ? `₹${t.pnl.toLocaleString("en-IN", { maximumFractionDigits: 0 })}` : "—"}
                          </td>
                        </tr>
                      ))}
                      {closedTrades.length === 0 && (
                        <tr>
                          <td colSpan={6} style={{ textAlign: "center", color: C.muted, padding: "1.5rem" }}>
                            No closed trades yet.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="paper-section">
                <div className="paper-section-title">Signal Feed</div>
                <div className="card" style={{ overflow: "auto" }}>
                  <table className="paper-table">
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Symbol</th>
                        <th>Type</th>
                        <th>Reason</th>
                        <th>Acted On</th>
                      </tr>
                    </thead>
                    <tbody>
                      {signals.map((s) => (
                        <tr key={s.id}>
                          <td className="mono">{new Date(s.timestamp).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
                          <td className="mono">{s.symbol ?? "—"}</td>
                          <td>
                            <span
                              className={`badge ${s.signal_type === "entry_long" ? "badge-success" : s.signal_type === "entry_short" ? "badge-error" : "badge-warn"}`}
                            >
                              {s.signal_type}
                            </span>
                          </td>
                          <td style={{ color: C.muted, fontSize: "0.8rem" }}>{s.reason ?? "—"}</td>
                          <td>{s.acted_on ? "✓" : "—"}</td>
                        </tr>
                      ))}
                      {signals.length === 0 && (
                        <tr>
                          <td colSpan={5} style={{ textAlign: "center", color: C.muted, padding: "1.5rem" }}>
                            No signals logged yet.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

function StatCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="card paper-stat-card">
      <div className="paper-stat-label">{label}</div>
      <div className="mono" style={{ color: color ?? C.text, fontSize: "1.4rem", fontWeight: 700, lineHeight: 1.2 }}>
        {value}
      </div>
      {sub && <div className="paper-stat-sub">{sub}</div>}
    </div>
  );
}
