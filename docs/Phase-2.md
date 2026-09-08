# HereWeGoAgain AI — Phase 2 Specification for Claude

## Purpose

This document is the implementation brief for **Phase 2 — Market State & Feature Engineering** of HereWeGoAgain AI.

### North-star question

> **What was the state of the Indian market at decision time T, using only information that was actually available at T?**

Phase 2 should create a trustworthy, deterministic feature/state layer that becomes the common input language for later:

- forecasting models
- market regime detection
- anomaly/event detection
- historical analogue retrieval ("Market Memory")
- probabilistic model ensembles
- the AI research agent
- evaluation and monitoring

The goal is **not** to build a stock predictor in Phase 2. The goal is to make market state reconstructable, point-in-time correct, reproducible, and explicit about data quality.

---

# 1. Non-negotiable branch safety

Before changing anything:

1. Inspect the current Git branch and working tree.
2. Confirm that the existing Phase 1 work is present.
3. Create a **new Phase 2 branch** from the current Phase 1 branch/master state.
4. Do **not** modify `master`.
5. Do **not** merge anything into `master`.
6. Preserve unrelated user changes.
7. If the working tree contains unexpected changes, stop and report them before proceeding.

The user must be able to test the Phase 2 branch independently and merge it later only after explicit approval.

---

# 2. Read Phase 1 before implementing Phase 2

Claude must inspect the actual repository rather than assuming the architecture.

At minimum, review:

- `docs/current-architecture.md`
- `docs/data-contract.md`
- `docs/point-in-time-data.md`
- `docs/validation.md`
- `docs/phase-1-summary.md`
- `marketdata/contracts.py`
- `marketdata/sessions.py`
- `marketdata/validation.py`
- `marketdata/quality.py`
- `marketdata/report.py`
- `marketdata/access.py`
- Phase 1 tests
- the actual OHLCV/tick schema
- the existing downloader/integration points

Pay particular attention to:

- bar timestamps being left-labelled/opening-minute timestamps
- bar usability only after the bar is complete
- `as_of` access semantics
- `[start, end)` windows
- NSE session boundaries
- tick timestamp semantics and any sequence-number handling
- missing-data semantics
- the distinction between raw/source/derived fields
- the fact that Phase 1 does not repair, interpolate, reorder, or mutate raw data

Do not replace Phase 1 semantics with a new parallel interpretation.

---

# 3. Phase 2 scope

Phase 2 is divided into three checkpoints.

## Phase 2A — Market State Contract

Define the canonical state representation and feature metadata/registry.

## Phase 2B — Core Feature Engine

Implement the actual deterministic feature families.

## Phase 2C — Historical State Dataset

Build reproducible historical `MarketState` datasets suitable for later ML training and evaluation.

**Do not implement all three checkpoints at once.**

Complete 2A, test it, document it, and STOP for explicit approval before starting 2B.

After 2B, STOP again before 2C.

---

# 4. Phase 2A — Market State Contract

## Objective

Create a strongly defined but lightweight contract for representing everything knowable about an instrument at a decision time.

The central object should be conceptually:

```text
MarketState
    instrument
    decision_time
    timeframe
    features
    data_quality
```

The exact schema must be based on the repository's actual data availability.

Do not invent fields simply because they would be useful in a hypothetical market-data system.

---

## 4.1 Proposed types

Create a small, extensible type layer, for example:

```text
features/
    __init__.py
    types.py
    registry.py
```

Potential types:

### `FeatureDefinition`

Should capture at least:

- feature name
- feature family
- source/timeframe
- lookback requirement
- required inputs
- availability/latency semantics
- missing-data/nullability policy
- description/meaning
- units or value semantics where useful

Example conceptual metadata:

```text
FeatureDefinition(
    name="return_5m",
    family="returns",
    timeframe="5m",
    lookback="5m",
    requires=["close"],
    availability="after source bar closes",
    nullable=True,
)
```

This is illustrative, not a mandate for the exact API.

### `FeatureValue`

Optionally encapsulate:

- value
- feature name
- timestamp
- quality/status
- provenance if needed

Keep this simple unless the repository demonstrates a real need for a richer object.

### `MarketState`

Should provide a stable representation of:

- instrument identity
- decision time
- timeframe
- feature values
- data-quality information

The representation should be deterministic and serializable.

---

# 5. Canonical MarketState design

The state should support these feature families eventually.

## 5.1 Price and return state

Potential features:

- 1m return
- 5m return
- 15m return
- 30m return
- session return
- distance from session open
- distance from session high
- distance from session low
- range expansion
- normalized price/range measures

