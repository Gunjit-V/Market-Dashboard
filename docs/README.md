# Documentation

Read in order. Each document assumes the ones before it.

| # | Document | What it answers |
|---|---|---|
| 1 | [01-architecture.md](01-architecture.md) | What is this system, what runs, where does data live, and how do I operate it? |
| 2 | [02-market-data.md](02-market-data.md) | What exactly is in each table, what may I assume about time, and how is any of it checked? |
| 3 | [03-features.md](03-features.md) | What was knowable at a decision time, and how do I build and read a feature dataset? |
| 4 | [04-project-history.md](04-project-history.md) | How did it get this way, and what is still broken or deferred? |
| 5 | [05-phase-2-brief.md](05-phase-2-brief.md) | The original Phase 2 specification, archived unchanged. |
| 6 | [06-feature-reference.md](06-feature-reference.md) | **Reference, not a sequential read.** Every feature and label one at a time: what it measures, why it exists, the exact formula, a worked example, and its measured distribution. |
| 7 | [07-phase-3-brief.md](07-phase-3-brief.md) | The Phase 3 implementation brief: what Phase 2 settled, the checkpoints and stop gates, and the evaluation rules that are not negotiable. |
| 8 | [08-evaluation.md](08-evaluation.md) | The Phase 3A evaluation harness: splits, baselines, metrics and reports — how the brief's rules are enforced in code rather than remembered. |

## Shortcuts

**Just want to query the database?** [01-architecture.md — Join key](01-architecture.md#join-key),
then [Operational notes](01-architecture.md#operational-notes). The join column
and the pre-open prints are the two things that bite first.

**About to write a feature or a backtest?**
[02-market-data.md — How future information must be excluded](02-market-data.md#how-future-information-must-be-excluded-from-features).
Six rules; all of them matter.

**Confused by a hole in a dataset?**
[03-features.md — Feature status](03-features.md#feature-status). Every absent
value carries its reason in a parallel `__status` column.

**What does this column actually mean?**
[06-feature-reference.md](06-feature-reference.md) documents all 18 features and
9 labels individually — formula, worked example, failure modes, distribution.

**About to build a model?**
[07-phase-3-brief.md — Non-negotiables](07-phase-3-brief.md#4-non-negotiables).
Seven rules, each written because breaking it produced a wrong answer during
Phase 2. The baseline one matters most.

**About to compare a model against something?**
[08-evaluation.md — What the harness refuses](08-evaluation.md#what-the-harness-refuses).
The rules above are enforced in `research/`, so a leaky split or a result
without a baseline raises rather than reporting a number.

**Wondering whether something is a bug or by design?**
[04-project-history.md — Open limitations](04-project-history.md#open-limitations).
The 49-vs-29 instrument counts, zero volume on indices, low candle counts on
deep-OTM strikes and gaps across days are all intended behaviour and are
documented as such.

## Conventions these documents share

* **Timestamps are naive IST.** Every column, every example, everywhere.
  Nothing in the schema enforces it.
* **Bars are left-labelled and usable only once closed** — a 5-minute bar
  stamped `09:15` is knowable at `09:20`, not at `09:15`.
* **Windows are half-open**, `[start, end)`.
* **Nothing is repaired.** Validation detects and reports; it never coerces,
  re-orders, interpolates or fills. An absence is information.
* **Nothing here is aspirational.** Every component named has a file behind it,
  and every number quoted was measured against the real database or the real
  test suite. Where a figure is a point-in-time probe, it says so.

## Operational notes for coding agents

`CLAUDE.md` at the repository root carries the same operational facts as
[01-architecture.md — Operational notes](01-architecture.md#operational-notes),
condensed. It is the agent-facing copy; this directory is the human-facing one.
When one changes, check the other.
