# Feature Contract

What a feature must declare about itself before it may enter a
[`MarketState`](market-state.md), and how a set of features is versioned.

Phase 2A defines the contract. The features themselves arrive in Phase 2B; this
document describes the shape every one of them must fit.

---

## 1. A feature is data, not just code

The naive design writes one function per feature and stops. That works until
someone asks "which features need five days of warmup?" or "did last month's
dataset use a 20-bar or a 30-bar volatility?" — and the only way to answer is
to read the code.

So `features/spec.py` makes every feature a `FeatureSpec`: a description of
what the feature *means*, with the arithmetic attached separately as `fn`.

```python
FeatureSpec(
    name="ret_1",
    family="price",
    timeframe="5m",
    lookback_bars=2,          # needs the previous close too
    scope=INTRADAY,
    description="Return over the previous completed bar",
    fn=compute_ret_1,
)
```

That description is what lets the pipeline fetch history once for a whole set,
lets the registry hash a definition, and lets a reviewer audit a feature without
reading its implementation.

---

## 2. Required metadata

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

---

## 3. Scope — the session-boundary rule

Three scopes, encoding a decision rather than leaving it to memory.

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

---

## 4. Warmup — when a value becomes honest

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
baseline, which is why `rv_regime` reports 375 where `rv_20` reports 20.

**Cost of warmup.** A 20-bar 5-minute feature is unavailable until roughly
10:55 each session. A 5-session trailing baseline is unavailable for an
instrument's first week — correct, since you genuinely do not know what is
normal for a contract that has existed for two days. Across 694 sessions of
index history that is negligible; for a newly listed option it is most of its
life, and the status makes that visible rather than silently thin.

---

## 5. Ordering

`FeatureSet` preserves registration order and never sorts. The vector form is
positional, so silently reordered columns between training and serving is a
genuine and unpleasant production failure. `select()` returns a subset in the
**set's** order, not the caller's, so a caller cannot permute the vector by
accident.

Re-registering a name for a timeframe raises rather than replacing: two
definitions of one column name is exactly the ambiguity versioning exists to
prevent.

---

## 6. Versioning

`feature_set_version(fs)` is a deterministic content hash over what every spec
in a set *means*:

```
fs_5m_1a6b3994
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

---

## 7. Point-in-time obligations

Every feature, without exception:

1. **Reads only through `marketdata.access.get_market_data(..., as_of=T)`.**
   No feature queries a table directly. The cut-off lives in SQL, so a bar that
   had not closed at `T` cannot be returned — look-ahead becomes structurally
   impossible rather than something reviewers must catch.
2. **Uses bar close, never bar label, as the availability test.** See
   [`market-state.md`](market-state.md) §3.
3. **Never forward-fills silently.** An absent bar means nobody traded. If a
   feature needs continuity, the fill is explicit, documented, and recorded.
4. **Never mutates source data.** Features are computed on read. Nothing in
   `features/` writes to a market-data table.
5. **Is a pure function of its inputs.** No wall-clock reads, no randomness, no
   network, no hidden state — so historical and live computation of the same
   `decision_time` agree exactly.

---

## 8. Missing-data policy

Per feature, the outcome of each condition must be one of the statuses in
[`market-state.md`](market-state.md) §4 — never a substituted number:

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

---

## 9. Feature families

Planned for Phase 2B, in the brief's priority order:

| Family | Status |
|---|---|
| `price` — returns, momentum, acceleration, range, candle structure, overnight gap | Planned |
| `volatility` — realized vol, regime ratio, vol shocks | Planned, reusing `backtest/rv.py` estimators |
| `volume` — z-score, relative volume, acceleration | Planned; **meaningful only on FUTIDX/OPTIDX** |
| `cross` — relative returns, correlation, divergence | Planned; four indices, 694 co-covered sessions |
| `label` — forward return, direction, forward RV | Planned; horizons parameterised |

**Volume on cash indices.** The four tracked indices are `AMXIDX` and report
`volume = 0` by construction. Volume features there are degenerate by design,
not by defect; the futures series is the usable proxy for index activity.

**Tick/microstructure is deferred.** `TICK_RETENTION_DAYS = 7` caps tick
history, so order-book features cannot be trained on today. See
[`phase-2a-summary.md`](phase-2a-summary.md) for the recommendation.

---

## 10. Design principles

**Small and well-defined beats large and vague.** A hundred correlated
technical indicators is not a better feature set than twenty features whose
definitions a reviewer can check. Every feature needs a reason to exist.

**No ambiguous names.** `volatility` alone is not a feature name. The estimator,
lookback, sampling frequency, annualisation and minimum observations all belong
in the spec, and all reach the version hash.

**Explicit beats clever.** A reviewer should be able to determine, without
running anything, exactly when a feature becomes available and what it does
when its inputs are missing.
