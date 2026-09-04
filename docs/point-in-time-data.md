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
| `tick_data` | The snapshot's `last_traded_timestamp` | Event time — **with a fallback to processing time**, see §5 |

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

### 5.1 Tick timestamps mix event time, processing time and timezones

`downloader/tick_downloader.py: parse_tick` does:

```python
raw_ts = raw.get("last_traded_timestamp")
timestamp = datetime.fromtimestamp(int(raw_ts)) if raw_ts else datetime.now()
```

Two distinct problems:

* **Mixed clocks.** When the feed omits `last_traded_timestamp`, the row
  silently carries *processing* time instead of event time. Nothing in the row
  distinguishes the two afterwards.
* **Mixed timezones.** `datetime.fromtimestamp()` without `tz` uses the
  *process's* local timezone. `docker-compose.yml` sets `TZ: Asia/Kolkata` for
  `ohlcv-scheduler`, `instrument-sync-scheduler` and `paper-trading-scheduler`
  — but **not** for `tick-downloader`, and the `Dockerfile` sets no `TZ`
  either. In that container the timezone is UTC, so tick timestamps land 5h30m
  behind the IST-naive timestamps in `ohlcv_1min` / `ohlcv_5min`. Joining
  ticks to bars on time would then be wrong by 5h30m, and a 09:15 IST tick
  would be stored as 03:45.

**Detection (added in Phase 1):** `validate_ticks` raises an
`out_of_session` warning for any tick whose timestamp falls outside a live
trading session. A UTC-stamped session shows up as a wall of 03:45–10:00
warnings — the signature of exactly this bug. Run:

```bash
python -m marketdata.report --instrument "Nifty 50" \
    --from 2026-09-01 --to 2026-09-05 --timeframes tick
```

**Not fixed here, deliberately.** Setting `TZ: Asia/Kolkata` on the
`tick-downloader` service would fix new rows, but it would also make historical
`tick_data` timestamps inconsistent with new ones at an undocumented cut-over
point. That is a data-semantics change and needs a human decision plus a
backfill plan; it is listed as recommended follow-up work in
`docs/phase-1-summary.md`.

### 5.2 Ticks sharing a timestamp are dropped

`UNIQUE(instrument_id, timestamp)` plus `ON CONFLICT DO NOTHING` means only the
first snapshot at a given timestamp survives. This is lossy but *not* a
leakage risk — it discards information rather than adding future information.
`validate_ticks` counts duplicates so the loss is measurable.

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
