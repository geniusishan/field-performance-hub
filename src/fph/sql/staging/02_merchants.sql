-- Clean merchant signings: only rows that passed every rule.
SELECT
    merchant_id,
    merchant_name,
    territory_id,
    signed_by_rep_id AS rep_id,
    signed_date,
    CAST(DATE_TRUNC('month', signed_date) AS DATE) AS signed_month,
    industry,
    segment,
    expected_annual_volume_usd,
    source_system
FROM staging.merchants_flagged
WHERE dq_issue IS NULL
