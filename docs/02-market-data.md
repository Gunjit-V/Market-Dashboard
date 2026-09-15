# Market Data

Canonical expectations for every market-data type HereWeGoAgain stores, the
temporal rules every feature and backtest must follow, and how data quality is
checked.

This document has three parts:

1. [Data contracts](#data-contracts) — field-by-field schemas
2. [Point-in-time semantics](#point-in-time-semantics) — leakage rules
3. [Validation and quality reporting](#validation-and-quality-reporting) — checks and how to run them

---

# Data contracts

The canonical expectations for every market-data type HereWeGoAgain stores.

These contracts describe **what the system actually holds today** — every field
listed exists in `db/init_schema.sql`, and the executable version in
`marketdata/contracts.py` is asserted against that file by
`tests/test_contracts.py`. No field is invented, and nothing the Angel One feed
does not supply is promised here.

Each field is labelled by origin:

| Origin | Meaning |
|---|---|
| **source** | Comes from the Angel One feed. Its meaning is the vendor's, not ours. |
| **derived** | Produced by HereWeGoAgain during ingestion. |
| **storage** | Database bookkeeping. Not part of the data itself; never a feature. |

"Required" means *required of any record claiming to satisfy this contract* —
which matches the `NOT NULL` columns in the schema.

## Common rules

* **Identity.** A record is identified by `(instrument_id, timestamp)` on bars,
  and `(instrument_id, timestamp, sequence_number)` on ticks. These are the
  `UNIQUE` constraints, so a second record with the same key is silently dropped
  on insert (`ON CONFLICT DO NOTHING`), never merged and never overwritten.
* **`instrument_id`** is *derived*: the feed speaks in exchange tokens, and
  ingestion resolves the token to an `instruments.id` before writing.
* **Timezone.** Every timestamp column is PostgreSQL `TIMESTAMP` (no time
  zone). The contract is **naive Asia/Kolkata (IST)**. Nothing in the database
  records the offset, so the convention is the only thing keeping it
  consistent — see [Tick timestamps](#tick-timestamps--fixed-with-a-labelled-residual-fallback)
  for one place where the convention was at risk.
* **Prices** are in rupees. The tick feed publishes paise and the collector
  divides by 100 (`_paise_to_rupees`); the candle API already returns rupees.
* **Precision.** Bars store `DECIMAL(12,4)`, ticks `DECIMAL(12,2)`. psycopg2
  returns `Decimal`; the validators and the access layer both accept it.
* **No repair.** A record that violates this contract is reported and — if the
  operator opts in — quarantined. It is never silently corrected.

## Tick (`tick_data`)

One row per SNAP_QUOTE snapshot received over the WebSocket. A tick is a
**snapshot of the market**, not a single trade print: it carries the last trade
plus day aggregates and the top of the book at that moment.

| Field | Type | Required | Origin | Meaning |
|---|---|---|---|---|
| `id` | `BIGSERIAL` | no | storage | Surrogate key. |
| `instrument_id` | `INTEGER` | **yes** | derived | FK → `instruments.id`, resolved from the feed token. |
| `timestamp` | `TIMESTAMP` | **yes** | source | The exchange feed clock (`exchange_timestamp`, epoch ms), falling back to `last_traded_timestamp` (epoch s), then to the collector's wall clock as a labelled last resort. See the caveat below. |
| `sequence_number` | `BIGINT` | no | source | Feed's per-packet ordering token, part of the uniqueness key. `0` marks rows collected before migration 001. |
| `ltp` | `DECIMAL(12,2)` | **yes** | source | Last traded price, rupees. Must be > 0; the collector already drops ticks with `ltp <= 0`. |
| `ltq` | `INTEGER` | no | source | Last traded quantity. |
| `open` | `DECIMAL(12,2)` | no | source | Day open. |
| `high` | `DECIMAL(12,2)` | no | source | Day high **so far**, as of this snapshot. |
| `low` | `DECIMAL(12,2)` | no | source | Day low **so far**, as of this snapshot. |
| `close` | `DECIMAL(12,2)` | no | source | ⚠️ The **previous day's close** (the feed's `closed_price`), *not* the current price. The column name is misleading and is retained for backward compatibility. |
| `avg_trade_price` | `DECIMAL(12,2)` | no | source | Day VWAP, from the feed's `average_traded_price`. (Rows written before that field name was corrected are `NULL`.) |
| `volume` | `BIGINT` | no | source | **Cumulative** traded volume for the day, not per-tick volume. Expected to be non-decreasing within a session and to reset at the next session. |
| `total_buy_qty` | `BIGINT` | no | source | Total pending buy quantity across the book. |
| `total_sell_qty` | `BIGINT` | no | source | Total pending sell quantity. |
| `open_interest` | `BIGINT` | no | source | Derivatives only; `NULL` for cash indices. |
| `best_5_buy` | `JSONB` | no | source | Top 5 bids as `[{price, quantity, orders}]`, prices already in rupees. |
| `best_5_sell` | `JSONB` | no | source | Top 5 asks, same shape. |
| `created_at` | `TIMESTAMP` | no | storage | Row insertion time (processing time). |

### Session boundaries

The feed keeps publishing snapshots after the 15:30 close — unchanged `ltp`,
`volume = 0`, `ltq = 0` — because the exchange is closed, not because anything
traded. The collector now ends its session at the close
(`in_market_hours()` in `downloader/tick_downloader.py`) rather than running
until the socket dies, so those snapshots are no longer stored.

Rows collected before that change existed outside 09:15-15:30 (21,914 of them,
stamped as late as 18:53 and in one case the following Monday) and have been
archived and removed via
`python scripts/purge_tick_data.py --out-of-session-only`.

Every `tick_data` row is therefore expected to fall inside a live session.
`marketdata.validation.validate_ticks` reports any that do not as
`out_of_session` warnings.

### Ordering and duplicates

* Ticks are expected to be **non-decreasing** in `timestamp`, not strictly
  increasing: a snapshot feed can legitimately publish several snapshots
  carrying the same last-traded timestamp. Out-of-order arrival is therefore a
  **warning**, not an error.
* The uniqueness key is `(instrument_id, timestamp, sequence_number)` as of
  `db/migrations/001_tick_sequence_number.sql`. Exchange timestamps are only
  **second-resolution**, so the previous `(instrument_id, timestamp)` key
  silently dropped every second and subsequent snapshot within the same second.
  Including the feed's own sequence number preserves them all while keeping
  re-inserts idempotent.

### Timestamp caveat

`resolve_tick_timestamp()` prefers `exchange_timestamp` (epoch **milliseconds**,
present in every mode) over `last_traded_timestamp` (epoch **seconds**,
SNAP_QUOTE only and `0` until the instrument trades). Conversion goes through an
explicit IST offset, so the stored naive value does not depend on the
collector's timezone.

When neither clock is usable the tick carries the collector's wall time and is
labelled `time_source="received"`; those fallbacks are counted per batch and in
the session summary. `time_source` is **not** persisted — there is no column for
it — so for rows already in the table, use `sequence_number > 0` to identify
rows written from the exchange clock. Timestamp precision does **not** work for
this; see [Tick timestamps](#tick-timestamps--fixed-with-a-labelled-residual-fallback)
for the two attempts that failed.

Full history and the pre-fix data caveat: [Tick timestamps](#tick-timestamps--fixed-with-a-labelled-residual-fallback).

## 1-minute OHLCV (`ohlcv_1min`)

One row per instrument per trading minute, as returned by Angel One's
`ONE_MINUTE` interval.

| Field | Type | Required | Origin | Meaning |
|---|---|---|---|---|
| `id` | `BIGSERIAL` | no | storage | Surrogate key. |
| `instrument_id` | `INTEGER` | **yes** | derived | FK → `instruments.id`. |
| `timestamp` | `TIMESTAMP` | **yes** | source | **Bar start** (left-labelled), naive IST. Normalised by `downloader.ohlcv.normalize_timestamp`. |
| `open` | `DECIMAL(12,4)` | **yes** | source | First traded price in the bar. Must be > 0. |
| `high` | `DECIMAL(12,4)` | **yes** | source | Highest traded price in the bar. Must be > 0. |
| `low` | `DECIMAL(12,4)` | **yes** | source | Lowest traded price in the bar. Must be > 0. |
| `close` | `DECIMAL(12,4)` | **yes** | source | Last traded price in the bar. Must be > 0. |
| `volume` | `BIGINT` | **yes** | source | Quantity traded **during this bar** (not cumulative). Must be ≥ 0. |
| `created_at` | `TIMESTAMP` | no | storage | Row insertion time. |

**Invariants** (enforced by `marketdata.validation.validate_ohlcv`):

```
high >= max(open, close)
low  <= min(open, close)
high >= low
open, high, low, close > 0
volume >= 0
```

**Grid.** Bars sit on a 1-minute grid anchored at the session open, 09:15 IST.
A regular session has **375** one-minute bars, 09:15 … 15:29. No bar starts at
15:30 — that is the close.

**Zero volume is valid.** Cash-index instruments (`AMXIDX`, e.g. Nifty 50)
structurally report `volume = 0`. This is a property of an index, not a defect,
and must never be treated as missing data.

**Ordering.** Strictly increasing per instrument. Duplicates and regressions
are **errors** (unlike ticks), because a bar series with two 09:15 bars is
simply wrong.

## 5-minute OHLCV (`ohlcv_5min`)

Identical field-for-field to `ohlcv_1min`, with `DECIMAL(12,4)` prices and the
same invariants. The differences are entirely temporal:

* **Grid:** 5-minute, anchored at 09:15 IST. A regular session has **75** bars,
  09:15 … 15:25. (The same 75 that `backtest/rv.py: CANDLES_PER_DAY` annualises
  with — a test asserts the two agree.)
* **Two provenances.** Rows normally come from the API's `FIVE_MINUTE`
  interval, but `downloader.ohlcv.backfill_from_one_minute()` can also build
  them by aggregating `ohlcv_1min` (only where exactly 5 one-minute bars exist
  for a bucket, and only via `ON CONFLICT DO NOTHING`, so existing rows are
  never replaced). **The table does not record which path produced a row.**
  Listed as a known limitation in [04-project-history.md](04-project-history.md#open-limitations).

## Derived and optional fields elsewhere

Nothing in `ohlcv_1min` / `ohlcv_5min` / `tick_data` is computed by
HereWeGoAgain beyond `instrument_id` resolution and timestamp normalisation.
All analytics — realized volatility (`backtest/rv.py`), implied volatility
(`api/utils/iv.py`), order-book wall/absorption events (`dashboard/server.py`),
strategy signals — are computed **on read** and are never written back into the
market-data tables. Simulation outputs live in their own tables (`trades`,
`equity_curve`, `signals`) and are not market data.

---

# Point-in-time semantics

Rules every future feature, model or backtest in HereWeGoAgain must follow to
avoid look-ahead bias.

Look-ahead leakage is the failure mode that makes a research result worthless
without making it *look* wrong: a model trained on a bar that had not finished
forming, or on a row back-filled hours later, will show excellent backtest
performance and fail in production. The rules below exist so that the
distinction between "what was knowable at time *t*" and "what is in the
database now" is explicit rather than assumed.

## What a timestamp represents

| Table | `timestamp` is | Event or processing time |
|---|---|---|
| `ohlcv_1min` | The bar's **opening minute** | Event time |
| `ohlcv_5min` | The bar's **opening minute** | Event time |
| `tick_data` | The exchange feed clock (`exchange_timestamp`), falling back to `last_traded_timestamp` | Event time — with a labelled last-resort fallback to processing time, see [Tick timestamps](#tick-timestamps--fixed-with-a-labelled-residual-fallback) |

`created_at` on all three tables is **processing time**: when the row was
inserted. It is the only honest record of when the data became available
locally, and it is never a feature. It is also not a reliable "knowable at"
marker, because a backfill inserts old bars with a recent `created_at`.

All timestamps are naive and are to be read as **Asia/Kolkata (IST)**. The
database columns are `TIMESTAMP WITHOUT TIME ZONE`, so this convention is
carried by code and documentation only — nothing in the schema enforces it.

## How bars are timestamped, and when a bar is complete

Bars are **left-labelled**: the timestamp is the bar's start.

```
bar 09:15 (5m) covers [09:15, 09:20)  and is complete at 09:20
bar 09:15 (1m) covers [09:15, 09:16)  and is complete at 09:16
```

Formally, for a bar labelled `t` with width `w`:

* it aggregates trades in `[t, t + w)`;
* it is **incomplete** for any observer before `t + w`;
* it is **knowable** — safe to use as an input — only at or after `t + w`.

This is consistent with the rest of the system: `scheduler/nse_calendar.py`
downloads the 09:15 bar at the 09:20 slot and the last bar at the 15:30 slot,
and `downloader/ohlcv.py` waits `OHLCV_BAR_DELAY_SECONDS` past each boundary
before asking for it. `marketdata.sessions.bar_close(t, w)` is the single
implementation of `t + w`, and `marketdata.access.Bar.close_time` exposes it on
every bar the access layer returns.

**Session grid.** Bars are anchored at the 09:15 session open, not at the top
of the hour. A regular NSE session contains 375 one-minute bars (09:15–15:29)
and 75 five-minute bars (09:15–15:25). No bar starts at 15:30.

## How future information must be excluded from features

**Rule 1 — Use the bar close, never the bar label, as the decision time.**
A feature computed for decision time `T` may only use bars with
`timestamp + bar_minutes <= T`. Using bars with `timestamp <= T` leaks up to
one full bar of future information, which for a 5-minute bar is the difference
between a good backtest and a fictional one.

**Rule 2 — Ask the access layer, do not filter by hand.**

```python
from marketdata.access import get_market_data

bars = get_market_data(
    conn,
    instrument="Nifty 50",
    start=datetime(2026, 9, 3, 9, 15),
    end=datetime(2026, 9, 3, 15, 30),
    timeframe="5m",
    as_of=decision_time,   # ← the point-in-time cut-off
)
```

With `as_of` set, the SQL filter is
`timestamp + make_interval(mins => bar_minutes) <= as_of`, so Rule 1 is
enforced in one place rather than re-derived per notebook. For ticks, `as_of`
filters on `timestamp <= as_of`: a tick is a point event and is knowable the
instant it is stamped.

**Rule 3 — Windows are half-open, `[start, end)`.**
Consecutive windows tile exactly: `[09:15, 09:20)` and `[09:20, 09:25)` share
no bar. (Note this differs from the pre-existing `/ohlcv` REST endpoint, which
uses an inclusive `<=` end bound. That endpoint is unchanged for backward
compatibility; new code should use the access layer.)

**Rule 4 — Labels must not be shifted to "fix" alignment.**
If a feature needs the bar that was complete at `T`, select it with `as_of`;
do not rewrite timestamps. Re-labelling a bar changes what the data means and
is exactly the kind of silent repair this project forbids.

**Rule 5 — The target of a prediction must be strictly after the features.**
For a model predicting the `t+1` bar's return from the `t` bar, the features
are knowable at `t + w` and the label at `t + 2w`. Never build a label from a
bar that also contributes to a feature.

**Rule 6 — Never use `created_at` or an unfiltered "latest row" as a feature.**
Both reflect when data landed in this database, not when the market produced
it, and both change if a backfill runs.

## How historical datasets should be constructed

1. **Pick the decision times first** — the timestamps at which the model would
   have acted (typically bar closes on trading days from
   `marketdata.sessions.expected_bar_starts`).
2. **For each decision time, read with `as_of` set to it.** Do not read the
   whole period once and slice afterwards; slicing is where the off-by-one-bar
   leak reappears.
3. **Validate before training.** Run `marketdata.validation` over the extracted
   window and record the result alongside the dataset. A `FAIL` status means
   the dataset contains contract violations and must not be used silently.
4. **Record the extraction time.** The market-data tables have no
   as-of-versioning (see [No as-of versioning of the data itself](#no-as-of-versioning-of-the-data-itself)),
   so the only way to know what a dataset saw is to write down when it was pulled.
5. **Treat gaps as information, not as something to fill.** An absent bar in an
   illiquid option means nobody traded. Forward-filling it invents a price that
   never existed. If a model needs a continuous series, the fill must happen in
   the feature pipeline, explicitly and documented — never in storage.

## Known ambiguities and leakage risks

These are **documented, not silently redesigned**. Phase 1 adds detection for
them and changes no ingestion behaviour.

### Tick timestamps — fixed, with a labelled residual fallback

**Resolved.** `downloader/tick_downloader.py` now derives a tick's event time
through `resolve_tick_timestamp()`, in this order:

1. **`exchange_timestamp`** (epoch **milliseconds**) — the exchange's own feed
   clock. Present in every subscription mode and populated on every packet.
   Measured against local receipt on live data it runs ~1.8s behind (max 7.7s),
   so it is a genuine event clock, not a delayed field.
2. **`last_traded_timestamp`** (epoch **seconds**) — SNAP_QUOTE only, and `0`
   until the instrument actually trades.
3. The collector's wall clock — last resort, and the tick is **labelled**
   `time_source="received"` so fallbacks are counted per batch and in the
   session summary rather than passing silently as market time.

Two concrete bugs this fixed, both confirmed by probing the live table:

* **98.7% of ticks were stamped with `datetime.now()`.** Only 60,183 of
  4,796,304 rows carried a real feed timestamp. `last_traded_timestamp` is `0`
  — falsy — for anything that has not traded, and the old
  `if raw_ts` test sent every such packet to the `datetime.now()` fallback.
  Cash indices, which never "trade", were affected on essentially every tick.
  Drift on those rows reached **63,323s (17.6 hours)** versus 7.7s on real
  feed rows, which is what produced the 16:00/18:00/21:00 tick clusters.
* **Timezone binding.** `datetime.fromtimestamp()` without a `tz` bound the
  value to the collector's local zone. Conversion now goes through an explicit
  `IST_TZ` offset, so the stored naive timestamp means the same thing whether
  the collector runs on the IST host or inside a container. `TZ=Asia/Kolkata`
  was also added to the `tick-downloader` compose service to match the other
  three, but the code no longer depends on it.

**Historical data was not corrupted by the timezone issue.** The collector has
been running natively on an IST host, so `fromtimestamp()` resolved to IST all
along; stored session hours are 09:00–15:30 IST as expected. The timezone bug
was a latent landmine that would have fired on first containerised run, not
existing damage.

**Historical data *is* affected by the fallback issue.** Rows written before
this fix are distinguishable: feed timestamps land on whole seconds, while
`datetime.now()` values carry microseconds.

```sql
-- Rows written from the exchange clock. sequence_number is set only on that
-- path, so it is the one unambiguous marker.
SELECT count(*) FROM tick_data WHERE sequence_number > 0;   -- event time
SELECT count(*) FROM tick_data WHERE sequence_number = 0;   -- unknown/pre-fix
```

Timestamp precision cannot be used for this. Two earlier attempts both failed:
testing `microseconds <> 0` (whole seconds) misclassifies genuine millisecond
feed timestamps such as `13:53:39.025000`, and testing millisecond alignment
misclassifies `datetime.now()` values — Windows clock granularity puts ~59% of
them on a millisecond boundary. Only `sequence_number` separates the two
cleanly, and it exists only from migration 001 onward, so rows written before
it are correctly reported as *unknown* rather than as fallbacks.

Those rows are still usable as "a tick happened around then" but must not be
treated as precise event times, and should not be joined to bars at
sub-minute resolution. No backfill is possible — the original exchange clock
was never stored.

### Ticks sharing a timestamp — fixed by migration 001

**Resolved.** `UNIQUE(instrument_id, timestamp)` plus `ON CONFLICT DO NOTHING`
meant only the first snapshot at a given timestamp survived. Because Angel One
publishes exchange timestamps at **one-second resolution**, that discarded
every additional genuine snapshot inside the same second.

This was masked before the timestamp fix: microsecond `datetime.now()` values
almost never collided. Real exchange clocks collide often, so the constraint
became actively lossy exactly when timestamps became correct.

`db/migrations/001_tick_sequence_number.sql` adds the feed's own
`sequence_number` (a monotonic per-packet token the SDK parses and the
collector previously discarded) to the uniqueness key:

```sql
UNIQUE (instrument_id, timestamp, sequence_number)
```

Verified against the live table: three snapshots sharing one exchange second
now store as three rows (previously one), while replaying the same batch still
inserts nothing — deduplication is preserved, only genuine distinct packets are
kept.

Rows collected before the migration carry `sequence_number = 0`, which doubles
as a "pre-migration" marker. The column is `NOT NULL` deliberately: with NULLs,
Postgres treats each NULL as distinct and the constraint would stop protecting
legacy rows.

This was never a *leakage* risk — it discarded information rather than adding
future information — but it did make tick history an incomplete record.

### Five-minute bars have two possible provenances

`ohlcv_5min` rows may come from the API directly or from
`backfill_from_one_minute()` aggregating `ohlcv_1min`. The table records no
provenance flag. Both paths produce a correctly-labelled, complete bar, so
neither leaks — but a bar produced by backfill became *available* later than
its label suggests, which matters for a strict "what was knowable in real
time?" reconstruction.

### No as-of versioning of the data itself

If the vendor revises a bar, the current schema cannot represent both the
original and the revision (`ON CONFLICT DO NOTHING` keeps the first version and
discards the revision entirely). Point-in-time correctness is therefore
guaranteed with respect to *bar completion*, not with respect to *vendor
revisions*. Bitemporal storage is deliberately out of scope for Phase 1.

### The holiday calendar covers 2026 only

`scheduler/nse_calendar.py` ships a curated holiday list for 2026 and accepts
`NSE_HOLIDAYS` / `NSE_SPECIAL_TRADING_DAYS` overrides. Outside 2026 only
weekends are known to be non-trading, so gap counts for other years are
over-stated. The quality report attaches an explicit note rather than hiding
this.

## Quick reference

| Question | Answer |
|---|---|
| What does a bar timestamp mean? | Its opening minute (left-labelled). |
| When is a bar usable? | At `timestamp + bar_minutes`, not before. |
| What timezone? | Naive IST, by convention — nothing enforces it. |
| How do I read history safely? | `marketdata.access.get_market_data(..., as_of=T)`. |
| Are windows inclusive? | `[start, end)` — start inclusive, end exclusive. |
| Is a missing bar an error? | No. Only in-session absences are even reported, and then as a warning. |
| Can I forward-fill in storage? | No. Fill in the feature pipeline, explicitly. |
| Can I trust `created_at` as "knowable at"? | No — a backfill breaks it. |
| Are tick timestamps event time? | Yes where `sequence_number > 0` (exchange clock). Rows with `sequence_number = 0` predate the fix and their clock source is unknowable — see [Tick timestamps](#tick-timestamps--fixed-with-a-labelled-residual-fallback). |

---

# Validation and quality reporting

How HereWeGoAgain checks that its market data is trustworthy, what each check
means, and how to run it.

The governing principle: **detect → report → optionally reject/quarantine.**
Validation never modifies, coerces, re-orders, interpolates or "repairs" market
data. Financial data that silently changes shape is worse than data that is
known to be wrong.

## Where the code lives

| Module | Responsibility |
|---|---|
| `marketdata/contracts.py` | Executable field definitions for tick / 1m / 5m (see [Data contracts](#data-contracts)). |
| `marketdata/sessions.py` | NSE session model — which bar timestamps *should* exist. |
| `marketdata/validation.py` | Pure validators returning structured issues. |
| `marketdata/quality.py` | Aggregates validation results into a report. |
| `marketdata/ingest.py` | The non-destructive hook used by the downloader. |
| `marketdata/access.py` | Deterministic, point-in-time-aware reads. |
| `marketdata/report.py` | Assembles a report from stored data; CLI entry point. |
| `api/routes/quality.py` | Read-only HTTP access to the same report. |

The whole package depends on nothing outside the Python standard library
(`scheduler.nse_calendar` is itself stdlib-only), so it imports and tests
without a database, without network access and without the Angel One SDK.

## Severity

| Severity | Meaning | Effect |
|---|---|---|
| **ERROR** | The record violates the data contract or an arithmetic invariant. It cannot be trusted. | Marks the record invalid; withheld in `reject` mode. Report status → `FAIL`. |
| **WARNING** | The record is internally consistent, but the dataset shows something a human should look at. | Never withholds anything. Report status → `WARNING`. |

A report is `PASS` only when no issue of either severity was found.

## Checks

### Schema

| Code | Severity | Trigger |
|---|---|---|
| `missing_field` | ERROR | A required field is absent or `NULL`. |
| `unexpected_type` | ERROR | A field's value is not of a type the contract allows. Numeric strings and `Decimal` are accepted (the feed sends the former, psycopg2 returns the latter); `bool` never is. |
| `unknown_field` | WARNING | A column not in the contract. Usually a caller passing extra data, not corruption. |

### Timestamps

| Code | Severity | Trigger |
|---|---|---|
| `malformed_timestamp` | ERROR | A string timestamp that is not parseable ISO-8601. |
| `timezone_inconsistent` | ERROR | A timezone-aware timestamp. The contract stores naive IST; the value is **reported, never converted**, so the one timezone decision stays in `downloader.ohlcv.normalize_timestamp` instead of being made twice. |
| `timestamp_not_aligned` | ERROR (bars only) | A bar not on the timeframe grid anchored at 09:15 (e.g. a 5-minute bar at 09:22). |
| `out_of_session` | WARNING | A record outside a live trading session — a holiday, a weekend, or outside 09:15–15:30. Often the signature of a timezone bug (see [Tick timestamps](#tick-timestamps--fixed-with-a-labelled-residual-fallback)) rather than bad prices, hence a warning. |

### Ordering and duplicates

| Code | Severity (bars) | Severity (ticks) | Trigger |
|---|---|---|---|
| `duplicate_timestamp` | ERROR | WARNING | Two records with the same timestamp. Bars: a series with two 09:15 bars is wrong. Ticks: a snapshot feed can legitimately republish a last-traded time — and since migration 001 the uniqueness key is `(instrument_id, timestamp, sequence_number)`, so genuine same-second snapshots are kept rather than dropped. The count stays visible because rows written before that migration all carry `sequence_number = 0`, where a same-second collision *was* silent data loss. |
| `out_of_order` | ERROR | WARNING | A timestamp not after its predecessor. Bars must be strictly increasing; ticks only non-decreasing. |

### OHLC invariants (bars)

`ohlc_invariant`, ERROR. Every bar must satisfy:

```
high >= max(open, close)
low  <= min(open, close)
high >= low
```

A flat bar where all four prices are equal is valid.

`non_positive_price`, ERROR: any of `open`/`high`/`low`/`close` ≤ 0.

### OHLC invariants (ticks)

* `ohlc_invariant`, ERROR — the snapshot's day `high` is below its day `low`.
* `ltp_outside_day_range`, WARNING — `ltp` outside `[low, high]`. A snapshot's
  day range can lag the LTP by one message, so this is a staleness signal
  rather than corruption.

### Volume

| Code | Severity | Trigger |
|---|---|---|
| `negative_value` | ERROR | A negative volume, quantity or open interest. |
| `volume_regression` | WARNING | Tick `volume` (cumulative for the day) fell within the same session. A reset at the next session is expected and is **not** flagged. |

**Zero volume is valid.** Cash-index instruments (`AMXIDX`) structurally report
`volume = 0`. This is never treated as missing data.

### Gaps — the careful one

`unexpected_gap`, WARNING, bars only.

A market-data gap is **not** automatically an error, and a naive "timestamps
must be continuous" check would flag every night, weekend and holiday. The
validator instead compares the data against an explicit session model
(`marketdata/sessions.py`):

* Only bars **inside a live trading session** are ever expected. The overnight
  close, weekends, and exchange holidays produce no expectation at all.
* The trading-day calendar comes from `scheduler/nse_calendar.py` — the same
  one the schedulers already run on, including its `NSE_HOLIDAYS` /
  `NSE_SPECIAL_TRADING_DAYS` overrides and the Sunday budget-session case.
* Expectations are computed **only across the observed span**
  `[first bar, last bar]`. History that simply has not been downloaded yet is
  not a gap.
* A gap is always a **warning**. For an illiquid option, no bar genuinely means
  no trades — which is information, not a defect.
* Gap checking is switched off entirely for ingestion batches
  (`check_gaps=False`), because a single API chunk is expected to be partial.

Because the bundled calendar covers 2026 only, the report attaches an explicit
note when the requested period falls outside it, rather than quietly
over-counting gaps.

## Running validation

### From the command line

```bash
# Bars for one instrument over a period
python -m marketdata.report --instrument "Nifty 50" \
    --from 2026-01-01 --to 2026-09-01

# Include ticks, machine-readable
python -m marketdata.report --instrument "Nifty 50" \
    --from 2026-09-01 --to 2026-09-05 \
    --timeframes tick,1m,5m --json
```

`--from` is inclusive, `--to` is exclusive. Both accept an ISO date
(`2026-01-01`) or a full timestamp (`2026-01-01T09:15:00`), interpreted as IST.

Exit codes are usable in a cron job or a CI gate:

| Code | Meaning |
|---|---|
| `0` | PASS |
| `1` | WARNING |
| `2` | FAIL |
| `3` | Usage or data error (unknown symbol, bad timeframe) |

Sample output:

```
Instrument: Nifty 50
Period: 2026-09-03 00:00:00 → 2026-09-04 00:00:00

1m OHLCV  [WARNING]
  Records         : 372
  Expected bars   : 375 (completeness 99.2%)
  Duplicates      : 0
  Out-of-order    : 0
  Schema invalid  : 0
  Invalid OHLC    : 0
  Unexpected gaps : 3
  Out-of-session  : 0
  Volume issues   : 0

5m OHLCV  [PASS]
  Records         : 75
  Expected bars   : 75 (completeness 100.0%)
  ...

Overall status: WARNING
```

### Over HTTP

```bash
curl "http://localhost:8000/quality/Nifty%2050?from_date=2026-09-01T00:00:00&to_date=2026-09-05T00:00:00&timeframes=1m,5m"
```

Read-only, no API key required, same numbers as the CLI. Defaults to the last
7 days and to `1m,5m` if not specified. Note the endpoint reads every row in
range, so keep the window sensible; the `tick` timeframe is additionally capped
by `max_ticks`.

### From Python

```python
from marketdata.validation import validate_ohlcv, validate_ticks
from marketdata.report import assess_instrument

result = validate_ohlcv(records, "5m")     # records are plain dicts/rows
print(result.ok, result.by_code())

report = assess_instrument(conn, "Nifty 50", start, end, ("1m", "5m"))
print(report.render())
```

## Validation during ingestion

`downloader/ohlcv.py: save_candles_to_db()` screens each batch through
`marketdata.ingest.screen_candles()` before inserting.

| Mode | `MARKETDATA_VALIDATION_MODE` | Behaviour |
|---|---|---|
| **report** (default) | unset or `report` | Validate, log a one-line summary per batch (details at `DEBUG`), then insert **every candle**, unchanged. Byte-for-byte the pre-Phase-1 insertion set. |
| **reject** | `reject` | Additionally withhold candles carrying an ERROR. Warning-only candles are still inserted — quarantining them would lose real data. |

```env
# .env — opt in to rejecting contract violations
MARKETDATA_VALIDATION_MODE=reject
MARKETDATA_QUARANTINE_FILE=logs/quarantine.jsonl
```

With `MARKETDATA_QUARANTINE_FILE` set, each withheld row is appended verbatim
as JSONL alongside the reason codes:

```json
{"quarantined_at": "2026-09-04T10:15:02", "instrument_id": 42, "timeframe": "5m",
 "row": ["2026-09-03T09:25:00+05:30", 100.0, 98.0, 99.0, 99.5, 900],
 "reasons": ["ohlc_invariant", "ohlc_invariant"]}
```

Quarantining is **storage, not repair**: the row is preserved exactly as it
arrived so a human can decide what to do with it.

An unrecognised mode falls back to `report` with a warning — a typo in `.env`
can never cause data to be dropped.

Tick ingestion is **not** screened. `parse_tick` already drops non-positive
LTPs, and adding a per-tick validation step to a hot WebSocket path is not
justified by Phase 1's goals; ticks are validated on read instead.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite is deterministic and hermetic: no database, no network, no live
market data, and no dependence on today's date. Tests that care about trading
days inject their own calendar predicate, so results cannot drift as the
bundled NSE holiday list is extended.
