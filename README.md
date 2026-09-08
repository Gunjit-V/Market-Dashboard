# Indian Stock Market Data Platform

A comprehensive algorithmic trading and market data collection platform designed for the Indian stock market. The project leverages Angel One's SmartAPI to download historical OHLCV data, real-time ticks, and instruments, and provides a powerful FastAPI backend along with a live WebSocket-powered dashboard to visualize the data.

## 🚀 Features

*   **Market Data Downloader (`downloader/`)**: Robust scripts to fetch FO instruments, daily OHLCV data, and real-time market ticks using the Angel One SmartAPI.
*   **FastAPI Backend (`api/`)**: A high-performance RESTful API serving market data (Instruments, OHLCV, Ticks, Volatility metrics).
*   **Live Dashboard (`dashboard/`)**: A WebSocket-enabled live dashboard that processes and visualizes real-time market data without expensive polling mechanisms.
*   **Frontend Client (`frontend/`)**: React-based frontend client for rich data exploration and visualizations.
*   **Database (`db/`)**: PostgreSQL integration to persist and manage vast amounts of tick and OHLCV data efficiently.
*   **Scheduler (`scheduler/`)**: Automated job scheduling for routine data extraction and maintenance tasks.

## 🛠️ Tech Stack

*   **Backend & API**: Python 3.x, FastAPI, Uvicorn
*   **Market Data Source**: Angel One SmartAPI (`smartapi-python`)
*   **Database**: PostgreSQL (`psycopg2`)
*   **Real-time Communication**: WebSockets (`websocket-client`)
*   **Frontend**: React.js (in `frontend/`), Vanilla HTML/JS (in `dashboard/`)
*   **Data Processing**: Pandas, NumPy

## 📁 Project Structure

```bash
.
├── api/             # FastAPI application and route definitions
├── backtest/        # Simulation engine and strategies
├── dashboard/       # Simple WebSocket-powered live dashboard client & server
├── data/            # Local data storage and exports
├── db/              # Database models, schemas, and connection utilities
├── docs/            # Architecture, data contracts, validation, point-in-time semantics
├── downloader/      # Scripts for tick collection, OHLCV fetching, and instruments loading
├── frontend/        # React frontend application
├── logs/            # Application and script log files
├── marketdata/      # Data contracts, validation, quality reporting, dataset access
├── scheduler/       # Cron jobs and automated pipeline scripts
├── tests/           # Pytest suite (no database or network required)
└── .env             # Environment variables (API keys, DB credentials)
```

## ⚙️ Installation & Setup

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/yourusername/your-repo-name.git
    cd your-repo-name
    ```

2.  **Set Up the Python Environment**
    It is recommended to use a virtual environment.
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows use: venv\Scripts\activate
    pip install -r requirements.txt
    ```

3.  **Environment Variables**
    Create a `.env` file in the root directory and add your credentials:
    ```env
    # SmartAPI Credentials
    ANGEL_API_KEY=your_api_key
    ANGEL_CLIENT_ID=your_client_id
    ANGEL_PASSWORD=your_password
    ANGEL_TOTP_SECRET=your_totp_secret
    
    # Database Settings
    DATABASE_URL=postgresql://user:password@localhost:5432/dbname
    ```

4.  **Database Migration (Optional/If applicable)**
    Run the necessary SQL scripts within `db/` to initialize tables.
    ```bash
    # e.g., psql -d dbname -f db/schema.sql
    ```

## 🎯 Running the Application

### 1. Market Data API
To start the FastAPI backend server:
```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```
*   **Swagger Documentation**: `http://localhost:8000/docs`
*   **Health Check**: `http://localhost:8000/health/`

### 2. Live WebSocket Dashboard
To run the live dashboard server:
```bash
python dashboard/server.py
```
Then open `dashboard/live_dashboard.html` in your browser.

### 3. Real-time Ticks Downloader
To start collecting ticks from SmartAPI:
```bash
python downloader/tick_downloader.py
```

### 4. Automated 1-minute and 5-minute candles
The Docker stack includes an `ohlcv-scheduler` service. During market hours
(09:15–15:30 IST) it submits a 1-minute download on every trading minute and
a 5-minute download on each five-minute boundary, skips configured NSE
holidays, and resumes from the latest stored candle. By default it updates
whichever active instruments match `NIFTY,BANKNIFTY,SENSEX,BANKEX` across
`AMXIDX,FUTIDX,OPTIDX` — in practice this is the four tracked indices, the
Nifty future, and whatever Nifty options `instrument-sync-scheduler` (below)
has activated for the day.

