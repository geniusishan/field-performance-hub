-- Auto-generated from src/fph/sql/marts/01_dim_rep.sql by sqlglot. Target: fph_marts.dim_rep
CREATE OR REPLACE TABLE fph_marts.dim_rep AS
/* One row per rep with full org hierarchy, tenure and employment status as of the reporting month. */
SELECT
  r.rep_id,
  r.rep_name,
  r.role,
  r.manager_id,
  CAST(r.hire_date AS DATE) AS hire_date,
  CAST(r.termination_date AS DATE) AS termination_date,
  CASE
    WHEN r.termination_date IS NULL
    OR CAST(r.termination_date AS DATE) > p.as_of_month_end
    THEN 'Active'
    ELSE 'Left'
  END AS status,
  DATE_DIFF(p.as_of_month, CAST(r.hire_date AS DATE), MONTH) AS tenure_months,
  t.territory_id,
  t.territory_name,
  t.merchant_universe,
  mk.market_id,
  mk.market_name,
  mk.region
FROM fph_raw.reps AS r
JOIN fph_raw.territories AS t
  ON r.territory_id = t.territory_id
JOIN fph_raw.markets AS mk
  ON t.market_id = mk.market_id
CROSS JOIN fph_staging.run_params AS p;
