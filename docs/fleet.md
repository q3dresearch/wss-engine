# Running a fleet on GitHub Actions

A domain repo needs five small workflows, none of which ever name a source.
The canonical templates live in `examples/workflows/`; a domain repo copies
them once and never touches them again.

## The shape

```
plan ──▶ capture (matrix over shards) ──▶ commit (rebase-retry)
```

1. **plan** — `wss plan --cadence weekly --shards 20` prints a JSON
   array of non-empty shards (`["1/20","7/20",…]`). The workflow feeds it to
   `fromJSON` as the job matrix. That single indirection is what lets ten
   workflows drive thousands of sources.
2. **capture** — each matrix job runs
   `wss capture --cadence weekly --shard <i>/<n>` over its slice, then
   uploads only the *new* files (a tarball of staged changes plus quarantine)
   as an artifact. `fail-fast: false` so one bad source cannot stop the rest.
3. **commit** — downloads every shard's delta, drops `quarantine/` (it stays
   in CI artifacts for 90 days, never in git), writes the heartbeat, commits
   with a pull-rebase/push retry loop. It runs `if: always()` so a red shard
   still gets its successful sources committed.

## Rules of thumb

- **Matrix cap is 256 jobs per run** — shard to ~20 jobs of ~50 sources each.
- **Shard count is a politeness decision, not only a speed one.** The per-host
  delay is enforced per process, so N shards means N runners can hit the same
  host simultaneously with no spacing between them. Before raising `SHARDS`,
  check which hosts would then run concurrently — a repo whose sources cluster
  on one publisher (especially a volunteer-run one) should stay on few shards,
  or one. Parallelism buys nothing on a five-source repo anyway.
- **Schedule off the hour** (`10 22 * * *`): GitHub's cron scheduler is
  contended at `:00`.
- One `concurrency` group shared by every repo-writing workflow
  (`group: fleet-commits`) serializes pushes; the rebase-retry loop mops up
  anything that still races.
- **`WSS_CONTACT`** (an email for the user-agent) must be set as a
  repo secret before enabling schedules — capture refuses to run without it.
- Every run commits `state/last_run.json` even when nothing changed: it
  proves the cron is alive and the activity stops GitHub disabling scheduled
  workflows after 60 days of quiet.

## The other workflows

- **health.yml** — daily, after capture: `wss health`, open a GitHub
  issue per newly auto-disabled source (from `state/auto_disabled.json`),
  commit `health/` + flipped registry files.
- **derive.yml** — scheduled: rebuild `derived/` and commit. On pull
  requests: rebuild and `git diff --exit-code -- derived` — **byte-identical
  or red**. A PR that changes a parser must contain the rebuilt output.
- **validate.yml** — every push/PR: `wss validate`. An invalid
  registry never lands on main.

## What not to build

No per-source workflows. No parsing at capture time. No orchestrator
(Airflow/Dagster/Prefect) — Actions is correct until there are cross-source
dependencies or backfill needs. No serving API or web UI.
