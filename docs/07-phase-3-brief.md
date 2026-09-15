# Phase 3 — Forecasting & Evaluation

Implementation brief. Written after Phase 2, and grounded in what Phase 2
actually measured rather than in what would be nice to build.

Read [04-project-history.md — What the dataset says so far](04-project-history.md#what-the-dataset-says-so-far)
before starting. It contains the results this brief is built on.

---

## 1. The question

Phase 2 answered *what was knowable at time T*. Phase 3 asks:

> **Given this state, what is predictable, with what uncertainty, and does it
> hold out of sample?**

And it has a sharper form, because Phase 2 already narrowed it:

> **Can we forecast realized volatility better than the option market does?**

---

## 2. What Phase 2 settled — do not re-litigate these

These were measured on the built datasets. Treat them as starting conditions,
not open questions.

### Direction is not predictable

| | Model | Best naive |
|---|---|---|
| 5m, 30-minute horizon | 59.3% | **63.0%** |
| 5m, 60-minute horizon | 60.4% | **64.6%** |
| 1m, 5-minute horizon | 47.4% | **56.0%** |

Ridge/logistic on all 18 features lost to *always predict down* at every
horizon on both timeframes. The strongest single feature correlation with
`fwd_ret_30m` across the whole dataset is **0.038**.

**Do not build a direction classifier.** If you believe you have found one,
you have almost certainly compared against the wrong baseline — see §4.1.

### Forward return is not predictable

Over a 20-session holdout it lost to a train-mean baseline at all three
horizons, with **negative** correlations (−0.06, −0.12, −0.15).

### Forward volatility is predictable, modestly

Over the same 20 sessions: correlations **+0.21 / +0.25 / +0.27** against
persistence's +0.11 / +0.14 / +0.11 — beating it on both error and ranking. At
n≈1,000 that is roughly six standard errors from zero.

**This is where Phase 3 should aim.**

### Implied volatility is the real benchmark

On stored NIFTY options, implied volatility predicts subsequent realized
volatility at **+0.56** correlation (≤7 days to expiry). That is the bar.

Trailing 5-session realized volatility (`rv_baseline`) reached **+0.61**, and
retained **+0.51** correlation with the residual after removing everything
implied volatility explains.

That is the central Phase 3 hypothesis. It is **a lead, not a result**: 60
overlapping observations across ~21 trading days and 3 expiries.

---

## 3. Structure — three checkpoints, two stop gates

Do **not** implement all three at once. Complete 3A, test it, document it, and
**stop for explicit approval** before 3B. Stop again before 3C.

This mirrors the Phase 2 brief's structure, which caught four real bugs
precisely because work was reviewed before more was built on it.

| | Checkpoint | Deliverable |
|---|---|---|
| **3A** | Evaluation harness | Splits, baselines, metrics — **no models** |
| **3B** | Volatility forecasting | Can anything beat persistence, then implied? |
| **3C** | Economic evaluation | Does the edge survive costs? |

---

## 4. Non-negotiables

Each of these exists because it was violated during Phase 2's exploratory work
and produced a wrong answer.

### 4.1 Compare against the *best* naive baseline, never a convenient one

On a down day, "always predict up" scores 35% and "always predict down" scores
65%. Comparing a 60% model against always-up shows a 25-point edge that does
not exist.

**Rule:** for any classification, the baseline is `max(always_up, always_down)`
computed **on the test window itself**. For regression, the baseline is the
train-set mean. For volatility, it is persistence *and* bias-corrected
persistence.

This error was made twice in Phase 2, once by a subagent given explicit warning
about it. Build the baseline into the harness so it cannot be skipped.

### 4.2 Report ranking and error separately, always

A model can beat persistence on RMSE by 53–67% while its correlation is
**negative** — it wins by shrinking toward a sane level, not by ranking
turbulent periods above calm ones.

**Rule:** every regression result reports RMSE *and* Pearson correlation. A
result that improves one while degrading the other is reported as such, never
summarised as a win.

Note that bias-correcting a predictor by a positive scalar cannot change its
correlation at all. Level and ranking are genuinely separate questions.

### 4.3 Split chronologically, never randomly

A random split puts tomorrow in training and today in test. Overlapping feature
windows then leak across the boundary and the result is meaningless.

**Rule:** all splits are by date. The test window is strictly later than the
training window.

### 4.4 Walk forward; a single holdout is not evidence

**Rule:** report performance across multiple rolling out-of-sample windows, and
report the *consistency* across them, not just the mean. An edge that appears
in one window and not others is noise.

### 4.5 Respect the sample-size arithmetic

| Detectable edge | Rows needed | 5m sessions |
|---|---|---|
| 5pp | 782 | 10 |
| 2pp | 4,898 | 65 |
| **1pp** | **19,598** | **261** |
| 0.5pp | 78,398 | 1,045 |

A genuine 51% edge **looks like a loss 38.8% of the time at n=50**. A worthless
model scores ≥55% a quarter of the time at n=50.

**Rule:** no conclusion from a test window below ~1,000 rows. State the
detectable effect size for whatever window is used.

### 4.6 Count your hypotheses

Testing 9 labels with **zero** signal produces at least one "significant"
result **26%** of the time. This does not improve with more data — it is a
property of how many things you test.

**Rule:** state how many hypotheses were tested and adjust the threshold, or
pre-register which one is primary.

### 4.7 Inherit Phase 2's point-in-time machinery, do not rebuild it

Features come from the built datasets or from
`features.engine.build_market_state(..., as_of=T)`. Never query
`ohlcv_*` directly for model inputs.

Dataset and live paths are asserted row-for-row identical; keep it that way.

---

## 5. Phase 3A — Evaluation harness

**Build the yardstick before anything to measure with it.**

### Deliverables

A new `research/` package (name it what you like; keep it out of `features/`):

* **Chronological splitter** — train / validation / test by date, with a
  guaranteed gap so overlapping label windows cannot straddle the boundary.
* **Walk-forward generator** — rolling or expanding origin, yielding many
  (train, test) date pairs.
* **Baseline library** — best-naive classifier, train-mean regressor,
  persistence, bias-corrected persistence. Each a first-class object evaluated
  exactly like a model.
* **Metric set** — accuracy with confidence interval; RMSE; Pearson
  correlation; and for every accuracy, whether the CI includes the baseline.
* **A report object** that refuses to present a model result without its
  baseline alongside.

### Explicitly not in 3A

No models. No fitting. The harness must be able to evaluate a constant
predictor and report it honestly.

### Acceptance

1. Given a labelled dataset, produces baseline scores with confidence intervals.
2. A deliberately leaky split (random rather than chronological) is **detected
   and rejected**, not silently accepted.
3. Walk-forward yields the expected number of windows over a known date range.
4. Every metric is unit-tested against hand-computed values.
5. Tests need no database and no network, matching the existing suite.

**Then stop.**

---

## 6. Phase 3B — Volatility forecasting

Only after 3A is approved.

### The two questions, in order

**Q1: Can anything beat persistence?**
Target `fwd_rv_60m` (12 returns — the least noisy of the three; `fwd_rv_15m`
rests on 3 returns and is statistically thin). Baseline: persistence and
bias-corrected persistence. Evaluate walk-forward.

**Q2: Does anything add information beyond implied volatility?**
This is the real question, and it needs option data — see §8.

Procedure: regress subsequent realized volatility on implied volatility, then
test whether feature residuals carry remaining signal. Phase 2's probe found
`rv_baseline` retaining +0.51 residual correlation on a 60-observation sample.
Confirm or refute it on more expiries.

### Model guidance

**Start with linear.** With correlations in the 0.2–0.6 range a ridge or linear
model captures most of what is there and its coefficients are readable. If a
gradient-boosted model dramatically beats a linear one, **suspect leakage before
celebrating** — check the split, check the label horizons, check that no
feature is a transform of the target.

Note the built-in redundancies before interpreting any coefficient:
`body_ratio + upper_wick + lower_wick = 1` exactly; `rv_regime` is
`rv_short / rv_baseline` with all three present; the return horizons are nested.
See [06-feature-reference.md — Redundancy](06-feature-reference.md#redundancy-worth-knowing-before-modelling).

### Acceptance

1. Walk-forward results across ≥10 windows, with per-window numbers shown, not
   just an average.
2. Every result carries its baseline and both metrics.
3. A clear statement of the detectable effect size for the window used.
4. **"Nothing beat the baseline" is an acceptable and complete outcome.** It is
   more valuable than a flattering result obtained by a weak comparison.

**Then stop.**

---

## 7. Phase 3C — Economic evaluation

Only after 3B, and only if 3B found something.

A forecasting edge is not a trading edge. Phase 2's option-pricing probe showed
that across every strike, with direction 50/50 and fair pricing, expected value
is **zero** — payoff asymmetry is priced in, and risk/reward cannot manufacture
an edge. What can is a volatility view.

So 3C asks: does the 3B edge survive contact with costs?

* Bid-ask spread, brokerage, STT on the actual option instruments.
* Slippage assumptions stated explicitly.
* Connection to the existing `vrp_reversion` strategy, which already trades the
  implied-minus-realized gap — the natural comparison.
* Tail behaviour: selling volatility wins ~97% of the time and loses
  catastrophically in the rest. The dataset contains a 91% annualised
  volatility reading. Any sizing conclusion must survive that.

Note that Phase 2's probe found the variance risk premium **negative** over its
window (−0.64pp, implied above realized on only 35% of days), contrary to the
textbook. Whether that is regime-specific or a data artefact is unresolved and
matters for any premium-selling conclusion.

---

## 8. Data prerequisites

**Phase 3B Q2 needs more expiries.** The current option data covers **3
expiries** (2026-08-25, 09-08, 09-15) from 2026-05-27, giving ~21 trading days
of genuinely short-dated observations. Weekly expiries accumulate ~50/year, so
this capability strengthens with time and cannot be rushed.

Interim options: run Q2 on what exists and label it a lead; or begin with Q1,
which needs no option data.

**Still open from Phase 2:** per-minute tick aggregates. `TICK_RETENTION_DAYS`
keeps days, not years, so microstructure history never accumulates unless
something starts persisting summaries. See
[04-project-history.md — Open recommendation](04-project-history.md#open-recommendation-tick-aggregates).

---

## 9. Out of scope

Do not build in Phase 3:

* A direction classifier — §2 settles it
* Deep learning of any kind (LSTM, Transformer, diffusion)
* Reinforcement learning
* Live or paper trading, order routing, execution
* Vector databases, embeddings, RAG, agents — the scale does not justify it;
  brute-force nearest-neighbour over the whole feature matrix takes **8.9 ms**
* Kafka, Airflow, Kubernetes, cloud services
* Model registry / experiment-tracking infrastructure

Historical analogue retrieval ("find sessions like today") is a reasonable small
addition *if* it enforces a strict past-only filter — for a query in the middle
of history, **5 of its 10 nearest neighbours are in its future** without one.

---

## 10. Acceptance gates for Phase 3 overall

1. **Baselines** — every result reports the best naive comparison.
2. **Chronology** — no result rests on a random split.
3. **Walk-forward** — multiple windows, consistency reported.
4. **Power** — detectable effect size stated for every claim.
5. **Multiplicity** — number of hypotheses tested is stated.
6. **Both metrics** — error and ranking reported separately.
7. **Point-in-time** — inputs come only through the Phase 2 access path.
8. **Reproducibility** — same data and seed produce the same numbers.
9. **Regression safety** — Phase 1 and 2 tests still pass, untouched.
10. **Honest negatives** — a null result is documented as a result.

---

## 11. The disposition this phase needs

Phase 2's value came from finding out what *wasn't* there. The same applies
here, more so.

The most valuable outcome of Phase 3 may be a well-evidenced *"the features do
not beat the option market."* That would save real money. A flattering backtest
obtained by a weak baseline would cost it.

Two specific habits:

**Distrust good results.** Every impressive number in Phase 2's exploratory work
— a 22-point direction edge, a 67% RMSE improvement — evaporated under a correct
baseline or a second metric. Assume the same until shown otherwise.

**Report the baseline first.** State what the trivial answer scores, then what
the model scores. In that order, so the comparison cannot be omitted.
