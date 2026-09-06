-- =============================================================================
-- Database Bootstrapping Script
-- =============================================================================
-- Run this on a fresh PostgreSQL instance so the API and downloaders have the
-- schema they expect.  Every statement is idempotent (IF NOT EXISTS).
--
-- Usage:
--   psql "$DATABASE_URL" -f db/init_schema.sql
-- =============================================================================

-- ── 1. instruments ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS instruments (
    id                  SERIAL PRIMARY KEY,
    symbol              VARCHAR(100) NOT NULL,
    token               VARCHAR(50) NOT NULL,
    name                VARCHAR(255),
    exchange            VARCHAR(20) NOT NULL,
    instrument_type     VARCHAR(20) NOT NULL,
    expiry              DATE,
    strike              DECIMAL(12, 2),
    lot_size            INTEGER,
    is_active           BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(token, exchange)
);

CREATE INDEX IF NOT EXISTS idx_instruments_type
ON instruments(instrument_type);

CREATE INDEX IF NOT EXISTS idx_instruments_exchange
ON instruments(exchange);


-- ── 2. ohlcv_1min ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ohlcv_1min (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       INTEGER NOT NULL REFERENCES instruments(id),
    timestamp           TIMESTAMP NOT NULL,
    open                DECIMAL(12, 4) NOT NULL,
    high                DECIMAL(12, 4) NOT NULL,
    low                 DECIMAL(12, 4) NOT NULL,
    close               DECIMAL(12, 4) NOT NULL,
    volume              BIGINT NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(instrument_id, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_1min_instrument_timestamp
ON ohlcv_1min(instrument_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_ohlcv_1min_timestamp
ON ohlcv_1min(timestamp DESC);


-- ── 3. ohlcv_5min ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ohlcv_5min (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       INTEGER NOT NULL REFERENCES instruments(id),
    timestamp           TIMESTAMP NOT NULL,
    open                DECIMAL(12, 4) NOT NULL,
    high                DECIMAL(12, 4) NOT NULL,
    low                 DECIMAL(12, 4) NOT NULL,
    close               DECIMAL(12, 4) NOT NULL,
    volume              BIGINT NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(instrument_id, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_5min_instrument_timestamp
ON ohlcv_5min(instrument_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_ohlcv_5min_timestamp
ON ohlcv_5min(timestamp DESC);


-- ── 4. tick_data ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tick_data (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       INTEGER NOT NULL REFERENCES instruments(id),
    timestamp           TIMESTAMP NOT NULL,
    -- Feed's per-packet ordering token. Part of the uniqueness key because
    -- exchange timestamps are only second-resolution: without it, two genuine
    -- snapshots in the same second collide and one is lost. 0 marks rows
    -- collected before db/migrations/001_tick_sequence_number.sql.
    sequence_number     BIGINT NOT NULL DEFAULT 0,
    ltp                 DECIMAL(12, 2) NOT NULL,
    ltq                 INTEGER,
    open                DECIMAL(12, 2),
    high                DECIMAL(12, 2),
    low                 DECIMAL(12, 2),
    close               DECIMAL(12, 2),
    avg_trade_price     DECIMAL(12, 2),
    volume              BIGINT,
    total_buy_qty       BIGINT,
    total_sell_qty      BIGINT,
    open_interest       BIGINT,
    best_5_buy          JSONB,
    best_5_sell         JSONB,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(instrument_id, timestamp, sequence_number)
);

CREATE INDEX IF NOT EXISTS idx_tick_data_instrument_timestamp
ON tick_data(instrument_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_tick_data_timestamp
ON tick_data(timestamp DESC);


-- ── 5. download_log ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS download_log (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       INTEGER NOT NULL REFERENCES instruments(id),
    last_downloaded_at  TIMESTAMP,
    last_run_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status              VARCHAR(20) NOT NULL,
    candles_inserted    INTEGER DEFAULT 0,
    candles_skipped     INTEGER DEFAULT 0,
    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_download_log_instrument
ON download_log(instrument_id);

CREATE INDEX IF NOT EXISTS idx_download_log_status
ON download_log(status);

CREATE INDEX IF NOT EXISTS idx_download_log_last_run
ON download_log(last_run_at DESC);


-- ── 6. strategies ────────────────────────────────────────────────────────────
-- Registry of strategy definitions. `params` holds the JSON config a given
-- run/session used (thresholds, windows, etc.) so results stay reproducible
-- even as code defaults change over time.

CREATE TABLE IF NOT EXISTS strategies (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(100) NOT NULL UNIQUE,
    description         TEXT,
    params              JSONB NOT NULL DEFAULT '{}',
    is_active           BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


-- ── 7. backtest_runs ─────────────────────────────────────────────────────────
-- One row per backtest execution — summary stats for quick comparison across
-- strategies/parameter sets without recomputing from raw trades each time.

CREATE TABLE IF NOT EXISTS backtest_runs (
    id                  SERIAL PRIMARY KEY,
    strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
    params              JSONB NOT NULL DEFAULT '{}',
    from_date           TIMESTAMP NOT NULL,
    to_date             TIMESTAMP NOT NULL,
    starting_capital    DECIMAL(14, 2) NOT NULL,
    ending_capital      DECIMAL(14, 2),
    total_trades        INTEGER DEFAULT 0,
    winning_trades      INTEGER DEFAULT 0,
    losing_trades       INTEGER DEFAULT 0,
    total_pnl           DECIMAL(14, 2),
    max_drawdown_pct    DECIMAL(8, 4),
    sharpe_ratio        DECIMAL(8, 4),
    win_rate_pct        DECIMAL(6, 2),
    status              VARCHAR(20) NOT NULL DEFAULT 'running',
    error_message       TEXT,
    started_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at        TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy
ON backtest_runs(strategy_id);


-- ── 8. trades ────────────────────────────────────────────────────────────────
-- Individual simulated trades. Shared by backtests (backtest_run_id set,
-- is_paper=FALSE) and live paper trading (backtest_run_id NULL, is_paper=
-- TRUE) so both paths reuse one fill/PnL representation and one set of
-- reporting queries.

CREATE TABLE IF NOT EXISTS trades (
    id                  BIGSERIAL PRIMARY KEY,
    backtest_run_id     INTEGER REFERENCES backtest_runs(id) ON DELETE CASCADE,
    strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
    is_paper            BOOLEAN NOT NULL DEFAULT FALSE,
    instrument_id        INTEGER NOT NULL REFERENCES instruments(id),
    side                VARCHAR(10) NOT NULL,   -- 'LONG' or 'SHORT'
    signal_reason       VARCHAR(255),
    entry_time          TIMESTAMP NOT NULL,
    entry_price         DECIMAL(12, 4) NOT NULL,
    quantity            INTEGER NOT NULL,
    exit_time           TIMESTAMP,
    exit_price          DECIMAL(12, 4),
    exit_reason         VARCHAR(50),            -- 'signal_exit', 'stop_loss', 'take_profit', 'expiry', 'eod', 'manual'
    pnl                 DECIMAL(14, 2),
    pnl_pct             DECIMAL(8, 4),
    status              VARCHAR(20) NOT NULL DEFAULT 'open',  -- 'open' or 'closed'
    metadata            JSONB DEFAULT '{}',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_backtest_run
ON trades(backtest_run_id);

CREATE INDEX IF NOT EXISTS idx_trades_strategy_paper
ON trades(strategy_id, is_paper);

CREATE INDEX IF NOT EXISTS idx_trades_status
ON trades(status) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_trades_instrument
ON trades(instrument_id);


-- ── 9. equity_curve ──────────────────────────────────────────────────────────
-- Periodic portfolio-value snapshots for both backtests and paper trading,
-- so charts don't need to replay every trade to draw an equity line.

CREATE TABLE IF NOT EXISTS equity_curve (
    id                  BIGSERIAL PRIMARY KEY,
    backtest_run_id     INTEGER REFERENCES backtest_runs(id) ON DELETE CASCADE,
    strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
    is_paper            BOOLEAN NOT NULL DEFAULT FALSE,
    timestamp           TIMESTAMP NOT NULL,
    equity              DECIMAL(14, 2) NOT NULL,
    cash                DECIMAL(14, 2) NOT NULL,
    open_positions_value DECIMAL(14, 2) DEFAULT 0,
    drawdown_pct        DECIMAL(8, 4) DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_equity_curve_run
ON equity_curve(backtest_run_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_equity_curve_strategy_paper
ON equity_curve(strategy_id, is_paper, timestamp DESC);


-- ── 10. signals ──────────────────────────────────────────────────────────────
-- Log of every signal a live strategy evaluation produced, whether or not it
-- resulted in a trade (e.g. filtered by an existing open position) — useful
-- for auditing "why didn't it trade" and for a live signal feed in the UI.

CREATE TABLE IF NOT EXISTS signals (
    id                  BIGSERIAL PRIMARY KEY,
    strategy_id         INTEGER NOT NULL REFERENCES strategies(id),
    instrument_id       INTEGER REFERENCES instruments(id),
    timestamp           TIMESTAMP NOT NULL,
    signal_type         VARCHAR(20) NOT NULL,   -- 'entry_long', 'entry_short', 'exit'
    reason              VARCHAR(255),
    metrics             JSONB DEFAULT '{}',      -- e.g. {"rv_short": 0.18, "rv_long": 0.12, "ratio": 1.5}
    acted_on            BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_signals_strategy_time
ON signals(strategy_id, timestamp DESC);


-- ── 11. Notification trigger (from setup_trigger.sql) ─────────────────────────

CREATE OR REPLACE FUNCTION notify_new_tick()
RETURNS TRIGGER AS $$
BEGIN
  PERFORM pg_notify('new_tick', row_to_json(NEW)::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tick_data_notify_trigger ON tick_data;

CREATE TRIGGER tick_data_notify_trigger
AFTER INSERT ON tick_data
FOR EACH ROW
EXECUTE FUNCTION notify_new_tick();
