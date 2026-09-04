# Data Validation & Quality Reporting

How HereWeGoAgain checks that its market data is trustworthy, what each check
means, and how to run it.

The governing principle: **detect → report → optionally reject/quarantine.**
Validation never modifies, coerces, re-orders, interpolates or "repairs" market
data. Financial data that silently changes shape is worse than data that is
known to be wrong.

---

## Where the code lives

| Module | Responsibility |
|---|---|
| `marketdata/contracts.py` | Executable field definitions for tick / 1m / 5m (see `docs/data-contract.md`). |
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

---

## Severity

| Severity | Meaning | Effect |
|---|---|---|
| **ERROR** | The record violates the data contract or an arithmetic invariant. It cannot be trusted. | Marks the record invalid; withheld in `reject` mode. Report status → `FAIL`. |
| **WARNING** | The record is internally consistent, but the dataset shows something a human should look at. | Never withholds anything. Report status → `WARNING`. |

A report is `PASS` only when no issue of either severity was found.

---

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
| `out_of_session` | WARNING | A record outside a live trading session — a holiday, a weekend, or outside 09:15–15:30. Often the signature of a timezone bug (see `docs/point-in-time-data.md` §5.1) rather than bad prices, hence a warning. |

### Ordering and duplicates

| Code | Severity (bars) | Severity (ticks) | Trigger |
|---|---|---|---|
| `duplicate_timestamp` | ERROR | WARNING | Two records with the same timestamp. Bars: a series with two 09:15 bars is wrong. Ticks: a snapshot feed can legitimately republish a last-traded time — but `UNIQUE(instrument_id, timestamp)` means the second row is *silently dropped on insert*, so the count is real data loss and must stay visible. |
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

---

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

---

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

---

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite is deterministic and hermetic: no database, no network, no live
market data, and no dependence on today's date. Tests that care about trading
days inject their own calendar predicate, so results cannot drift as the
bundled NSE holiday list is extended.
