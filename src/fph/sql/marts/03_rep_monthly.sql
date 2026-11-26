-- Rep x month performance grain (only months the rep was employed) that every scorecard,
-- dashboard and the AI agent reads from.
-- new_merchant_volume_usd = charge volume in the month from merchants the rep signed in the
-- same calendar year (the "new business" a field team is credited for).
WITH months AS (
    SELECT DISTINCT signed_month AS month FROM marts.fct_signings
),
rep_months AS (
    SELECT d.rep_id, mo.month
    FROM marts.dim_rep AS d
    CROSS JOIN months AS mo
    WHERE mo.month >= CAST(DATE_TRUNC('month', d.hire_date) AS DATE)
      AND (d.termination_date IS NULL OR mo.month <= d.termination_date)   -- employed in the month
),
sign AS (
    SELECT
        rep_id,
        signed_month AS month,
        COUNT(*) AS signings,
        SUM(is_mid_large) AS mid_large_signings,
        SUM(expected_annual_volume_usd) AS expected_annual_volume_usd,
        SUM(activated_90d * activation_eligible) AS activated_90d,
        SUM(activation_eligible) AS activation_eligible
    FROM marts.fct_signings
    GROUP BY rep_id, signed_month
),
new_vol AS (
    SELECT
        s.rep_id,
        v.month,
        SUM(v.charge_volume_usd) AS new_merchant_volume_usd
    FROM staging.merchant_volume AS v
    JOIN marts.fct_signings AS s
        ON v.merchant_id = s.merchant_id
    WHERE EXTRACT(YEAR FROM v.month) = EXTRACT(YEAR FROM s.signed_month)
    GROUP BY s.rep_id, v.month
)
SELECT
    rm.month,
    d.rep_id,
    d.rep_name,
    d.role,
    d.territory_id,
    d.territory_name,
    d.market_id,
    d.market_name,
    d.region,
    DATE_DIFF('month', d.hire_date, rm.month) AS tenure_months,
    COALESCE(s.signings, 0) AS signings,
    COALESCE(s.mid_large_signings, 0) AS mid_large_signings,
    COALESCE(s.expected_annual_volume_usd, 0) AS expected_annual_volume_usd,
    COALESCE(nv.new_merchant_volume_usd, 0) AS new_merchant_volume_usd,
    COALESCE(s.activated_90d, 0) AS activated_90d,
    COALESCE(s.activation_eligible, 0) AS activation_eligible
FROM rep_months AS rm
JOIN marts.dim_rep AS d
    ON rm.rep_id = d.rep_id
LEFT JOIN sign AS s
    ON rm.rep_id = s.rep_id AND rm.month = s.month
LEFT JOIN new_vol AS nv
    ON rm.rep_id = nv.rep_id AND rm.month = nv.month
