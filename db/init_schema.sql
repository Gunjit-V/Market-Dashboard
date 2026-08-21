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
    UNIQUE(instrument_id, timestamp)
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


-- ── 6. Notification trigger (from setup_trigger.sql) ─────────────────────────

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
