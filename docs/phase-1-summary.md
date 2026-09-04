# Phase 1 — Trustworthy Market Data Foundation

Branch: `claude/herewego-phase-1-data-foundation-wo8gsq`
Baseline: `8287049` ("Default Instruments to active-only; add scheduler
heartbeat and API auth") on `master`.

Phase 1 makes the existing HereWeGoAgain market-data foundation explicit,
validated, reproducible and documented. It builds no models, no agents and no
new infrastructure. `master` was not modified.

---

## Completed

### 1. Documentation of the system as it exists

* **`docs/current-architecture.md`** — the actual components, the real data
  flow (with a Mermaid diagram that reflects only files that exist), the Angel
  One endpoints in use, the storage layout (tables, keys, indexes, and the
  explicit absence of partitioning and retention), and the pre-Phase-1 state of
  tests, linting and CI.
* **`docs/data-contract.md`** — field-by-field contracts for ticks, 1-minute
  OHLCV and 5-minute OHLCV, each field labelled *source* / *derived* /
  *storage*, with the invariants, the grid, and the ordering and duplicate
  rules per dataset.
* **`docs/point-in-time-data.md`** — what a timestamp means, when a bar is
  complete, six rules for excluding future information, how to construct a
  historical dataset, and the temporal ambiguities that exist today.
* **`docs/validation.md`** — every check, its severity and its rationale, plus
  how to run validation from the CLI, over HTTP and from Python.

### 2. Executable data contracts — `marketdata/contracts.py`

The contracts are code, not just prose: `FieldSpec` / `DatasetContract` encode
required-ness, types, origin, positivity and non-negativity for all three
datasets. `tests/test_contracts.py` parses `db/init_schema.sql` and asserts the
contracts match the real tables column-for-column, so the documentation cannot
drift away from the schema unnoticed.

### 3. Session model — `marketdata/sessions.py`

An explicit NSE session model (09:15–15:30 IST, close exclusive), reusing the
existing `scheduler/nse_calendar.py` trading-day calendar rather than
duplicating it. Provides expected bar timestamps, grid alignment anchored at
the session open, and `bar_close()`. A regression test asserts it agrees with
both `scheduler.nse_calendar.LAST_DOWNLOAD_SLOT` and
`backtest.rv.CANDLES_PER_DAY`.

### 4. Validation layer — `marketdata/validation.py`

Pure functions returning structured `Issue` objects with stable codes and two
severities. Covers schema, timestamps and timezones, alignment, ordering,
duplicates, OHLC invariants, price positivity, volume, out-of-session records
and session-aware gaps. `partition()` splits records by validity without
altering a single value.

### 5. Non-destructive ingestion hook — `marketdata/ingest.py`

`downloader/ohlcv.py: save_candles_to_db()` now screens each batch before
inserting. The default `report` mode logs and inserts **exactly** the rows it
inserted before Phase 1. `MARKETDATA_VALIDATION_MODE=reject` is opt-in and only
withholds contract-violating rows, optionally appending them verbatim to
`MARKETDATA_QUARANTINE_FILE` as JSONL. An unrecognised mode falls back to
`report`, so a typo in `.env` can never drop data.

### 6. Data-quality reporting — `marketdata/quality.py`, `marketdata/report.py`

Per-instrument, per-period report with per-dataset counts, completeness against
the session model, and an overall `PASS` / `WARNING` / `FAIL`. Every number is
derived from validating rows actually read from PostgreSQL. A dataset that was
not validated is reported as **absent**, never as clean. CLI:

```bash
python -m marketdata.report --instrument "Nifty 50" --from 2026-01-01 --to 2026-09-01
```

Exit codes 0/1/2/3 make it usable as a scheduled check or a CI gate.

### 7. Reproducible, point-in-time-aware access — `marketdata/access.py`

`get_market_data(conn, instrument, start, end, timeframe, as_of=...)`:
deterministic ordering, half-open `[start, end)` windows, whitelisted
timeframes (a timeframe string can never reach SQL unchecked), and an `as_of`
cut-off that admits a bar only once it has closed
(`timestamp + make_interval(mins => n) <= as_of`).

### 8. HTTP surface — `api/routes/quality.py`

`GET /quality/{symbol}` returns the same report as JSON. Read-only; a test
asserts the module registers no non-`GET` routes.

---

## Architecture changes

Small and additive. One new package, one new read-only route, one hook.

```
marketdata/          NEW — contracts, sessions, validation, quality, ingest,
                     access, report CLI (stdlib-only)
tests/               NEW — 173 tests, no DB / network / live data
docs/                NEW — architecture, contracts, validation, point-in-time,
                     this summary
api/routes/quality.py  NEW — read-only GET /quality/{symbol}
pytest.ini           NEW — test discovery
requirements-dev.txt NEW — pytest only

api/main.py          +1 router registration
downloader/ohlcv.py  +validation screening in save_candles_to_db();
                     +_interval_label() helper; `interval` parameter added
                     with a default so existing call sites are unaffected
README.md            +Phase 1 section and docs index; project tree updated
```

**Not changed:** the database schema (no migration, no `ALTER TABLE`), the
existing REST endpoints and their response shapes, ingestion behaviour under
default settings, the schedulers, the tick downloader, the dashboard,
`backtest/`, the frontend, `docker-compose.yml`, `Dockerfile`, and
`requirements.txt`.

`.dockerignore` already excludes `tests/`, so image builds are unaffected. The
`marketdata` package needs no new runtime dependency.

---

## Data-quality guarantees

What is now validated, and at what severity:

| Area | Check | Severity |
|---|---|---|
| Schema | Missing/null required field | ERROR |
| Schema | Wrong type (`Decimal` and numeric strings accepted; `bool` never) | ERROR |
| Schema | Unknown column | WARNING |
| Timestamps | Malformed ISO-8601 string | ERROR |
| Timestamps | Timezone-aware value (contract is naive IST) | ERROR |
| Timestamps | Bar off the timeframe grid anchored at 09:15 | ERROR |
| Timestamps | Record outside a live trading session | WARNING |
| Ordering | Duplicate timestamp — bars / ticks | ERROR / WARNING |
| Ordering | Out-of-order timestamp — bars / ticks | ERROR / WARNING |
| OHLC | `high >= max(open, close)`, `low <= min(open, close)`, `high >= low` | ERROR |
| OHLC | Non-positive price | ERROR |
| OHLC (tick) | Snapshot day `high < low` | ERROR |
| OHLC (tick) | `ltp` outside the snapshot day range | WARNING |
| Volume | Negative volume / quantity / OI | ERROR |
| Volume | Cumulative tick volume falling within a session | WARNING |
| Missing data | In-session bar absent | WARNING |

Guarantees about behaviour, not just checks:

* **Nothing is repaired.** No validator, and no code path they feed, mutates,
  coerces, re-orders or interpolates a record. Tests assert input equality
  before and after validation, and object identity of quarantined rows.
* **Closures are never mistaken for defects.** Overnight closes, weekends and
  exchange holidays produce no expectation at all; gaps are looked for only
  inside the observed span, and only ever as warnings.
* **Zero volume is valid.** Cash-index bars structurally report 0.
* **Absence of validation is visible.** A dataset that was not checked is
  reported as absent, not as passing.

---

## Tests

173 tests (172 pass, 1 skipped — an integration check that needs FastAPI and
psycopg2 installed). Runtime ~0.2s. Deterministic: no database, no network, no
live market data, and no dependence on the current date. Tests that care about
trading days inject their own calendar predicate.

| File | Tests | Covers |
|---|---|---|
| `test_contracts.py` | 12 | Contracts match `db/init_schema.sql` column-for-column; required fields; origins; the `close`-means-previous-close trap. |
| `test_sessions.py` | 17 | 375 one-minute and 75 five-minute bars per session; no bar at 15:30; grid anchoring; weekend/holiday exclusion; agreement with `backtest.rv.CANDLES_PER_DAY`. |
| `test_validation_ohlcv.py` | 39 | Valid data passes; every broken invariant (`high < open`, `low > close`, `high < low`, …) fails; duplicates; out-of-order; negative volume; zero volume valid; malformed and tz-aware timestamps; misalignment; **weekend, holiday and overnight closures are not flagged as missing**; in-session gaps are; non-mutation. |
| `test_validation_ticks.py` | 18 | Snapshot contract; duplicates as warnings (and the insert-time data loss they represent); non-decreasing ordering; cumulative volume regression, with the daily reset not flagged; out-of-session detection; non-mutation. |
| `test_ingest.py` | 20 | **`report` mode passes every candle through unchanged, preserving order and object identity** (backward compatibility); `reject` withholds only ERROR rows; warning-only rows still inserted; quarantine JSONL contents; mode resolution and fallback; per-batch logging. |
| `test_quality.py` | 13 | Every metric traced back to the validation result; PASS/WARNING/FAIL; completeness; rendering; JSON serialisability; unvalidated datasets absent. |
| `test_access.py` | 23 | Half-open windows; deterministic ordering; `as_of` SQL and parameters for bars and ticks; timeframe whitelisting; `Decimal` → `float`; access output validates cleanly. |
| `test_report.py` | 16 | Report assembly per timeframe; only requested datasets read; tick truncation note; calendar caveat; CLI parsing, output and exit codes. |
| `test_regression.py` | 15 (1 skipped) | NSE calendar and download slots unchanged; `backtest.rv` numerics unchanged; `download_historical_data` signature unchanged; `save_candles_to_db` still backward compatible and failure-safe; `INTERVALS` unchanged; schema untouched; all pre-existing routers still registered; `/quality` is read-only. |

**Baseline:** there were **no tests before Phase 1** — no `tests/` directory,
no runner configuration, no CI. There were therefore no pre-existing test
failures to inherit.

---

## Known limitations

1. **Tick timestamps mix event time, processing time and timezones.**
   `parse_tick` calls `datetime.fromtimestamp()` with no timezone and falls
   back to `datetime.now()` when the feed omits `last_traded_timestamp`. The
   `tick-downloader` compose service sets no `TZ` (unlike the three scheduler
   services), so in Docker its timestamps are UTC-naive while OHLCV timestamps
   are IST-naive — a 5h30m mismatch when joining ticks to bars. **Phase 1 adds
   detection, not a fix**: `out_of_session` warnings flag it. Fixing it changes
   the meaning of stored data at an undocumented cut-over point and needs a
   human decision plus a backfill plan. See `docs/point-in-time-data.md` §5.1.
2. **The holiday calendar covers 2026 only.** Outside 2026, only weekends are
   known non-trading days, so gap counts are over-stated. The report attaches
   an explicit note; `NSE_HOLIDAYS` extends the calendar without a code change.
3. **Ticks are validated on read, not on ingest.** Adding per-tick validation
   to a hot WebSocket path was not justified here. `parse_tick` already drops
   non-positive LTPs.
4. **Ticks sharing a timestamp are still silently dropped** by
   `UNIQUE(instrument_id, timestamp)`. Phase 1 makes the loss *measurable*; it
   does not change the schema to preserve them.
5. **Five-minute bars have no provenance flag.** A row may come from the API or
   from `backfill_from_one_minute()`; the table cannot tell you which.
6. **No as-of versioning of vendor revisions.** `ON CONFLICT DO NOTHING` keeps
   the first version of a bar and discards any revision. Point-in-time
   correctness holds with respect to bar completion, not vendor restatements.
7. **One session model.** A single 09:15–15:30 regular session is assumed;
   pre-open, closing-auction and any special-session windows are not modelled.
   The model is injectable, so this is extensible without rework.
8. **The quality report reads every row in range.** Fine for days-to-months of
   bars; ticks are capped by `max_ticks` (default 500,000) with an explicit
   truncation note. There is no incremental or cached quality history.
9. **The `/quality` endpoint has no pagination or caching**, and follows the
   existing route convention of returning errors as `status: "error"` with HTTP
   200 rather than raising.
10. **Still no CI.** The suite runs locally with `python -m pytest`; nothing
    enforces it on push. Adding a workflow was out of Phase 1 scope.
11. **No linter is configured in-repo.** Phase 1 files were checked with
    `ruff` and are clean; ~20 pre-existing findings remain in untouched files
    and were deliberately not fixed, to keep the diff reviewable.

---

## Deferred work (later phases, deliberately not built)

* Feature engineering and a feature store
* Forecasting / predictive models
* Regime detection
* Anomaly detection (beyond deterministic contract validation)
* Historical analogue retrieval
* Embeddings, vector databases, RAG
* An AI research agent or any LLM integration
* An evaluation harness and backtest-quality metrics for models
* MLOps: experiment tracking, model registry, serving
* Infrastructure: Kafka, Airflow, Kubernetes, microservices, cloud services
* Bitemporal storage / vendor-revision history
* Table partitioning and retention policies
* Automated remediation or backfill of detected gaps

---

## Recommendation

**Ready for human review.**

The branch is additive: one new stdlib-only package, one read-only endpoint,
one guarded hook in the downloader, tests and documentation. Under default
settings the ingestion path writes exactly what it wrote before — asserted by
`tests/test_ingest.py`, not just claimed. No schema migration is required, no
runtime dependency is added, and no existing behaviour was refactored.

Suggested review order:

1. `docs/data-contract.md` and `docs/point-in-time-data.md` — confirm the
   documented semantics match your intent, especially the tick timestamp and
   `close`-means-previous-close findings.
2. `marketdata/validation.py` — confirm the severity assignments (in
   particular: gaps are warnings, tick duplicates are warnings, bar duplicates
   are errors).
3. `downloader/ohlcv.py` — confirm the ingestion hook is as minimal as claimed.
4. Run `python -m pytest`, then point the CLI at your real database:
   `python -m marketdata.report --instrument "Nifty 50" --from … --to …`.

Two decisions are yours before Phase 2:

* **The tick-downloader timezone (limitation 1).** Whether to set
  `TZ: Asia/Kolkata` on that service, and what to do about the historical rows
  already written under the other convention. Any future model that joins ticks
  to bars is blocked on this.
* **Whether ingestion should move to `reject` mode.** Running `report` mode for
  a while first and reading the logs is the low-risk path.
