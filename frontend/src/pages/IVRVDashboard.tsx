/*
 * pages/IVRVDashboard.tsx
 *
 * IV vs RV Dashboard — shows IV smile, IV time series,
 * and RV overlay for Nifty options.
 *
 * Auto-detects ATM strike on load, lets user pick nearby strikes.
 */

import { useEffect, useState, useCallback } from "react";
import {
    LineChart,
    Line,
    XAxis,
    YAxis,
    Tooltip,
    ResponsiveContainer,
    CartesianGrid,
    Legend,
    ReferenceLine,
} from "recharts";
import { api } from "../api/client";
import type {
    ATMInfo,
    IVChainData,
    IVHistoryData,
} from "../api/types";
import "./IVRVDashboard.css";

// ── Colour tokens ────────────────────────────────────────────────────────────
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
    purple: "#a855f7",
    cyan: "#06b6d4",
    orange: "#f97316",
};

// ── Tooltip ──────────────────────────────────────────────────────────────────
function IVTooltip({
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
        <div className="ivrv-tooltip">
            <div className="ivrv-tooltip-ts">{label}</div>
            {payload.map((p) => (
                <div key={p.name} className="ivrv-tooltip-row">
                    <span style={{ color: p.color }}>{p.name}</span>
                    <span className="mono">
                        {p.value != null ? `${p.value.toFixed(2)}%` : "—"}
                    </span>
                </div>
            ))}
        </div>
    );
}

