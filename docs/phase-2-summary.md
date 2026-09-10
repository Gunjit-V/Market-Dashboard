# Phase 2 — Market State & Feature Engineering

Branch: `phase-2`
Baseline: `6af05b8` on `master`.

Phase 2 turns the validated market data of Phase 1 into an ML-ready
representation: a reproducible, point-in-time-safe `feature_vector(t)`.
`master` was not modified, and no Phase 1 file was changed.

Specification: [`Phase-2.md`](Phase-2.md).
Checkpoint 2A is recorded separately in
[`phase-2a-summary.md`](phase-2a-summary.md).

---

## The exit criterion

> A versioned feature dataset exists where `timestamp → feature_vector(timestamp)`
> is reproducible and point-in-time safe.

**Met.** Two datasets, both versioned and both verified against the live
one-at-a-time path:

| Timeframe | Rows | Sessions | Size | Version | Features valid | Labels valid |
|---|---|---|---|---|---|---|
| 5m | 52,200 | 696 | 10.7 MB | `fs_5m_381e35d4` | 97.1% | 91.4% |
| 1m | 261,000 | 696 | 46.9 MB | `fs_1m_dab72e07` | 98.8% | 95.1% |

Both span 2023-11-21 to 2026-09-10, and the leakage property is asserted as an
executable test rather than claimed.

---

## Scope

Narrowed deliberately during review, each decision recorded here because each
excluded something the original brief listed.

| Decision | Rationale |
|---|---|
| **Nifty 50 only** | One instrument, done properly, before breadth. |
| **No volume family** | The four tracked indices are `AMXIDX` and report `volume = 0` **by construction** — 100.00% of bars. Volume features there would be degenerate, not informative. |
| **No cross-index family** | Deferred with the second instrument. Cost is real: the state cannot distinguish "Nifty fell alone" from "everything fell". |
| **No tick/microstructure** | `TICK_RETENTION_DAYS = 7` leaves ~3 days of ticks. Order-book features are computable but not *trainable*, and never will be unless aggregates start being retained. See Open recommendation. |
| **In-session bars only** | 39 pre-open and 907 post-close 1-minute rows exist; a "session open" taken from the first observed bar would occasionally be a 09:10 pre-open print. |
| **1m and 5m both** | Same code, windows matched by elapsed time. |

---

## What was built

### Contract layer (2A)

`features/spec.py`, `registry.py`, `state.py`, `quality.py` — feature
definitions as data, a content-hash version, the `MarketState` object, the
six-status model, and the bridge from Phase 1 validation. Detail in
[`phase-2a-summary.md`](phase-2a-summary.md).

### Feature engine (2B)

18 features and 9 labels, on both timeframes.

| Family | Features |
|---|---|
| Price (12) | `ret_5m` `ret_15m` `ret_30m` `ret_60m` `accel_1` `range_rel` `body_ratio` `upper_wick` `lower_wick` `session_ret` `pos_in_range` `overnight_gap` |
| Volatility (6) | `rv_short` `parkinson_short` `atr_rel` `rv_baseline` `rv_regime` `rv_accel` |
| Labels (9) | `fwd_{ret,dir,rv}_{15m,30m,60m}` |

Definitions, formulas and windows: [`feature-contract.md`](feature-contract.md).

### Dataset generation (2C)

`features/dataset.py`, `store.py`, `build.py` — versioned Parquet datasets with
a JSON manifest. See [`feature-dataset.md`](feature-dataset.md).

---

## The four design decisions that mattered

### 1. Scope: intraday features, trailing baselines

`INTRADAY` features reset at 09:15 so one column keeps one meaning. Without the
rule, a "previous bar return" column would silently hold an 18-hour overnight
move at every session open.

`TRAILING` baselines read 5 completed prior sessions, so "unusual" has a
reference level. A baseline drawn from this morning alone would mostly measure
the normal intraday U-shape.

