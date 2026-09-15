# Project History

What each phase built, what it deliberately did not build, and which
limitations are still open. The other documents describe the system as it is
now; this one records how it got there and why the awkward decisions were made
the way they were.

Phases 1 and 2 are complete. Neither modified `master`, and no Phase 1 file was
changed by Phase 2.

| Phase | Branch | Delivered | Exit criterion |
|---|---|---|---|
| **1** — Trustworthy market data | `claude/herewego-phase-1-data-foundation-wo8gsq` | `marketdata/`, the contracts, the validators, the quality report, point-in-time access | Stored market data is validated, documented and readable point-in-time safely |
| **2A** — Market state contract | `phase-2` | `features/spec.py`, `registry.py`, `state.py`, `quality.py` | A feature can declare itself; a feature set has a version |
| **2B** — Feature engine | `phase-2` | `features/price.py`, `volatility.py`, `labels.py`, `windows.py`, `engine.py` | 18 features and 9 labels compute, deterministically and without look-ahead |
| **2C** — Historical datasets | `phase-2` | `features/dataset.py`, `store.py`, `build.py` | A versioned dataset exists where `timestamp → feature_vector(timestamp)` is reproducible |

---

## Phase 1 — Trustworthy Market Data Foundation

Baseline: `8287049` on `master`. Phase 1 made the existing foundation explicit,
validated, reproducible and documented. It built no models, no agents and no
new infrastructure.

### What it added

* **Documentation of the system as it exists** — architecture, field-by-field
  data contracts, point-in-time semantics, and every validation check with its
  rationale. Now [01-architecture.md](01-architecture.md) and
  [02-market-data.md](02-market-data.md).
* **Executable data contracts** (`marketdata/contracts.py`) — `FieldSpec` /
  `DatasetContract` encode required-ness, types, origin and positivity for all
  three datasets. `tests/test_contracts.py` parses `db/init_schema.sql` and
  asserts the contracts match the real tables column-for-column, so the
  documentation cannot drift from the schema unnoticed.
* **A session model** (`marketdata/sessions.py`) — which bar timestamps
  *should* exist, reusing `scheduler/nse_calendar.py` rather than duplicating
  it. A regression test asserts it agrees with both
  `scheduler.nse_calendar.LAST_DOWNLOAD_SLOT` and `backtest.rv.CANDLES_PER_DAY`.
* **A validation layer** (`marketdata/validation.py`) — pure functions
  returning structured `Issue` objects with stable codes and two severities.
  `partition()` splits records by validity without altering a single value.
* **A non-destructive ingestion hook** (`marketdata/ingest.py`) — the default
  `report` mode inserts **exactly** the rows it inserted before Phase 1.
  `reject` is opt-in; an unrecognised mode falls back to `report`, so a typo in
  `.env` can never drop data.
* **Quality reporting** (`marketdata/quality.py`, `report.py`) — per-instrument
  `PASS` / `WARNING` / `FAIL` with exit codes 0/1/2/3, usable as a CI gate. A
  dataset that was not validated is reported as **absent**, never as clean.
* **Point-in-time-aware access** (`marketdata/access.py`) — half-open windows,
  whitelisted timeframes, and an `as_of` cut-off that admits a bar only once it
  has closed.
* **A read-only HTTP surface** (`api/routes/quality.py`) — a test asserts the
  module registers no non-`GET` routes.

Not changed: the database schema, the existing REST endpoints, ingestion
behaviour under default settings, the schedulers, the tick downloader, the
dashboard, `backtest/`, the frontend, `docker-compose.yml`, `Dockerfile` and
`requirements.txt`.

### Two bugs found by writing the contracts down

Both were found by probing the live table against the documented intent, and
both were fixed after Phase 1 closed.

