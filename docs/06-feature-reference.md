# Feature Reference

Every feature and every label built in Phase 2, one at a time: what it
measures, why it exists, exactly how it is computed, and what it does when it
cannot be computed.

This is a **lookup document**. [03-features.md](03-features.md) explains the
system the features live in — the state object, the status model, the
versioning, the datasets. This explains the eighteen numbers and nine targets
themselves.

**18 features + 9 labels**, built for **Nifty 50 only**, on both the 1-minute
and 5-minute bar series.

---

## Contents

* [How to read this document](#how-to-read-this-document)
* [Shared machinery](#shared-machinery) — the parts every feature depends on
* **Price features** —
  [returns](#the-return-family-ret_) ·
  [accel_1](#accel_1) ·
  [range_rel](#range_rel) ·
  [body_ratio](#body_ratio) ·
  [upper_wick](#upper_wick--lower_wick) ·
  [lower_wick](#upper_wick--lower_wick) ·
  [session_ret](#session_ret) ·
  [pos_in_range](#pos_in_range) ·
  [overnight_gap](#overnight_gap)
* **Volatility features** —
  [rv_short](#rv_short) ·
  [parkinson_short](#parkinson_short) ·
  [atr_rel](#atr_rel) ·
  [rv_baseline](#rv_baseline) ·
  [rv_regime](#rv_regime) ·
  [rv_accel](#rv_accel)
* **Labels** —
  [fwd_ret_*](#fwd_ret_h) ·
  [fwd_dir_*](#fwd_dir_h) ·
  [fwd_rv_*](#fwd_rv_h)
* [Quick reference table](#quick-reference-table)

---

## How to read this document

### Notation

At a decision time `T`, write the **completed** bars — those that had closed at
`T` — most-recent-first:

```
c[0]   close of the most recent completed bar
c[-1]  close of the bar before it
o[0], h[0], l[0]   open, high, low of the most recent completed bar
```

A bar labelled `09:15` on a 5-minute series covers `[09:15, 09:20)` and is
complete at **09:20**. So at `T = 14:00`, `c[0]` is the close of the bar
labelled **13:55**, not 14:00. This is the whole point-in-time discipline in one
sentence; see [03-features.md — Bar availability](03-features.md#bar-availability).

### The running example

Every worked example below uses the same real moment, so the features can be
read together as one coherent picture:

> **Nifty 50, 5-minute, decision time 2026-09-10 14:00**

The bars in play:

| | open | high | low | close |
|---|---|---|---|---|
| 13:40 | 23441.20 | 23446.35 | 23436.35 | 23440.00 |
| 13:45 | 23440.50 | 23440.85 | 23428.05 | 23431.90 |
| 13:50 | 23433.55 | 23436.70 | 23429.30 | 23434.70 |
| **13:55** (`[0]`) | **23432.85** | **23433.55** | **23423.05** | **23425.20** |

Session context: opened at **23446.60**, high so far **23494.95**, low so far
**23403.05**. The previous session (2026-09-09) closed at **23431.50**.

### Distributions

Each feature carries its measured distribution over the 5-minute dataset —
52,200 rows, 696 sessions, 2023-11-21 to 2026-09-10. These are what the numbers
actually look like, not what they theoretically could be.

---

## Shared machinery

Five things that most features depend on. Understanding these once makes every
individual feature short.

### 1. The bar window

Each feature receives a `BarWindow`: the completed, **in-session** bars
available at `T`, plus three ways to slice them.

| Slice | Returns | Used by |
|---|---|---|
| `intraday()` | bars from **today's session only** | every price feature |
| `rolling(n)` | the last `n` bars, **ignoring session boundaries** | `rv_short`, `parkinson_short`, `atr_rel`, `rv_accel` |
| `trailing(n)` | all bars from the last `n` **observed prior sessions** | `rv_baseline`, `rv_regime` |

Bars outside 09:15–15:30 are filtered out before any of this. The tables
contain 39 pre-open and 907 post-close 1-minute rows; a "session open" taken
from the first observed bar would occasionally be a 09:10 pre-open print.

`trailing` selects sessions by the dates **actually present in the data**, never
from the holiday calendar — `scheduler/nse_calendar` ships 2026 holidays only,
while ~75% of stored history predates that year.

### 2. Log returns, and the boundary exclusion

```python
r[i] = ln( close[i] / close[i-1] )
```

Log returns are used for every volatility estimate because they are **additive
over time** — the log return over an hour is the sum of the twelve 5-minute log
returns inside it — and because their distribution is better behaved in the
tails than simple returns.

**Any return spanning a session boundary is discarded.** This is not a
refinement; it is load-bearing. Measured on the stored Nifty 50 5-minute bars:

| | Std dev | Variance vs intraday |
|---|---|---|
| Intraday 5-minute return | 0.0716% | — |
| Session-boundary return | **0.5455%** | **58×** |

A single boundary return inside a 20-return window inflates the variance **3.9×**
and overstates realized volatility **2.0×**. And because the contaminated
window is always the first twenty bars of a session, the error would appear
every single morning, in a pattern indistinguishable from the genuine
opening-session volatility it would be mistaken for. That is the worst kind of
bug: plausible and wrong.

The overnight move is not discarded from the system — it is reported by
[`overnight_gap`](#overnight_gap) in its own column.

### 3. Population standard deviation

```
sd(x) = sqrt( (1/n) · Σ (xᵢ − x̄)² )
```

Divided by `n`, not `n−1`. This matches `backtest/rv.py`, so the dashboard, the
backtester and the feature layer report the same volatility number — a test
asserts the agreement. Returns fewer than two observations → no value.

### 4. Annualisation

A standard deviation of 5-minute returns is a tiny number. Multiplying by the
square root of the number of such periods in a year converts it to the
conventional "annualised volatility" that VIX-style figures are quoted in:

```
annualisation(tf) = sqrt( bars_per_session(tf) × 252 )
```

| Timeframe | Bars/session | Factor |
|---|---|---|
| 5m | 75 | **√18,900 ≈ 137.48** |
| 1m | 375 | **√94,500 ≈ 307.41** |

So `rv_short = 0.0402` means: *if the market kept moving this way for a year,
the annual standard deviation of returns would be about 4%.*

The √time scaling assumes returns are independent across bars. They are not
perfectly — volatility clusters — so treat the annualised figure as a
**comparable unit**, not a literal forecast of next year.

`backtest/rv.py` hardcodes the 5-minute factor; this module derives it per
timeframe, which is why the 1-minute set is correct rather than 4× too small.

### 5. Windows are matched by elapsed time, not bar count

A "20-bar window" means 100 minutes at 5m and 20 minutes at 1m — different
things under one name. So every window is declared in **minutes** and converted:

| Constant | Minutes | 5m bars | 1m bars |
|---|---|---|---|
| `RV_WINDOW_MINUTES` | 100 | 20 | 100 |
| `ATR_WINDOW_MINUTES` | 70 | 14 | 70 |
| `RV_ACCEL_LAG_MINUTES` | 30 | 6 | 30 |

The same applies to the return horizons: `ret_15m` is 3 bars at 5m and 15 bars
at 1m. This is why the two datasets agree so closely on shared quantities.

---

# Price features

Twelve features describing **where price is and how it got there**. All are
computed from today's session only (`INTRADAY`), except `overnight_gap`.

**Why intraday.** Every feature here is either a point-to-point span
(`c[0]/c[-n]`) or a property of a single bar. A span reaching into yesterday
would carry the overnight move *inside a single ratio*, where — unlike a
volatility sum — there is no term to drop. One column would then hold 5-minute
moves on most rows and 18-hour moves at every session open.

---

## The return family (`ret_*`)

Four features, same computation, different horizons.

| Timeframe | Features (bars back) |
|---|---|
| **5m** | `ret_5m` (1) · `ret_15m` (3) · `ret_30m` (6) · `ret_60m` (12) |
| **1m** | `ret_1m` (1) · `ret_5m` (5) · `ret_15m` (15) · `ret_30m` (30) |

### What it measures

The plainest question you can ask: **how far has price moved over the last N
minutes?** Positive means up.

### Why it exists

Price *level* is useless to a model — 23,425 is high compared to 2023 and low
compared to some future year, so a model trained on levels learns a range rather
than a behaviour. A **return** is scale-free: it means the same thing at Nifty
23,000 as at 30,000.

Four horizons rather than one because the *shape* across them carries
information a single number cannot. In the worked example the hour is down
0.09% but the last half hour only 0.03% — the decline decelerated. One horizon
cannot say that.

### Exact calculation

```
ret_Nm = c[0] / c[-N] − 1
```

where `N` is the bar count for that horizon at that timeframe.

### Worked example — `ret_15m` at 14:00

`ret_15m` is 3 bars at 5m, so it spans 13:40 → 13:55:

```
23425.20 / 23440.00 − 1 = −0.000631
```

All four at this moment:

| | Spans | Value |
|---|---|---|
| `ret_5m` | 13:50 → 13:55 | −0.000405 |
| `ret_15m` | 13:40 → 13:55 | −0.000631 |
| `ret_30m` | 13:25 → 13:55 | −0.000254 |
| `ret_60m` | 12:55 → 13:55 | −0.000908 |

### When it has no value

| Condition | Status | Detail |
|---|---|---|
| Fewer than N+1 bars today | `insufficient_history` | `needs 13 completed bars, 4 available` |
| A missing bar stretched the span | `missing` | `gap stretched the window to 20 min, expected 15` |
| Reference close ≤ 0 | `missing` | `non-positive reference close` |

**The gap check matters.** On 2026-09-10 the 15:15 bar is absent, so "three
bars back from 15:20" reaches 15:05 — a 20-minute span in a column named for
15. A point-to-point return is fixed entirely by its two endpoints, so a
stretched span is not slightly biased, it is **a different measurement wearing
the same name**. Those rows report `missing` instead.

### Distribution (5m)

| Feature | Valid | p25 | median | p75 | min / max |
|---|---|---|---|---|---|
| `ret_5m` | 97.2% | −0.00033 | +0.00001 | +0.00033 | −1.13% / +1.50% |
| `ret_15m` | 94.5% | −0.00056 | +0.00001 | +0.00057 | −2.05% / +2.42% |
| `ret_30m` | 90.5% | −0.00078 | +0.00002 | +0.00079 | −3.38% / +2.90% |
| `ret_60m` | 82.4% | −0.00111 | +0.00002 | +0.00111 | −4.28% / +3.34% |

Medians essentially zero and quartiles near-symmetric — as an efficient market
requires. Note the spread widens with horizon roughly as √time, which is what
you would expect if returns were close to independent.

Availability falls with horizon because a longer lookback disqualifies more of
each session's opening: `ret_60m` needs 13 bars, so the first 12 decision times
of every session cannot have it.

---

## `accel_1`

### What it measures

**Is the move speeding up or fading?** The change in the one-bar return from
one bar to the next — the discrete second derivative of price.

### Why it exists

`ret_5m` says price fell. It cannot say whether the fall is accelerating into
something, or petering out. Momentum and its rate of change are different
signals, and a trend that is decelerating often behaves differently from one
that is not.

### Exact calculation

```
latest   = c[0]  / c[-1] − 1
previous = c[-1] / c[-2] − 1
accel_1  = latest − previous
```

Units are the difference of two returns, so read it as *percentage points of
return per bar*.

### Worked example at 14:00

```
latest   = 23425.20 / 23434.70 − 1 = −0.000405
previous = 23434.70 / 23431.90 − 1 = +0.000119
accel_1  = −0.000405 − 0.000119   = −0.000525
```

Price flipped from drifting **up** to drifting **down** — a genuine turn, not
merely a continuation.

### When it has no value

Needs **3** completed bars today. The span 13:45 → 13:55 is also gap-checked
against the clock (2 bars = 10 minutes at 5m), so a missing bar anywhere inside
it yields `missing`.

### Distribution (5m)

Valid 95.9%. Median −0.0000004, p25 −0.00049, p75 +0.00048, range ±1.9%.
Symmetric around zero, as a second difference of a near-random walk should be.

---

## `range_rel`

### What it measures

**How wide was the last bar**, as a fraction of its own price.

### Why it exists

A 10-point bar means something completely different on a 5,000-point index than
on a 23,000-point one. Dividing by the close makes bar width comparable across
price levels and across time — the same normalisation logic as using returns
instead of prices.

It is also the cheapest possible turbulence signal: one bar, no history.

### Exact calculation

```
range_rel = (h[0] − l[0]) / c[0]
```

### Worked example at 14:00

```
(23433.55 − 23423.05) / 23425.20 = 10.50 / 23425.20 = 0.000448
```

The bar spanned 0.045% of price.

### When it has no value

| Condition | Status |
|---|---|
| No completed bar today | `insufficient_history` |
| Close ≤ 0 | `missing` |

**A flat bar gives a valid `0.0`, not an absence.** `high == low` means nothing
moved, which is a measurement. Only division by a non-positive close is
undefined. This is the distinction from the three shape features below, which
divide *by* the range and therefore genuinely cannot be computed when it is
zero.

### Distribution (5m)

Valid 98.6%. Median 0.00077 (0.077% of price), p25 0.00054, p75 0.00113, max
0.0223. Right-skewed, as volatility measures always are — most bars are quiet,
a few are not.

---

## `body_ratio`

### What it measures

**Was the bar decisive or indecisive?** What fraction of the bar's total range
was covered by the net move from open to close.

### Why it exists

Two bars can have identical ranges and opposite meanings. One opens at the low
and closes at the high — a clean directional push. The other swings the full
range and closes where it opened — a fight with no winner. `range_rel` cannot
tell them apart; `body_ratio` can.

* **near 1.0** — price went one way and stayed; strong conviction
* **near 0.0** — a doji; the bar travelled and returned

### Exact calculation

```
body_ratio = |c[0] − o[0]| / (h[0] − l[0])
```

Absolute value, so this measures **conviction, not direction** — the direction
is already in `ret_*`. Bounded in `[0, 1]` by construction, since the body
cannot exceed the range.

### Worked example at 14:00

```
|23425.20 − 23432.85| / (23433.55 − 23423.05)
       = 7.65 / 10.50
       = 0.729
```

73% of the bar's movement was net directional. A decisive down-bar.

### When it has no value

| Condition | Status | Detail |
|---|---|---|
| No completed bar today | `insufficient_history` | |
| `high == low` | `missing` | `flat bar: high equals low` |

The flat-bar case is a genuine division by zero: there is no meaningful
body-to-range ratio when there is no range. Defaulting it to 0 or 1 would
invent data. On Nifty 50 this is **31 of 52,011 bars (0.06%)**; on illiquid
options it reaches ~6.6%.

### Distribution (5m)

Valid 98.5%. Median 0.469, p25 0.246, p75 0.686, full range 0 to 1. Well spread
with no pile-up at either end — bars genuinely vary in decisiveness.

---

## `upper_wick` / `lower_wick`

### What they measure

**Rejection.** How much of the bar's range was territory price visited and then
abandoned — above the body (`upper_wick`) or below it (`lower_wick`).

### Why they exist

A long upper wick means price pushed higher and was sold back down: sellers
defended that level. A long lower wick means the opposite. Together with
`body_ratio` they decompose the bar completely:

```
body_ratio + upper_wick + lower_wick = 1
```

The three always sum to exactly one, because the body and the two wicks tile the
range. That makes them a proper decomposition rather than three loosely related
indicators — and it means one is redundant given the other two, which is worth
knowing before feeding all three to a linear model.

### Exact calculation

```
upper_wick = ( h[0] − max(o[0], c[0]) ) / (h[0] − l[0])
lower_wick = ( min(o[0], c[0]) − l[0] ) / (h[0] − l[0])
```

`max(open, close)` is the top of the body regardless of whether the bar rose or
fell, so the formula does not need a direction branch.

### Worked example at 14:00

The bar fell, so the body runs from 23425.20 (close) up to 23432.85 (open):

```
upper_wick = (23433.55 − 23432.85) / 10.50 = 0.70 / 10.50 = 0.067
lower_wick = (23425.20 − 23423.05) / 10.50 = 2.15 / 10.50 = 0.205
check:       0.729 + 0.067 + 0.205 = 1.000 ✓
```

Almost no rejection from above; a modest tail below — price dipped to 23423.05
and recovered 2.15 points before the bar closed.

### When they have no value

Identical to `body_ratio`: `insufficient_history` with no bar, `missing` on a
flat bar.

### Distribution (5m)

Both valid 98.5%. `upper_wick` median 0.214, `lower_wick` median 0.231 — nearly
symmetric, with the lower wick very slightly larger on average.

---

## `session_ret`

### What it measures

**Where the day stands.** Return from today's opening price to the latest close.

### Why it exists

The `ret_*` family describes recent motion over fixed windows. `session_ret`
anchors to something a trader actually watches — the day's open — and answers
"are we up or down today?", which resets every morning and accumulates through
the session.

### Exact calculation

```
session_ret = c[0] / open_of_the_09:15_bar − 1
```

Note the denominator is the **open** of the session's first bar, not its close.
That is the first traded price of the day.

### Worked example at 14:00

```
23425.20 / 23446.60 − 1 = −0.000913
```

Down 0.09% on the day.

### When it has no value

| Condition | Status | Detail |
|---|---|---|
| No completed bar today (09:15) | `insufficient_history` | |
| Bars exist but **no 09:15 bar** | `missing` | `session opening bar absent from the data` |

These two are deliberately different. At 09:15 the opening bar has not *closed*
yet — normal, every session. But one Nifty 50 session in 697 (**2025-10-21**, a
Muhurat session) begins at **11:15**, and there the opening bar is genuinely
absent from the data. Anchoring to the 11:15 bar would produce a number that
looks like a session return but measures four hours of an unknown day.

That single session accounts for exactly 50 `missing` rows in the 5m dataset —
precisely its 50 decision times from 11:15 onward.

### Distribution (5m)

Valid 98.5%. Median −0.00022, p25 −0.0028, p75 +0.0025, range −8.1% to +2.4%.
Much wider than `ret_5m`, as it should be — it accumulates the whole session.

---

## `pos_in_range`

### What it measures

**Where price sits within today's range so far.** 0 at the session low, 1 at the
session high.

### Why it exists

`session_ret` says whether we are up or down versus the open. `pos_in_range`
says something different and often more useful: whether we are pressed against
the day's extremes or sitting in the middle. Closing near the high of a range
after a wide day is a different market state from closing mid-range, even with
identical `session_ret`.

It is also **bounded in [0, 1] by construction**, which makes it well-behaved
for models without any scaling.

### Exact calculation

Over all completed bars of today's session:

```
session_low  = min( l[i] )   over today's completed bars
session_high = max( h[i] )   over today's completed bars

pos_in_range = ( c[0] − session_low ) / ( session_high − session_low )
```

The high and low use each bar's **high and low**, not its close — the true
extremes of where price traded, not just where bars ended.

### Worked example at 14:00

```
(23425.20 − 23403.05) / (23494.95 − 23403.05)
       = 22.15 / 91.90
       = 0.241
```

In the lower quarter of the day's range — consistent with `session_ret` being
negative, and a useful cross-check that the two agree.

### When it has no value

Same gate as `session_ret`: requires the 09:15 opening bar to exist, so a
session missing its open reports `missing` for both.

Additionally, if `session_high == session_low` the value is `missing`
(`session range is zero`) — only possible with a single flat bar.

### Distribution (5m)

Valid 98.5%. Median **0.518**, p25 0.224, p75 0.808, spanning the full 0–1.

That the median sits almost exactly at 0.5 is a meaningful sanity check: if
price is roughly equally likely to be anywhere in its realised range, this is
where it should centre. A median at 0.2 or 0.8 would signal a bug.

---

## `overnight_gap`

### What it measures

**The overnight move** — today's open versus the previous session's close.

### Why it exists

Two reasons, and the second is the more important.

First, the gap is genuinely informative: it carries everything that happened
while the Indian market was shut — US closes, Asian opens, overnight news.

Second, and structurally: **isolating the gap in its own column is what allows
every other price feature to reset at 09:15 cleanly.** If the intraday features
reached back into yesterday, each would silently absorb an 18-hour move roughly
7.6× the size of a typical 5-minute move. Giving the gap its own home means
nothing is lost by excluding it elsewhere.

### Exact calculation

```
overnight_gap = open_of_the_09:15_bar / close_of_the_previous_session − 1
```

The previous session is whichever session **most recently appears in the data**
before today — not "yesterday" by calendar. Over a long weekend that may be four
calendar days back, and the observed data shows gaps of 1, 2, 3 and 4 days.

### Worked example at 14:00

```
23446.60 / 23431.50 − 1 = +0.000644
```

Nifty opened 0.06% above the previous close.

This value is **constant for the entire session** — it is `SESSION`-scoped,
computed once from the session boundary. If it drifted intraday, something would
be wrong.

### Availability — a subtlety worth knowing

The gap is **not** available at 09:15. The 09:15 bar has not closed yet, so its
open is not knowable from bar data until **09:20**. Conceptually the opening
print exists within a second of the open, but this feature set reads completed
bars only, so 09:20 is the honest answer.

### When it has no value

| Condition | Status |
|---|---|
| No completed bar today | `insufficient_history` |
| No 09:15 bar in the data | `missing` |
| No prior session (first session of all history) | `insufficient_history` |
| Previous close ≤ 0 | `missing` |

### Distribution (5m)

Valid 98.4%. Median +0.00073, p25 −0.0014, p75 +0.0028, range −5.0% to +4.9%,
**standard deviation 0.57%**.

That 0.57% is the number behind the whole boundary-exclusion design: it is
**7.6× the 0.0716%** standard deviation of an intraday 5-minute return.

---

# Volatility features

Six features describing **how turbulent the market is, and whether that is
normal**. These are the only features permitted to read across session
boundaries.

**Why they may.** Each is a *path-dependent aggregate* — a sum over per-bar
terms — so a term spanning two sessions can simply be dropped and the remaining
sum stays valid. A point-to-point return has no term to drop.

**What this buys.** Reset each morning, a 20-bar estimate is absent for the
first 26.8% of every 5-minute session. Rolling, it is absent only for the first
~20 bars of *all history* — about 13,920 recovered rows per instrument.

---

## `rv_short`

### What it measures

**Realized volatility** over the last 100 minutes, annualised. The standard
close-to-close estimator.

### Why it exists

The most direct answer to "how much is this market moving right now?".
Volatility is the single most forecastable quantity in finance — it clusters,
meaning turbulent periods follow turbulent periods — which makes it valuable
both as a feature and as a prediction target.

### Exact calculation

Four steps.

**1. Collect a generous window of bars.** For `n` returns needed, fetch
`2n + 4` bars. Over-fetching is deliberate: boundary returns will be discarded,
so the bar count needed is not known in advance, and under-fetching would
silently shorten the window.

**2. Compute log returns, dropping boundary-spanning ones:**

```
r[i] = ln( close[i] / close[i−1] )     for consecutive in-session bars
```

**3. Take the last `n` and compute the population standard deviation:**

```
sd = sqrt( (1/n) · Σ (r[i] − r̄)² )
```

**4. Annualise:**

```
rv_short = sd × sqrt( bars_per_session × 252 )
```

Window: `n = 20` at 5m (100 minutes), `n = 100` at 1m (also 100 minutes).

### Worked example at 14:00

The 20 within-session returns ending at 13:55 have standard deviation
0.0002927. Annualising:

```
0.0002927 × 137.48 = 0.0402
```

**4.0% annualised volatility.**

### When it has no value

| Condition | Status | Detail |
|---|---|---|
| Fewer than `n` within-session returns available | `insufficient_history` | `needs 20 within-session observations, 4 available` |

In practice this happens only at the very start of all stored history — **21 of
52,200 rows (0.04%)**. Every other row has a value, including at 09:15, because
the window reaches into yesterday.

### Distribution (5m)

Valid **100.0%**. Median 7.35%, p25 5.39%, p75 10.14%, min 1.49%, max 91.0%.

The 91% maximum is real: violent sessions exist. Strongly right-skewed, which is
the characteristic shape of every volatility series.

---

## `parkinson_short`

### What it measures

Realized volatility again — but estimated from each bar's **high-low range**
instead of its close-to-close move.

### Why it exists

Close-to-close volatility throws away information. If price rockets up and
falls back within a bar, the close-to-close return is ~0 and `rv_short` sees a
quiet bar. The high and low saw the turbulence.

The Parkinson estimator uses that range, and is roughly **5× more efficient**
than close-to-close for the same number of bars — meaning it achieves comparable
precision from far fewer observations.

Having both is informative: when `parkinson_short > rv_short`, price ranged
*within* bars more than the close-to-close path suggests — intrabar movement
that got retraced.

**It is also structurally immune to the boundary problem.** Every term is
computed inside a single bar, so no term ever spans two sessions and none needs
dropping. The same is true of the Garman-Klass and Rogers-Satchell estimators in
`backtest/rv.py`.

### Exact calculation

```
                 ┌                              ┐
                 │   1        1     n           │
parkinson = sqrt │ ─────── · ───  · Σ ln(hᵢ/lᵢ)² │  × annualisation
                 │ 4·ln(2)    n    i=1          │
                 └                              ┘
```

In code:

```python
k = 1 / (4 * math.log(2))                    # ≈ 0.360674
total = sum(math.log(b.high / b.low) ** 2 for b in bars)
value = math.sqrt(k * total / len(bars))
parkinson_short = value * annualization(timeframe)
```

**Where the constant comes from.** For a driftless geometric Brownian motion,
the expected squared log-range over a period relates to the variance by exactly
`4·ln(2) ≈ 2.7726`. Dividing by it converts the average squared log-range into
an unbiased variance estimate. It is not a tuning parameter — it falls out of
the mathematics of the assumed price process.

Window: 20 bars at 5m, 100 bars at 1m.

### Worked example at 14:00

```
parkinson_short = 0.0472    (4.7% annualised)
```

Compare to `rv_short = 0.0402`. Parkinson reads **higher**, indicating price
ranged within bars more than the closes alone reveal.

### When it has no value

Needs `n` bars with `high > 0` and `low > 0`. Bars failing that are dropped
before the count is checked.

### Distribution (5m)

Valid **100.0%**. Median 7.79%, p25 5.84%, p75 10.44%, min 1.01%, max 76.7%.

Slightly higher than `rv_short` at every quantile — the expected relationship,
since it captures intrabar movement that close-to-close misses.

---

## `atr_rel`

### What it measures

**Average True Range**, normalised by price. A plain, unannualised measure of
typical bar movement over the last 70 minutes.

### Why it exists

ATR is the most widely used practical volatility measure in trading, because it
answers a directly actionable question: *how far does this thing typically move
in a bar?* — which is what position sizing and stop placement depend on.

It differs from the two estimators above in an important way: **true range
includes gaps between bars**, not just movement within them.

### Exact calculation

True range for a bar is the largest of three candidate spans:

```
TR = max(  high − low,                  the bar's own range
           |high − previous_close|,     gap up then reversal
           |low  − previous_close|  )   gap down then reversal
```

The last two terms exist to catch the case where price jumps between bars: if a
bar opens well above the previous close, its own high−low understates how far
price actually travelled.

**The session-boundary adjustment.** At the first bar of a session, the
"previous close" is *yesterday's*, so those two terms would measure the
overnight gap — the same 58×-variance contamination. So the first bar of every
session contributes **only its own high − low**:

```python
if index == 0 or crosses_session(bars[index - 1], bar):
    out.append(high - low)          # own span only
else:
    out.append(max(span, |high − prev_close|, |low − prev_close|))
```

Then average and normalise:

```
atr_rel = ( mean of the last n true ranges ) / c[0]
```

Window: 14 bars at 5m (70 minutes), 70 bars at 1m. Fourteen is the conventional
ATR period, here expressed as the elapsed time it corresponds to.

**Not annualised** — deliberately. This is a "typical bar movement" figure, and
the annualised versions are already available in `rv_short` and
`parkinson_short`.

### Worked example at 14:00

```
atr_rel = 0.000552
```

The average bar over the last 70 minutes spanned **0.055% of price** — about 13
points at this level.

### When it has no value

| Condition | Status |
|---|---|
| Fewer than `n` bars | `insufficient_history` |
| Latest close ≤ 0 | `missing` |

### Distribution (5m)

Valid **100.0%**. Median 0.00085, p25 0.00064, p75 0.00114, max 0.0098.

Note this sits slightly above `range_rel`'s median (0.00077) — as it must,
since true range is ≥ high−low by definition.

---

## `rv_baseline`

### What it measures

**What counts as normal volatility for this market, lately.** Realized
volatility computed over the last 5 completed sessions.

### Why it exists

This feature is not really meant to be used on its own. It exists so that
[`rv_regime`](#rv_regime) can exist.

`rv_short = 4%` is uninterpretable in isolation. Is that calm or wild? The
answer depends entirely on what this instrument has been doing — and that is
what `rv_baseline` supplies.

**Why a trailing baseline and not an intraday one.** A baseline drawn from this
morning alone would mostly measure the normal intraday U-shape — volatile open,
quiet lunch, volatile close — rather than anything about the market's current
character. Five sessions is roughly a week: long enough to be stable, short
enough to track a genuine regime shift.

### Exact calculation

```
1. Identify the last 5 session dates OBSERVED IN THE DATA, before today.
2. Take every in-session bar from those sessions.
3. Compute log returns, dropping the 4 boundary-spanning ones.
4. Population standard deviation × annualisation.
```

Roughly 370 returns at 5m, 1,870 at 1m.

**Observed dates, not calendar dates.** `scheduler/nse_calendar` carries
holidays for 2026 only, while ~75% of stored history predates it. Asking the
calendar for "the last five trading days" in 2024 would name dates that were
actually holidays, find no bars there, and silently build a four-session
baseline. Selecting from dates present in the data is immune to that.

**Constant within a session.** It uses only *completed prior sessions*, so it
does not move between 09:15 and 15:25. A reference level that drifted intraday
would confound the very thing it normalises.

### Worked example at 14:00

```
rv_baseline = 0.0932    (9.3% annualised, over 2026-09-03 .. 2026-09-09)
```

### When it has no value

| Condition | Status | Detail |
|---|---|---|
| Fewer than 5 prior sessions observed | `insufficient_history` | `needs 5 within-session observations, 2 available` |

This is why exactly **375 rows** (5 sessions × 75 bars) are absent in the 5m
dataset — the first five sessions of all history, a clean and checkable number.

### Distribution (5m)

Valid 99.3%. Median 8.81%, p25 7.26%, p75 10.96%, min 3.97%, max 33.8%.

Notice it is **less dispersed** than `rv_short` (p25–p75 of 3.7pp versus 4.7pp)
and its maximum is far lower (33.8% vs 91.0%). That is exactly right — averaging
over a week smooths out single violent sessions.

---

## `rv_regime`

### What it measures

**Is the market unusually volatile right now, relative to its own recent
normal?** The single most informative feature in the set.

### Why it exists

This is the feature the entire trailing-baseline design was built to support.

* **1.0** — today is behaving like the past week
* **2.0** — twice as turbulent as the past week
* **0.5** — half as turbulent; unusually calm

Because it is a **ratio**, it is comparable across instruments and across eras
in a way that raw volatility is not. 4% volatility means something completely
different in a calm year than in a crisis; `rv_regime = 0.43` means the same
thing in both.

### Exact calculation

```
rv_regime = rv_short / rv_baseline
```

Both computed exactly as described above — the same 100-minute short window and
the same 5-session baseline.

### Worked example at 14:00

```
0.0402 / 0.0932 = 0.432
```

The market was running at **43% of its recent normal** — an unusually quiet
afternoon.

### When it has no value

It **inherits the reason** from whichever input failed:

* `rv_short` absent → `rv_regime` carries `rv_short`'s status and detail
* `rv_baseline` absent → carries the baseline's status and detail
* `rv_baseline == 0` → `missing`, `trailing baseline volatility is zero`

Inheriting rather than reporting a generic failure means the reason survives:
"needs 5 within-session observations, 2 available" tells you the baseline was
short, not that the ratio mysteriously failed.

In practice its availability is identical to `rv_baseline`'s (99.3%), since that
is always the binding constraint.

### Distribution (5m)

Valid 99.3%. **Median 0.843**, p25 0.633, p75 1.116, min 0.126, max 10.0.

**Centred near 1.0** — the strongest single validation in the dataset. If the
ratio were systematically 2.0 or 0.4, the short window and the baseline would be
measuring different things.

It sits slightly *below* 1.0 for a real reason, not a bias: `rv_short` measures
a 100-minute intraday window, while `rv_baseline` covers whole prior sessions
**including their volatile opens**. The typical intraday window really is a
little quieter than a full-session reference.

The 10.0 maximum is a genuine volatility explosion, not an artifact.

---

## `rv_accel`

### What it measures

**Is volatility rising or falling?** The change in short-window volatility over
the last 30 minutes.

### Why it exists

`rv_regime` says whether volatility is high. `rv_accel` says whether it is
*heading* there. Volatility expansion and contraction are distinct regimes — a
market at 1.5× normal and rising behaves differently from one at 1.5× and
subsiding.

### Exact calculation

Both endpoints are computed from the **same list of boundary-clean returns**,
offset by the lag:

```
returns = boundary-excluded log returns, most recent last

current  = sd( returns[−n:] )               the last n returns
earlier  = sd( returns[−(n+lag) : −lag] )   the n returns ending lag bars ago

rv_accel = current / earlier − 1
```

with `n = 20, lag = 6` at 5m (100-minute window, 30-minute lag) and
`n = 100, lag = 30` at 1m.

Expressed as a **fractional change**: −0.137 means volatility is 13.7% lower
than it was 30 minutes ago. Annualisation cancels in the ratio, so it does not
appear.

### Worked example at 14:00

```
rv_accel = −0.1367
```

Volatility was **13.7% lower** than half an hour earlier. Combined with
`rv_regime = 0.43`: already quiet, and still calming.

### An interpretive caveat

Both endpoints are individually boundary-clean — no contaminated return enters
either standard deviation. But across a session boundary the *comparison* shifts
meaning: at 09:15 it compares this morning's window against yesterday's late
session. The arithmetic is sound; what it is comparing is different. Worth
remembering when reading early-session values.

### When it has no value

| Condition | Status |
|---|---|
| Fewer than `n + lag` within-session returns | `insufficient_history` |
| Earlier volatility is zero | `missing` |

### Distribution (5m)

Valid 99.9%. Median −0.0015, p25 −0.108, p75 +0.107, min −0.877, max +12.0.

Median almost exactly zero — volatility is as likely to be rising as falling at
a random moment. The asymmetric tails (−88% floor, +1200% ceiling) are
structural: a ratio is bounded below by −1 but unbounded above.

---

# Labels

Nine targets — what a model would be asked to predict.

## What makes a label different from a feature

A label is **not part of the market state**, and this separation is
architectural rather than stylistic.

```
        bars up to 13:55  ──────►  18 features   (X, knowable at T)
                                        │
        bars 14:00 - 14:55  ────►   9 labels     (y, NOT knowable at T)
```

Both come from the same bar series, but from **disjoint, non-overlapping time
periods**. No bar is ever both an input and part of the answer. This is Rule 5
of [02-market-data.md](02-market-data.md#how-future-information-must-be-excluded-from-features),
and `features/registry.py` enforces it by excluding the `label` family from
`state_features()` entirely.

Labels are **not computed from the features**. If they were, a model would
learn a formula you already knew and predict nothing.

## Two rules every label obeys

**1. Labels never cross a session boundary.** A 12-bar forward return at 15:20
would otherwise reach into the next morning and be dominated by the overnight
gap — the same 58×-variance term the volatility features exclude, except here
it cannot be dropped, because a forward return is a single point-to-point ratio.

The cost is the last `h` bars of every session, which is ~16.5% of rows at the
60-minute horizon. The benefit is that the remaining rows all mean one thing.

**2. The horizon must mean what its name says.** If a missing bar stretches the
forward window, the label reports `missing` rather than a number.

This was found in live data: on 2026-09-10, a Thursday expiry, the 15:15 bar is
absent, so "three bars after 15:00" reached 15:20 — a **20-minute** return in a
column named `fwd_ret_15m`, reading **+0.71%** because it swallowed a 400-point
closing spike. Since a label is what a model is asked to predict, a horizon that
does not mean what it says would be learned as though it did.

## Horizons

| Timeframe | Horizons (bars) |
|---|---|
| **5m** | 15m (3) · 30m (6) · 60m (12) |
| **1m** | 5m (5) · 15m (15) · 30m (30) |

Three kinds × three horizons = nine labels per timeframe.

---

## `fwd_ret_<h>`

### What it measures

**How far price moves over the next `h` minutes.** The most direct prediction
target.

### Exact calculation

With `f[1..h]` the bars strictly after the decision bar, within the same
session:

```
fwd_ret_h = f[h].close / c[0] − 1
```

Note it uses the close of the bar **at** the horizon, not the last available
bar — so the horizon is exact.

### Worked example at 14:00

Decision bar 13:55, close 23425.20. Forward closes: 14:00 → 23420.30,
14:05 → 23419.45, **14:10 → 23417.90** (the 3rd bar, the 15-minute horizon).

```
fwd_ret_15m = 23417.90 / 23425.20 − 1 = −0.000312
```

All three horizons at this moment:

| | Horizon bar | Value |
|---|---|---|
| `fwd_ret_15m` | 14:10 | −0.000312 |
| `fwd_ret_30m` | 14:25 | −0.000280 |
| `fwd_ret_60m` | 14:55 | −0.000113 |

Down at all three, but the magnitude **shrinks** with horizon — price dipped to
23406.60 at 14:35 and recovered to 23425.30 by 14:45.

### When it has no value

| Condition | Status | Detail |
|---|---|---|
| Fewer than `h` bars remain in the session | `missing` | `only 4 bars remain in the session, need 6` |
| A gap stretched the horizon | `missing` | `gap stretched the horizon to 20 min, expected 15` |
| Decision close ≤ 0 | `missing` | `non-positive close at the decision bar` |
| No bar has closed yet today | `insufficient_history` | `no bar has closed yet in this session` |

### Distribution (5m)

| Label | Valid | p25 | median | p75 | min / max |
|---|---|---|---|---|---|
| `fwd_ret_15m` | 95.5% | −0.00056 | +0.00002 | +0.00057 | −2.05% / +2.42% |
| `fwd_ret_30m` | 91.5% | −0.00078 | +0.00002 | +0.00080 | −3.38% / +2.90% |
| `fwd_ret_60m` | 83.5% | −0.00112 | +0.00002 | +0.00111 | −4.28% / +3.34% |

These distributions are **near-identical to the backward `ret_*` features** —
as they must be, since they measure the same quantity over the same series,
merely shifted in time. A large discrepancy would indicate a bug.

---

## `fwd_dir_<h>`

### What it measures

**Which way price moves next**: −1 down, +1 up, **0 unchanged**.

### Why a separate label from `fwd_ret`

Direction is often the more robust modelling target. Forward returns have fat
tails, so a regression model can be dominated by a handful of extreme moves.
Classifying direction sidesteps that, at the cost of discarding magnitude.

### Exact calculation

```
fwd_dir_h = sign( fwd_ret_h )        ∈ {−1, 0, +1}
```

Implemented as `float((value > 0) - (value < 0))`, which yields exactly 0 when
the forward return is exactly zero.

### Why three-valued, not two

**35 of 47,760** valid 30-minute observations have a forward return of exactly
zero — price closed unchanged.

Folding those into "up" or "down" would invent a direction the market did not
take, and the choice would be **unrecoverable** afterwards. Whether to merge the
zero class is a modelling decision for a later phase; discarding the
distinction now would remove the option.

### When it has no value

Inherits entirely from `fwd_ret_<h>` — same conditions, same details.

### Distribution (5m), `fwd_dir_30m`

| Class | Count | Share |
|---|---|---|
| +1 (up) | 24,162 | 50.59% |
| −1 (down) | 23,563 | 49.34% |
| **0 (flat)** | **35** | **0.07%** |

**Read this before modelling.** The classes are nearly balanced, which means
the naive baseline "always predict up" scores **50.63%**. A model must beat
that, not 50%, to have learned anything. Measured baselines on this dataset:

| Strategy | Accuracy |
|---|---|
| Always UP | 50.63% |
| Fade the last 5m return | **51.07%** |
| Follow the last 5m return | 48.68% |

---

## `fwd_rv_<h>`

### What it measures

**How turbulent the next `h` minutes are** — annualised realized volatility over
the forward window.

### Why it exists

Volatility is genuinely more forecastable than direction, because it clusters:
turbulent periods follow turbulent periods, which is one of the most robust
empirical regularities in finance.

Measured on this dataset, the strongest feature-to-`fwd_ret_30m` correlation is
**0.038** — direction is close to a coin flip. Forward volatility is a far
friendlier first target, and it pairs directly with the existing
`vrp_reversion` strategy, which trades the gap between implied and realized
volatility.

### Exact calculation

```
1. Take the decision bar plus the next h bars:  [c[0], f[1], ..., f[h]]
2. Compute log returns across them  →  h returns
3. Population standard deviation
4. × annualisation
```

The decision bar is included as the window's **starting point**, contributing
its close only — it supplies no forward information. Its own OHLC is already a
feature input.

### Worked example at 14:00

```
fwd_rv_15m = 0.0104     over 14:00 .. 14:10  (3 returns)
fwd_rv_30m = 0.0268     over 14:00 .. 14:25  (6 returns)
fwd_rv_60m = 0.0366     over 14:00 .. 14:55  (12 returns)
```

Rising with horizon here because the first three forward bars were an unusually
smooth drift, while the twelve-bar window caught the real dip and recovery.

### A statistical caveat on the short horizon

**`fwd_rv_15m` is a standard deviation of 3 returns.** That is very thin — the
estimate is dominated by sampling noise, and single observations move it
sharply.

`fwd_rv_60m` (12 returns) is the more trustworthy of the three. If you model
forward volatility, start with the longest horizon.

### When it has no value

Same conditions as `fwd_ret_<h>`, plus: fewer than 2 forward returns →
`missing`.

### Distribution (5m)

| Label | Valid | p25 | median | p75 | max |
|---|---|---|---|---|---|
| `fwd_rv_15m` | 95.5% | 2.83% | 4.79% | 7.73% | 138.7% |
| `fwd_rv_30m` | 91.5% | 4.16% | 6.08% | 8.91% | 130.9% |
| `fwd_rv_60m` | 83.5% | 4.75% | 6.60% | 9.30% | 110.9% |

Medians **rise** with horizon (4.79% → 6.08% → 6.60%) while maxima **fall**
(138.7% → 110.9%). Both are the expected signature of an estimator gaining
observations: short windows are noisier in both directions, so they produce both
more extreme highs and more spuriously low readings.

---

# Quick reference table

## Features (5-minute parameters shown)

| Feature | Family | Scope | Window | Formula | Valid |
|---|---|---|---|---|---|
| `ret_5m` | price | intraday | 1 bar | `c[0]/c[-1] − 1` | 97.2% |
| `ret_15m` | price | intraday | 3 bars | `c[0]/c[-3] − 1` | 94.5% |
| `ret_30m` | price | intraday | 6 bars | `c[0]/c[-6] − 1` | 90.5% |
| `ret_60m` | price | intraday | 12 bars | `c[0]/c[-12] − 1` | 82.4% |
| `accel_1` | price | intraday | 3 bars | `ret(0) − ret(-1)` | 95.9% |
| `range_rel` | price | intraday | 1 bar | `(h−l)/c` | 98.6% |
| `body_ratio` | price | intraday | 1 bar | `\|c−o\|/(h−l)` | 98.5% |
| `upper_wick` | price | intraday | 1 bar | `(h−max(o,c))/(h−l)` | 98.5% |
| `lower_wick` | price | intraday | 1 bar | `(min(o,c)−l)/(h−l)` | 98.5% |
| `session_ret` | price | intraday | session | `c[0]/open_0915 − 1` | 98.5% |
| `pos_in_range` | price | intraday | session | `(c−low)/(high−low)` | 98.5% |
| `overnight_gap` | price | session | boundary | `open_0915/prev_close − 1` | 98.4% |
| `rv_short` | volatility | **rolling** | 20 returns | `sd(log returns) × 137.48` | 100.0% |
| `parkinson_short` | volatility | **rolling** | 20 bars | `sqrt(k·mean(ln(h/l)²)) × 137.48` | 100.0% |
| `atr_rel` | volatility | **rolling** | 14 bars | `mean(true range)/c[0]` | 100.0% |
| `rv_baseline` | volatility | **trailing** | 5 sessions | `sd(log returns) × 137.48` | 99.3% |
| `rv_regime` | volatility | **trailing** | ratio | `rv_short / rv_baseline` | 99.3% |
| `rv_accel` | volatility | **rolling** | 20 + lag 6 | `sd(now)/sd(30m ago) − 1` | 99.9% |

## Labels

| Label | Horizon (5m) | Formula | Valid |
|---|---|---|---|
| `fwd_ret_15m` | 3 bars | `f[3].close/c[0] − 1` | 95.5% |
| `fwd_dir_15m` | 3 bars | `sign(fwd_ret_15m)` | 95.5% |
| `fwd_rv_15m` | 3 bars | `sd(3 fwd returns) × 137.48` | 95.5% |
| `fwd_ret_30m` | 6 bars | `f[6].close/c[0] − 1` | 91.5% |
| `fwd_dir_30m` | 6 bars | `sign(fwd_ret_30m)` | 91.5% |
| `fwd_rv_30m` | 6 bars | `sd(6 fwd returns) × 137.48` | 91.5% |
| `fwd_ret_60m` | 12 bars | `f[12].close/c[0] − 1` | 83.5% |
| `fwd_dir_60m` | 12 bars | `sign(fwd_ret_60m)` | 83.5% |
| `fwd_rv_60m` | 12 bars | `sd(12 fwd returns) × 137.48` | 83.5% |

## 1-minute parameter differences

Everything is identical in structure; only the bar counts change, because
windows are matched by **elapsed time**:

| | 5m | 1m |
|---|---|---|
| Return horizons | 1, 3, 6, 12 bars (5/15/30/60 min) | 1, 5, 15, 30 bars (1/5/15/30 min) |
| `rv_short`, `parkinson_short` | 20 | 100 |
| `atr_rel` | 14 | 70 |
| `rv_accel` lag | 6 | 30 |
| Annualisation | 137.48 | 307.41 |
| Label horizons | 3, 6, 12 bars | 5, 15, 30 bars |

---

# Redundancy worth knowing before modelling

Three relationships that matter when feeding these to a model:

1. **`body_ratio + upper_wick + lower_wick = 1` exactly.** Any one is
   determined by the other two. Feeding all three to a linear model creates
   perfect multicollinearity.

2. **`rv_regime = rv_short / rv_baseline`.** All three are in the set. A model
   given all of them has the ratio and both its components.

3. **The return horizons are nested and heavily correlated.** `ret_60m` contains
   `ret_30m` contains `ret_15m` contains `ret_5m`.

None of this is a defect — each feature is individually well-defined and
interpretable, and tree-based models handle redundancy comfortably. But a linear
model will need regularisation, and feature-importance figures should be read
with these dependencies in mind.

# Where the numbers came from

Every distribution figure in this document was measured on the 5-minute dataset
built 2026-09-11 — 52,200 rows across 696 sessions, 2023-11-21 to 2026-09-10,
feature-set version `fs_5m_381e35d4`. The worked examples are real output from
`build_market_state` at 2026-09-10 14:00, cross-checked by hand against the raw
bars.

Implementations: `features/price.py`, `features/volatility.py`,
`features/labels.py`, with the shared machinery in `features/windows.py`.
