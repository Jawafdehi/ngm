BEGIN;

-- PRE-FLIGHT CHECK 1: Confirm degenerate rows (names starting with /)
-- These 24 rows will be handled separately below
SELECT id, case_number, side, name
FROM court_case_entities
WHERE court_identifier = 'supreme'
AND name LIKE '%/%'
AND trim(split_part(name, '/', 1)) = '';

-- PRE-FLIGHT CHECK 2: Confirm any rows have non-null nes_id
-- If count > 0, those nes_ids will be cleared during backfill
SELECT COUNT(*) AS rows_with_nes_id
FROM court_case_entities
WHERE court_identifier = 'supreme'
AND name LIKE '%/%'
AND nes_id IS NOT NULL;

-- STEP 1: Preview what will be fixed (normal rows only)
SELECT id, case_number, side, name
FROM court_case_entities
WHERE court_identifier = 'supreme'
AND name LIKE '%/%'
AND trim(split_part(name, '/', 1)) != '';

-- STEP 2: Handle degenerate rows where name starts with /
-- Strip the leading slash and keep the valid name after it
-- nes_id cleared since combined string was never a valid entity
UPDATE court_case_entities
SET
    name = trim(substring(name FROM 2)),
    nes_id = NULL,
    updated_at = now() AT TIME ZONE 'Asia/Kathmandu'
WHERE court_identifier = 'supreme'
AND name LIKE '%/%'
AND trim(split_part(name, '/', 1)) = ''
AND trim(substring(name FROM 2)) != '';

-- STEP 3: Insert new rows for all names after the first (normal rows)
-- nes_id explicitly NULL so split rows are resolved independently
-- WITH ORDINALITY ensures deterministic ordering of split parts
INSERT INTO court_case_entities (
    case_number, court_identifier, side, name,
    address, nes_id, created_at, updated_at
)
SELECT
    case_number, court_identifier, side,
    trim(u.split_name),
    NULL,   -- address: not available for split rows
    NULL,   -- nes_id: must be NULL so each split entity is resolved independently
    created_at,
    now() AT TIME ZONE 'Asia/Kathmandu'
FROM court_case_entities
CROSS JOIN LATERAL (
    SELECT split_name, ord
    FROM unnest(string_to_array(name, '/')) WITH ORDINALITY AS t(split_name, ord)
) u
WHERE court_identifier = 'supreme'
AND name LIKE '%/%'
AND trim(split_part(name, '/', 1)) != ''
AND trim(u.split_name) != ''
AND u.ord > 1;

-- STEP 4: Update original rows with just the first name
-- nes_id cleared since combined string was never a valid entity
UPDATE court_case_entities
SET
    name = trim(split_part(name, '/', 1)),
    nes_id = NULL,
    updated_at = now() AT TIME ZONE 'Asia/Kathmandu'
WHERE court_identifier = 'supreme'
AND name LIKE '%/%'
AND trim(split_part(name, '/', 1)) != '';

-- STEP 5: Verify results
-- Confirm no slash-separated rows remain
SELECT COUNT(*) AS remaining_rows_with_slash
FROM court_case_entities
WHERE court_identifier = 'supreme'
AND name LIKE '%/%';

-- Summarize affected rows to confirm split succeeded
SELECT case_number, side, name, COUNT(*) AS row_count
FROM court_case_entities
WHERE court_identifier = 'supreme'
AND case_number IN (
    SELECT DISTINCT case_number
    FROM court_case_entities
    WHERE court_identifier = 'supreme'
    AND name LIKE '%/%'
)
GROUP BY case_number, side, name
ORDER BY case_number, side, name;

-- Change ROLLBACK to COMMIT when ready to apply in production
ROLLBACK;