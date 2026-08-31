-- Analysis over the long-format observation table, built and tested against
-- planted ground truth before any real data exists. Loaded by run.py into
-- sqlite as: observations(series_id, entity_id, observed_at, captured_at,
-- metric, value, unit, source_id, raw_ref, parser_version)

-- query: latest_downloads
WITH obs AS (
  SELECT entity_id, observed_at, CAST(value AS REAL) AS v
  FROM observations WHERE metric = 'downloads_30d'
)
SELECT entity_id, CAST(v AS INTEGER) AS downloads_30d
FROM obs
WHERE observed_at = (SELECT MAX(observed_at) FROM obs)
ORDER BY v DESC;

-- query: growth_28d
WITH obs AS (
  SELECT entity_id, observed_at, CAST(value AS REAL) AS v
  FROM observations WHERE metric = 'downloads_30d'
),
ranked AS (
  SELECT entity_id, observed_at, v,
         ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY observed_at DESC) AS rn
  FROM obs
),
latest AS (
  SELECT entity_id, observed_at AS latest_at, v AS latest_v FROM ranked WHERE rn = 1
),
past AS (
  SELECT r.entity_id, r.v AS past_v,
         ROW_NUMBER() OVER (PARTITION BY r.entity_id ORDER BY r.observed_at DESC) AS rn
  FROM obs r
  JOIN latest l ON l.entity_id = r.entity_id
  WHERE r.observed_at <= datetime(l.latest_at, '-28 days')
)
SELECT l.entity_id,
       CAST(l.latest_v AS INTEGER) AS latest,
       CAST(p.past_v AS INTEGER)   AS past_28d,
       ROUND((l.latest_v - p.past_v) * 100.0 / p.past_v, 1) AS growth_pct_28d
FROM latest l
LEFT JOIN past p ON p.entity_id = l.entity_id AND p.rn = 1
ORDER BY growth_pct_28d DESC;

-- query: overtake
WITH obs AS (
  SELECT entity_id, observed_at, CAST(value AS REAL) AS v
  FROM observations WHERE metric = 'downloads_30d'
),
pair AS (
  SELECT a.observed_at, a.v AS challenger, b.v AS incumbent
  FROM obs a
  JOIN obs b ON a.observed_at = b.observed_at
  WHERE a.entity_id = 'sandbox/nova-lm' AND b.entity_id = 'sandbox/legacy-gpt'
)
SELECT MIN(observed_at) AS overtake_at
FROM pair WHERE challenger > incumbent;

-- query: lifespan
WITH obs AS (
  SELECT entity_id, observed_at
  FROM observations WHERE metric = 'downloads_30d'
),
bounds AS (SELECT MAX(observed_at) AS series_end FROM obs)
SELECT o.entity_id,
       MIN(o.observed_at) AS first_seen,
       MAX(o.observed_at) AS last_seen,
       CASE WHEN MAX(o.observed_at) < b.series_end THEN 'gone' ELSE 'alive' END AS status
FROM obs o, bounds b
GROUP BY o.entity_id
ORDER BY o.entity_id;
