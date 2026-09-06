-- =============================================================================
-- Migration 001 — preserve every tick snapshot
-- =============================================================================
-- Problem
--   tick_data was keyed UNIQUE(instrument_id, timestamp). Angel One publishes
--   exchange timestamps at ONE-SECOND resolution, so two genuine snapshots for
--   the same instrument inside the same second collided and the second one was
--   silently discarded by ON CONFLICT DO NOTHING.
--
--   This was masked before the timestamp fix: 98.7% of rows carried
--   microsecond-precision datetime.now() values, which almost never collided.
--   Now that `timestamp` holds a real exchange clock, collisions become common.
--
-- Fix
--   Include the feed's own sequence_number in the uniqueness key. It is a
--   monotonic per-packet token the SDK already parses (smartWebSocketV2.py
--   line 352) and the collector previously discarded.
--
-- Legacy rows
--   Pre-migration rows have no sequence number and are backfilled with 0.
--   0 is never sent for a real packet, so it doubles as a "collected before
--   migration 001" marker. NOT NULL is used deliberately: with NULLs, Postgres
--   treats each NULL as distinct and the unique constraint would stop
--   protecting legacy rows entirely.
--
-- Safety
--   Idempotent. Re-running is a no-op. Run with the collector stopped.
--
-- Usage
--   psql "$DATABASE_URL" -f db/migrations/001_tick_sequence_number.sql
-- =============================================================================

BEGIN;

-- ── 1. Add the column ────────────────────────────────────────────────────────
-- BIGINT matches the SDK's signed 64-bit ("q") unpack. DEFAULT 0 backfills
-- every existing row in place.

ALTER TABLE tick_data
    ADD COLUMN IF NOT EXISTS sequence_number BIGINT NOT NULL DEFAULT 0;


-- ── 2. Swap the uniqueness key ───────────────────────────────────────────────
-- Old:  UNIQUE(instrument_id, timestamp)              → drops same-second ticks
-- New:  UNIQUE(instrument_id, timestamp, sequence_number)
--
-- Dropping first would briefly leave the table unprotected, but this runs
-- inside a transaction with the collector stopped, so there is no window.

ALTER TABLE tick_data
    DROP CONSTRAINT IF EXISTS tick_data_instrument_id_timestamp_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'tick_data'::regclass
          AND conname  = 'tick_data_instrument_timestamp_seq_key'
    ) THEN
        ALTER TABLE tick_data
            ADD CONSTRAINT tick_data_instrument_timestamp_seq_key
            UNIQUE (instrument_id, "timestamp", sequence_number);
    END IF;
END $$;


-- ── 3. Keep the read paths fast ──────────────────────────────────────────────
-- The existing (instrument_id, timestamp DESC) and (timestamp DESC) indexes are
-- unchanged and still serve every current query — the dashboard, the /ticks API
-- and marketdata.access all filter on instrument and time, never on sequence.

COMMIT;


-- ── Verification (run manually) ──────────────────────────────────────────────
-- SELECT conname, pg_get_constraintdef(oid)
-- FROM pg_constraint WHERE conrelid = 'tick_data'::regclass AND contype = 'u';
--
-- SELECT count(*) FILTER (WHERE sequence_number = 0) AS legacy,
--        count(*) FILTER (WHERE sequence_number > 0) AS post_migration
-- FROM tick_data;
