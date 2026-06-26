-- Data-quality backfills for ngm_v1. PREPARED FOR REVIEW — DO NOT auto-run.
-- Each block is independent and idempotent (re-running is safe). Run on the CNPG
-- primary; wrap in BEGIN/…/ROLLBACK first to dry-run and confirm the row counts.
--
-- ORDERING NOTE (avoid re-pollution): deploy the corresponding CODE fixes first:
--   * B1 verdict sentinel  -> after district_case_enrichment sentinel guard ships
--   * B4 clear-13 orders    -> after the pipeline transient-classification fix ships
-- B2/B3 are pure reads of already-scraped data and can run any time.

-- ───────────────────────────────────────────────────────────────────────────
-- B1. Verdict sentinel: '****-**-**' means "no verdict yet", not a date.
--     Expected: ~140,797 rows (district only; supreme/special verified 0).
-- ───────────────────────────────────────────────────────────────────────────
UPDATE court_cases
SET verdict_date_bs = NULL
WHERE verdict_date_bs = '****-**-**';

-- ───────────────────────────────────────────────────────────────────────────
-- B2. High-court core fields stranded in extra_data under raw Devanagari keys
--     (the enrichment label-match missed the trailing-dot / alt-spelling labels).
--     Recovers ~486k rows WITHOUT re-scraping. COALESCE => never overwrites a
--     value that is already populated. Registration DATE needs BS->AD conversion,
--     so it is intentionally left to a Python backfill (normalize_date +
--     convert_bs_to_ad), not done here.
-- ───────────────────────────────────────────────────────────────────────────
UPDATE court_cases
SET
    registration_number = COALESCE(registration_number, NULLIF(extra_data->>'दर्ता_नँ.', '')),
    case_status         = COALESCE(case_status,         NULLIF(extra_data->>'मुद्दाको_स्थिती', ''))
WHERE court_identifier IN (
        'biratnagarhc','illamhc','dhankutahc','okhaldhungahc','janakpurhc',
        'rajbirajhc','birganjhc','patanhc','hetaudahc','pokharahc','baglunghc',
        'tulsipurhc','butwalhc','nepalgunjhc','surkhethc','jumlahc','dipayalhc',
        'mahendranagarhc'
    )
  AND status = 'enriched'
  AND (registration_number IS NULL OR case_status IS NULL);

-- ───────────────────────────────────────────────────────────────────────────
-- B3. Supreme hearing judge_names are empty (~360k) but the judges live in the
--     case's extra_data 'enrichment_hearings' array; join on the (already
--     normalized) BS hearing date. Needs ix_cch_case_court for sane performance.
-- ───────────────────────────────────────────────────────────────────────────
UPDATE court_case_hearings h
SET judge_names = eh.value->>'judges'
FROM court_cases c,
     jsonb_array_elements(c.extra_data->'enrichment_hearings') eh
WHERE h.court_identifier = 'supreme'
  AND c.court_identifier = 'supreme'
  AND c.case_number = h.case_number
  AND (h.judge_names IS NULL OR h.judge_names = '')
  AND eh.value->>'date' = h.hearing_date_bs
  AND COALESCE(eh.value->>'judges', '') <> '';

-- ───────────────────────────────────────────────────────────────────────────
-- B4. Release the 13 transient FilesPipeline (FileException) order failures that
--     were wrongly persisted as permanent, so the orders crawl retries them.
--     Verified (BEGIN/ROLLBACK) to match exactly 13 rows (3 supreme, 10 special).
-- ───────────────────────────────────────────────────────────────────────────
UPDATE court_cases
SET extra_data = (extra_data - 'orders_failed' - 'orders_error' - 'orders_failed_at')
WHERE court_identifier IN ('special', 'supreme')
  AND extra_data->>'orders_failed' = 'true'
  AND extra_data->>'orders_error' LIKE '%scrapy.pipelines.files.FileException%'
  AND extra_data->>'orders_error' LIKE '%_process_request%'
  AND (extra_data->'court_orders') IS NULL;  -- never touch a case that has docs

-- After B4: SELECT count(*) FROM court_cases
--   WHERE court_identifier IN ('special','supreme')
--     AND extra_data->>'orders_error' LIKE '%FileException%'
--     AND extra_data->>'orders_failed' = 'true';   -- expect 0
