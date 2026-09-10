# Feature Datasets

How a historical feature dataset is generated, what is in it, and how to read
it back.

---

## 1. Building one

```bash
python -m features.build --instrument "Nifty 50" --timeframe 5m
python -m features.build --instrument "Nifty 50" --timeframe 1m
python -m features.build --instrument "Nifty 50" --from 2026-01-01 --to 2026-09-10
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

---

## 2. Why the filename carries a version

`fs_5m_381e35d4` is the content hash of the feature definitions. Rebuild with a
14-session trailing baseline instead of 5 and a **second** file appears beside
the first rather than silently replacing it, so two definitions can be compared
rather than confused. See [`feature-contract.md`](feature-contract.md) §6.

---

## 3. Reading it back

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
for points. Both produce identical values; §6 explains why that matters.

---

## 4. Schema

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

---

## 5. What a real build produced

Nifty 50, 5-minute, 2023-11-21 → 2026-09-10:

```
rows         52,200 across 696 sessions      10.7 MB
features     912,669/939,600 valid (97.1%)
labels       423,564/463,320 valid (91.4%)
source data  WARNING  (696 sessions, 0 errors, 261 warnings)
```

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
rows         261,000 across 696 sessions     46.9 MB
features     4,643,226/4,698,000 valid (98.8%)
labels       2,232,738/2,349,000 valid (95.1%)
source data  WARNING
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

---

## 6. The fast path, and why it is safe

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

---

## 7. The manifest

Everything needed to judge a dataset without rebuilding it:

* the instrument, timeframe and date range;
* **the extraction time**, in UTC and IST — the market-data tables carry no
  as-of versioning ([`point-in-time-data.md`](point-in-time-data.md) §5.4), so
  when the data was pulled is the only honest record of what it saw;
* the full definition of every feature and label, with versions and digests;
* per-column status counts;
* the per-session source-validation verdict, and the detail of every session
  that was not clean.

The overall verdict is the **worst** session, never an average — averaging is
how one broken day disappears into a reassuring number.

A `WARNING` verdict does not make a dataset unusable. It usually means absent
in-session bars, which is information about the market rather than damage. The
`FAIL` case — a contract violation — has never occurred in this history: 0
errors across 696 sessions, consistent with the 0 violations found across all
2.17M stored bars.

---

## 8. Known limitations

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
