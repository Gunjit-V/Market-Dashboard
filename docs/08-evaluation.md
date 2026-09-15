# Evaluation

The Phase 3A answer to: **how do you tell a result from a number?**

Phase 2 asked what was knowable at a decision time. Phase 3 asks what is
predictable — and the honest answer to that depends far more on how a result is
compared than on what is fitted. Every impressive figure in Phase 2's
exploratory work (a 22-point direction edge, a 67% error improvement)
disappeared under a correct baseline or a second metric.

So Phase 3A builds the yardstick and **nothing else**. There are no models in
`research/`, by design and by acceptance criterion. What there is:

1. [Quickstart](#quickstart) — running it
2. [The panel](#the-panel) — the rows, and the answers it hides
3. [Splits](#splits) — chronology, the gap, walk-forward
4. [Baselines](#baselines) — the trivial answers, as first-class objects
5. [Metrics](#metrics) — accuracy, error, ranking, power, multiplicity
6. [Reports](#reports) — verdicts that refuse to flatter
7. [What the harness refuses](#what-the-harness-refuses) — every rule, and where it is enforced
8. [Limitations](#limitations) — what this cannot tell you

The rules it enforces are numbered in
[07-phase-3-brief.md §4](07-phase-3-brief.md#4-non-negotiables). Each one was
written because breaking it produced a wrong answer during Phase 2, and each is
enforced in exactly one place here.

---

## Quickstart

```bash
python -m research.run --instrument "Nifty 50" --label fwd_rv_60m
python -m research.run --label fwd_dir_30m --train 20 --test 5
python -m research.run --label fwd_rv_60m --carrier rv_short --expanding
```

Phase 3A ships no models, so what the command runs is the trivial answers
against each other: bias-corrected persistence measured against plain
persistence and the training mean, or "always up" measured against the best
naive answer. That is the point rather than a placeholder — it is the
acceptance test for the yardstick itself.

```
bias-corrected-persistence(rv_short) on fwd_rv_60m (Nifty 50, 5m) -- 4 walk-forward windows, 900 test rows

window #0 2026-03-24..2026-03-26 (n=225)
  baseline  train-mean                             rmse=0.017582 r=undefined (the predictions are constant) bias=-0.000460
  baseline  persistence(rv_short)                  rmse=0.017096 r=+0.7392 [0.6735, 0.7933] (n=225) bias=+0.012235
  model     bias-corrected-persistence(rv_short)   rmse=0.011840 r=+0.7392 [0.6735, 0.7933] (n=225) bias=-0.000225
  verdict   beats (error beats, ranking ties)
...
consistency
  windows: 4
  verdicts: {'beats': 4}
  won_on_error: 4
  won_on_ranking: 0
  won_overall: 4

power: 900 rows across 4 windows, smallest 225; correlation intervals above are
the per-window statement of what that supports
hypotheses: 1 tested, 1 independent; at alpha=0.05 one would look significant
5.0% of the time with no signal. primary hypothesis pre-registered (fwd_rv_60m)
```

Read it in the order it prints: **the baseline first**, then the model, then
the verdict. Note what that example actually says — bias correction wins on
level in every window and wins nothing at all on ranking, because rescaling by
a positive constant cannot change a correlation.

In Python:

```python
from research.dataset import load_panel
from research.baselines import Persistence, TrainMeanRegressor
from research.splits import walk_forward
from research.evaluate import walk_forward_study
from research.report import HypothesisLedger

panel, usability = load_panel("Nifty 50", "5m", "fwd_rv_60m")
print(usability.render())          # what was dropped, and why

splits = walk_forward(panel.sessions, train_sessions=20, test_sessions=5)
report = walk_forward_study(
    Persistence("rv_short"),
    [TrainMeanRegressor()],
    panel,
    splits,
    HypothesisLedger(tested=("fwd_rv_60m",), primary="fwd_rv_60m"),
)
print(report.render())
```

---

## The panel

A `Panel` is one instrument, one timeframe, one label, and the rows where both
the features and that label are real. It is not a DataFrame: it is immutable,
ordered, aware of its label's forward horizon, and able to refuse to show its
answers.

| Field | Why it is there |
|---|---|
| `instrument`, `timeframe` | A result is about a specific instrument on a specific bar width and is not portable between them |
| `label`, `kind` | `kind` decides whether the harness scores accuracy or RMSE-and-correlation |
| `horizon_minutes` | How far past its decision time the label reaches — used for the leakage check, never for arithmetic |
| `feature_names`, `rows` | Positional, in one fixed order |
| `decision_times` | Strictly ascending, one row each |
| `targets` | The answers — or `None` when masked |

### It only holds valid rows

`research.dataset` is the one door between Phase 2's datasets and the harness.
It reads through `features.store.read_dataset` and resolves names through
`features.registry`; it never touches `ohlcv_*` and never recomputes a feature
([brief §4.7](07-phase-3-brief.md#47-inherit-phase-2s-point-in-time-machinery-do-not-rebuild-it)).

A row survives only if the label and every requested feature are `valid` in the
[Phase 2 status sense](03-features.md#feature-status). Nothing is imputed — the
Phase 1 rule that nothing is repaired does not stop at the dataset boundary —
and the `Usability` object records exactly what went:

```
2 of 4 rows usable for fwd_rv_60m (50.0%); 2 dropped
  fwd_rv_60m: missing 1
  rv_short: insufficient_history 1
```

The first bars of each session and the last hour are systematically absent
(rolling windows are not full yet; the forward horizon would cross the close),
so a large drop count is usually structural. Reading the reasons is how you
tell that apart from a fault.

### It hides the answers from models

`panel.masked()` returns the same rows with the outcomes withheld, and
`panel.y` **raises** rather than returning `None`:

```python
>>> panel.masked().y
LeakageError: the fwd_rv_60m outcomes are hidden from this panel; only a
baseline that declares uses_test_labels may read them
```

`research.evaluate` hands every model a masked panel. Exactly one kind of
predictor legitimately reads test labels — the best-naive classifier, whose
definition *is* "the best constant answer on this window" — and it must declare
`uses_test_labels = True`. The evaluator accepts that declaration only from a
baseline; a model that sets it is refused.

---

## Splits

[Brief §4.3 and §4.4](07-phase-3-brief.md#43-split-chronologically-never-randomly)
made structural: a leaky split is not something the harness warns about, it is
something that **cannot be constructed**.

```python
>>> Split(train=tuple(sorted(shuffled[:20])), test=tuple(sorted(shuffled[20:])))
LeakageError: test starts 2026-03-04 but train runs to 2026-04-10; a split is
by date and the later block must be strictly later
```

Three rules, all checked in `Split.__post_init__`:

**Order.** Every training session is strictly earlier than every test session.
A random split fails this immediately — with the dates interleaved, the latest
training date is later than the earliest test date, however tidily each half is
sorted afterwards.

**A gap.** At least one session is dropped between the blocks. Labels in the
Phase 2 catalogue [never cross a session boundary](03-features.md#scope--the-session-boundary-rule),
so one dropped session is a complete guarantee that no training label was
observed during the test window, whatever the horizon. The gap is counted in
**sessions**, not days: a weekend is not a session, and "one calendar day"
between a Friday and a Monday would drop nothing at all.

**Named sessions.** A split holds dates, never row positions. Row counts vary
per session, so a split by row count would land in a different place on every
instrument and would not reproduce.

### Walk-forward

A single holdout is not evidence ([§4.4](07-phase-3-brief.md#44-walk-forward-a-single-holdout-is-not-evidence)).

```python
splits = walk_forward(sessions, train_sessions=20, test_sessions=5)   # rolling
splits = walk_forward(sessions, train_sessions=20, test_sessions=5, expanding=True)
```

Each window is `train | gap | test`. The origin advances by `test_sessions` by
default, so test blocks tile the calendar without overlapping — overlapping
test blocks would count the same rows twice and make the windows look more
agreeing than they are. `window_count(n, train, test, gap, step)` gives the
number of windows in closed form, without generating them, so a study can state
up front how many it will report.

A calendar too short for even one window raises rather than returning nothing:
an empty run would otherwise be reported as a null result.

---

## Baselines

[Brief §4.1](07-phase-3-brief.md#41-compare-against-the-best-naive-baseline-never-a-convenient-one)
is the rule Phase 2 broke twice — once by a subagent that had been warned about
it in writing. A rule that lives in a reviewer's head gets skipped, so every
baseline here is a `Predictor`, fitted and scored by the same code path as a
model, and a report refuses to exist without at least one.

| Baseline | Predicts | For |
|---|---|---|
| `BestNaiveClassifier` | the best constant class **on the test window** | direction |
| `AlwaysPredict(c)` | one class, always | the building block of the above |
| `TrainMeanRegressor` | the training mean | any regression label |
| `Persistence(column)` | a feature, carried forward unchanged | volatility |
| `BiasCorrectedPersistence(column)` | the same, rescaled to the training level | volatility |
| `ConstantRegressor(v)` | one number, always | the harness's own floor |

### Why the naive classifier reads the test labels

Because that is its definition. On a down day "always predict down" scores 65%
and "always predict up" scores 35%; the honest baseline is the better of the
two, and *which* is better is a property of the window being scored. A baseline
chosen on the training window instead would be beatable by luck, restoring
exactly the error §4.1 forbids.

The direction label is three-valued — 0.181% of Nifty 50 five-minute bars close
exactly unchanged, and Phase 2 kept that distinction — so the candidates are
whatever classes the window contains, not a hard-coded pair. Ties go to the
smaller class value, purely so two runs produce the same report.

### Why bias-corrected persistence is a separate baseline

Multiplying a predictor by a positive constant **cannot change its
correlation**. Bias correction therefore only ever moves the level, which is
precisely why it belongs beside plain persistence: the pair separates "got the
level right" from "got the order right". A model that only does the former is
beaten by a one-line rescale of the trivial answer.

The scale is `mean(train label) / mean(train persistence)`, learned on the
training window only.

---

## Metrics

Nothing in `research.metrics` returns a bare float. "61.2% accurate" and "RMSE
0.043" are both compatible with a worthless model.

Everything is standard library — `statistics.NormalDist` for the normal
quantiles, `math.lgamma` for the binomial tail — so every number can be checked
by hand, and `tests/test_research_metrics.py` does exactly that.

### Accuracy comes with an interval

Wilson's interval, not the textbook `p ± z√(pq/n)`, which misbehaves exactly
where this harness is most at risk of being believed: small windows and rates
near 0 or 1, where it produces bounds outside `[0, 1]`.

`Accuracy.beats(rate)` is true only when the **whole interval** sits above the
baseline rate. A point estimate above it is the ordinary appearance of noise: at
n=50 a worthless model scores 55% or better a quarter of the time.

### Error and ranking, never one alone

`regression_metrics` returns RMSE *and* Pearson's r *and* the signed bias
together, because [§4.2](07-phase-3-brief.md#42-report-ranking-and-error-separately-always)
is not advisory. On single sessions in Phase 2 a volatility model beat
persistence on RMSE by 53–67% while its correlation was **negative** — it won by
shrinking toward a sane level, not by ranking turbulent periods above calm ones.

### A correlation can be undefined

A constant predictor has no ordering, so its rank agreement with anything is
undefined:

```python
>>> pearson([1, 2, 3, 4], [7, 7, 7, 7])
undefined (the predictions are constant)
```

Returning `0.0` there would make "ranks nothing" and "ranks badly" the same
number. The check is `min == max` rather than `variance == 0`, because a
constant series does not have zero variance in floating point: sixty copies of
`0.12` have a variance of 5e-34, enough for the textbook formula to return
`r = -2e-15` complete with a confidence interval.

### Power — what could this window even detect?

```python
>>> rows_needed(0.01)          # a one-point edge at 80% power
19623
>>> detectable_edge(50)        # what 50 rows can see
0.1981
```

[§4.5](07-phase-3-brief.md#45-respect-the-sample-size-arithmetic) requires every
accuracy claim to state this, so that "the model did not beat the baseline" can
be read as either "there is no edge" or "this window could never have seen one".
The brief's table quotes 782 / 4,898 / 19,598 / 78,398 rows for edges of 5 / 2 /
1 / 0.5 percentage points; the exact quantiles give 785 / 4,906 / 19,623 /
78,489. The difference is under 0.4% and changes no decision.

`probability_of_scoring` reproduces the brief's other two figures exactly, from
an exact binomial tail rather than a normal approximation: a worthless model
scores ≥55% **24.0%** of the time at n=50, and a genuine 51% edge looks like a
loss **38.8%** of the time.

### Multiplicity

```python
>>> family_error_rate(0.05, 6)
0.2649
```

The brief's 26% comes from here. The nine registered labels are **not** nine
independent questions — `fwd_dir_h` is the sign of `fwd_ret_h`, leaving six
independent quantities. Counting all nine would give 0.370 instead. The number
is sensitive to how independence is counted, which is why `HypothesisLedger`
asks for that count explicitly rather than inferring it.

Either pre-register a primary hypothesis, in which case its threshold stays at
α, or accept the Bonferroni-adjusted one
([§4.6](07-phase-3-brief.md#46-count-your-hypotheses)).

---

## Reports

### A result without a baseline does not exist

```python
>>> Comparison(model=result, baselines=())
ReportError: ... a result needs at least one baseline beside it.
Brief S4.1 -- the comparison is the result.
```

`Comparison` also refuses a baseline that is not one, a baseline of the wrong
kind, and a baseline scored on a different window.

### Verdicts

Regression carries **two** verdicts and a combined word:

| `error_verdict` | `ranking_verdict` | `verdict` |
|---|---|---|
| beats | beats | **beats** |
| beats | ties | **beats** |
| beats | loses | **mixed** |
| beats | inconclusive | **mixed** |
| loses | loses | **loses** |
| ties | ties | **ties** |

`mixed` is the whole point. A model that improves error while degrading — or
simply not having — a ranking is never summarised as a win. A constant
predictor that beats persistence on RMSE outright lands squarely there.

`ties` is not decoration either: two metrics closer than `METRIC_TOLERANCE`
(1e-12 relative) are the same number. Without it, a rescale that provably cannot
change a correlation still moves it in the sixteenth digit, and bias-corrected
persistence would be reported as ranking *worse* than the predictor it is a
rescale of.

Classification has one: `beats` only when the accuracy's whole interval sits
above the best naive rate, `loses` when the point estimate is below it, and
`inconclusive` in between — which is most of the time at realistic sample sizes.

### Consistency, not an average

`WalkForwardReport.consistency()` counts **windows won**, per axis:

```
windows: 10
verdicts: {'beats': 2, 'mixed': 5, 'loses': 3}
won_on_error: 7
won_on_ranking: 2
won_overall: 2
```

An edge in one window out of ten is noise, and an average would hide which it
was. `won_overall` counts the combined verdict; a window won on error and
exactly tied on ranking counts there without counting as a ranking win.

The report also carries the detectable effect size for its smallest window and
the hypothesis ledger, both required arguments rather than optional ones — a
question that can be skipped by leaving out an argument is a question that will
be skipped.

---

## What the harness refuses

Every refusal, the error it raises, and the rule behind it. All of them are
covered by tests.

| Situation | Error | Rule |
|---|---|---|
| A split whose train and test dates interleave | `LeakageError` | §4.3 |
| A split with no session gap | `LeakageError` | §4.3 |
| A validation block outside `train < validation < test` | `LeakageError` | §4.3 |
| A training label observed at or after the first test decision | `LeakageError` | §4.3 |
| A model reading the test panel's answers | `LeakageError` | §4.1 |
| A non-baseline declaring `uses_test_labels` | `LeakageError` | §4.1 |
| A label used as a feature (`fwd_dir_30m` → `fwd_ret_30m`) | `LeakageError` | §4.7 |
| A feature that is not in the Phase 2 registry | `ResearchError` | §4.7 |
| A result presented with no baseline | `ReportError` | §4.1 |
| A baseline scored on a different window | `ReportError` | §4.1 |
| A walk-forward report with no hypothesis ledger | `TypeError` | §4.6 |
| A metric over zero rows, or mismatched lengths | `MetricError` | — |
| A NaN or infinite value in a panel | `ResearchError` | Phase 2 status model |
| A calendar too short for one window | `ResearchError` | §4.4 |
| Two instruments or two timeframes in one frame | `ResearchError` | — |

---

## Reproducibility

Nothing in `research/` reads a clock, a database, a network or an unseeded
random source. The same panel and the same splits produce byte-identical
`render()` output and identical `describe()` dictionaries — asserted, not
assumed ([gate 8](07-phase-3-brief.md#10-acceptance-gates-for-phase-3-overall)).

A predictor's `params` are recorded in every result, so a Phase 3B model with a
random seed records that seed in the report itself rather than in someone's
memory.

---

## Tests

`tests/test_research_*.py`, 238 tests, no database and no network — matching the
rest of the suite. `tests/researchlib.py` builds the synthetic panels from a
seeded uniform draw, so every assertion holds on every machine.

Two real defects were caught by writing them:

* **A constant predictor appeared to have a correlation.** Sixty copies of
  `0.12` have a floating-point variance of 5e-34, and the textbook formula
  returned `r = -2e-15` with a confidence interval around it — precisely the
  plausible-looking nothing the Phase 2 status model exists to prevent, arriving
  by a different door.
* **A provably identical ranking was reported as a loss.** Bias-corrected
  persistence is plain persistence times a positive scalar, so their
  correlations are equal in principle and differ by ~1e-16 in practice. A bare
  `<` turned that into "the model ranks worse", in the one comparison built
  specifically to show that rescaling changes nothing about ranking.

---

## Limitations

**The harness cannot tell you a result is right.** It can only stop several
specific ways of being wrong. A study that passes every check here can still be
measuring a regime that has since ended.

**The best-naive classifier is an oracle baseline.** It reads the test window's
class balance, which no live predictor could. That makes it strictly harder to
beat than anything achievable, which is the intent — but it means "lost to the
best naive answer" is a weaker statement than "would have lost money".

**One gap size fits all.** The one-session gap is sufficient for every label in
the Phase 2 catalogue because those labels never cross a session boundary. A
future label that did would need a wider gap; the row-level check in
`assert_no_leakage` is what would catch it, and it raises rather than widening
the gap on its own.

**Nothing here handles more than one instrument.** A panel is one instrument on
one timeframe. Cross-sectional work would need a different object.

**No models, deliberately.** Phase 3B is gated on explicit approval of this
checkpoint ([brief §3](07-phase-3-brief.md#3-structure--three-checkpoints-two-stop-gates)).
When it arrives, it plugs into `Predictor` and inherits every refusal above
without a line of new enforcement.