// ── Main Component ───────────────────────────────────────────────────────────
export default function IVRVDashboard() {
    // ATM / control state
    const [atm, setAtm] = useState<ATMInfo | null>(null);
    const [selectedExpiry, setSelectedExpiry] = useState<string>("");
    const [selectedStrike, setSelectedStrike] = useState<number | null>(null);
    const [optType, setOptType] = useState<"CE" | "PE">("CE");

    // Data state
    const [chain, setChain] = useState<IVChainData | null>(null);
    const [ivHistory, setIvHistory] = useState<IVHistoryData | null>(null);

    // Loading
    const [loadingATM, setLoadingATM] = useState(true);
    const [loadingChain, setLoadingChain] = useState(false);
    const [loadingHistory, setLoadingHistory] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // ── Load ATM info on mount ──────────────────────────────────────────
    useEffect(() => {
        setLoadingATM(true);
        setError(null);
        api
            .volatilityATM()
            .then((r) => {
                if (r.data) {
                    setAtm(r.data);
                    if (r.data.expiries.length > 0) setSelectedExpiry(r.data.expiries[0]);
                    setSelectedStrike(r.data.atm_strike);
                }
            })
            .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
            .finally(() => setLoadingATM(false));
    }, []);

    // ── Load IV chain when expiry changes ───────────────────────────────
    const loadChain = useCallback(() => {
        if (!selectedExpiry) return;
        setLoadingChain(true);
        api
            .volatilityChain(selectedExpiry)
            .then((r) => {
                if (r.data) setChain(r.data);
            })
            .catch(() => { })
            .finally(() => setLoadingChain(false));
    }, [selectedExpiry]);

    useEffect(() => {
        loadChain();
    }, [loadChain]);

    // ── Load IV history when strike/expiry/optType changes ──────────────
    useEffect(() => {
        if (!chain || selectedStrike == null) return;

        // Find the symbol for this strike/optType in the chain
        const row = chain.chain.find((r) => r.strike === selectedStrike);
        if (!row) return;
        const sym = optType === "CE" ? row.ce_symbol : row.pe_symbol;
        if (!sym) return;

        setLoadingHistory(true);
        api
            .volatilityIVHistory(sym)
            .then((r) => {
                if (r.data) setIvHistory(r.data);
            })
            .catch(() => { })
            .finally(() => setLoadingHistory(false));
    }, [chain, selectedStrike, optType]);

    // ── Derived values ──────────────────────────────────────────────────
    const atmStrike = atm?.atm_strike ?? null;

    // Nearby strikes (ATM ± 10 strikes = ±500 pts for Nifty)
    const nearbyStrikes = (chain?.chain ?? [])
        .filter((r) => {
            if (atmStrike == null) return true;
            return Math.abs(r.strike - atmStrike) <= 500;
        })
        .map((r) => r.strike);

    // Selected row in chain
    const selectedRow = chain?.chain.find((r) => r.strike === selectedStrike);
    const currentIV =
        optType === "CE" ? selectedRow?.ce_iv : selectedRow?.pe_iv;

    // IV smile data for chart
    const smileData = (chain?.chain ?? [])
        .filter((r) => {
            if (atmStrike == null) return true;
            return Math.abs(r.strike - (atmStrike ?? 0)) <= 1000;
        })
        .map((r) => ({
            strike: r.strike,
            "Call IV": r.ce_iv,
            "Put IV": r.pe_iv,
        }));

    // IV history for chart
    const historyChartData = (ivHistory?.series ?? []).map((p) => ({
        ts: new Date(p.timestamp).toLocaleDateString("en-IN", {
            month: "short",
            day: "numeric",
        }),
        "IV %": p.iv_pct,
    }));

    // ── Render ──────────────────────────────────────────────────────────
    if (loadingATM) {
        return (
            <div className="page">
                <div className="loading">Loading ATM data…</div>
            </div>
        );
    }

    if (error) {
        return (
            <div className="page">
                <div className="error-msg">{error}</div>
            </div>
        );
    }

    return (
        <div className="page">
            {/* ── Header ── */}
            <div className="ivrv-header">
                <div>
                    <h1 className="ivrv-title">IV vs RV Dashboard</h1>
                    <p className="ivrv-subtitle">
                        Implied Volatility · Realized Volatility · Nifty Options
                    </p>
                </div>
                {atm && (
                    <div className="ivrv-header-badge">
                        <span style={{ color: C.cyan }}>◆</span>
                        <span style={{ color: C.muted }}>Underlying</span>
                        <span
                            className="mono"
                            style={{ color: C.text, fontSize: "1.1rem", fontWeight: 700 }}
                        >
                            ₹
                            {atm.underlying.toLocaleString("en-IN", {
                                minimumFractionDigits: 2,
                            })}
                        </span>
                    </div>
                )}
            </div>

            {/* ── Controls ── */}
            <div className="card ivrv-controls">
                {/* Expiry selector */}
                <div className="ivrv-control-group">
                    <label className="ivrv-control-label">Expiry</label>
                    <select
                        className="input ivrv-select"
                        value={selectedExpiry}
                        onChange={(e) => setSelectedExpiry(e.target.value)}
                    >
                        {(atm?.expiries ?? []).map((exp) => (
                            <option key={exp} value={exp}>
                                {new Date(exp).toLocaleDateString("en-IN", {
                                    day: "numeric",
                                    month: "short",
                                    year: "numeric",
                                })}
                            </option>
                        ))}
                    </select>
                </div>

                {/* CE/PE toggle */}
                <div className="ivrv-control-group">
                    <label className="ivrv-control-label">Option Type</label>
                    <div className="ivrv-pill-group">
                        <button
                            className={`ivrv-pill ${optType === "CE" ? "ivrv-pill-active" : ""}`}
                            onClick={() => setOptType("CE")}
                        >
                            Call (CE)
                        </button>
                        <button
                            className={`ivrv-pill ${optType === "PE" ? "ivrv-pill-active" : ""}`}
                            onClick={() => setOptType("PE")}
                        >
                            Put (PE)
                        </button>
                    </div>
                </div>

                {/* Strike selector */}
                <div className="ivrv-control-group" style={{ flex: 1, minWidth: 200 }}>
                    <label className="ivrv-control-label">
                        Strike (ATM: ₹{atmStrike?.toLocaleString("en-IN") ?? "—"})
                    </label>
                    <div className="ivrv-strike-scroll">
                        {nearbyStrikes.map((s) => (
                            <button
                                key={s}
                                className={`ivrv-pill ${selectedStrike === s ? "ivrv-pill-active" : ""
                                    } ${s === atmStrike ? "ivrv-pill-atm" : ""}`}
                                onClick={() => setSelectedStrike(s)}
                            >
                                {s.toLocaleString("en-IN")}
                            </button>
                        ))}
                    </div>
                </div>
            </div>

            {/* ── Stat Cards ── */}
            <div className="ivrv-stats-grid">
                <div className="card ivrv-stat-card">
                    <div className="ivrv-stat-label">
                        {optType} IV @ ₹{selectedStrike?.toLocaleString("en-IN") ?? "—"}
                    </div>
                    <div
                        className="ivrv-stat-value mono"
                        style={{ color: currentIV != null ? C.blue : C.muted }}
                    >
                        {currentIV != null ? `${currentIV.toFixed(2)}%` : "—"}
                    </div>
                </div>
                <div className="card ivrv-stat-card">
                    <div className="ivrv-stat-label">ATM Strike</div>
                    <div className="ivrv-stat-value mono" style={{ color: C.accent }}>
                        ₹{atmStrike?.toLocaleString("en-IN") ?? "—"}
                    </div>
                </div>
                <div className="card ivrv-stat-card">
                    <div className="ivrv-stat-label">Expiry</div>
                    <div className="ivrv-stat-value mono" style={{ color: C.warn }}>
                        {selectedExpiry
                            ? new Date(selectedExpiry).toLocaleDateString("en-IN", {
                                day: "numeric",
                                month: "short",
                            })
                            : "—"}
                    </div>
                    {chain && (
                        <div className="ivrv-stat-sub">
                            T = {(chain.time_to_expiry_years * 365).toFixed(0)} days
                        </div>
                    )}
                </div>
                <div className="card ivrv-stat-card">
                    <div className="ivrv-stat-label">Underlying</div>
                    <div className="ivrv-stat-value mono" style={{ color: C.text }}>
                        ₹{atm?.underlying.toLocaleString("en-IN", { minimumFractionDigits: 2 }) ?? "—"}
                    </div>
                    <div className="ivrv-stat-sub">{atm?.futures_symbol}</div>
                </div>
            </div>

            {/* ── IV Smile Chart ── */}
            <div className="ivrv-section">
                <div className="ivrv-section-head">
                    <h2 className="ivrv-section-title">IV Smile</h2>
                    <span className="ivrv-section-sub">
                        Call &amp; Put IV across strikes ·{" "}
                        {selectedExpiry
                            ? new Date(selectedExpiry).toLocaleDateString("en-IN", {
                                day: "numeric",
                                month: "short",
                                year: "numeric",
                            })
                            : ""}
                    </span>
                </div>
                <div className="card ivrv-chart-card">
                    {loadingChain ? (
                        <div className="ivrv-chart-loading">Loading IV chain…</div>
                    ) : smileData.length === 0 ? (
                        <div className="ivrv-chart-empty">
                            No IV data available for this expiry. Run the OHLCV downloader for
                            options first.
                        </div>
                    ) : (
                        <ResponsiveContainer width="100%" height={320}>
                            <LineChart
                                data={smileData}
                                margin={{ top: 8, right: 24, left: 0, bottom: 0 }}
                            >
                                <CartesianGrid stroke={C.border} strokeDasharray="3 3" />
                                <XAxis
                                    dataKey="strike"
                                    tick={{ fill: C.muted, fontSize: 11 }}
                                    axisLine={{ stroke: C.border }}
                                    tickLine={false}
                                    tickFormatter={(v) => `${(v / 1000).toFixed(1)}k`}
                                />
                                <YAxis
                                    tick={{ fill: C.muted, fontSize: 11 }}
                                    axisLine={{ stroke: C.border }}
                                    tickLine={false}
                                    tickFormatter={(v) => `${v}%`}
                                    width={52}
                                />
                                <Tooltip content={<IVTooltip />} />
                                <Legend wrapperStyle={{ color: C.muted, fontSize: 12 }} />
                                {atmStrike && (
                                    <ReferenceLine
                                        x={atmStrike}
                                        stroke={C.accent}
                                        strokeDasharray="4 4"
                                        strokeOpacity={0.6}
                                        label={{
                                            value: "ATM",
                                            fill: C.accent,
                                            fontSize: 10,
                                            position: "top",
                                        }}
                                    />
                                )}
                                <Line
                                    dataKey="Call IV"
                                    stroke={C.blue}
                                    dot={false}
                                    strokeWidth={2}
                                    connectNulls
                                />
                                <Line
                                    dataKey="Put IV"
                                    stroke={C.orange}
                                    dot={false}
                                    strokeWidth={2}
                                    connectNulls
                                />
                            </LineChart>
                        </ResponsiveContainer>
                    )}
                </div>
            </div>

            {/* ── IV History Chart ── */}
            <div className="ivrv-section">
                <div className="ivrv-section-head">
                    <h2 className="ivrv-section-title">
                        IV Time Series — {optType} ₹
                        {selectedStrike?.toLocaleString("en-IN") ?? "—"}
                    </h2>
                    <span className="ivrv-section-sub">
                        {ivHistory?.symbol ?? "Select a strike"}
                    </span>
                </div>
                <div className="card ivrv-chart-card">
                    {loadingHistory ? (
                        <div className="ivrv-chart-loading">Loading IV history…</div>
                    ) : historyChartData.length === 0 ? (
                        <div className="ivrv-chart-empty">
                            {selectedStrike
                                ? "No IV history data for this strike. The option may have insufficient trading data."
                                : "Select a strike to view IV history."}
                        </div>
                    ) : (
                        <ResponsiveContainer width="100%" height={300}>
                            <LineChart
                                data={historyChartData}
                                margin={{ top: 8, right: 24, left: 0, bottom: 0 }}
                            >
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
                                    tickFormatter={(v) => `${v}%`}
                                    width={52}
                                />
                                <Tooltip content={<IVTooltip />} />
                                <Line
                                    dataKey="IV %"
                                    stroke={optType === "CE" ? C.blue : C.orange}
                                    dot={false}
                                    strokeWidth={2}
                                    activeDot={{ r: 4 }}
                                />
                            </LineChart>
                        </ResponsiveContainer>
                    )}
                </div>
            </div>

            {/* ── IV Chain Table ── */}
            {chain && chain.chain.length > 0 && (
                <div className="ivrv-section">
                    <div className="ivrv-section-head">
                        <h2 className="ivrv-section-title">Option Chain — IV</h2>
                        <span className="ivrv-section-sub">
                            {chain.chain.length} strikes · Expiry{" "}
                            {new Date(selectedExpiry).toLocaleDateString("en-IN")}
                        </span>
                    </div>
                    <div className="card" style={{ overflow: "auto" }}>
                        <table className="ivrv-chain-table">
                            <thead>
                                <tr>
                                    <th>CE IV</th>
                                    <th>CE Price</th>
                                    <th>CE Vol</th>
                                    <th style={{ textAlign: "center" }}>Strike</th>
                                    <th>PE IV</th>
                                    <th>PE Price</th>
                                    <th>PE Vol</th>
                                </tr>
                            </thead>
                            <tbody>
                                {chain.chain
                                    .filter((r) => {
                                        if (atmStrike == null) return true;
                                        return Math.abs(r.strike - atmStrike) <= 1000;
                                    })
                                    .map((r) => (
                                        <tr
                                            key={r.strike}
                                            className={`${r.strike === atmStrike ? "ivrv-chain-row-atm" : ""
                                                } ${r.strike === selectedStrike
                                                    ? "ivrv-chain-row-selected"
                                                    : ""
                                                }`}
                                            style={{ cursor: "pointer" }}
                                            onClick={() => setSelectedStrike(r.strike)}
                                        >
                                            <td
                                                className="mono"
                                                style={{
                                                    color: r.ce_iv != null ? C.blue : C.muted,
                                                }}
                                            >
                                                {r.ce_iv != null ? `${r.ce_iv.toFixed(2)}%` : "—"}
                                            </td>
                                            <td className="mono" style={{ color: C.text }}>
                                                {r.ce_close != null
                                                    ? `₹${r.ce_close.toFixed(2)}`
                                                    : "—"}
                                            </td>
                                            <td className="mono" style={{ color: C.muted }}>
                                                {r.ce_volume != null
                                                    ? r.ce_volume.toLocaleString()
                                                    : "—"}
                                            </td>
                                            <td
                                                className="mono"
                                                style={{
                                                    textAlign: "center",
                                                    fontWeight: 700,
                                                    color:
                                                        r.strike === atmStrike ? C.accent : C.text,
                                                }}
                                            >
                                                ₹{r.strike.toLocaleString("en-IN")}
                                                {r.strike === atmStrike && (
                                                    <span
                                                        className="badge badge-success"
                                                        style={{
                                                            marginLeft: 6,
                                                            fontSize: "0.6rem",
                                                            verticalAlign: "middle",
                                                        }}
                                                    >
                                                        ATM
                                                    </span>
                                                )}
                                            </td>
                                            <td
                                                className="mono"
                                                style={{
                                                    color: r.pe_iv != null ? C.orange : C.muted,
                                                }}
                                            >
                                                {r.pe_iv != null ? `${r.pe_iv.toFixed(2)}%` : "—"}
                                            </td>
                                            <td className="mono" style={{ color: C.text }}>
                                                {r.pe_close != null
                                                    ? `₹${r.pe_close.toFixed(2)}`
                                                    : "—"}
                                            </td>
                                            <td className="mono" style={{ color: C.muted }}>
                                                {r.pe_volume != null
                                                    ? r.pe_volume.toLocaleString()
                                                    : "—"}
                                            </td>
                                        </tr>
                                    ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}
        </div>
    );
}
