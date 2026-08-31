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
- `expected_interval_h` — from cadence (hourly=1, daily=24, weekly=168,
  monthly=720); compare with `staleness_h` to spot silent stalls.
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
