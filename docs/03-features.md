# Features

The Phase 2 answer to: **what was knowable about one instrument at one decision
time, and what was not?**

This document covers:

1. [Market state](#market-state) — the `MarketState` object and status model
2. [Feature contract](#feature-contract) — metadata, scopes, versioning
3. [Feature inventory](#feature-inventory) — the 18 features and 9 labels built in Phase 2
4. [Feature datasets](#feature-datasets) — building, reading and interpreting datasets

---

# Market state

The Phase 2 answer object. Everything knowable about one instrument at one
decision time, and an explicit account of what was *not* knowable.

The north-star question:

> What was the state of the market at decision time `T`, using only
> information that was actually available at `T`?

## Decision time

`decision_time` is **the instant a decision would have been made**. It is not a
bar label, not a timestamp read from a row, and not the moment the dataset was
built.

Everything in a `MarketState` was derived from data that had already become
knowable at that instant. For bars that means bars which had **closed** — see
[Bar availability](#bar-availability) — and the cut-off is enforced in SQL by
`marketdata.access.get_market_data(..., as_of=T)` rather than by convention.

`decision_time` is **naive**, read as Asia/Kolkata, matching every stored
timestamp in the project. A timezone-aware value is rejected outright:
silently comparing an aware datetime against naive stored data would mean
something different from every other timestamp in the system.

## The object

```
MarketState
    instrument            symbol, as stored in instruments.symbol
    decision_time         naive IST, the instant of the decision
    timeframe             "1m" or "5m" — the bar series behind the features
    values                one FeatureValue per feature, in feature-set order
    feature_set_version   which definitions produced these columns
    quality               feature-level quality summary
```

Order is part of the contract. The vector form is positional, so columns
reordering between the dataset a model trained on and the one it is served is a
real production failure — `FeatureSet` preserves registration order and never
sorts.

`feature_set_version` (e.g. `fs_5m_381e35d4`) is a content hash of the feature
definitions. It answers "what did these columns mean?" for any state, however
long after it was built. See [Versioning](#versioning).

## Bar availability

Bars are left-labelled: a bar stamped `09:15` on a 5-minute series covers
`[09:15, 09:20)` and is **complete at 09:20**.

It must therefore not be used at 09:15, 09:16, 09:17, 09:18 or 09:19. At a
decision time of 11:30, the newest usable 5-minute bar is the one labelled
**11:25**, because that is the most recent one that had closed.

Using bars with `timestamp <= T` instead of `timestamp + w <= T` leaks up to
one full bar of future information. For a 5-minute bar that is the difference
between a plausible backtest and a fictional one.

This is Phase 1's interpretation, unchanged. Phase 2 consumes it; it does not
redefine it.

## Feature status

Every value carries a status. A bare `NaN` would collapse several genuinely
different situations into one symbol:

| Status | Meaning | Expected? |
|---|---|---|
| `valid` | Computed from a full window of contract-valid data | — |
| `insufficient_history` | Fewer completed bars exist than the feature needs | **Yes** — early session, or a newly listed instrument |
| `missing` | The source data is absent — no bar was recorded | Sometimes: nobody traded |
| `stale` | Source data exists but is older than the feature tolerates | Investigate |
| `invalid_source` | Source data violates its Phase 1 contract | **No** — a defect |
| `market_closed` | The decision time is outside a live session | Yes |

The first and the last two are very different things, and a single `NaN` cannot
tell them apart. The requirement this satisfies: the system never silently
turns absence into a plausible-looking number.

`MARKET_CLOSED` is an addition to the five statuses named in the Phase 2 brief.
A holiday is not missing data — the feature has no meaning at that instant
rather than merely lacking inputs.

### The invariant

`FeatureValue` enforces it in both directions:

* `VALID` **must** carry a finite number. `NaN` and infinity are rejected —
  both propagate silently through any downstream arithmetic, which is exactly
  the failure the status model exists to prevent.
* Every other status **must** carry `None`. A number wearing an absent status
  would be believed by anything reading the value column alone.

Zero is a value, not an absence. Cash indices structurally report zero volume
(see [02-market-data.md — Data contracts](02-market-data.md#data-contracts));
that is data.

## Quality

Two layers, deliberately separate.

**Feature-level** (`MarketState.quality`) — how much of this state is real:
totals, per-status counts, and completeness. Every status is counted including
zeros, so "no features were stale" is distinguishable from "stale was never
considered".

**Source-level** (`features/quality.py`) — whether the underlying market data
was sound. Carried from Phase 1's `marketdata.validation`, not re-derived.

Validation runs **once per extraction window**, not once per state: building
52,000 states would otherwise mean 52,000 validation passes over largely the
same bars. The verdict is attached to every state built from that window and
recorded in the dataset manifest.

The cost of that choice is precision — the verdict describes the window rather
than one state's exact inputs. `affects_decision_time()` narrows the question
when it matters, by asking whether a flagged bar actually falls inside the
history behind a particular decision.

A `FAIL` window does **not** suppress features. States are still built and
*labelled*, and the label reaches the manifest, so the problem is visible
before training rather than discovered afterwards. That mirrors Phase 1's
stance on ingestion: detect and report, never silently repair.

## Serialisation

Two forms, both deterministic — equal states produce byte-identical output.

**`as_dict()`** — nested JSON, with `decision_time` as ISO-8601. For inspection
and for the manifest.

**`to_row()`** — flat, for tabular storage. Each feature emits **two** columns:

```
instrument  decision_time  timeframe  feature_set_version
ret_5m  ret_5m__status   ret_15m  ret_15m__status   ...
```

The parallel `__status` column is what stops a stored dataset from losing the
reason a value is absent. `to_vector()` uses `None` rather than `NaN` for the
same reason: "no value" stays distinguishable from a genuine number all the way
into the dataset.

## Worked example

Real output from `build_market_state`, one bar into the session at 09:20 on a
5-minute series (2026-09-03, Nifty 50):

```json
{
  "instrument": "Nifty 50",
  "decision_time": "2026-09-03T09:20:00",
  "timeframe": "5m",
  "feature_set_version": "fs_5m_381e35d4",
  "features": [
    {"name": "ret_5m",        "value": null,      "status": "insufficient_history",
     "detail": "needs 2 completed bars, 1 available"},
    {"name": "ret_60m",       "value": null,      "status": "insufficient_history",
     "detail": "needs 13 completed bars, 1 available"},
    {"name": "range_rel",     "value": 0.0027014, "status": "valid"},
    {"name": "session_ret",   "value": -0.001221, "status": "valid"},
    {"name": "overnight_gap", "value": 0.0034916, "status": "valid"},
    {"name": "rv_short",      "value": 0.0715074, "status": "valid"},
    {"name": "rv_regime",     "value": 0.7596374, "status": "valid"}
  ],
  "quality": {"total": 18, "valid": 13, "absent": 5, "completeness": 0.7222}
}
```

Two things this shows that a bare `NaN` could not:

* The five absences are all `insufficient_history` and all **expected** — it is
  the first decision time of the session, and the `INTRADAY` return features
  have one completed bar to work with. The detail line says exactly how short
  each one is.
* `rv_short` and `rv_regime` are **valid at 09:20**, not absent. They are
  `ROLLING` and `TRAILING`, so they carry yesterday's completed bars across the
  boundary. That is the whole point of those scopes: see
  [`ROLLING` — and the 58× term](#rolling--and-the-58-term).

### When absences are not expected

The Muhurat session of 2025-10-21 opened at 11:15 and ran 17 bars. At 12:00 in
that session the state is 7 valid, 4 `insufficient_history` and 7 `missing`,
and each `missing` carries a different reason:

```
ret_5m        missing   gap stretched the window to 25 min, expected 5
body_ratio    missing   flat bar: high equals low
session_ret   missing   session opening bar absent from the data
```

Three genuinely different facts — a bar absent inside the span, a bar with no
range to normalise by, and a session whose opening bar is not in the data at
all. Collapsing them into one symbol would make the first indistinguishable
from the third, and the first is the one that silently changes what a column
named `ret_5m` actually measures.

A decision time outside a live session — a holiday, or 00:00 on a trading day —
produces a fully-shaped state where all 18 values are `market_closed`, not
`None`, so the column set stays consistent across the dataset and the reason
travels with the row.

## Determinism

A `MarketState` reads no wall clock, makes no network call, uses no randomness
and holds no mutable shared state. The same inputs always produce an identical
object and an identical serialisation.

This is what makes the historical and live paths agree: the same
`decision_time` produces the same state whether it is computed months later
from stored history or at that moment in a live session.

## Known limitations

1. **Window-level validation is coarser than per-state.** A `FAIL` verdict
   describes the window; `affects_decision_time()` narrows it, but the default
   label is the window's.
2. **`STALE` has no default threshold.** The status exists and is enforced, but
   what counts as stale is per-feature and is set when features are implemented
   (Phase 2B).
3. **Source quality covers bars only.** Tick-derived features are deferred.
4. **No bitemporal versioning.** Inherited from Phase 1: if a vendor revises a
   bar, the original is not retained, so a state is reproducible with respect to
   bar completion, not vendor restatements. Recording the extraction time is
   the only honest mitigation.

---

# Feature contract

What a feature must declare about itself before it may enter a
[`MarketState`](#market-state), and how a set of features is versioned.

Phase 2A defines the contract. Phase 2B implements the features; this section
describes the shape every one of them must fit.

## A feature is data, not just code

The naive design writes one function per feature and stops. That works until
someone asks "which features need five days of warmup?" or "did last month's
dataset use a 20-bar or a 30-bar volatility?" — and the only way to answer is
to read the code.

So `features/spec.py` makes every feature a `FeatureSpec`: a description of
what the feature *means*, with the arithmetic attached separately as `fn`.

```python
FeatureSpec(                      # the real ret_5m, from features/price.py
    name="ret_5m",
    family="price",
    timeframe="5m",
    lookback_bars=2,              # one bar back, plus the bar it is measured from
    scope=INTRADAY,
    params={"bars": 1},
    description="Simple return over the last 1 completed bar(s)",
    fn=compute_return,
)
```

That description is what lets the pipeline fetch history once for a whole set,
lets the registry hash a definition, and lets a reviewer audit a feature without
reading its implementation.

## Required metadata

Every spec answers these, and the registry exposes them:

| Field | Answers |
|---|---|
| `name` | What is this column called? |
| `family` | `price`, `volatility`, `volume`, `cross`, `label` |
| `timeframe` | `1m` or `5m` — a spec is bound to one |
| `lookback_bars` | How many completed bars does it read? |
| `scope` | May it cross a session boundary? |
| `trailing_sessions` | How long is its baseline, in sessions? |
| `warmup_bars` | When does its value become honest? |
| `params` | What are its tunables? |
| `inputs` | Which other instruments does it need? |
| `description` | What does it mean, in one line? |

A spec is bound to **one timeframe** because its windows are counted in bars,
and a 20-bar window means 20 minutes on a 1-minute series and 100 on a
5-minute one. The same feature at two timeframes is two specs.

## Scope — the session-boundary rule

**Four** scopes, encoding a decision rather than leaving it to memory. The
first three were defined in Phase 2A; `ROLLING` was added in 2B once the cost
of resetting volatility every morning was measured.

| Scope | May read prior sessions? | Used by |
|---|---|---|
| `INTRADAY` (default) | No | all 11 price features except the gap |
| `ROLLING` | Yes, as a continuous bar window | `rv_short`, `parkinson_short`, `atr_rel`, `rv_accel` |
| `TRAILING` | Yes, as a whole-session baseline | `rv_baseline`, `rv_regime` |
| `SESSION` | The boundary itself | `overnight_gap` |

### `INTRADAY` (the default)

Restarts at every session open. At 09:15 it has no history at all, and it never
reaches back into yesterday.

This is what keeps one column meaning one thing. Consider a "return over the
previous bar" column without this rule: at Wednesday 09:15 it would compute
`open_wed_0915 / close_tue_1525 - 1` — an 18-hour overnight move — and drop it
into the same column that otherwise holds 5-minute moves. Two different
quantities, one name, and nothing downstream able to tell them apart.

### `TRAILING`

May read **completed prior sessions**, and only as a baseline.

It exists so that "unusual" can mean something. A volatility regime feature
asks *is the market choppier than usual?*, and "than usual" needs a reference
level. A baseline drawn from this morning alone mostly measures the normal
intraday U-shape — volatile open, quiet lunch, volatile close — rather than
anything informative.

Trailing windows are counted in **sessions, not bars**, because "five days of
history" is the meaningful unit and its bar count differs per timeframe:

| Timeframe | Bars per session | 5 sessions |
|---|---|---|
| `5m` | 75 | 375 bars |
| `1m` | 375 | 1,875 bars |

`DEFAULT_TRAILING_SESSIONS = 5` — roughly a week: long enough to be a stable
notion of normal, short enough to follow a genuine regime shift. It is a
**default, not a constant**: a 14-session alternative was considered, and
because the value is a spec parameter it lands in the version hash, so the two
can be built as separately-labelled datasets and compared rather than argued
about.

Trailing features still read only *completed* history, so the point-in-time
guarantee is untouched.

### `SESSION`

Computed once per session from its boundary, and knowable at the open. The
overnight gap (`today_open / yesterday_close - 1`) is the only such feature
planned.

The gap deserves its own column precisely so the intraday columns stay clean:
the overnight move is real information, and isolating it is what lets
`INTRADAY` features reset without losing it.

### `ROLLING` — and the 58× term

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

### Trailing windows selected by observed dates

`scheduler/nse_calendar` ships holidays for **2026 only**, while **75%** of
stored history predates that year. Asking it for "the last five trading days"
in 2024 would name dates that were holidays, find no bars, and silently build a
four-session baseline. Sessions are therefore selected from the dates actually
present in the data.

## Warmup — when a value becomes honest

`warmup_bars` is how many completed bars must exist before the feature may emit
a number. It defaults to `lookback_bars`, and setting it **below**
`lookback_bars` is rejected: emitting a value computed from a partial window
reports a number derived from fewer bars than the feature claims to use, which
is a lie dressed as data.

Before warmup completes, the feature emits `INSUFFICIENT_HISTORY` — not a
value, and not silence. The detail line records both what was needed and what
existed:

```
needs 20 completed bars, 4 available
```

`required_bars` is what the pipeline must actually fetch. For intraday and
session features that is the warmup; for trailing features it also spans the
baseline, which is why `rv_regime` reports 375 where `rv_short` reports 21.

**Cost of warmup.** A 20-bar 5-minute feature is unavailable until roughly
10:55 each session. A 5-session trailing baseline is unavailable for an
instrument's first week — correct, since you genuinely do not know what is
normal for a contract that has existed for two days. Across ~700 sessions of
index history that is negligible; for a newly listed option it is most of its
life, and the status makes that visible rather than silently thin.

## Ordering

`FeatureSet` preserves registration order and never sorts. The vector form is
positional, so silently reordered columns between training and serving is a
genuine and unpleasant production failure. `select()` returns a subset in the
**set's** order, not the caller's, so a caller cannot permute the vector by
accident.

Re-registering a name for a timeframe raises rather than replacing: two
definitions of one column name is exactly the ambiguity versioning exists to
prevent.

## Versioning

`feature_set_version(fs)` is a deterministic content hash over what every spec
in a set *means*:

```
fs_5m_381e35d4
│  │   └─ first 8 hex of a SHA-256 over the spec identities
│  └───── timeframe
└──────── prefix
```

What it covers — and does not:

| Change | Version |
|---|---|
| Window 20 → 30 | **changes** |
| Trailing baseline 5 → 14 sessions | **changes** |
| Feature added, removed or reordered | **changes** |
| Scope or warmup changed | **changes** |
| Description reworded | unchanged |
| `params` key order | unchanged |
| Same definitions, different machine | unchanged |

The hash tracks **definitions, not values**. Rebuilding the same features over
more trading days must not change the version; changing what a feature means
must. That distinction is what makes the version usable as a dataset label.

Every state and every dataset manifest carries its version, so "which features
was this model trained on?" always has an exact answer, and two datasets built
from different definitions cannot be silently mixed.

`HASH_SCHEME_VERSION` is bumped only if the hashing scheme itself changes —
i.e. if two identical spec sets would otherwise hash differently across a
release — so old version strings stay interpretable.

## Point-in-time obligations

Every feature, without exception:

1. **Reads only through `marketdata.access.get_market_data(..., as_of=T)`.**
   No feature queries a table directly. The cut-off lives in SQL, so a bar that
   had not closed at `T` cannot be returned — look-ahead becomes structurally
   impossible rather than something reviewers must catch.
2. **Uses bar close, never bar label, as the availability test.** See
   [Bar availability](#bar-availability).
3. **Never forward-fills silently.** An absent bar means nobody traded. If a
   feature needs continuity, the fill is explicit, documented, and recorded.
4. **Never mutates source data.** Features are computed on read. Nothing in
   `features/` writes to a market-data table.
5. **Is a pure function of its inputs.** No wall-clock reads, no randomness, no
   network, no hidden state — so historical and live computation of the same
   `decision_time` agree exactly.

## Missing-data policy

Per feature, the outcome of each condition must be one of the statuses in
[Feature status](#feature-status) — never a substituted number:

| Condition | Status |
|---|---|
| Window not full yet | `insufficient_history` |
| Source bar absent | `missing` |
| Source older than tolerated | `stale` |
| Source violates its contract | `invalid_source` |
| Outside a live session | `market_closed` |
| Value legitimately zero | `valid`, value `0.0` |

The last row matters: cash indices structurally report zero volume, and that is
data. Treating it as absence would discard a fact about the instrument.

## Design principles

**Small and well-defined beats large and vague.** A hundred correlated
technical indicators is not a better feature set than twenty features whose
definitions a reviewer can check. Every feature needs a reason to exist.

**No ambiguous names.** `volatility` alone is not a feature name. The estimator,
lookback, sampling frequency, annualisation and minimum observations all belong
in the spec, and all reach the version hash.

**Explicit beats clever.** A reviewer should be able to determine, without
running anything, exactly when a feature becomes available and what it does
when its inputs are missing.

---

# Feature inventory

Phase 2B implemented **18 features and 9 labels** on both timeframes, for
**Nifty 50 only**.

Scope decisions recorded during review:

| Decision | Rationale |
|---|---|
| **Nifty 50 only** | One instrument, done properly, before breadth. |
| **No volume family** | The four tracked indices are `AMXIDX` and report `volume = 0` **by construction** — 100.00% of bars. Volume features there would be degenerate, not informative. |
| **No cross-index family** | Deferred with the second instrument. Cost is real: the state cannot distinguish "Nifty fell alone" from "everything fell". |
| **No tick/microstructure** | `TICK_RETENTION_DAYS = 7` leaves ~3 days of ticks. Order-book features are computable but not *trainable*, and never will be unless aggregates start being retained. |
| **In-session bars only** | 39 pre-open and 907 post-close 1-minute rows exist; a "session open" taken from the first observed bar would occasionally be a 09:10 pre-open print. |
| **1m and 5m both** | Same code, windows matched by elapsed time. |

## Implemented features

| Family | Features |
|---|---|
| Price (12) | `ret_5m` `ret_15m` `ret_30m` `ret_60m` `accel_1` `range_rel` `body_ratio` `upper_wick` `lower_wick` `session_ret` `pos_in_range` `overnight_gap` |
| Volatility (6) | `rv_short` `parkinson_short` `atr_rel` `rv_baseline` `rv_regime` `rv_accel` |
| Labels (9) | `fwd_{ret,dir,rv}_{15m,30m,60m}` |

> **Every one of these is documented individually in
> [06-feature-reference.md](06-feature-reference.md)** — what it measures, why
> it exists, the exact formula, a worked example against real bars, what it does
> when it cannot be computed, and its measured distribution. Read that before
> using any of these columns.

Implementations: `features/price.py`, `features/volatility.py`,
`features/labels.py`, with shared machinery in `features/windows.py`.

## Deferred families

| Family | Status |
|---|---|
| `volume` — z-score, relative volume, acceleration | Deferred; **meaningful only on FUTIDX/OPTIDX** |
| `cross` — relative returns, correlation, divergence | Deferred; four indices, co-covered on every stored session |
| Order-book / liquidity | Deferred; `TICK_RETENTION_DAYS = 7` caps tick history |

**Volume on cash indices.** The four tracked indices are `AMXIDX` and report
`volume = 0` by construction. Volume features there are degenerate by design,
not by defect; the futures series is the usable proxy for index activity.

**Tick/microstructure is deferred.** `TICK_RETENTION_DAYS = 7` caps tick
history, so order-book features cannot be trained on today.

### Open recommendation: tick aggregates

The Phase 2 brief calls order-book features "an important differentiator", and
the book data is genuinely there — every stored tick carries a fully populated
`best_5_buy` / `best_5_sell`. But retention keeps days, not years, so there is
no tick *history* to train on and never will be unless per-minute aggregates
start being persisted. The full argument, and the decision it needs, is in
[04-project-history.md](04-project-history.md#open-recommendation-tick-aggregates).

---

# Feature datasets

How a historical feature dataset is generated, what is in it, and how to read
it back.

## Building one

```bash
python -m features.build --instrument "Nifty 50" --timeframe 5m
python -m features.build --instrument "Nifty 50" --timeframe 1m
python -m features.build --instrument "Nifty 50" --from 2026-01-01 --to 2026-09-10
python -m features.build --instrument "Nifty 50" --append   # refresh, seconds not minutes
python -m features.build --list
python -m features.build --dry-run          # report the plan, write nothing
```

Exit codes match `marketdata.report`, so this works as a scheduled check:
**0** clean, **1** source warnings, **2** source errors, **3** usage.

Output lands in `data/features/` (gitignored) as a **pair**:

```
nifty_50_5m_fs_5m_381e35d4.parquet    the rows
nifty_50_5m_fs_5m_381e35d4.json       the manifest
```

## Why the filename carries a version

`fs_5m_381e35d4` is the content hash of the feature definitions. Rebuild with a
14-session trailing baseline instead of 5 and a **second** file appears beside
the first rather than silently replacing it, so two definitions can be compared
rather than confused. See [Versioning](#versioning).

## Keeping a dataset current

A full rebuild recomputes every session from the beginning, which is wasted
work when only the newest session is new:

| Timeframe | Full rebuild | `--append` |
|---|---|---|
| 5m | 4m 43s | **2.5 s** |
| 1m | ~87 min | **20 s** |

```bash
python -m features.build --instrument "Nifty 50" --timeframe 5m --append
python -m features.build --instrument "Nifty 50" --timeframe 1m --append
```

Exit codes are unchanged, so this drops into a scheduler beside the existing
services.

### It restarts *at* the last stored session, not after it

This is the one design decision worth knowing. A dataset written while a
session was still running holds only part of that session. Appending strictly
after the last stored timestamp would leave the remainder missing permanently,
and nothing would ever say so. Rebuilding one session costs seconds; silently
losing half of one is unrecoverable.

Consequences:

* **Re-running is idempotent.** Rows from that session onward are replaced, not
  duplicated.
* **A partially written session is completed** rather than skipped past.
* **Definition drift surfaces.** If the stored session no longer matches what
  the current code produces, the rebuild reveals it — which is exactly how the
  stale 5-minute dataset described below was found.

### Counts are recomputed, not accumulated

Manifest totals are derived from the final rows rather than added to the
previous run's. Incremental arithmetic would have to stay in step with which
sessions were replaced, and a drift there would be invisible; counting what is
actually in the file cannot drift.

The per-session validation summary is *merged* instead, because only windows
that found something are retained in a manifest and the aggregate cannot be
recovered from that filtered list. The manifest also gains an `incremental`
block recording what the last run rebuilt.

## Reading it back

```python
import pandas as pd
from features.registry import state_features
from features.store import read_dataset

df = read_dataset("Nifty 50", state_features("5m"))

df[df.rv_regime > 2.0]                       # unusually volatile moments
df[df.decision_time.dt.date == date(2026, 9, 10)]

# rows usable for training: both the feature and the label must be real
usable = df[(df.rv_regime__status == "valid") & (df.fwd_ret_30m__status == "valid")]
```

`read_dataset` accepts `columns=` and `filters=`, which Parquet applies while
reading — a query touching three of fifty-eight columns reads three.

### One timestamp does not need the file

For a single moment — historical or live — call the engine directly:

```python
from features.engine import build_market_state
state = build_market_state(conn, "Nifty 50", decision_time, "5m")
```

~89 ms, straight from PostgreSQL. Use the dataset for populations, the engine
for points. Both produce identical values; see [The fast path](#the-fast-path-and-why-it-is-safe).

## Schema

58 columns for the 5-minute set:

| Group | Columns |
|---|---|
| Identity | `instrument`, `decision_time`, `timeframe`, `feature_set_version` |
| Features | 18 values, each followed by `<name>__status` |
| Labels | 9 values, each followed by `<name>__status` |

Every value column has a status partner, so an empty cell always carries its
reason: `valid`, `insufficient_history`, `missing`, `stale`, `invalid_source`,
`market_closed`. A dataset that lost those reasons would make "the window was
not full yet" indistinguishable from "the source data was broken".

**One row per decision time**, 09:15 to 15:25 on the bar grid — 75 per session
at 5m, 375 at 1m. The session open is included because the rolling volatility
features already carry the previous session's history there.

## What a real build produced

Nifty 50, 5-minute, 2023-11-21 → 2026-09-11:

```
rows         52,275 across 697 sessions      10.7 MB
features     913,978/940,950 valid (97.1%)
labels       424,149/470,475 valid (90.2%)
source data  WARNING  (697 windows, 0 errors, 262 warnings)
```

Both 5m and 1m datasets span 2023-11-21 to 2026-09-11. Version hashes:
`fs_5m_381e35d4` and `fs_1m_dab72e07`.

> **The label figure was previously reported as 91.4%, over a denominator of
> 463,320.** That denominator was short by 6,480 entries — the 720 rows whose
> label columns were missing entirely under the ragged-row bug (696 session
> opens, plus the 24 pre-open slots of the 11:15 session). Rows that emitted no
> labels could not be counted as invalid, so the old percentage was flattered by
> the defect it was hiding. 90.2% is measured over every row.

Availability is exactly what the design predicts, and the numbers
cross-check:

| Feature | Valid | Why the rest are absent |
|---|---|---|
| `rv_short`, `parkinson_short`, `atr_rel` | ~100% | Rolling scope: only the first ~20 bars of *all* history lack a window |
| `rv_baseline`, `rv_regime` | 99.3% | Exactly **375** absent = 5 sessions × 75 bars, the trailing warmup |
| `range_rel` | 98.6% | Exactly **720** absent = 696 session opens + 24 slots of the one session that starts at 11:15 |
| `session_ret`, `pos_in_range` | 98.5% | The same 720, plus **50** `missing` — the 50 decision times in that 11:15 session where bars exist but the opening bar does not |
| `ret_60m` | 82.4% | Needs 13 bars, so the first 12 decision times of every session |
| `fwd_ret_60m` | 83.5% | Needs 12 bars forward, so the last 12 of every session |

That the 11:15 session accounts for precisely 24 `insufficient_history` and 50
`missing` slots — rather than approximately — is a useful check that the
session-anchoring rules do what they claim.

### The 1-minute set

```
rows         261,375 across 697 sessions     47.0 MB
features     4,649,885/4,704,750 valid (98.8%)
labels       2,235,837/2,352,375 valid (95.0%)
source data  WARNING  (697 windows, 0 errors, 1,425 warnings)
```

Availability is *higher* at 1m than at 5m, and the reason is the horizon
tables rather than anything about resolution. Each timeframe starts its return
horizons at its own bar size, so the longest lookback is `ret_60m` at 5m but
only `ret_30m` at 1m:

| | Longest horizon | Bars needed | Share of session |
|---|---|---|---|
| 5m | `ret_60m` | 13 of 75 | **17.3%** |
| 1m | `ret_30m` | 31 of 375 | **8.3%** |

The same asymmetry applies to the labels, whose longest horizon is likewise 60
minutes at 5m and 30 at 1m. Extending the 1m set to a 60-minute horizon would
bring its availability down to match.

Quantities that mean the same thing on both timeframes agree closely, which is
a useful independent cross-check that the time-matched windows are matched
correctly:

| Column | 1m median | 5m median |
|---|---|---|
| `session_ret` | −0.00023 | −0.00022 |
| `overnight_gap` | +0.00071 | +0.00073 |
| `pos_in_range` | +0.51679 | +0.51778 |
| `rv_baseline` | +0.08981 | +0.08810 |

`overnight_gap` agreeing to four decimal places is the strongest of these: it
is the same computation over the same session boundaries, differing only
because the previous session's last bar is 15:29 at 1m and 15:25 at 5m.

`rv_regime` differs more (0.895 vs 0.843) because its short window holds 100
one-minute returns against 20 five-minute ones — the same elapsed time,
different estimator noise.

### Distributions

| Column | Median | Sanity check |
|---|---|---|
| `ret_5m` | +0.00001 | Mean ≈ 0, sd 0.071% — matches the 0.0716% measured directly from raw bars |
| `overnight_gap` | +0.00073 | sd 0.57% — matches the 0.5455% measured for boundary returns |
| `pos_in_range` | 0.518 | Centred on 0.5, as it must be if price is equally likely anywhere in its range |
| `body_ratio` | 0.469 | Centred, no pile-up at 0 or 1 |
| `rv_short` | 7.35% | Plausible annualised realized volatility for Nifty |
| `rv_regime` | 0.843 | **Centred near 1.0** — the ratio is doing its job |
| `fwd_ret_30m` | +0.00002 | Mean ≈ 0, as an efficient market requires |
| `fwd_dir_30m` | — | 24,162 up / 23,563 down / **35 flat** |

`rv_regime` sitting slightly below 1.0 is expected rather than a bias:
`rv_short` measures a 100-minute intraday window, while `rv_baseline` covers
whole prior sessions including their volatile opens, so the typical intraday
window really is a little quieter than the full-session reference.

Those **35 flat** rows are why `fwd_dir` is three-valued. Folding them into up
or down would invent a direction the market did not take, and the choice would
be unrecoverable afterwards.

## The fast path, and why it is safe

Building one state at a time costs ~89 ms — 77 minutes for this dataset, and
roughly six hours at 1m — because almost all of it is a database round trip
re-reading bars the previous decision time already fetched.

The builder therefore reads **once per session** and slides the decision time
through bars held in memory: ~700 queries instead of 52,000, and the build
completes in minutes.

An optimisation here is only acceptable if it changes nothing, so
`tests/test_feature_dataset.py` asserts the fast path is **row-for-row
identical** to `build_market_state`, statuses included. It was also checked
against the live database across the full three-year span: 40 sampled rows ×
18 features, **zero mismatches**.

Without that guarantee a model could be trained on this dataset and served by
subtly different arithmetic — a failure that produces no error anywhere.

## The manifest

Everything needed to judge a dataset without rebuilding it:

* the instrument, timeframe and date range;
* **the extraction time**, in UTC and IST — the market-data tables carry no
  as-of versioning, so when the data was pulled is the only honest record of
  what it saw;
* the full definition of every feature and label, with versions and digests;
* per-column status counts;
* the per-session source-validation verdict, and the detail of every session
  that was not clean.

The overall verdict is the **worst** session, never an average — averaging is
how one broken day disappears into a reassuring number.

A `WARNING` verdict does not make a dataset unusable. It usually means absent
in-session bars, which is information about the market rather than damage. The
`FAIL` case — a contract violation — has never occurred in this history: 0
errors across 697 sessions, consistent with the 0 violations found across all
2.17M stored bars.

## Known limitations

1. **Gap-stretched spans are dropped, not repaired.** A missing in-session bar
   widens a point-to-point span, so those rows report `missing` rather than a
   number that would not mean what its column name says. Volatility aggregates
   are deliberately exempt: averaging twenty terms bounds the effect, whereas
   a return is fixed entirely by its two endpoints.
2. **Trailing baselines vary in bar count.** Sessions are not uniformly full
   (5% of Nifty 50 sessions are short, one has 17 bars), so a five-session
   baseline spans five *sessions*, not a constant number of returns.
3. **`fwd_rv_15m` rests on three returns.** Statistically thin and noisy; the
   60-minute horizon is the more trustworthy of the three.
4. **No vendor-revision history**, inherited from Phase 1. A dataset is
   reproducible with respect to bar completion, not restatements.
5. **One instrument per file.** Cross-instrument datasets would need the cross
   family, which is out of scope for this phase.
