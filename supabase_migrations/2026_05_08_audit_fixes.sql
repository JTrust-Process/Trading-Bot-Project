-- ============================================================================
-- 2026-05-08  audit fixes
--
-- DIALECT: PostgreSQL (Supabase). Run this in the Supabase SQL editor.
-- Some local SQL clients (dbtools / VS Code SQLTools) parse against ANSI SQL
-- or T-SQL by default and will flag PostgreSQL-only constructs like
-- `ALTER COLUMN ... SET DEFAULT` and `CREATE INDEX ... WHERE ...`. Those
-- warnings can be ignored — Supabase runs PostgreSQL 15+, which accepts
-- everything below.
--
-- Every statement is idempotent. Safe to re-run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. ID defaults on bot_runs / bot_events / bot_errors
--    Code already sends UUIDs, but make the schema robust to future callers.
-- ---------------------------------------------------------------------------
ALTER TABLE bot_runs ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE bot_events ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE bot_errors ALTER COLUMN id SET DEFAULT gen_random_uuid();

-- ---------------------------------------------------------------------------
-- 2. Partial unique index on positions(symbol) where status = 'open'
--    Prevents duplicate "open" rows from racy upserts.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS positions_open_uniq
  ON positions (symbol)
  WHERE status = 'open';

-- ---------------------------------------------------------------------------
-- 3. New table: bot_state
--    Mirrors the ephemeral local state.json so per-symbol cooldowns,
--    drawdown peaks, and day-start values survive across GHA runs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot_state (
  symbol     text PRIMARY KEY,
  state      jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE bot_state ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'bot_state'
      AND policyname = 'Allow public read'
  ) THEN
    CREATE POLICY "Allow public read" ON bot_state FOR SELECT USING (true);
  END IF;
END $$;

-- Optional: dashboard live updates (Supabase Realtime)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'bot_state'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE bot_state;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. (Optional) helpful index for the trades-per-day count query
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS bot_logs_event_timestamp_idx
  ON bot_logs (event, timestamp_utc);