Configure its scope in `.env` as needed:
```env
OHLCV_INSTRUMENT_TYPES=AMXIDX,FUTIDX,OPTIDX
OHLCV_NAMES=NIFTY,BANKNIFTY,SENSEX,BANKEX
OHLCV_BAR_DELAY_SECONDS=10

# Comma-separated ISO dates layered on top of the built-in NSE calendar
# (scheduler/nse_calendar.py).
NSE_HOLIDAYS=2027-01-26
NSE_SPECIAL_TRADING_DAYS=2027-02-01
```

To run it outside Docker:
```bash
python -m scheduler.ohlcv_scheduler
```

### 5. Automated daily instrument sync
The Docker stack includes an `instrument-sync-scheduler` service. Once per
trading day, at or after `INSTRUMENT_SYNC_TIME` (default `08:45` IST, before
market open), it runs `downloader/sync_instruments.py`: re-downloads the full
instrument master, upserts it, deactivates expired contracts, and activates
the current-month instruments. This keeps futures rollover and newly listed
option series up to date without manual intervention.

It also recomputes the Nifty ATM strike from a live spot quote and activates
Nifty options within `OPTIONS_STRIKE_RANGE` strikes of ATM (default 10, i.e.
21 strikes × CE/PE = 42 contracts) at the nearest expiry — since the ATM
strike moves daily, this activation set is fully recomputed (previously
active options are deactivated first) rather than accumulated. The
`ohlcv-scheduler` then downloads 1m/5m candles for whichever options are
currently active, alongside the Nifty future and the tracked indices.

Configure it in `.env` as needed:
```env
INSTRUMENT_SYNC_TIME=08:45
INSTRUMENT_SYNC_SAVE_CSV=true
OPTIONS_STRIKE_RANGE=10
```

To run it outside Docker:
```bash
python -m scheduler.instrument_sync_scheduler
```

### 6. Strategy backtesting
The `backtest/` package holds the simulation engine (`backtest/engine.py`),
realized-volatility estimators (`backtest/rv.py`, mirroring
`frontend/src/utils/rv.ts`), and two strategies:

*   **`rv_breakout`** — trades Nifty 50 directionally when short-window
    realized vol expands past a long-window baseline (a volatility regime
    shift), exiting once the regime normalizes.
*   **`vrp_reversion`** — sells/buys Nifty index option premium based on the
    volatility risk premium (implied vol computed via
    `api/utils/iv.py`'s Black-Scholes solver, minus the underlying's
    realized vol), one strategy instance per option symbol.

Both share one `Simulator`/fill/PnL model, backed by the `strategies`,
`backtest_runs`, `trades`, and `equity_curve` tables (see
`db/init_schema.sql`). Backtests replay history (`backtest/runner.py`).

Run a backtest via the API:
```bash
curl -X POST http://localhost:8000/strategies/backtests/run \
  -H "Content-Type: application/json" \
  -d '{"strategy":"rv_breakout","symbol":"Nifty 50"}'
```

Results are visible in the frontend under **Strategies**.

## 🩺 Scheduler health & API access

`GET /health/schedulers` reports a last-seen timestamp and `ok`/`stale`
status for each background service (`tick_downloader`, `ohlcv_scheduler`,
`instrument_sync_scheduler`), derived from the
latest row each one writes (`tick_data`, `download_log`, `instruments`).
A service is only flagged `stale` during market hours, so
after-hours quiet is not treated as a failure. The Dashboard page polls this
every 60s and shows a status dot per service — this is what would have
surfaced the tick-downloader outage (dead silently for ~5 months, see git
history) immediately instead of by accident.

Mutating endpoints (`POST /download/trigger`, `POST /strategies/backtests/run`)
are gated behind an optional `X-API-Key` header, checked against `API_KEY` in
`.env`. Leaving `API_KEY` unset disables the check (local dev default); set
it before exposing the API beyond localhost, and set `VITE_API_KEY` in the
frontend's environment to match. CORS origins default to local dev ports and
can be restricted via `CORS_ALLOWED_ORIGINS` (comma-separated) once deployed.

## 📐 Data contracts, validation & quality reporting