Only implement horizons supported by actual data and clearly document how they are computed.

---

## 5.2 Volatility state

Potential features:

- rolling realized volatility
- intraday volatility
- volatility percentile
- volatility acceleration
- volatility-of-volatility
- true range
- normalized range

Definitions must be mathematically explicit.

Do not use ambiguous names such as `volatility` without documenting:

- estimator
- lookback
- sampling frequency
- annualization, if any
- minimum observations
- missing-history behavior

---

## 5.3 Momentum and trend structure

Potential features:

- rolling momentum
- moving-average distance
- moving-average slope
- trend persistence
- directional efficiency
- breakout distance
- price position within a recent range

Avoid adding dozens of correlated indicators merely because they are common technical-analysis features.

Every feature should have a reason for existing.

---

## 5.4 Volume and activity

Potential features:

- rolling volume
- volume z-score
- relative volume
- volume acceleration
- cumulative session volume
- activity concentration

Remember that **zero volume can be valid** for cash indices and must not automatically be interpreted as missing data.

---

## 5.5 Order-book and liquidity state

The existing tick data contains best-5 book information, so this is an important differentiator.

Potential features:

- bid-ask spread
- relative spread
- top-5 bid quantity
- top-5 ask quantity
- book imbalance
- depth ratio
- depth concentration
- imbalance change
- liquidity shock

Do not assume that every tick has complete book information. Missingness and stale snapshots must have explicit semantics.

---

## 5.6 Derivatives

Only implement this family if the repository's actual data supports it.

Potential features:

- open interest
- change in open interest
- price/OI relationships
- ATM distance
- call/put structure

Do not create synthetic derivatives data merely to populate the schema.

---

## 5.7 Cross-asset / cross-sectional context

This may be Phase 2B or 2B+ depending on the actual repository.

Potential features:

- NIFTY return
- BANKNIFTY return
- SENSEX return
- relative strength
- dispersion
- cross-instrument correlation
- breadth, if constituent data exists

Cross-sectional features should only be included when their source data and point-in-time semantics are trustworthy.

---

# 6. Point-in-time correctness is the central invariant

This is the most important requirement of Phase 2.

Every feature must answer:

> Could this value actually have been known at `decision_time = T`?

All feature computation must build on the Phase 1 access semantics:

```text
marketdata.access.get_market_data(
    ...,
    as_of=T
)
```

Do not bypass the access layer to query raw tables directly if doing so can admit future information.

---

## 6.1 Bar completion semantics

A bar labelled:

```text
09:15
```

for a 5-minute series represents the interval:

```text
09:15–09:20
```

Therefore it must not be used at:

```text
09:15
09:16
09:17
09:18
09:19
```

It becomes usable only when the bar is complete.

The feature engine must preserve the existing Phase 1 interpretation rather than silently changing it.

---

## 6.2 Future-data invariance test

A critical test should establish:

```text
state(T)
```

is identical if data from:

```text
T + 1
T + 2
T + 3
...
```

is appended or modified.

This should be a first-class regression test.

---

## 6.3 Other temporal boundaries

Explicitly test:

- 09:15
- 09:16
- 09:20
- 15:29
- 15:30
- overnight transitions
- weekends
- holidays
- insufficient historical data

Do not assume a calendar boundary is equivalent to a data boundary.

---

# 7. Missing-data semantics

Do not blindly forward-fill features.

For each feature, explicitly define what happens when:

- source data is missing
- source data is stale
- insufficient lookback exists
- a market is closed
- the source value is legitimately zero
- an upstream validation warning/error exists

Possible statuses may include concepts such as:

```text
VALID
MISSING
INSUFFICIENT_HISTORY
STALE
INVALID_SOURCE
```

The exact enum/status model should be chosen based on repository needs.

The important requirement is that the system **does not silently turn absence into a plausible-looking number**.

Feature-level quality should feed into `MarketState.data_quality`.

---

# 8. Feature metadata / registry

Create a registry that makes the feature set inspectable.

Conceptually:

```text
registry.get("return_5m")
registry.list()
registry.by_family("volatility")
```

Each registered feature should expose enough metadata to answer:

1. What is this feature?
2. How is it calculated?
3. What data does it require?
4. What lookback does it require?
5. When does it become available?
6. Can it be null?
7. What does null mean?
8. What timeframe does it operate on?
9. Is it source-derived or computed?
10. What assumptions does it make?

The registry should become useful later for dataset construction and ML feature audits.

Do not build a heavyweight feature-store system.

---

