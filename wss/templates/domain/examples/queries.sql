-- Starter analysis over the long-format observation table. These work on any
-- domain repo, because every one emits the same schema:
--   observations(series_id, entity_id, observed_at, captured_at, metric,
--                value, unit, source_id, raw_ref, parser_version)
-- Edit the metric names to match your parsers.

-- Latest snapshot: the current leaderboard for one metric
WITH obs AS (
  SELECT entity_id, observed_at, CAST(value AS REAL) AS v
  FROM observations WHERE metric = 'count'
)
SELECT entity_id, CAST(v AS INTEGER) AS value
FROM obs
WHERE observed_at = (SELECT MAX(observed_at) FROM obs)
ORDER BY v DESC
LIMIT 25;

-- 28-day growth per entity: who is accelerating, who is decaying
WITH obs AS (
  SELECT entity_id, observed_at, CAST(value AS REAL) AS v
  FROM observations WHERE metric = 'count'
),
latest AS (
  SELECT entity_id, observed_at AS latest_at, v AS latest_v,
         ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY observed_at DESC) AS rn
  FROM obs
),
past AS (
  SELECT o.entity_id, o.v AS past_v,
         ROW_NUMBER() OVER (PARTITION BY o.entity_id ORDER BY o.observed_at DESC) AS rn
  FROM obs o
  JOIN latest l ON l.entity_id = o.entity_id AND l.rn = 1
  WHERE o.observed_at <= datetime(l.latest_at, '-28 days')
)
SELECT l.entity_id,
       CAST(l.latest_v AS INTEGER) AS latest,
       CAST(p.past_v AS INTEGER)   AS four_weeks_ago,
       ROUND((l.latest_v - p.past_v) * 100.0 / p.past_v, 1) AS growth_pct
FROM latest l
JOIN past p ON p.entity_id = l.entity_id AND p.rn = 1
WHERE l.rn = 1
ORDER BY growth_pct DESC;

-- Lifespans: entities whose last sighting predates the series end have
-- dropped out of the listing (or died)
WITH obs AS (
  SELECT entity_id, observed_at FROM observations WHERE metric = 'count'
)
SELECT entity_id,
       MIN(observed_at) AS first_seen,
       MAX(observed_at) AS last_seen
FROM obs
GROUP BY entity_id
HAVING MAX(observed_at) < (SELECT MAX(observed_at) FROM obs)
ORDER BY last_seen DESC;
