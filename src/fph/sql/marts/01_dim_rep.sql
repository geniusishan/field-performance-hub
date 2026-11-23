-- One row per rep with full org hierarchy and tenure as of the reporting month.
SELECT
    r.rep_id,
    r.rep_name,
    r.role,
    r.manager_id,
    CAST(r.hire_date AS DATE) AS hire_date,
    DATE_DIFF('month', CAST(r.hire_date AS DATE), p.as_of_month) AS tenure_months,
    t.territory_id,
    t.territory_name,
    t.merchant_universe,
    mk.market_id,
    mk.market_name,
    mk.region
FROM raw.reps AS r
JOIN raw.territories AS t
    ON r.territory_id = t.territory_id
JOIN raw.markets AS mk
    ON t.market_id = mk.market_id
CROSS JOIN staging.run_params AS p