# 9. Reproducibility contract

The eventual engine should expose a simple API along the lines of:

```text
build_market_state(
    instrument,
    decision_time,
    timeframe,
)
```

and later:

```text
build_market_states(
    instrument,
    start,
    end,
    decision_interval,
)
```

The exact API may differ if the existing codebase suggests a better design.

Requirements:

- same data + same inputs => same output
- deterministic ordering
- deterministic serialization
- no dependence on current wall-clock time
- no hidden network calls
- no random behavior
- no mutation of raw market data

---

# 10. Proposed architecture

The intended conceptual structure is:

```text
marketdata/
    access.py
    contracts.py
    sessions.py
    validation.py
    quality.py

features/
    __init__.py
    types.py
    registry.py
    returns.py
    volatility.py
    momentum.py
    volume.py
    liquidity.py
    derivatives.py
    cross_asset.py
    engine.py
    quality.py
```

This is a proposal, not a mandate.

Adapt it to the actual repository.

Avoid unnecessary package proliferation.

---

# 11. Phase 2B — Core Feature Engine

After 2A is explicitly approved, implement deterministic feature computation.

Priority order:

1. returns / price state
2. volatility
3. momentum / trend structure
4. volume / activity
5. order-book / liquidity
6. derivatives where supported
7. cross-asset context where supported

For every feature:

- document the formula
- document its lookback
- document availability time
- test normal values
- test boundary values
- test insufficient history
- test missing source data
- test point-in-time behavior

Avoid feature explosion.

A small set of well-defined features is preferable to hundreds of poorly specified indicators.

---

# 12. Phase 2C — Historical State Dataset

After 2B is explicitly approved, create reproducible historical state generation.

The objective is to produce datasets that later ML phases can consume without reimplementing feature logic.

The dataset should preserve:

- instrument
- decision time
- timeframe
- feature values
- feature quality/status
- relevant provenance/metadata
- deterministic ordering

The dataset must be generated through point-in-time-safe access.

Do not yet build:

- a model-training framework
- a feature store
- a vector database
- an online serving platform
- a forecasting model

Those belong to later phases.

---

# 13. Tests

Expected test areas include:

```text
tests/
    test_feature_returns.py
    test_feature_volatility.py
    test_feature_volume.py
    test_feature_liquidity.py
    test_feature_engine.py
    test_feature_point_in_time.py
    test_market_state.py
```

Adapt names to the repository's existing test organization.

At minimum test:

### Determinism

Same input produces the same state.

### No look-ahead

Future data cannot change a past state.

### Bar completion

A bar is unavailable until its completion time.

### Session boundaries

Correct behavior around:

- session open
- session close
- overnight
- weekend
- holiday

### Insufficient history

Features requiring N observations do not silently fabricate values.

### Missing data

Missing/stale/invalid inputs have explicit outcomes.

### Raw-data immutability

Feature generation never modifies source rows.

### Registry consistency

Every exposed feature has complete metadata.

### Regression

All Phase 1 tests continue to pass.

---

# 14. Documentation

Phase 2 should add at least:

```text
docs/market-state.md
docs/feature-contract.md
```

Also create:

```text
docs/phase-2a-summary.md
```

and later equivalent summaries for 2B/2C.

Documentation should explain decisions, not merely describe filenames.

In particular document:

- the definition of decision time
- bar availability
- feature formulas
- lookbacks
- missing-data policies
- quality/status semantics
- source dependencies
- known limitations
- assumptions

---

# 15. Phase 2 acceptance gates

Phase 2 is complete only when the following are demonstrably true.

## Gate 1 — Determinism

Same input + same decision time => identical `MarketState`.

## Gate 2 — No look-ahead

`state(T)` remains unchanged when future data is added or modified.

## Gate 3 — Boundary correctness

Correct behavior at:

- 09:15
- 09:16
- 09:20
- 15:29
- 15:30
- overnight
- weekends
- holidays

## Gate 4 — Insufficient history

Insufficient history is represented explicitly.

## Gate 5 — Missing-data semantics

Missing, stale, invalid, and legitimately-zero values are distinguishable where relevant.

## Gate 6 — Metadata completeness

Every feature has inspectable definition metadata.

## Gate 7 — Raw-data immutability

No feature computation mutates source data.

## Gate 8 — Historical usability

The engine can generate meaningful historical states with reasonable performance.

## Gate 9 — Regression safety

Existing Phase 1 behavior remains intact.

## Gate 10 — Test quality

The complete suite passes, including substantial deterministic feature tests.

---

# 16. Explicitly out of scope

