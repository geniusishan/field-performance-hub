-- Market x month series used for forecasting and the executive view.
SELECT
    month,
    market_id,
    market_name,
    region,
    SUM(signings) AS signings,
    SUM(mid_large_signings) AS mid_large_signings,
    SUM(new_merchant_volume_usd) AS new_merchant_volume_usd,
    SUM(activated_90d) AS activated_90d,
    SUM(activation_eligible) AS activation_eligible,
    COUNT(DISTINCT CASE WHEN signings > 0 THEN rep_id END) AS active_reps
FROM marts.rep_monthly
GROUP BY month, market_id, market_name, region