`SESSION` isolates the overnight gap in its own column — which is precisely
what lets the intraday columns reset without discarding the information.

### 2. The rolling scope, and the 58× term

Reset each morning, a 20-bar volatility estimate is absent for the first
**26.8%** of every 5-minute session. So `ROLLING` windows cross session
boundaries — but **exclude any term that spans one**.

That exclusion is not cosmetic. Measured on the stored bars:

| | Std dev | Variance |
|---|---|---|
| Intraday 5-minute return | 0.0716% | — |
| **Session-boundary return** | **0.5455%** | **58×** |

One boundary return inside a 20-return window inflates the variance **3.9×**
and overstates realized volatility **2.0×** — every morning, in a pattern
indistinguishable from the genuine opening-session volatility it would be
mistaken for.

The rule that follows: **path-dependent aggregates** may cross a boundary
because a sum survives losing one term; **point-to-point spans** may not,
because the gap sits inside a single ratio with nothing to drop. Parkinson,
Garman-Klass and Rogers-Satchell turn out to be structurally immune — every
term is within one bar.

Availability gained: `rv_short` absent for ~20 rows of all history instead of
13,940.

### 3. Trailing windows selected by observed dates

`scheduler/nse_calendar` ships holidays for **2026 only**, while **75%** of
stored history predates that year. Asking it for "the last five trading days"
in 2024 would name dates that were holidays, find no bars, and silently build a
four-session baseline. Sessions are therefore selected from the dates actually
present in the data.

### 4. Point-in-time safety is structural

Every feature reads through
`marketdata.access.get_market_data(..., as_of=T)`. The cut-off lives in SQL, so
a bar that had not closed **cannot be returned**. Leakage is prevented by
construction rather than by review.

---

## Bugs the tests caught

Each was a real defect, found before any data was generated from it.

1. **Column order depended on module import order.** Registration happens at
   import time, so importing `features.volatility` before `features.price`
   produced a different column order — and therefore a different version hash.
   Fixed by sorting specs into a canonical family order.
2. **`session_ret` conflated two absences.** "The opening bar has not closed
   yet" (09:15, every session) and "the opening bar is absent from the data"
   (the one session in 697 that starts at 11:15) are different facts. Now
   `INSUFFICIENT_HISTORY` and `MISSING` respectively.
3. **Gap-stretched spans reported the wrong quantity.** On 2026-09-10, a
   Thursday expiry, the 15:15 bar is absent, so "three bars after 15:00"
   reaches 15:20 — a 20-minute return in a column named `fwd_ret_15m`, reading
   **+0.71%** because it swallowed a 400-point closing spike. Point-to-point
   features and all nine labels now check the span against the clock.
4. **Rows at the session open had no label columns.** With no closed bar there
   was nothing to measure forward from, and the row came out ragged — a
   `KeyError` for any consumer, with the absence unexplained.

---

## Verification

### The leakage test

`state(T)` computed from full history is asserted **bit-identical** to `state(T)`
computed from history physically truncated at `T`, across five decision times,
plus appended divergent future bars. A companion test asserts those states are
fully populated, so the assertion cannot pass vacuously.

### Fast-path equivalence

The dataset builder reads once per session rather than once per decision time
(~700 queries instead of 52,000). Asserted row-for-row identical to
`build_market_state`, and checked against the live database across the full
span: **zero mismatches** in 720 sampled values.

### Distributions

Independently cross-checked against the raw bars:

* `ret_5m` sd **0.071%** matches the 0.0716% measured directly;
* `overnight_gap` sd **0.57%** matches the 0.5455% measured for boundary returns;
* `pos_in_range` median **0.518** — centred, as it must be;
* `rv_regime` median **0.843** — centred near 1.0;
* `fwd_ret_30m` mean **≈ 0**.

### Availability

