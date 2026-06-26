-- Migration: add the 4 enrichment columns the scrapers parse, plus performance
-- indexes. PREPARED FOR REVIEW — review before running against prod (ngm_v1).
--
-- WHY: the enrichment spiders parse verdict_type / case_subject / hearing_count
-- and stamp enriched_at, but those columns never existed, so every value was
-- silently discarded (confirmed against the live schema). The model now declares
-- them; NGM uses SQLAlchemy create_all (no migration framework) which only
-- creates missing TABLES, never columns — so this ALTER is required on prod
-- BEFORE deploying the new enrichment code (otherwise the UPDATE references
-- non-existent columns and fails).
--
-- Run via the CNPG primary, e.g.:
--   kubectl exec -n db pg-1 -c postgres -- psql -d ngm_v1 -f - < this_file.sql
-- Nullable ADD COLUMN with no default is instant in Postgres (no table rewrite).

BEGIN;

ALTER TABLE court_cases
    ADD COLUMN IF NOT EXISTS verdict_type   varchar(100),
    ADD COLUMN IF NOT EXISTS case_subject   text,
    ADD COLUMN IF NOT EXISTS hearing_count  varchar(20),
    ADD COLUMN IF NOT EXISTS enriched_at    timestamp;

CREATE INDEX IF NOT EXISTS ix_court_cases_enriched_at
    ON court_cases (enriched_at);

COMMIT;

-- Performance indexes (CONCURRENTLY cannot run inside a transaction — run each
-- on its own, after the COMMIT above):
--   * speeds up the court-orders case-list query (joins 5.1M hearings) and the
--     supreme judge_names backfill join.
--   * speeds up entity lookups / the enriched-but-no-entities audit.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cch_case_court
    ON court_case_hearings (case_number, court_identifier);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cce_court_case
    ON court_case_entities (court_identifier, case_number);
