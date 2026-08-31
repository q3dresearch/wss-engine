# Cohorts — frozen, followed forever

For adoption studies, the sample must be chosen once, on stated criteria, and
then followed forever — *including members that die*. Re-selecting "the top
1,000 today" every quarter silently deletes every failure from history and
turns the dataset into survivor worship.

`snapshotter.cohort` is generic (not tied to any publisher):

- **Selection is manual and quarterly, never on a cron.** A human runs a
  script, reviews the result, commits it.
- Two qualifying paths:
  - *established* — top-N by a metric, above a floor
  - *new entrant* — created since a date, **any size**. This path is what
    prevents a survivor-only sample: tomorrow's winner qualifies while tiny.
- Each selection writes a dated **vintage** file
  (`cohorts/<cohort>/<YYYY-MM-DD>.yml`) recording the criteria and members.
  Old vintages are never edited — `write_vintage` refuses to overwrite.
- The **effective cohort** is the union of every vintage. Once in, never out.

```python
from snapshotter.cohort import CohortCriteria, select_vintage, write_vintage

criteria = CohortCriteria(
    metric_key="downloads", created_key="createdAt",
    established_top_n=200, established_floor=10_000,
    new_entrant_since="2026-04-01",
)
vintage = select_vintage(candidates, criteria, selected_at="2026-09-01")
write_vintage("cohorts/my-cohort", vintage)
```

`effective_members(cohort_dir)` returns the union with each member's first
vintage — the input for per-member capture endpoints or analysis filters.

A member that dies keeps its endpoint in the registry; gate that source with
`expect_status: [200, 404]` so the 404 is archived as the death record rather
than treated as a failure.