Absence counts match the design exactly rather than approximately: `rv_baseline`
absent for **375** rows (5 × 75), `range_rel` for **720** (696 session opens +
the 24 slots of the 11:15 session), `session_ret` additionally `missing` for
**50** (that session's remaining decision times).

---

## Tests

**577 passed, 1 skipped** (~8 s). Phase 1's 245 unchanged. No database, no
network, no live market data.

| File | Covers |
|---|---|
| `test_feature_spec.py` | Field validation, scopes, `required_bars`, set ordering |
| `test_feature_registry.py` | Catalogue, the full version-hash matrix, import-order independence |
| `test_market_state.py` | Status invariants, `NaN`/infinity rejection, serialisation |
| `test_feature_quality.py` | The Phase 1 validation bridge, built from real `ValidationResult`s |
| `test_feature_windows.py` | Slicing, boundary exclusion, true-range handling |
| `test_feature_price.py` | Formulas, flat bars, gap-stretched spans |
| `test_feature_volatility.py` | Boundary exclusion, agreement with `backtest/rv.py` |
| `test_feature_labels.py` | Formulas, the zero class, session bounds |
| `test_feature_engine.py` | Assembly, failure isolation, determinism, immutability |
| `test_feature_point_in_time.py` | **The leakage suite** |
| `test_feature_dataset.py` | Decision times, **fast-path equivalence**, store round-trip |

---

## Acceptance gates

| Gate | Status |
|---|---|
| 1 — Determinism | **Met** — byte-identical serialisation asserted |
| 2 — No look-ahead | **Met** — the leakage suite |
| 3 — Boundary correctness | **Met** — 09:15, 09:20, 15:25, 15:30, overnight, weekend, holiday |
| 4 — Insufficient history | **Met** — explicit status with needed-vs-available |
| 5 — Missing-data semantics | **Met** — six distinguishable statuses; legitimate zero stays valid |
| 6 — Metadata completeness | **Met** — registry answers all ten §8 questions |
| 7 — Raw-data immutability | **Met** — asserted; `features/` has no write path to market data |
| 8 — Historical usability | **Met** — 52,200 rows in minutes |
| 9 — Regression safety | **Met** — Phase 1 untouched |
| 10 — Test quality | **Met** — 577 deterministic tests |

---

## Known limitations

1. **Single instrument, no cross-sectional context.** The state cannot tell
   "Nifty fell alone" from "everything fell".
2. **No microstructure**, and tick history is not accumulating.
3. **Gap-stretched point-to-point spans are dropped**, not repaired.
4. **Trailing baselines vary in bar count** — five sessions, not a constant
   number of returns.
5. **`fwd_rv_15m` rests on three returns** and is statistically thin.
6. **No vendor-revision history**, inherited from Phase 1.
7. **The 2026-only holiday calendar** is worked around, not fixed.
8. **No CI**, inherited from Phase 1.

---

## Open recommendation: tick aggregates

The Phase 2 brief calls order-book features "an important differentiator", and
the data is genuinely there — 1.97M ticks with `best_5_buy`/`best_5_sell`
fully populated.

But `TICK_RETENTION_DAYS = 7` means there is no tick *history* to train on, and
there never will be unless something starts retaining it. Per-minute aggregates
(intensity, book imbalance, spread, inter-arrival statistics) are tiny compared
with raw ticks. Starting now is the only way to accumulate that history;
deferring costs a year of it.

This needs a decision independently of Phase 3.

---

## Recommendation

**Ready for review.**

Suggested order:

1. [`feature-contract.md`](feature-contract.md) §3 — the scope rule.
2. [`feature-dataset.md`](feature-dataset.md) §5 — what a real build produced.
3. `tests/test_feature_point_in_time.py` — what "point-in-time safe" is
   asserted to mean.
4. `python -m pytest`, then
   `python -m features.build --instrument "Nifty 50" --dry-run`.

Phase 3 can now ask the harder question: given this state, what is predictable,
with what uncertainty, and does it hold out of sample?