**98.7% of tick timestamps were `datetime.now()`.** Only 60,183 of 4,796,304
rows carried a real feed timestamp. `last_traded_timestamp` is `0` — falsy —
for anything that has not traded, and the old `if raw_ts` test sent every such
packet to the wall-clock fallback. Cash indices, which never "trade", were
affected on essentially every tick; drift on those rows reached 17.6 hours,
which is what produced the 16:00/18:00/21:00 tick clusters. Fixed by
`resolve_tick_timestamp()`, which prefers `exchange_timestamp` and converts
through an explicit IST offset. Detail:
[02-market-data.md — Tick timestamps](02-market-data.md#tick-timestamps--fixed-with-a-labelled-residual-fallback).

**Same-second snapshots were silently discarded.** `UNIQUE(instrument_id,
timestamp)` plus `ON CONFLICT DO NOTHING` kept only the first snapshot per
exchange second — and Angel One publishes at one-second resolution. This was
masked while timestamps carried microseconds and became actively lossy exactly
when they became correct. Fixed by
`db/migrations/001_tick_sequence_number.sql`, which adds the feed's own
`sequence_number` to the uniqueness key.

Neither was a *leakage* risk — both discarded information rather than adding
future information — but together they made tick history an incomplete record,
and rows written before the fixes cannot be repaired.

---

## Phase 2 — Market State & Feature Engineering

Baseline: `6af05b8` on `master`. Phase 2 turned Phase 1's validated data into
an ML-ready representation: a reproducible, point-in-time-safe
`feature_vector(t)`. The brief is archived at
[05-phase-2-brief.md](05-phase-2-brief.md).

### The exit criterion

> A versioned feature dataset exists where `timestamp → feature_vector(timestamp)`
> is reproducible and point-in-time safe.

**Met.** Two datasets, both versioned and both verified against the live
one-at-a-time path:

| Timeframe | Rows | Sessions | Size | Version | Features valid | Labels valid |
|---|---|---|---|---|---|---|
| 5m | 52,275 | 697 | 10.7 MB | `fs_5m_381e35d4` | 97.1% | 90.2% |
| 1m | 261,375 | 697 | 47.0 MB | `fs_1m_dab72e07` | 98.8% | 95.0% |

Both span 2023-11-21 to 2026-09-11, and the leakage property is asserted as an
executable test rather than claimed. Full numbers:
[03-features.md — What a real build produced](03-features.md#what-a-real-build-produced).

Both were independently verified against the database after the fact: every
label for the final session was recomputed straight from `ohlcv_1min` /
`ohlcv_5min` without using `features/labels.py`, giving **4,050 comparisons with
zero mismatches** and 1,616 valid values equal bit-for-bit. The 2026-09-10
expiry gap — a missing 15:15 bar next to a 400-point spike — is handled exactly
as intended, with the stretched-horizon rows withheld rather than relabelled.

### Incremental building

A full rebuild takes 4m 43s at 5m and roughly 87 minutes at 1m, nearly all of it
recomputing sessions that have not changed. `--append` rebuilds only from the
last stored session onward: **2.5 seconds and 20 seconds respectively**, which
makes a daily refresh practical rather than an overnight job.

It restarts *at* the last stored session rather than after it. A dataset written
mid-session holds only part of that session, and appending strictly after the
last timestamp would leave the remainder missing permanently with nothing to
say so. Rebuilding one session costs seconds; silently losing half of one is
unrecoverable.

That decision paid for itself immediately — see
[A stale dataset, found by rebuilding](#a-stale-dataset-found-by-rebuilding).

Mechanics and guarantees:
[03-features.md — Keeping a dataset current](03-features.md#keeping-a-dataset-current).

### A stale dataset, found by rebuilding

The first `--append` run reported that the stored 2026-09-10 session no longer
matched what the code produced:

```
2026-09-10 09:15  fwd_ret_15m__status
  stored:   nan
  rebuilt:  insufficient_history
```

That is the ragged-row defect (bug 4 below). The 5-minute dataset had been
generated minutes *before* the fix landed and never regenerated, so **695 of its
697 session-open rows carried `NaN` label statuses**; the 1-minute dataset,
built after the fix, had none. Both have since been rebuilt and carry zero.

Two things this exposed:

* **The datasets were stale with respect to a bug fix, and nothing reported it.**
  A dataset records the feature-set version, which covers *definitions* — it
  does not cover a change in how absences are emitted. There is no automatic
  check that a stored dataset still matches current code.
* **The reported label validity was flattered by the bug.** 91.4% was computed
  over a denominator short by 6,480 entries, because rows that emitted no labels
  could not be counted as invalid. The correct figure is **90.2%**.

Had the append skipped past the last stored session — the obvious
implementation — neither would have surfaced, and every future append would
have inherited the inconsistency.

### Scope, narrowed during review

Each decision excluded something the original brief listed, so each is recorded.

| Decision | Rationale |
|---|---|
| **Nifty 50 only** | One instrument, done properly, before breadth. |
| **No volume family** | The four tracked indices are `AMXIDX` and report `volume = 0` **by construction** — 100.00% of bars. Volume features there would be degenerate, not informative. |
| **No cross-index family** | Deferred with the second instrument. Cost is real: the state cannot distinguish "Nifty fell alone" from "everything fell". |
| **No tick/microstructure** | `TICK_RETENTION_DAYS = 7` leaves days, not years, of ticks. Order-book features are computable but not *trainable*. |
| **In-session bars only** | 39 pre-open and 907 post-close 1-minute rows exist; a "session open" taken from the first observed bar would occasionally be a 09:10 pre-open print. |
| **1m and 5m both** | Same code, windows matched by elapsed time. |

### The four design decisions that mattered

1. **Scope: intraday features, trailing baselines.** `INTRADAY` features reset
   at 09:15 so one column keeps one meaning — without the rule, a "previous bar
   return" column would silently hold an 18-hour overnight move at every
   session open. `TRAILING` baselines read 5 completed prior sessions so
   "unusual" has a reference level. `SESSION` isolates the overnight gap in its
   own column, which is precisely what lets the intraday columns reset without
   discarding the information.

2. **The `ROLLING` scope, and the 58× term.** Reset each morning, a 20-bar
   volatility estimate is absent for the first 26.8% of every 5-minute session.
   So `ROLLING` windows cross session boundaries but **exclude any term that
   spans one** — a session-boundary return has 58× the variance of an intraday
   one, and a single such term inside a 20-return window overstates realized
   volatility 2.0×, every morning, in a pattern indistinguishable from genuine
   opening volatility. Availability gained: `rv_short` absent for 21 rows of
   all history instead of 13,940. Detail:
   [03-features.md](03-features.md#rolling--and-the-58-term).

3. **Trailing windows selected by observed dates.** `scheduler/nse_calendar`
   ships holidays for 2026 only, while 75% of stored history predates that
   year. Asking it for "the last five trading days" in 2024 would name dates
   that were holidays, find no bars, and silently build a four-session
   baseline. Sessions are selected from the dates actually present in the data.

4. **Point-in-time safety is structural.** Every feature reads through
   `marketdata.access.get_market_data(..., as_of=T)`. The cut-off lives in SQL,
   so a bar that had not closed **cannot be returned**. Leakage is prevented by
   construction rather than by review.

### Bugs the tests caught

Each was a real defect, found before any data was generated from it.

1. **Column order depended on module import order.** Registration happens at
   import time, so importing `features.volatility` before `features.price`
   produced a different column order — and therefore a different version hash.
   Fixed by sorting specs into a canonical family order.
2. **`session_ret` conflated two absences.** "The opening bar has not closed
   yet" (09:15, every session) and "the opening bar is absent from the data"
   (the 2025-10-21 Muhurat session, which opened at 11:15) are different facts.
   Now `INSUFFICIENT_HISTORY` and `MISSING` respectively.
3. **Gap-stretched spans reported the wrong quantity.** On 2026-09-10, a
   Thursday expiry, the 15:15 bar is absent, so "three bars after 15:00"
   reaches 15:20 — a 20-minute return in a column named `fwd_ret_15m`, reading
   **+0.71%** because it swallowed a 400-point closing spike. Point-to-point
   features and all nine labels now check the span against the clock.
4. **Rows at the session open had no label columns.** With no closed bar there
   was nothing to measure forward from, and the row came out ragged — a
   `KeyError` for any consumer, with the absence unexplained.

### Verification

* **The leakage test.** `state(T)` computed from full history is asserted
  **bit-identical** to `state(T)` computed from history physically truncated at
  `T`, across five decision times, plus appended divergent future bars. A
  companion test asserts those states are fully populated, so the assertion
  cannot pass vacuously.
* **Fast-path equivalence.** The dataset builder reads once per session rather
  than once per decision time (~700 queries instead of 52,000). Asserted
  row-for-row identical to `build_market_state`, and checked against the live
  database across the full span: zero mismatches in 720 sampled values.
* **Distributions and availability** cross-checked against the raw bars, with
  absence counts matching the design exactly rather than approximately. See
  [03-features.md](03-features.md#what-a-real-build-produced).

### Acceptance gates

All ten met. Gates 2, 3 and 8 were deferred from 2A (they need computed
features) and closed in 2B/2C.

| Gate | Status |
|---|---|
| 1 — Determinism | Met — byte-identical serialisation asserted |
| 2 — No look-ahead | Met — the leakage suite |
| 3 — Boundary correctness | Met — 09:15, 09:20, 15:25, 15:30, overnight, weekend, holiday |
| 4 — Insufficient history | Met — explicit status with needed-vs-available |
| 5 — Missing-data semantics | Met — six distinguishable statuses; legitimate zero stays valid |
| 6 — Metadata completeness | Met — the registry answers all ten §8 questions |
| 7 — Raw-data immutability | Met — `features/` has no write path to market data |
| 8 — Historical usability | Met — 52,200 rows in minutes |
| 9 — Regression safety | Met — no pre-existing file modified |
| 10 — Test quality | Met — 578 deterministic tests |

### Design decisions worth knowing about

**`spec.py`, not `types.py`.** The brief proposes `types.py`; its layout is
explicitly "a proposal, not a mandate". `spec.py` says what the file holds.

**A sixth status, `MARKET_CLOSED`.** The brief named five. A holiday is not
missing data — the feature has no meaning at that instant rather than merely
lacking inputs. Collapsing it into `MISSING` would make every holiday row look
like a collection failure.

**Trailing windows counted in sessions, not bars.** "Five days" is the unit a
person reasons in; the bar count is a consequence of the timeframe (375 at 5m,
1,875 at 1m).

**Window-level validation, not per-state.** 52,000 states would otherwise mean
52,000 validation passes over largely the same bars.
`affects_decision_time()` is the escape hatch. Recorded as a limitation below.

**`None`, not `NaN`, in the vector.** `NaN` is a float that survives
arithmetic; `None` does not, and that is the point.

---

## Tests

**577 passed, 1 skipped** (~6 s), from **578 collected**. No database, no
network, no live market data, and no dependence on the current date. Tests that
care about trading days inject their own calendar predicate, so results cannot
drift as the bundled NSE holiday list is extended.

There were **no tests before Phase 1** — no `tests/` directory, no runner
configuration, no CI — so there were no pre-existing failures to inherit.

| File | Tests | Covers |
|---|---|---|
| `test_contracts.py` | 13 | Contracts match `db/init_schema.sql` column-for-column; required fields; origins; the `close`-means-previous-close trap. |
| `test_sessions.py` | 17 | 375 one-minute and 75 five-minute bars per session; no bar at 15:30; grid anchoring; weekend/holiday exclusion; agreement with `backtest.rv.CANDLES_PER_DAY`. |
| `test_validation_ohlcv.py` | 39 | Every broken invariant fails; duplicates; out-of-order; negative volume; zero volume valid; misalignment; **weekend, holiday and overnight closures are not flagged as missing**; non-mutation. |
| `test_validation_ticks.py` | 18 | Snapshot contract; duplicates as warnings; non-decreasing ordering; cumulative volume regression with the daily reset not flagged; out-of-session detection. |
| `test_ingest.py` | 20 | **`report` mode passes every candle through unchanged**, preserving order and object identity; `reject` withholds only ERROR rows; quarantine JSONL; mode fallback. |
| `test_quality.py` | 13 | Every metric traced back to the validation result; PASS/WARNING/FAIL; unvalidated datasets absent. |
| `test_access.py` | 23 | Half-open windows; deterministic ordering; `as_of` SQL for bars and ticks; timeframe whitelisting. |
| `test_report.py` | 16 | Report assembly; tick truncation note; calendar caveat; CLI parsing and exit codes. |
| `test_regression.py` | 15 (1 skipped) | NSE calendar and download slots unchanged; `backtest.rv` numerics unchanged; schema untouched; all pre-existing routers still registered; `/quality` is read-only. |
| `test_tick_timestamps.py` | 33 | The tick timestamp fix and its fallback labelling. |
| `test_retention.py` | 13 | The retention scheduler. |
| `test_instrument_sync.py` | 7 | Daily instrument-master sync. |
| `test_ohlcv_pacing.py` / `test_ohlcv_rate_limit.py` | 12 / 7 | Scheduler pacing and Angel One backoff. |
| `test_feature_spec.py` | 37 | Field validation; warmup below lookback rejected; `required_bars` per scope; set ordering preserved. |
| `test_feature_registry.py` | 30 | The full version-hash matrix — what changes it and what does not — and import-order independence. |
| `test_market_state.py` | 38 | `VALID` requires a finite number; `NaN`/infinity rejected; absent statuses require `None`; zero is valid; byte-identical serialisation. |
| `test_feature_quality.py` | 26 | The Phase 1 validation bridge, built from **real** `ValidationResult`s so it cannot drift from Phase 1's output shape. |
| `test_feature_windows.py` | 34 | Slicing, boundary exclusion, true-range handling. |
| `test_feature_price.py` | 30 | Formulas, flat bars, gap-stretched spans. |
| `test_feature_volatility.py` | 22 | Boundary exclusion, agreement with `backtest/rv.py`. |
| `test_feature_labels.py` | 22 | Formulas, the zero class, session bounds. |
| `test_feature_engine.py` | 23 | Assembly, failure isolation, determinism, immutability. |
| `test_feature_point_in_time.py` | 30 | **The leakage suite.** |
| `test_feature_dataset.py` | 40 | Decision times, **fast-path equivalence**, store round-trip. |

---

## What the dataset says so far

Not part of the Phase 2 deliverable, but measured on it, and it shapes what
Phase 3 should ask. Models here are ridge and logistic regression on the 18
features, trained on every prior session.

**Direction is not predictable.** Against the *best* naive baseline — which on a
down day is "always predict down", not "always predict up" — the models lost at
both timeframes and every horizon:

| | Model | Best naive |
|---|---|---|
| 5m, 30-minute horizon | 59.3% | **63.0%** |
| 5m, 60-minute horizon | 60.4% | **64.6%** |
| 1m, 5-minute horizon | 47.4% | **56.0%** |

Choosing the convenient baseline instead would have shown a 22-point edge where
there is none. Any future evaluation must compare against `max(always-up,
always-down)`.

**Forward return is not predictable either.** Over a 20-session holdout it lost
to a train-mean baseline at all three horizons, with negative correlations
(−0.06, −0.12, −0.15).

**Forward volatility carries real signal.** Over the same 20 sessions the model
reached correlations of +0.21 / +0.25 / +0.27 against persistence's +0.11 /
+0.14 / +0.11 — beating it on both error and ranking. At n≈1,000 that is roughly
six standard errors from zero.

**RMSE and ranking are different questions.** On single sessions the volatility
model beat persistence on RMSE by 53–67% while its correlation was *negative*:
it wins by shrinking toward a sane level, not by ranking turbulent periods above
calm ones. Reporting only RMSE would have declared a decisive win for a
predictor whose ordering was inverted.

**Implied volatility is the real benchmark.** On stored NIFTY option data,
implied volatility predicts subsequent realized volatility with correlation
+0.56 at ≤7 days to expiry. Trailing 5-session realized volatility
(`rv_baseline`) reached +0.61, and retained +0.51 correlation with the residual
after removing everything implied volatility explains — i.e. it appears to carry
information the option market is not pricing. Sample: 60 overlapping
observations across ~21 trading days and 3 expiries, so this is a lead worth
testing, not a result. The variance risk premium over that window was *negative*
(−0.64pp), contrary to the textbook.

**One session cannot settle any of this.** A genuine 51% edge looks like a loss
38.8% of the time at n=50; a worthless model scores ≥55% a quarter of the time.
Detecting a 1pp edge at 80% power needs ~19,600 rows — about 261 five-minute
sessions. Testing nine labels at once produces at least one "significant" result
26% of the time with no signal at all.

## Open limitations

Carried forward from both phases. Each is documented rather than silently
worked around.

### Data

1. **No as-of versioning of vendor revisions.** `ON CONFLICT DO NOTHING` keeps
   the first version of a bar and discards any revision, so point-in-time
   correctness holds with respect to *bar completion*, not vendor
   restatements. Recording the extraction time in the dataset manifest is the
   only honest mitigation. Bitemporal storage is out of scope.
2. **Five-minute bars have no provenance flag.** A row may come from the API's
   `FIVE_MINUTE` interval or from `backfill_from_one_minute()`; the table
   cannot tell you which. Neither leaks, but a backfilled bar became
   *available* later than its label suggests.
3. **The holiday calendar covers 2026 only.** Outside 2026 only weekends are
   known non-trading, so gap counts for other years are over-stated. The
   quality report attaches an explicit note, and the feature pipeline works
   around it by selecting sessions from observed dates. `NSE_HOLIDAYS` extends
   the calendar without a code change.
4. **Pre-fix tick rows are unrepairable.** Rows with `sequence_number = 0`
   predate migration 001; their clock source is unknowable and the original
   exchange clock was never stored.
5. **One session model.** A single 09:15–15:30 regular session is assumed;
   pre-open, closing-auction and special sessions are not modelled. The
   2024-11-01 Muhurat session, which traded 18:00–19:00, therefore produces no
   decision times at all. The model is injectable, so this is extensible.
6. **Ticks are validated on read, not on ingest.** Adding per-tick validation
   to a hot WebSocket path was not justified. `parse_tick` already drops
   non-positive LTPs.
7. **Tick history is not accumulating.** `TICK_RETENTION_DAYS = 7` — see the
   open recommendation below.

### Features and datasets

8. **Single instrument, no cross-sectional context.** The state cannot tell
   "Nifty fell alone" from "everything fell".
9. **Gap-stretched point-to-point spans are dropped, not repaired.**
   Volatility aggregates are deliberately exempt: averaging twenty terms bounds
   the effect, whereas a return is fixed entirely by its two endpoints.
10. **Trailing baselines vary in bar count.** Sessions are not uniformly full
    (5% of Nifty 50 sessions are short; one has 17 bars), so a five-session
    baseline spans five *sessions*, not a constant number of returns.
11. **`fwd_rv_15m` rests on three returns** and is statistically thin. The
    60-minute horizon is the more trustworthy of the three.
12. **`STALE` has no default threshold.** The status exists and is enforced,
    but what counts as stale is per-feature and no feature sets one yet.
13. **Window-level validation is coarser than per-state.** A `FAIL` verdict
    describes the window; `affects_decision_time()` narrows it, but the default
    label is the window's.
14. **One instrument per file.** Cross-instrument datasets need the cross
    family.
15. **Nothing checks that a stored dataset still matches current code.** The
    feature-set version hash covers *definitions* — windows, scopes, params —
    not a behavioural change that leaves definitions untouched, such as the
    ragged-row fix. A dataset can therefore go stale against the code that
    would rebuild it, silently; that is how the 5-minute dataset carried 695
    pre-fix rows. `--append` catches such a divergence incidentally, but only
    in the last stored session, so an older one would stay hidden. Rebuilding a
    sampled session and comparing would close it.

### Engineering

16. **Still no CI.** The suite runs locally with `python -m pytest`; nothing
    enforces it on push. There is no `.github/` directory.
17. **No linter is configured in-repo.** Phase 1 and 2 files were checked with
    `ruff` and are clean; ~20 pre-existing findings remain in untouched files
    and were deliberately not fixed, to keep the diffs reviewable.
18. **The `/quality` endpoint has no pagination or caching**, reads every row
    in range, and follows the existing route convention of returning errors as
    `status: "error"` with HTTP 200 rather than raising.

---

## Open recommendation: tick aggregates

The Phase 2 brief calls order-book features "an important differentiator", and
the data is genuinely there — millions of ticks with `best_5_buy` /
`best_5_sell` fully populated.

But `TICK_RETENTION_DAYS = 7` means there is no tick *history* to train on, and
there never will be unless something starts retaining it. Two separable things:

1. **Liquidity features are computable now** for any `T` inside the retention
   window — useful for live state, not for training.
2. **Per-minute tick aggregates** (intensity, book imbalance, spread,
   inter-arrival statistics) are tiny compared with raw ticks and could be
   persisted permanently. Starting now is the only way to accumulate
   microstructure history; deferring costs a year of it.

This needs a decision independently of Phase 3.

---

## Deliberately not built

Out of scope for Phases 1 and 2, and listed so that their absence reads as a
decision rather than an oversight:

forecasting and predictive models · regime detection · anomaly detection beyond
deterministic contract validation · historical analogue retrieval · embeddings,
vector databases, RAG · an AI research agent or any LLM integration · an
evaluation harness for models · MLOps (experiment tracking, model registry,
serving) · Kafka, Airflow, Kubernetes, microservices, cloud services ·
bitemporal storage · table partitioning · automated remediation or backfill of
detected gaps.

Phase 3 can now ask the harder question: given this state, what is predictable,
with what uncertainty, and does it hold out of sample?
