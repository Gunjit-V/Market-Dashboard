# Data Contracts

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

---

## Common rules

* **Identity.** A record is identified by `(instrument_id, timestamp)`. This is
  the `UNIQUE` constraint on all three tables, so a second record with the same
  key is silently dropped on insert (`ON CONFLICT DO NOTHING`), never merged
  and never overwritten.
* **`instrument_id`** is *derived*: the feed speaks in exchange tokens, and
  ingestion resolves the token to an `instruments.id` before writing.
* **Timezone.** Every timestamp column is PostgreSQL `TIMESTAMP` (no time
  zone). The contract is **naive Asia/Kolkata (IST)**. Nothing in the database
  records the offset, so the convention is the only thing keeping it
  consistent — see `docs/point-in-time-data.md`, which documents one place
  where the convention is currently at risk.
* **Prices** are in rupees. The tick feed publishes paise and the collector
  divides by 100 (`_paise_to_rupees`); the candle API already returns rupees.
* **Precision.** Bars store `DECIMAL(12,4)`, ticks `DECIMAL(12,2)`. psycopg2
  returns `Decimal`; the validators and the access layer both accept it.
* **No repair.** A record that violates this contract is reported and — if the
  operator opts in — quarantined. It is never silently corrected.

---

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
it — so for rows already in the table, distinguish them by microseconds:
feed-derived timestamps land on whole seconds, `datetime.now()` values do not.

Full history and the pre-fix data caveat: `docs/point-in-time-data.md` §5.1.

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

---

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
  Listed as a known limitation in `docs/phase-1-summary.md`.

---

## Derived and optional fields elsewhere

Nothing in `ohlcv_1min` / `ohlcv_5min` / `tick_data` is computed by
HereWeGoAgain beyond `instrument_id` resolution and timestamp normalisation.
All analytics — realized volatility (`backtest/rv.py`), implied volatility
(`api/utils/iv.py`), order-book wall/absorption events (`dashboard/server.py`),
strategy signals — are computed **on read** and are never written back into the
market-data tables. Simulation outputs live in their own tables (`trades`,
`equity_curve`, `signals`) and are not market data.
