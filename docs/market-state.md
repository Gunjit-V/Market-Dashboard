# Market State

The Phase 2 answer object. Everything knowable about one instrument at one
decision time, and an explicit account of what was *not* knowable.

The north-star question:

> What was the state of the market at decision time `T`, using only
> information that was actually available at `T`?

---

## 1. Decision time

`decision_time` is **the instant a decision would have been made**. It is not a
bar label, not a timestamp read from a row, and not the moment the dataset was
built.

Everything in a `MarketState` was derived from data that had already become
knowable at that instant. For bars that means bars which had **closed** — see
§3 — and the cut-off is enforced in SQL by
`marketdata.access.get_market_data(..., as_of=T)` rather than by convention.

`decision_time` is **naive**, read as Asia/Kolkata, matching every stored
timestamp in the project. A timezone-aware value is rejected outright:
silently comparing an aware datetime against naive stored data would mean
something different from every other timestamp in the system.

---

## 2. The object

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

`feature_set_version` (e.g. `fs_5m_1a6b3994`) is a content hash of the feature
definitions. It answers "what did these columns mean?" for any state, however
long after it was built. See [`feature-contract.md`](feature-contract.md) §6.

---

## 3. Bar availability

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

---

## 4. Feature status

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
(see [`data-contract.md`](data-contract.md)); that is data.

---

## 5. Quality

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

---

## 6. Serialisation

Two forms, both deterministic — equal states produce byte-identical output.

**`as_dict()`** — nested JSON, with `decision_time` as ISO-8601. For inspection
and for the manifest.

**`to_row()`** — flat, for tabular storage. Each feature emits **two** columns:

```
instrument  decision_time  timeframe  feature_set_version
ret_1  ret_1__status   rv_20  rv_20__status   ...
```

The parallel `__status` column is what stops a stored dataset from losing the
reason a value is absent. `to_vector()` uses `None` rather than `NaN` for the
same reason: "no value" stays distinguishable from a genuine number all the way
into the dataset.

---

## 7. Worked example

Four bars into a session, at 09:35 on a 5-minute series:

```json
{
  "instrument": "Nifty 50",
  "decision_time": "2026-09-03T09:35:00",
  "timeframe": "5m",
  "feature_set_version": "fs_5m_1a6b3994",
  "features": [
    {"name": "ret_1",         "value": 0.00121, "status": "valid"},
    {"name": "rv_20",         "value": null,    "status": "insufficient_history",
     "detail": "needs 20 completed bars, 4 available"},
    {"name": "rv_regime",     "value": null,    "status": "insufficient_history",
     "detail": "needs 375 completed bars, 4 available"},
    {"name": "overnight_gap", "value": -0.0034, "status": "valid"},
    {"name": "bn_spread",     "value": null,    "status": "missing",
     "detail": "no Nifty Bank bar at 09:30"}
  ],
  "quality": {"total": 5, "valid": 2, "absent": 3, "completeness": 0.4}
}
```

Three absences, three different reasons. The two `insufficient_history` values
are **expected** — it is early in the session. The `missing` one is a data
problem worth investigating. Under a bare-`NaN` design all three would look
identical.

`rv_regime` needing 375 bars where `rv_20` needs 20 is the trailing-baseline
rule showing through: five completed sessions at 75 bars each. See
[`feature-contract.md`](feature-contract.md) §3.

A holiday produces a fully-shaped state where every value is
`market_closed` — not `None`, so the column set stays consistent across the
dataset and the reason travels with the row.

---

## 8. Determinism

A `MarketState` reads no wall clock, makes no network call, uses no randomness
and holds no mutable shared state. The same inputs always produce an identical
object and an identical serialisation.

This is what makes the historical and live paths agree: the same
`decision_time` produces the same state whether it is computed months later
from stored history or at that moment in a live session.

---

## 9. Known limitations

1. **Window-level validation is coarser than per-state.** A `FAIL` verdict
   describes the window; `affects_decision_time()` narrows it, but the default
   label is the window's.
2. **`STALE` has no default threshold.** The status exists and is enforced, but
   what counts as stale is per-feature and is set when features are implemented
   (Phase 2B).
3. **Source quality covers bars only.** Tick-derived features are deferred; see
   [`phase-2a-summary.md`](phase-2a-summary.md).
4. **No bitemporal versioning.** Inherited from Phase 1: if a vendor revises a
   bar, the original is not retained, so a state is reproducible with respect to
   bar completion, not vendor restatements. Recording the extraction time is
   the only honest mitigation.
