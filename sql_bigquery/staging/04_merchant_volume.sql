-- Auto-generated from src/fph/sql/staging/04_merchant_volume.sql by sqlglot. Target: fph_staging.merchant_volume
CREATE OR REPLACE TABLE fph_staging.merchant_volume AS
SELECT
  merchant_id,
  month,
  charge_volume_usd,
  transactions
FROM fph_staging.volume_flagged
WHERE
  dq_issue IS NULL;
