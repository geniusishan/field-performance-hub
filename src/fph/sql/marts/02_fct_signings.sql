-- One row per clean merchant signing, enriched with activation outcome.
-- Activated within 90 days = first transacting month no more than 2 months after the signing month.
-- A signing is only "eligible" for the activation KPI once its window has fully closed (2+ months ago),
-- so recent signings never drag the rate down unfairly.
WITH first_txn AS (
    SELECT merchant_id, MIN(month) AS first_txn_month
    FROM staging.merchant_volume
    WHERE charge_volume_usd > 0
    GROUP BY merchant_id
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
        WHEN f.first_txn_month IS NOT NULL
         AND DATE_DIFF('month', m.signed_month, f.first_txn_month) <= 2 THEN 1
        ELSE 0
    END AS activated_90d,
    CASE
        WHEN DATE_DIFF('month', m.signed_month, p.as_of_month) >= 2 THEN 1
        ELSE 0
    END AS activation_eligible
FROM staging.merchants AS m
JOIN marts.dim_rep AS d
    ON m.rep_id = d.rep_id
LEFT JOIN first_txn AS f
    ON m.merchant_id = f.merchant_id
CROSS JOIN staging.run_params AS p
