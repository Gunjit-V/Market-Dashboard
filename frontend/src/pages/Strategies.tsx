/*
 * pages/Strategies.tsx
 *
 * Strategy backtest lab. Lists registered strategies (created by running a
 * backtest), lets you kick off a new rv_breakout or vrp_reversion backtest,
 * and shows results — equity curve, trade log, summary stats — for the
 * selected run.
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
import type { Strategy, BacktestRun, EquityPoint, Trade } from "../api/types";
import "./Strategies.css";

const C = {
  bg: "#0b0f14",
  card: "#111820",
  border: "#1e2936",
  text: "#e2e8f0",
  muted: "#94a3b8",
  accent: "#22c55e",
  warn: "#eab308",
  error: "#ef4444",
  blue: "#3b82f6",
};

function StatTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { name: string; value: number; color: string }[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="strat-tooltip">
      <div className="strat-tooltip-ts">
        {label ? new Date(label).toLocaleString("en-IN") : ""}
      </div>
      {payload.map((p) => (
        <div key={p.name} className="strat-tooltip-row">
          <span style={{ color: p.color }}>{p.name}</span>
          <span className="mono">
            {p.value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}
          </span>
        </div>
      ))}
    </div>
  );
}

const STRATEGY_PRESETS: { value: "rv_breakout" | "vrp_reversion"; label: string; hint: string }[] = [
  { value: "rv_breakout", label: "RV Breakout", hint: "Trades Nifty 50 when short-window RV expands past a long-window baseline." },
  { value: "vrp_reversion", label: "VRP Reversion", hint: "Sells/buys option premium when IV diverges from realized vol." },
];

export default function Strategies() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loadingStrategies, setLoadingStrategies] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedStrategyId, setSelectedStrategyId] = useState<number | null>(null);
  const [backtests, setBacktests] = useState<BacktestRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [equityCurve, setEquityCurve] = useState<EquityPoint[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loadingRun, setLoadingRun] = useState(false);

  // New backtest form
  const [strategyType, setStrategyType] = useState<"rv_breakout" | "vrp_reversion">("rv_breakout");
  const [symbol, setSymbol] = useState("Nifty 50");
  const [optionSymbol, setOptionSymbol] = useState("");
  const [underlyingSymbol, setUnderlyingSymbol] = useState("NIFTY25AUG26FUT");
  const [submitting, setSubmitting] = useState(false);
  const [submitMessage, setSubmitMessage] = useState<string | null>(null);

  const loadStrategies = useCallback(() => {
    setLoadingStrategies(true);
    api
      .strategies()
      .then((r) => {
        const list = r.data ?? [];
        setStrategies(list);
        if (list.length > 0 && selectedStrategyId == null) {
          setSelectedStrategyId(list[0].id);
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoadingStrategies(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadStrategies();
  }, [loadStrategies]);

  // Load backtests for the selected strategy
  useEffect(() => {
    if (selectedStrategyId == null) return;
    api
      .strategyBacktests(selectedStrategyId)
      .then((r) => {
        const runs = r.data ?? [];
        setBacktests(runs);
        setSelectedRunId(runs.length > 0 ? runs[0].id : null);
      })
      .catch(() => setBacktests([]));
  }, [selectedStrategyId]);

  // Load equity curve + trades for the selected run
  useEffect(() => {
    if (selectedRunId == null) {
      setEquityCurve([]);
      setTrades([]);
      return;
    }
    setLoadingRun(true);
    Promise.all([
      api.backtestEquityCurve(selectedRunId),
      api.backtestTrades(selectedRunId),
    ])
      .then(([eq, tr]) => {
        setEquityCurve(eq.data ?? []);
        setTrades(tr.data ?? []);
      })
      .catch(() => {
        setEquityCurve([]);
        setTrades([]);
      })
      .finally(() => setLoadingRun(false));
  }, [selectedRunId]);

  // Poll while a backtest is running, so the user sees it complete without
  // manually refreshing.
  useEffect(() => {
    const hasRunning = backtests.some((b) => b.status === "running");
    if (!hasRunning || selectedStrategyId == null) return;
    const id = setInterval(() => {
      api.strategyBacktests(selectedStrategyId).then((r) => setBacktests(r.data ?? []));
    }, 5000);
    return () => clearInterval(id);
  }, [backtests, selectedStrategyId]);

  const handleRunBacktest = () => {
    setSubmitting(true);
    setSubmitMessage(null);
    const body =
      strategyType === "rv_breakout"
        ? { strategy: "rv_breakout" as const, symbol }
        : { strategy: "vrp_reversion" as const, option_symbol: optionSymbol, underlying_symbol: underlyingSymbol };

    api
      .runBacktest(body)
      .then((r) => {
        setSubmitMessage(r.message ?? "Backtest started.");
        // Refresh strategy list after a short delay — get_or_create_strategy
        // runs before the heavy simulation, so the strategy row should
        // exist quickly even though results take longer.
        setTimeout(loadStrategies, 1500);
      })
      .catch((e) => setSubmitMessage(e instanceof Error ? e.message : "Failed to start backtest"))
      .finally(() => setSubmitting(false));
  };

  const selectedRun = backtests.find((b) => b.id === selectedRunId) ?? null;

  const equityChartData = equityCurve.map((p) => ({
    ts: p.timestamp,
    Equity: p.equity,
  }));

  return (
    <div className="page">
      <div className="strat-header">
        <div>
          <h1 className="strat-title">Strategy Lab</h1>
          <p className="strat-subtitle">
            Backtest RV / VRP signal strategies against historical OHLCV data
          </p>
        </div>
      </div>

      {/* ── New backtest form ── */}
      <div className="card strat-form">
        <div className="strat-form-row">
          <div className="strat-control-group">
            <label className="strat-control-label">Strategy</label>
            <div className="strat-pill-group">
              {STRATEGY_PRESETS.map((p) => (
                <button
                  key={p.value}
                  className={`strat-pill ${strategyType === p.value ? "strat-pill-active" : ""}`}
                  onClick={() => setStrategyType(p.value)}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          {strategyType === "rv_breakout" ? (
            <div className="strat-control-group">
              <label className="strat-control-label">Symbol</label>
              <input
                className="input"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="Nifty 50"
              />
            </div>
          ) : (
            <>
              <div className="strat-control-group">
                <label className="strat-control-label">Option Symbol</label>
                <input
                  className="input"
                  value={optionSymbol}
                  onChange={(e) => setOptionSymbol(e.target.value)}
                  placeholder="NIFTY25AUG2624300CE"
                  style={{ minWidth: 220 }}
                />
              </div>
              <div className="strat-control-group">
                <label className="strat-control-label">Underlying</label>
                <input
                  className="input"
                  value={underlyingSymbol}
                  onChange={(e) => setUnderlyingSymbol(e.target.value)}
                  placeholder="NIFTY25AUG26FUT"
                />
              </div>
            </>
          )}

          <button className="btn btn-primary" onClick={handleRunBacktest} disabled={submitting}>
            {submitting ? "Starting…" : "Run Backtest"}
          </button>
        </div>
        <div className="strat-method-desc">
          {STRATEGY_PRESETS.find((p) => p.value === strategyType)?.hint}
        </div>
        {submitMessage && <div className="strat-submit-msg">{submitMessage}</div>}
      </div>

      {error && <div className="error-msg">{error}</div>}

      <div className="strat-layout">
        {/* ── Strategy list ── */}
        <div className="card strat-sidebar">
          <div className="strat-sidebar-title">Strategies</div>
          {loadingStrategies ? (
            <div className="loading">Loading…</div>
          ) : strategies.length === 0 ? (
            <div className="strat-empty">No strategies yet — run a backtest to create one.</div>
          ) : (
            <div className="strat-list">
              {strategies.map((s) => (
                <button
                  key={s.id}
                  className={`strat-list-item ${selectedStrategyId === s.id ? "strat-list-item-active" : ""}`}
                  onClick={() => setSelectedStrategyId(s.id)}
                >
                  <span className="mono">{s.name}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ── Backtest runs + results ── */}
        <div className="strat-main">
          {backtests.length > 0 && (
            <div className="strat-runs-scroll">
              {backtests.map((b) => (
                <button
                  key={b.id}
                  className={`strat-run-pill ${selectedRunId === b.id ? "strat-run-pill-active" : ""}`}
                  onClick={() => setSelectedRunId(b.id)}
                >
                  <span
                    className="strat-run-status-dot"
                    style={{
                      background:
                        b.status === "completed" ? C.accent : b.status === "failed" ? C.error : C.warn,
                    }}
                  />
                  {new Date(b.started_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}
                </button>
              ))}
            </div>
          )}

          {selectedRun && (
            <>
              {selectedRun.status === "failed" && (
                <div className="error-msg" style={{ marginBottom: "1rem" }}>
                  Backtest failed: {selectedRun.error_message}
                </div>
              )}
              {selectedRun.status === "running" && (
                <div className="strat-running-banner">Backtest running…</div>
              )}

              <div className="strat-stats-grid">
                <StatCard
                  label="Total PnL"
                  value={selectedRun.total_pnl != null ? `₹${selectedRun.total_pnl.toLocaleString("en-IN", { maximumFractionDigits: 0 })}` : "—"}
                  color={selectedRun.total_pnl != null ? (selectedRun.total_pnl >= 0 ? C.accent : C.error) : C.muted}
                />
                <StatCard
                  label="Win Rate"
                  value={selectedRun.win_rate_pct != null ? `${selectedRun.win_rate_pct.toFixed(1)}%` : "—"}
                  sub={`${selectedRun.winning_trades}W / ${selectedRun.losing_trades}L`}
                />
                <StatCard
                  label="Sharpe Ratio"
                  value={selectedRun.sharpe_ratio != null ? selectedRun.sharpe_ratio.toFixed(2) : "—"}
                  color={selectedRun.sharpe_ratio != null ? (selectedRun.sharpe_ratio >= 0 ? C.accent : C.error) : C.muted}
                />
                <StatCard
                  label="Max Drawdown"
                  value={selectedRun.max_drawdown_pct != null ? `${selectedRun.max_drawdown_pct.toFixed(2)}%` : "—"}
                  color={C.warn}
                />
                <StatCard label="Total Trades" value={String(selectedRun.total_trades)} />
                <StatCard
                  label="Period"
                  value={`${new Date(selectedRun.from_date).toLocaleDateString("en-IN")} – ${new Date(selectedRun.to_date).toLocaleDateString("en-IN")}`}
                  small
                />
              </div>

              <div className="strat-section">
                <div className="strat-section-title">Equity Curve</div>
                <div className="card strat-chart-card">
                  {loadingRun ? (
                    <div className="strat-chart-loading">Loading…</div>
                  ) : equityChartData.length === 0 ? (
                    <div className="strat-chart-empty">No equity data for this run.</div>
                  ) : (
                    <ResponsiveContainer width="100%" height={280}>
                      <LineChart data={equityChartData} margin={{ top: 8, right: 24, left: 0, bottom: 0 }}>
                        <CartesianGrid stroke={C.border} strokeDasharray="3 3" />
                        <XAxis
                          dataKey="ts"
                          tick={{ fill: C.muted, fontSize: 11 }}
                          axisLine={{ stroke: C.border }}
                          tickLine={false}
                          tickFormatter={(v) => new Date(v).toLocaleDateString("en-IN", { month: "short", year: "2-digit" })}
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
                        <Tooltip content={<StatTooltip />} />
                        <ReferenceLine y={selectedRun.starting_capital} stroke={C.muted} strokeDasharray="4 4" strokeOpacity={0.5} />
                        <Line dataKey="Equity" stroke={C.blue} dot={false} strokeWidth={2} />
                      </LineChart>
                    </ResponsiveContainer>
                  )}
                </div>
              </div>

              <div className="strat-section">
                <div className="strat-section-title">Trades ({trades.length})</div>
                <div className="card" style={{ overflow: "auto" }}>
                  <table className="strat-trade-table">
                    <thead>
                      <tr>
                        <th>Side</th>
                        <th>Symbol</th>
                        <th>Entry</th>
                        <th>Entry Price</th>
                        <th>Exit</th>
                        <th>Exit Price</th>
                        <th>Reason</th>
                        <th>PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {trades.map((t) => (
                        <tr key={t.id}>
                          <td>
                            <span className={`badge ${t.side === "LONG" ? "badge-success" : "badge-error"}`}>
                              {t.side}
                            </span>
                          </td>
                          <td className="mono">{t.symbol}</td>
                          <td className="mono">{new Date(t.entry_time).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
                          <td className="mono">₹{t.entry_price.toFixed(2)}</td>
                          <td className="mono">
                            {t.exit_time
                              ? new Date(t.exit_time).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
                              : "—"}
                          </td>
                          <td className="mono">{t.exit_price != null ? `₹${t.exit_price.toFixed(2)}` : "—"}</td>
                          <td style={{ color: C.muted, fontSize: "0.8rem" }}>{t.exit_reason ?? t.signal_reason ?? "—"}</td>
                          <td className="mono" style={{ color: t.pnl != null ? (t.pnl >= 0 ? C.accent : C.error) : C.muted, fontWeight: 600 }}>
                            {t.pnl != null ? `₹${t.pnl.toLocaleString("en-IN", { maximumFractionDigits: 0 })}` : "—"}
                          </td>
                        </tr>
                      ))}
                      {trades.length === 0 && (
                        <tr>
                          <td colSpan={8} style={{ textAlign: "center", color: C.muted, padding: "1.5rem" }}>
                            No trades in this run.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}

          {!selectedRun && backtests.length === 0 && selectedStrategyId != null && (
            <div className="strat-empty" style={{ marginTop: "1rem" }}>
              No backtest runs for this strategy yet.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  color,
  small,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
  small?: boolean;
}) {
  return (
    <div className="card strat-stat-card">
      <div className="strat-stat-label">{label}</div>
      <div
        className="mono"
        style={{
          color: color ?? C.text,
          fontSize: small ? "0.95rem" : "1.4rem",
          fontWeight: 700,
          lineHeight: 1.2,
        }}
      >
        {value}
      </div>
      {sub && <div className="strat-stat-sub">{sub}</div>}
    </div>
  );
}
