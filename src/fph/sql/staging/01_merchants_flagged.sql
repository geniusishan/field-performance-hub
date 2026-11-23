-- Every raw merchant row, tagged with the first data-quality rule it breaks (NULL = clean).
-- Rules are ordered by severity so each row carries exactly one reason code.
WITH ranked AS (
    SELECT
        m.*,
        ROW_NUMBER() OVER (PARTITION BY m.merchant_id ORDER BY m._loaded_at DESC) AS rn
    FROM raw.merchants AS m
)
SELECT
    r.merchant_id,
    r.merchant_name,
    r.territory_id,
    r.signed_by_rep_id,
    CAST(r.signed_date AS DATE) AS signed_date,
    r.industry,
    r.segment,
    r.expected_annual_volume_usd,
    r.source_system,
    r._loaded_at,
    CASE
        WHEN r.rn > 1 THEN 'DUPLICATE_RECORD'
        WHEN r.territory_id IS NULL THEN 'MISSING_TERRITORY'
        WHEN rep.rep_id IS NULL THEN 'ORPHAN_REP'
        WHEN CAST(r.signed_date AS DATE) > p.as_of_month_end THEN 'FUTURE_SIGNED_DATE'
        WHEN r.expected_annual_volume_usd IS NULL OR r.expected_annual_volume_usd <= 0 THEN 'INVALID_EXPECTED_VOLUME'
    END AS dq_issue
FROM ranked AS r
LEFT JOIN raw.reps AS rep
    ON r.signed_by_rep_id = rep.rep_id
CROSS JOIN staging.run_params AS p
