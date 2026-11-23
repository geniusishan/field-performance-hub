-- Monthly charge volume tagged with data-quality issues.
-- A row whose merchant exists in raw but was quarantined is PARENT_QUARANTINED,
-- which is different from a merchant id no system has ever seen (ORPHAN_MERCHANT).
WITH ranked AS (
    SELECT
        v.*,
        ROW_NUMBER() OVER (PARTITION BY v.merchant_id, v.month ORDER BY v._loaded_at DESC) AS rn
    FROM raw.merchant_volume AS v
),
raw_ids AS (
    SELECT DISTINCT merchant_id FROM raw.merchants
)
SELECT
    r.merchant_id,
    CAST(r.month AS DATE) AS month,
    r.charge_volume_usd,
    r.transactions,
    r._loaded_at,
    CASE
        WHEN r.rn > 1 THEN 'DUPLICATE_RECORD'
        WHEN r.charge_volume_usd < 0 THEN 'NEGATIVE_AMOUNT'
        WHEN ri.merchant_id IS NULL THEN 'ORPHAN_MERCHANT'
        WHEN m.merchant_id IS NULL THEN 'PARENT_QUARANTINED'
        WHEN CAST(r.month AS DATE) > p.as_of_month THEN 'FUTURE_PERIOD'
    END AS dq_issue
FROM ranked AS r
LEFT JOIN raw_ids AS ri
    ON r.merchant_id = ri.merchant_id
LEFT JOIN staging.merchants AS m
    ON r.merchant_id = m.merchant_id
CROSS JOIN staging.run_params AS p
