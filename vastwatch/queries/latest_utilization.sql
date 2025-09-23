WITH latest AS (
  SELECT *
  FROM offers_raw
  WHERE ts = (SELECT MAX(ts) FROM offers_raw)
)
SELECT
  gpu_name,
  COALESCE(type,'any') AS type,
  CASE
    WHEN COALESCE(verified, 0) = 1 THEN 'verified'
    WHEN COALESCE(deverified, 0) = 1 THEN 'deverified'
    ELSE 'unverified'
  END AS verification_status,
  COUNT(*) FILTER (WHERE rented=1) AS rented_offers,
  COUNT(*) FILTER (WHERE rented=0) AS available_offers,
  ROUND(
    100.0 * CAST(COUNT(*) FILTER (WHERE rented=1) AS FLOAT) /
    NULLIF(COUNT(*),0), 1
  ) AS utilization_pct,
  ROUND(AVG(CASE WHEN rented=1 THEN dph_total_usd END), 3) AS avg_price_rented,
  ROUND(AVG(CASE WHEN rented=0 THEN dph_total_usd END), 3) AS avg_price_available
FROM latest
GROUP BY 1,2,3
ORDER BY verification_status DESC, utilization_pct DESC, rented_offers DESC;
