WITH ordered AS (
  SELECT
    offer_id,
    machine_id,
    gpu_name,
    ts,
    rented,
    LEAD(ts) OVER (PARTITION BY offer_id ORDER BY ts) AS next_ts
  FROM offers_raw
  WHERE ts BETWEEN :since AND :until
), durations AS (
  SELECT
    offer_id,
    machine_id,
    gpu_name,
    rented,
    MAX(:since, ts) AS start_ts,
    MIN(:until, COALESCE(next_ts, datetime(ts, :poll_interval))) AS end_ts
  FROM ordered
), totals AS (
  SELECT
    offer_id,
    machine_id,
    gpu_name,
    SUM(
      (julianday(replace(end_ts, 'T', ' ')) - julianday(replace(start_ts, 'T', ' '))) * 24.0
    ) AS total_hours,
    SUM(
      CASE WHEN rented = 1 THEN
        (julianday(replace(end_ts, 'T', ' ')) - julianday(replace(start_ts, 'T', ' '))) * 24.0
      ELSE 0 END
    ) AS rented_hours
  FROM durations
  GROUP BY 1,2,3
)
SELECT
  offer_id,
  machine_id,
  gpu_name,
  ROUND(100.0 * rented_hours / NULLIF(total_hours, 0), 2) AS rented_pct,
  ROUND(rented_hours, 3) AS rented_hours,
  ROUND(total_hours, 3) AS total_hours
FROM totals
WHERE total_hours > 0
ORDER BY rented_pct DESC, rented_hours DESC;

