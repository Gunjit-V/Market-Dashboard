# CLAUDE.md

Operational notes for working in this repo. The README covers what the project
*is* and how to install it; this file covers the things that are easy to get
wrong and aren't visible from the code alone.

## Environment

- Windows host, PowerShell primary. Postgres runs **natively on Windows**, not
  in Docker — `docker-compose.yml` deliberately has no `postgres-db` service and
  the containers reach the host DB via `host.docker.internal`.
- Credentials live in `.env` (gitignored). Never print `DB_PASSWORD`,
  `ANGEL_API_KEY`, `ANGEL_PASSWORD`, or `ANGEL_TOTP_TOKEN` — mask them if a
  command would echo the file.

## Querying the database

Pull the password from `.env` rather than asking for it:

```bash
export PGPASSWORD=$(grep '^DB_PASSWORD=' .env | cut -d= -f2-)
psql -h localhost -U postgres -d marketdb -c "SELECT ..."
```

**Join key:** `ohlcv_1min.instrument_id` / `ohlcv_5min.instrument_id` /
`tick_data.instrument_id` reference **`instruments.id`**, not
`instruments.token` and not a column named `instrument_id` — `instruments` has
no such column. Joining on the wrong one errors out immediately.

`timestamp` columns are `timestamp without time zone` holding **IST**. Tick
timestamps are converted through an explicit IST offset in code; the containers
also set `TZ=Asia/Kolkata`, which is belt-and-braces rather than load-bearing.

## Instrument coverage — 49 vs 29 is correct

Two different subscription windows, both intentional:

- **OHLCV sweep** covers all **49** active instruments — 4 spot indices
  (Nifty 50, Nifty Bank, SENSEX, BANKEX), 3 Nifty futures, and 42 options
  (ATM ±10 strikes, CE+PE at 50-point steps).
- **Tick downloader** subscribes to **29** — ATM ±5 strikes plus indices and
  futures. See the `num_strikes=5` argument in `downloader/tick_downloader.py`.

A tick/OHLCV instrument-count mismatch is therefore expected, not a fault.

The option chain **re-centres on ATM every morning at 08:45**, so the active
strike set rotates with spot. Cross-day analysis pinned to a fixed strike will
hit gaps by design.

## Reading session health

- Session is **09:15–15:30 IST**. A full day is ~375 one-minute candles.
- Low candle counts on deep-OTM strikes are **genuine no-trade minutes**, not
  lost data. Judge health by `download_log` status, not by candle count alone:
  `SELECT status, count(*) FROM download_log WHERE last_run_at::date = CURRENT_DATE GROUP BY status;`
  `no_data` is a normal outcome; `success` with 0 failures is the healthy shape.
- `min(timestamp)` on a day is **not** the session start. Pre-open prints appear
  at 09:10 (SENSEX/BANKEX, volume 0, open == close) and 09:14 (a Nifty index
  print). Filter to `>= '09:15'` when you mean the open.

## Reading container logs

`docker logs` **persists across restarts**, so a bare
`docker logs <c> | grep -c ERROR` counts history from previous days and badly
overstates today. Always scope it:

```bash
docker logs --since <container-StartedAt> <container> 2>&1 | grep -ciE "traceback|exception|ERROR"
docker inspect <container> --format '{{.State.StartedAt}} restarts={{.RestartCount}}'
```

`RestartCount` is the fastest signal that something is wrong — the schedulers
normally sit at 0.

## Known fragility: the tick downloader

`market-tick-downloader` is the least reliable service and has two distinct
failure modes, both observed on 2026-09-11 (12 restarts, 2h34m of ticks lost):

1. The overnight websocket reconnect loop can hit
   `Max retry attempts reached`, die silently, and **not recover by the open** —
   producing no log output at all until something restarts it.
2. On restart, `generateSession` retries roughly 2s apart with **no backoff**,
   which trips Angel One's login rate limit
   (`DataException: Access denied because of exceeding access rate`) exactly
   when it's trying to recover.

Ticks lost this way are **unrecoverable** — the feed is websocket-only. OHLCV is
REST-backfilled and does recover the same window, so a tick gap usually leaves
candle data intact. Check both before concluding a day is lost.

Contrast with the OHLCV path, which already does exponential backoff
(2→4→8→16s) and survives the same throttling without data loss.

## Angel One rate limiting

Throttling of the OHLCV sweep is routine and mostly cosmetic: retries absorb it,
and sweeps still complete with 0 failures. It costs *latency* — sweeps intended
to take ~50s can take ~6 minutes, so 1m candles land in bursts and runs collide
(`Skipping 1m run: previous run is still active`). That matters only if you need
candles in near-real-time; end-of-day backtesting is unaffected.

## Services

Compose defines 7 services. In practice only the 4 schedulers run:

- Running: `market-tick-downloader`, `market-ohlcv-scheduler`,
  `market-instrument-sync-scheduler` (08:45 daily),
  `market-retention-scheduler` (16:30 daily, keeps 7 days)
- Usually stopped: `api-server` (:8000), `dashboard-server` (:8050),
  `react-frontend` (:5173) — these are the read path, not ingestion.
- `my-redis` is a **stale container unrelated to this project**. Nothing in the
  codebase references Redis. Don't treat it as a broken dependency.

## Tests

`pytest` from the repo root. The suite needs **no database and no network** —
`marketdata/` and its tests deliberately depend on nothing beyond the stdlib
(`pytest` itself is the only dev dependency). If a test wants a live DB or an
Angel One session, that's a bug in the test.

`research/` (the Phase 3A evaluation harness) keeps that rule and adds one: its
metrics are stdlib-only, so every number in `tests/test_research_metrics.py` is
checked against a value computed by hand rather than against the code's own
output. Its reports are deterministic — no clock, no unseeded randomness — so
two runs over the same panel must agree exactly.

## Docs

`docs/` carries the design record, numbered in reading order:
`01-architecture.md`, `02-market-data.md` (contracts + point-in-time +
validation), `03-features.md` (market state + feature contract + datasets),
`04-project-history.md` (phases and open limitations), `05-phase-2-brief.md`
and `07-phase-3-brief.md` (archived specs), `06-feature-reference.md` (every
feature and label individually), `08-evaluation.md` (the Phase 3A evaluation
harness and the rules it enforces). `docs/README.md` is the index. Check there
before inferring intent from code, especially around point-in-time semantics
and feature contracts.

The operational notes above are duplicated, in fuller prose, in
`docs/01-architecture.md`. When one changes, change the other.
