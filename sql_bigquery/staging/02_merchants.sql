-- Auto-generated from src/fph/sql/staging/02_merchants.sql by sqlglot. Target: fph_staging.merchants
CREATE OR REPLACE TABLE fph_staging.merchants AS
/* Clean merchant signings: only rows that passed every rule. */
SELECT
  merchant_id,
  merchant_name,
  territory_id,
  signed_by_rep_id AS rep_id,
  signed_date,
  CAST(TIMESTAMP_TRUNC(signed_date, MONTH) AS DATE) AS signed_month,
  industry,
  segment,
  expected_annual_volume_usd,
  source_system
FROM fph_staging.merchants_flagged
WHERE
  dq_issue IS NULL;
