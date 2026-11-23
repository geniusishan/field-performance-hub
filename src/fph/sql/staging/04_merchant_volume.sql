SELECT
    merchant_id,
    month,
    charge_volume_usd,
    transactions
FROM staging.volume_flagged
WHERE dq_issue IS NULL
