# Point-in-Time Data Semantics

Rules every future feature, model or backtest in HereWeGoAgain must follow to
avoid look-ahead bias.

Look-ahead leakage is the failure mode that makes a research result worthless
without making it *look* wrong: a model trained on a bar that had not finished
forming, or on a row back-filled hours later, will show excellent backtest
performance and fail in production. The rules below exist so that the
distinction between "what was knowable at time *t*" and "what is in the
database now" is explicit rather than assumed.

---

## 1. What a timestamp represents

| Table | `timestamp` is | Event or processing time |
|---|---|---|
| `ohlcv_1min` | The bar's **opening minute** | Event time |
| `ohlcv_5min` | The bar's **opening minute** | Event time |
| `tick_data` | The exchange feed clock (`exchange_timestamp`), falling back to `last_traded_timestamp` | Event time — with a labelled last-resort fallback to processing time, see §5.1 |

`created_at` on all three tables is **processing time**: when the row was
inserted. It is the only honest record of when the data became available
locally, and it is never a feature. It is also not a reliable "knowable at"
marker, because a backfill inserts old bars with a recent `created_at`.

All timestamps are naive and are to be read as **Asia/Kolkata (IST)**. The
database columns are `TIMESTAMP WITHOUT TIME ZONE`, so this convention is
carried by code and documentation only — nothing in the schema enforces it.

---

## 2. How bars are timestamped, and when a bar is complete

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

---

## 3. How future information must be excluded from features

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

---

## 4. How historical datasets should be constructed

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
   as-of-versioning (see §6), so the only way to know what a dataset saw is to
   write down when it was pulled.
5. **Treat gaps as information, not as something to fill.** An absent bar in an
   illiquid option means nobody traded. Forward-filling it invents a price that
   never existed. If a model needs a continuous series, the fill must happen in
   the feature pipeline, explicitly and documented — never in storage.

---

## 5. Known ambiguities and leakage risks

These are **documented, not silently redesigned**. Phase 1 adds detection for
them and changes no ingestion behaviour.

### 5.1 Tick timestamps — fixed, with a labelled residual fallback

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

### 5.2 Ticks sharing a timestamp — fixed by migration 001

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

### 5.3 Five-minute bars have two possible provenances

`ohlcv_5min` rows may come from the API directly or from
`backfill_from_one_minute()` aggregating `ohlcv_1min`. The table records no
provenance flag. Both paths produce a correctly-labelled, complete bar, so
neither leaks — but a bar produced by backfill became *available* later than
its label suggests, which matters for a strict "what was knowable in real
time?" reconstruction.

### 5.4 There is no as-of versioning of the data itself

If the vendor revises a bar, the current schema cannot represent both the
original and the revision (`ON CONFLICT DO NOTHING` keeps the first version and
discards the revision entirely). Point-in-time correctness is therefore
guaranteed with respect to *bar completion*, not with respect to *vendor
revisions*. Bitemporal storage is deliberately out of scope for Phase 1.

### 5.5 The holiday calendar covers 2026 only

`scheduler/nse_calendar.py` ships a curated holiday list for 2026 and accepts
`NSE_HOLIDAYS` / `NSE_SPECIAL_TRADING_DAYS` overrides. Outside 2026 only
weekends are known to be non-trading, so gap counts for other years are
over-stated. The quality report attaches an explicit note rather than hiding
this.

---

## 6. Quick reference

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
| Are tick timestamps event time? | Yes for new rows (exchange clock). Pre-fix rows with non-zero microseconds are processing time — see §5.1. |
