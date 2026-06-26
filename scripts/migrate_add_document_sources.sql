-- Migration: add court_cases.document_sources (jsonb).
--
-- WHY: court orders are stored today only as bare R2 path strings in
-- extra_data["court_orders"]. We now also surface each case's orders as a list
-- of DocumentSource structs (Jawafdehi API shape: links[{link,role}],
-- document_id, source_type) on a dedicated column, written by
-- SupremeCourtOrdersPipeline and consumable 1:1 by a future API import.
-- extra_data["court_orders"] stays the scrape-state marker the selection query
-- keys on — this column is purely additive (presentation surface).
--
-- NGM uses SQLAlchemy create_all (no migration framework), which only creates
-- missing TABLES, never columns — so this ALTER must run on prod (ngm_v1) BEFORE
-- the pipeline that writes document_sources is deployed.
--
-- Run via the CNPG primary, e.g.:
--   kubectl exec -n db <primary> -c postgres -- psql -d ngm_v1 -f - < this_file.sql
-- Nullable ADD COLUMN with no default is instant in Postgres (no table rewrite).

ALTER TABLE court_cases ADD COLUMN IF NOT EXISTS document_sources jsonb;