The `marketdata/` package holds the market-data foundation: the canonical data
contracts, a non-destructive validation layer, data-quality reporting and a
point-in-time-aware read interface. It depends on nothing outside the Python
standard library, so it imports and tests without a database or the Angel One
SDK.

Validation **never modifies market data**. The default behaviour is
detect → report; rejecting or quarantining records is opt-in.

Print a data-quality report for an instrument:
```bash
python -m marketdata.report --instrument "Nifty 50" \
    --from 2026-01-01 --to 2026-09-01 --timeframes 1m,5m
```
Exit code is 0 (PASS), 1 (WARNING), 2 (FAIL) or 3 (usage error). The same
report is available read-only over HTTP at `GET /quality/{symbol}`.

Read history with the point-in-time cut-off enforced:
```python
from marketdata.access import get_market_data

bars = get_market_data(conn, "Nifty 50", start, end, "5m", as_of=decision_time)
```

Optional ingestion settings (defaults preserve the pre-existing behaviour
exactly — every candle is still inserted, unchanged):
```env
MARKETDATA_VALIDATION_MODE=report        # or 'reject' to withhold invalid candles
MARKETDATA_QUARANTINE_FILE=logs/quarantine.jsonl
```

Run the tests:
```bash
pip install -r requirements-dev.txt
python -m pytest
```

### Documentation

*   [`docs/current-architecture.md`](docs/current-architecture.md) — the system as it exists today.
*   [`docs/data-contract.md`](docs/data-contract.md) — canonical schemas for ticks, 1m and 5m OHLCV.
*   [`docs/validation.md`](docs/validation.md) — validation rules, quality reporting, how to run both.
*   [`docs/point-in-time-data.md`](docs/point-in-time-data.md) — temporal semantics and leakage rules for future ML work.
*   [`docs/phase-1-summary.md`](docs/phase-1-summary.md) — what Phase 1 changed, and its known limitations.
*   [`docs/market-state.md`](docs/market-state.md) — the market state at a decision time, and its feature-status model.
*   [`docs/feature-contract.md`](docs/feature-contract.md) — what a feature must declare, and how a feature set is versioned.
*   [`docs/phase-2a-summary.md`](docs/phase-2a-summary.md) — what Phase 2A changed, and its known limitations.

## 🗄️ Tick data retention

Tick volume grows fast — roughly half a million rows per session, more now that
same-second snapshots are preserved. The `retention-scheduler` service archives
ticks older than a retention window to compressed Parquet, verifies the archive,
then deletes them from PostgreSQL.

It runs once daily at `RETENTION_RUN_TIME` (default 16:30 IST, after the close
so it never competes with the collector). Parquet compresses this data roughly
13x, so an archived day costs a fraction of what it does in the database.

```env
TICK_RETENTION_DAYS=7          # days to keep in PostgreSQL
RETENTION_RUN_TIME=16:30       # IST
RETENTION_ARCHIVE_DIR=         # defaults to data/archive/
RETENTION_VACUUM=false         # VACUUM FULL after purge (exclusive lock)
```

Archives land in `data/archive/` (bind-mounted, gitignored) as
`tick_data_before-<date>_<stamp>.parquet`. Read one back with:

```python
import pyarrow.parquet as pq
df = pq.read_table("data/archive/tick_data_before-2026-09-03_....parquet").to_pandas()
```

Deletion only happens after the archive is written **and** its row count is
verified, so a failed archive leaves the table untouched.

To archive/purge manually instead:

```bash
python scripts/purge_tick_data.py --keep-from 2026-09-01   # dry run
python scripts/purge_tick_data.py --keep-from 2026-09-01 --execute
```

## 🐳 Running with Docker

You can easily run the entire stack (Database, API, Dashboard, Tick Downloader, and Frontend) using Docker Compose.

1.  Make sure you have [Docker](https://www.docker.com/products/docker-desktop) installed.
2.  Ensure your `.env` file is set up with your SmartAPI and Postgres credentials.
3.  Run the following command from the root of the project:
    ```bash
    docker-compose up -d --build
    ```

This will expose:
*   **FastAPI Backend**: `http://localhost:8000`
*   **Frontend Client**: `http://localhost:5173`
*   **PostgreSQL**: `localhost:5432`

## 🤝 Contributing

Contributions are always welcome. Please feel free to open an issue or submit a Pull Request.

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
