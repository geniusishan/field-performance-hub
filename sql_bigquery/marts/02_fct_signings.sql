-- Auto-generated from src/fph/sql/marts/02_fct_signings.sql by sqlglot. Target: fph_marts.fct_signings
CREATE OR REPLACE TABLE fph_marts.fct_signings AS
/* One row per clean merchant signing, enriched with activation outcome. */
/* Activated within 90 days = first transacting month no more than 2 months after the signing month. */
/* A signing is only "eligible" for the activation KPI once its window has fully closed (2+ months ago), */
/* so recent signings never drag the rate down unfairly. */
WITH first_txn AS (
  SELECT
    merchant_id,
    MIN(month) AS first_txn_month
  FROM fph_staging.merchant_volume
  WHERE
    charge_volume_usd > 0
  GROUP BY
    merchant_id
)
SELECT
  m.merchant_id,
  m.rep_id,
  d.territory_id,
  d.market_id,
  d.region,
  m.signed_date,
  m.signed_month,
  m.industry,
  m.segment,
  CASE WHEN m.segment IN ('Mid', 'Large') THEN 1 ELSE 0 END AS is_mid_large,
  m.expected_annual_volume_usd,
  f.first_txn_month,
  CASE
    WHEN NOT f.first_txn_month IS NULL
    AND DATE_DIFF(f.first_txn_month, m.signed_month, MONTH) <= 2
    THEN 1
    ELSE 0
  END AS activated_90d,
  CASE WHEN DATE_DIFF(p.as_of_month, m.signed_month, MONTH) >= 2 THEN 1 ELSE 0 END AS activation_eligible
FROM fph_staging.merchants AS m
JOIN fph_marts.dim_rep AS d
  ON m.rep_id = d.rep_id
LEFT JOIN first_txn AS f
  ON m.merchant_id = f.merchant_id
CROSS JOIN fph_staging.run_params AS p;
