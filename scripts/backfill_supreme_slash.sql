BEGIN;

-- STEP 1: Preview what will be fixed
SELECT id, case_number, side, name
FROM court_case_entities
WHERE court_identifier = 'supreme'
AND name LIKE '%/%';

-- STEP 2: Insert new rows for all names after the first
INSERT INTO court_case_entities (
    case_number, court_identifier, side, name,
    address, nes_id, created_at, updated_at
)
SELECT
    case_number, court_identifier, side,
    trim(u.split_name),
    address, nes_id, created_at, NOW()
FROM court_case_entities
CROSS JOIN LATERAL (
    SELECT split_name, row_number() OVER () AS rn
    FROM unnest(string_to_array(name, '/')) AS split_name
) u
WHERE court_identifier = 'supreme'
AND name LIKE '%/%'
AND trim(u.split_name) != ''
AND u.rn > 1;

-- STEP 3: Update original rows with just the first name
UPDATE court_case_entities
SET
    name = trim(split_part(name, '/', 1)),
    updated_at = NOW()
WHERE court_identifier = 'supreme'
AND name LIKE '%/%';

-- STEP 4: Verify results
SELECT id, case_number, side, name
FROM court_case_entities
WHERE court_identifier = 'supreme'
ORDER BY case_number, id
LIMIT 20;

-- Change ROLLBACK to COMMIT when ready to apply in production
ROLLBACK;