Do **not** implement any of the following in Phase 2:

- LSTM
- Transformer forecasting
- XGBoost prediction
- stock-price prediction
- regime classifier
- anomaly detector
- trading strategy
- automated trading
- LLM/RAG
- embeddings
- vector database
- agent
- Kafka
- Airflow
- Kubernetes
- Redis feature store
- Feast or equivalent feature-store infrastructure
- cloud deployment
- production serving infrastructure

Phase 2 is the **state representation and feature foundation**.

---

# 17. Engineering principles

Use these principles throughout:

### Prefer explicitness over cleverness

A reviewer should be able to understand exactly when a feature becomes available.

### Prefer deterministic functions

Feature functions should be as close to pure functions as practical.

### Prefer a small, high-quality feature set

Do not optimize for the number of indicators.

### Preserve provenance

It should remain possible to understand where a feature came from.

### Make uncertainty visible

Do not convert poor-quality inputs into apparently precise state.

### Separate source data from derived data

Raw market data must remain untouched.

### Do not prematurely optimize infrastructure

A clean Python feature engine is enough at this stage.

### Do not build ML before the dataset contract is trustworthy

Later model quality is bounded by the quality of this layer.

---

# 18. Phase 2A execution protocol for Claude

For the first implementation pass, Claude should do **only Phase 2A**.

### Step 1

Inspect repository, branch, status, and Phase 1 implementation.

### Step 2

Create a new Phase 2A branch.

### Step 3

Read the Phase 1 docs and tests.

### Step 4

Design:

- `MarketState`
- `FeatureDefinition`
- feature registry
- feature quality/status semantics

### Step 5

Implement only the contract/types/registry layer.

### Step 6

Add deterministic unit tests.

### Step 7

Write:

- `docs/market-state.md`
- `docs/feature-contract.md`
- `docs/phase-2a-summary.md`

### Step 8

Run the full test suite.

### Step 9

Review the diff for accidental changes to Phase 1.

### Step 10

Report:

- files changed
- design decisions
- assumptions
- tests
- test results
- unresolved questions
- examples of `MarketState`
- anything that should be reviewed before 2B

### Step 11 — STOP

Do not start Phase 2B.

Do not merge.

Wait for explicit approval.

---

# 19. What I should learn to assess Phase 2 intelligently

This section is for the project owner/reviewer, not for Claude implementation.

You do **not** need to become a quant researcher before reviewing Phase 2. You need enough ML/time-series knowledge to detect bad assumptions, leakage, meaningless features, and weak evaluation design.

## A. Highest priority: time-series fundamentals

Learn:

- time-series indexing
- stationarity vs non-stationarity
- returns vs prices
- log returns
- rolling windows
- expanding windows
- lagging
- autocorrelation
- volatility
- seasonality/intraday effects
- regime changes
- concept drift

You should be able to answer:

> If I compute a 20-period rolling statistic at time T, exactly which observations are allowed to enter it?

This is foundational.

---

## B. Data leakage and point-in-time correctness

This is probably the **single most important ML topic for this project**.

Learn:

- look-ahead bias
- target leakage
- train/test contamination
- temporal leakage
- feature leakage
- label leakage
- survivorship bias
- backfill/revision leakage
- publication/availability timestamps
- embargo/purging concepts for time-series evaluation

You should be able to inspect a feature and ask:

> Was this information actually observable at the moment the prediction would have been made?

For this project, that question matters more than whether the feature sounds sophisticated.

---

## C. Feature engineering for time series

Learn:

- lag features
- rolling statistics
- rolling z-scores
- normalization
- scaling
- rank/percentile transforms
- volatility normalization
- interaction features
- feature redundancy/correlation
- missing-value strategies
- feature availability

Understand why:

```text
price change
```

is generally more useful as a model input than blindly feeding raw price levels, and why normalization can matter across instruments/regimes.

---

## D. Statistics needed for market features

You should understand:

- mean / variance / standard deviation
- covariance
- correlation
- quantiles
- z-scores
- percentiles
- distributions
- skewness
- kurtosis
- confidence intervals
- sampling error

Then learn:

- realized volatility
- rolling volatility
- volatility clustering
- heteroskedasticity
- correlation instability

You don't need advanced stochastic calculus yet.

---

## E. ML fundamentals

Before Phase 3, learn:

- supervised learning
- regression vs classification
- feature matrix X / target y
- train/validation/test
- overfitting
- underfitting
- regularization
- bias/variance
- cross-validation
- hyperparameter tuning
- feature importance
- baseline models

