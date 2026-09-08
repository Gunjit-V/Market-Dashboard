# Phase 2A — Market State Contract

Branch: `phase-2`
Baseline: `6af05b8` ("Reduce derivative first-run backfill window from 90 to 30
days") on `master`.

Phase 2A defines **how market state is represented**. It builds no features and
computes no numbers — that is Phase 2B. `master` was not modified.

Specification: [`Phase-2.md`](Phase-2.md).

---

## Completed

### 1. Feature definitions — `features/spec.py`

`FeatureSpec` makes a feature *data describing itself*, with the arithmetic
attached separately. It declares the history it reads (`lookback_bars`), when
its value becomes honest (`warmup_bars`), whether it may cross a session
boundary (`scope`), its tunables (`params`) and any counterpart instruments
(`inputs`).

`FeatureSet` is an ordered collection sharing one timeframe. Order is part of
the contract — the vector form is positional — so it preserves registration
order and never sorts.

### 2. Session-boundary scopes

The three scopes encode the intraday-with-trailing-baselines decision:

* **`INTRADAY`** (default) — restarts at each session open, never reaches into
  yesterday. Keeps one column meaning one thing: without it, a "previous bar
  return" column would silently hold an 18-hour overnight move at every 09:15.
* **`TRAILING`** — may read completed prior sessions, as a **baseline only**,
  so that "unusual" has a reference level. Counted in sessions rather than
  bars, because five days is the meaningful unit and its bar count differs per
  timeframe (375 at 5m, 1,875 at 1m).
* **`SESSION`** — computed once per session from its boundary, knowable at the
  open. The overnight gap is the only planned instance, given its own column
  precisely so the intraday columns stay clean.

`DEFAULT_TRAILING_SESSIONS = 5`, as a parameter rather than a constant, so the
14-session alternative can be built and compared as a separately-versioned
dataset.

### 3. Versioned registry — `features/registry.py`

The catalogue, plus `feature_set_version()` — a deterministic SHA-256 over what
every spec *means*, rendered as `fs_5m_1a6b3994`.

Changing a window, a trailing baseline, a scope, or the set's membership or
order changes the version. Rewording a description does not, and neither does
`params` key order. The hash covers definitions, not values, so rebuilding over
more trading days leaves it unchanged.

Re-registering a name raises rather than replacing it.

### 4. Market state — `features/state.py`

`MarketState` carries instrument, decision time, timeframe, ordered values,
feature-set version and a quality summary. Deterministic and serialisable:
equal states produce byte-identical JSON.

`decision_time` must be naive, matching the IST convention every stored
timestamp already follows; an aware datetime is rejected rather than silently
compared against naive data.

### 5. The status model

`FeatureStatus` replaces a bare `NaN`, which would collapse four different
situations into one symbol: `VALID`, `INSUFFICIENT_HISTORY`, `MISSING`,
`STALE`, `INVALID_SOURCE`, and `MARKET_CLOSED`.

`FeatureValue` enforces the invariant in **both** directions:

* `VALID` must carry a finite number — `NaN` and infinity are rejected, since
  both propagate silently through downstream arithmetic;
* every other status must carry `None` — a number wearing an absent status
  would be believed by anything reading the value column alone.

`to_row()` emits each value beside a `<name>__status` column, so a stored
dataset never loses the reason a hole is there. `to_vector()` uses `None`
rather than `NaN` for the same reason.

`empty_state()` handles the case where one condition rules out everything (a
holiday, no source data): a fully-shaped state, not `None`, so the column set
stays consistent and the reason travels with the row.

### 6. Source quality — `features/quality.py`

A bridge from Phase 1 validation, not a second validator. `window_quality()`
converts a `ValidationResult` into a compact `SourceQuality` carrying the
verdict, counts, and the timestamps of invalid and missing bars.

Validation runs **once per extraction window**, not per state — 52,000 states
would otherwise mean 52,000 passes over largely the same bars. The verdict is
attached to every state from that window and recorded in the manifest.

`affects_decision_time()` recovers the precision that trade-off costs, by
asking whether a flagged bar actually falls in the history behind one decision.
`worst_verdict()` reports the most severe window rather than an average, since
averaging is how one broken instrument disappears into a reassuring number.

A `FAIL` window does not suppress features — states are built and *labelled*,
mirroring Phase 1's detect-and-report stance on ingestion.

### 7. Documentation

* [`market-state.md`](market-state.md) — decision time, bar availability,
  status semantics, quality layering, serialisation, a worked example.
* [`feature-contract.md`](feature-contract.md) — required metadata, the scope
  rule, warmup, ordering, versioning, point-in-time obligations, the
  missing-data policy.

---

## Architecture changes

Purely additive. One new package, no schema change, no runtime dependency.

```
features/            NEW — spec, registry, state, quality (stdlib-only)
tests/               NEW — 127 tests, no DB / network / live data
docs/                NEW — market-state, feature-contract, this summary,
                     and Phase-2.md (the specification itself)
```

**Not changed:** the database schema, every existing REST endpoint, ingestion
behaviour, the schedulers, the tick downloader, the dashboard, `backtest/`, the
frontend, `marketdata/`, `docker-compose.yml`, `Dockerfile`,
`requirements.txt`, and every Phase 1 test.

`git diff --stat master...HEAD` touches no pre-existing file.

---

## Tests

**372 passed, 1 skipped** (~2s). Phase 1's 245 are unchanged; 127 are new.
No database, no network, no live market data, no dependence on the current
date.

| File | Tests | Covers |
|---|---|---|
| `test_feature_spec.py` | 37 | Validation of every field; warmup below lookback rejected; `trailing_sessions` rejected off `TRAILING` scope; `required_bars` per scope and timeframe; set ordering preserved, duplicates rejected, `select()` keeps set order; identity excludes description. |
| `test_feature_registry.py` | 26 | Registration order becomes column order; duplicates raise; timeframes isolated; the full version-hash matrix — what changes it (params, baseline, lookback, scope, membership, order) and what does not (description, params key order). |
| `test_market_state.py` | 38 | `VALID` requires a finite number; `NaN`/infinity rejected; absent statuses require `None`; zero is valid; naive-datetime enforcement; vector uses `None`; `to_row()` status pairing; byte-identical serialisation; quality counts including zeros; `empty_state()` shape. |
| `test_feature_quality.py` | 26 | Verdicts built from **real** `ValidationResult`s via `marketdata.validation`; gaps warn rather than fail; invalid-bar timestamps recovered; `affects_decision_time()` before/after/at the decision and against a lookback bound; one bad window not averaged away. |

`test_feature_quality.py` deliberately validates real records through
`marketdata.validation` rather than hand-rolling result objects, so the bridge
cannot drift from Phase 1's actual output shape unnoticed.

---

## Acceptance gates

Phase 2A covers the gates that apply to a contract layer. Gates 2, 3 and 8
require computed features and are Phase 2B/2C.

| Gate | Status |
|---|---|
| 1 — Determinism | **Met** — no wall clock, randomness or shared state; byte-identical serialisation asserted. |
| 2 — No look-ahead | **Deferred to 2B** — the obligation is specified (`as_of` only); the future-invariance test needs computed features. |
| 3 — Boundary correctness | **Partial** — `MARKET_CLOSED` and `empty_state()` exist; 09:15/15:30/holiday behaviour is testable once features compute. |
| 4 — Insufficient history | **Met** — explicit status, with needed-vs-available in the detail. |
| 5 — Missing-data semantics | **Met** — six distinguishable statuses; legitimate zero stays `VALID`. |
| 6 — Metadata completeness | **Met** — the registry answers all ten §8 questions. |
| 7 — Raw-data immutability | **Met** — `features/` contains no write path. |
| 8 — Historical usability | **Deferred to 2C**. |
| 9 — Regression safety | **Met** — 245 Phase 1 tests pass; no pre-existing file modified. |
| 10 — Test quality | **Met** — 127 deterministic tests. |

---

## Design decisions

**`spec.py`, not `types.py`.** The brief proposes `types.py`; the layout is
explicitly "a proposal, not a mandate". `spec.py` says what the file holds.

**A sixth status, `MARKET_CLOSED`.** A holiday is not missing data — the
feature has no meaning at that instant rather than merely lacking inputs.
Collapsing it into `MISSING` would make every holiday row look like a
collection failure.

**Trailing windows counted in sessions, not bars.** "Five days" is the unit a
person reasons in; the bar count is a consequence of the timeframe.

**Window-level validation.** Chosen over per-state for cost, with
`affects_decision_time()` as the escape hatch. Recorded as a known limitation.

**`None`, not `NaN`, in the vector.** `NaN` is a float that survives
arithmetic; `None` does not, and that is the point.

**Typing imports follow `marketdata/`.** `from typing import ...` rather than
ruff's `collections.abc` preference, to keep the two packages consistent. Phase
1 left comparable findings untouched for the same reason.

---

## Known limitations

1. **No features exist yet.** 2A is the contract; `fn` is unset on every spec
   until 2B.
2. **`STALE` has no default threshold.** Enforced as a status, but what counts
   as stale is per-feature and is set in 2B.
3. **Window-level validation is coarser than per-state**, as above.
4. **Volume features will be degenerate on cash indices.** The four tracked
   indices are `AMXIDX` with `volume = 0` by construction; the futures series is
   the usable proxy. To be handled explicitly in 2B rather than emitting zeros
   that look like data.
5. **Tick/microstructure deferred.** See below.
6. **No bitemporal versioning**, inherited from Phase 1: a state is
   reproducible with respect to bar completion, not vendor restatements.

---

## Data findings

Probed from the live database while scoping (counts move as collection
continues):

| Dataset | Rows | Span | Instruments |
|---|---|---|---|
| `ohlcv_1min` | ~1.90M | 2023-11-21 → current | 92 |
| `ohlcv_5min` | ~275k | 2023-11-21 → current | 50 |
| `tick_data` | ~1.97M | 3 days | 35 |

* **Contract violations: zero**, across all 2.17M bars — every OHLC invariant
  and positivity rule holds. This is why ingestion stays in `report` mode:
  switching to `reject` would add a mode that can withhold data, to solve a
  problem that has never occurred.
* **Cross-index coverage is excellent** — NIFTY, BANKNIFTY, SENSEX and BANKEX
  all have 1-minute bars on the same **694** sessions. Four indices, not three;
  BSE-vs-NSE divergence is available as well as NIFTY-vs-BANKNIFTY. **FINNIFTY
  is not collected.**
* **Cash indices report zero volume** by construction (limitation 4).
* **Tick history is ~3 days** under `TICK_RETENTION_DAYS = 7`, though book
  fields are fully populated (1.97M rows with `best_5_buy`/`best_5_sell`).
  Only 894k rows carry `sequence_number > 0`, i.e. a true exchange clock.

---

## Open recommendation: tick aggregates

The brief calls order-book features "an important differentiator" (§5.5), and
the book data is genuinely there. But with a 7-day retention window there is no
tick *history* to train on, and there never will be unless something starts
retaining it.

Two separable things:

1. **Liquidity features are computable now** for any `T` inside the retention
   window — useful for live state, not for training.
2. **Per-minute tick aggregates** (intensity, book imbalance, spread,
   inter-arrival statistics) are tiny compared to raw ticks and could be
   persisted permanently. Starting now is the only way to accumulate
   microstructure history; deferring costs a year of it.

This needs a decision before 2B fixes the family list.

---

## Recommendation

**Ready for review, per the §18 protocol — Phase 2B not started.**

Suggested review order:

1. [`market-state.md`](market-state.md) §4 and §7 — confirm the status
   semantics and the worked example match your intent.
2. [`feature-contract.md`](feature-contract.md) §3 — confirm the scope rule is
   the intraday/trailing split you asked for.
3. `features/state.py` — confirm the `FeatureValue` invariants.
4. Run `python -m pytest`.

Decisions outstanding before 2B:

* **Tick aggregates** — above.
* **Label definitions.** Forward return, direction and forward RV are planned
  with parameterised horizons; the choice of primary target can wait for
  Phase 3, but the horizons should be sanity-checked.
* **Volume on indices** — confirm that degenerate-by-design (rather than
  omitting the family for `AMXIDX`) is the behaviour you want.
