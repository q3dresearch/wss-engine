# Health — what makes 1,000 sources survivable

The binding constraint at fleet scale is human attention, not compute. At any
moment something is broken; without auto-disable one dead source burns
attention forever. Triage is a weekly pass over a sorted table, never a
stream of alerts.

`wss health` derives everything from the manifest and writes
`health/health.csv`, worst first:

```
source_id, first_success_at, last_success_at, last_attempt_at,
consecutive_failures, expected_interval_h, staleness_h,
gate_fail_rate_28d, status
```

`first_success_at` → `last_success_at` is each series' coverage range — the
answer to "from when to when does this data exist", kept machine-readable
for every source including retired ones.

- `consecutive_failures` — trailing `error`/`quarantined` rows since the last
  success; `skipped` (robots) counts as neither.
- `expected_interval_h` — from cadence (weekly=168, monthly=720,
  quarterly=2160); compare with `staleness_h` to spot silent stalls.
- `gate_fail_rate_28d` — quarantined ÷ attempts over the last 28 days; a
  creeping rate means the page is drifting under the gates before it dies.

## Auto-disable

**5 consecutive failures flips `status: active` to `auto_disabled`** by
rewriting only the status line of the registry file (the rest of the entry,
comments included, is untouched). Newly disabled sources are listed in
`state/auto_disabled.json`; the health workflow opens one GitHub issue per
source from that file. The engine itself never talks to GitHub.

Re-enabling is a human decision: fix the cause, set `status: active`, commit.

A source with many endpoints only accumulates failures while *nothing*
succeeds between them — one dead endpoint among many healthy ones shows up in
`gate_fail_rate_28d`, not as a disable. For cohort-member sources where
individual members are expected to die, gate with `expect_status: [200, 404]`
so a death is archived as evidence instead of counted as a failure.

`--dry-run` computes the table without flipping anything; `--threshold N`
overrides the default of 5.


## A rotted source no longer reds the run

`wss capture` used to exit 1 on any quarantined or errored source. At fleet
scale some source is always rotting, so the red tick was permanently on --
wss-mining-pipeline went red with 12 of 17 endpoints succeeding, and a red tick
that is always on is one nobody reads.

Red is now reserved for a run that could not function: **zero successes with at
least one failure**, which points at the network, the credentials or the engine
rather than at one publisher. A missing credential is the exception and still
fails immediately, because it is our misconfiguration and will not heal.

Per-source rot escalates on its own path instead:

1. the manifest row records the failure every run
2. `consecutive_failures` climbs; at five, health auto-disables the source and
   opens an issue naming it
3. the weekly fleet sift reports it as `rot` long before that -- a monthly
   source that fails its single attempt shows a 100% `gate_fail_rate_28d`

`wss capture --strict` restores the old all-or-nothing rule.