You should understand why:

> A sophisticated model beating a naive baseline on one historical split proves almost nothing.

---

## F. Time-series model evaluation

This is essential before you trust any future forecasting result.

Learn:

- chronological train/validation/test splits
- walk-forward validation
- rolling-origin evaluation
- expanding-window evaluation
- out-of-sample testing
- benchmark baselines
- stability across periods
- performance under regime changes

For a market project, you should become suspicious whenever someone reports only:

```text
accuracy = 73%
```

without explaining the temporal split, target definition, baseline, and evaluation period.

---

## G. Probability and calibration

This becomes important later when the project moves toward probabilistic forecasting.

Learn:

- probability forecasts
- confidence vs probability
- calibration
- Brier score
- log loss
- reliability diagrams
- prediction intervals
- quantile forecasts
- uncertainty estimation

A model saying:

```text
P(return > 0) = 0.72
```

is more useful if that 72% means roughly 72% historically under comparable conditions.

---

## H. Financial ML / quant basics

You should understand at least:

- OHLCV
- tick data
- bid/ask
- spread
- order book
- market depth
- liquidity
- slippage
- market impact
- turnover
- transaction costs
- open interest
- volume vs open interest
- index vs constituent data
- market microstructure

This is especially important because HereWeGoAgain already has best-5 order-book information.

---

## I. Order-book / microstructure basics

For Phase 2's liquidity features, learn:

- bid price
- ask price
- mid-price
- spread
- depth
- book imbalance
- queue/depth concepts
- liquidity shocks
- stale quotes
- trade vs quote data

You do not need to become an HFT specialist.

You should simply be able to judge whether a proposed feature has a sensible interpretation.

For example:

```text
book_imbalance
```

should have a precise numerator/denominator definition and clear handling when one side of the book is missing.

---

## J. Feature selection and model interpretability

Learn:

- multicollinearity
- feature redundancy
- permutation importance
- SHAP at a conceptual level
- ablation studies
- feature importance instability

Later, this will help answer:

> Did this new feature actually add information, or did it just duplicate five existing features?

---

# 20. Topics to learn later, not before Phase 2

Do not spend significant time on these yet:

- Transformers for time series
- LSTMs
- diffusion models
- reinforcement learning
- vector databases
- RAG
- autonomous agents
- distributed feature stores
- Kubernetes
- Kafka
- complex MLOps stacks

Those become relevant after the basic state/evaluation foundation is trustworthy.

---

# 21. A practical learning sequence

A good order is:

```text
1. Python + NumPy/Pandas time-series operations
        ↓
2. Statistics fundamentals
        ↓
3. Time-series fundamentals
        ↓
4. Data leakage / point-in-time correctness
        ↓
5. Feature engineering
        ↓
6. ML fundamentals
        ↓
7. Time-series evaluation / walk-forward validation
        ↓
8. Probability + calibration
        ↓
9. Financial ML / market microstructure
        ↓
10. Model selection + interpretability
        ↓
11. Probabilistic forecasting
        ↓
12. AI agents + evaluation
```

For **Phase 2 specifically**, items 1–7 are the priority.

---

# 22. Questions you should be able to answer before approving Phase 2

Use these as your reviewer checklist.

### Data/time

1. What exactly does `decision_time` mean?
2. When does each source observation become usable?
3. Can future data change a previously computed state?
4. What happens at 09:15 and 09:20?
5. What happens at 15:30?
6. What happens if the historical lookback is incomplete?

### Features

7. What is the mathematical definition of every feature?
8. What is its lookback?
9. What data does it require?
10. Why does this feature exist?
11. Is it redundant with another feature?
12. Is it normalized appropriately?

### Quality

13. What does `null` mean?
14. How is stale data handled?
15. How is invalid source data handled?
16. Can legitimate zero values be distinguished from missing values?

### ML readiness

17. Can the state be reconstructed deterministically?
18. Can the state be serialized into an ML dataset without ambiguity?
19. Can every feature be audited for leakage?
20. Can a future model consume the state without directly querying raw market tables?

If Claude cannot answer these clearly, Phase 2 is not ready to be considered complete.

---

# 23. Final definition of done

Phase 2 should leave the project with a reliable answer to:

> **Given instrument I and decision time T, what was knowable about the market at T, how do we know it was knowable, what data quality caveats apply, and can we reproduce that state exactly?**

If the answer is yes, Phase 2 has succeeded.

The next phase can then ask a much harder question:

> **Given this trustworthy market state, what can we predict, with what uncertainty, and how well does it generalize out of sample?**
