# Registry

The registry drives the fleet. **There is never a workflow per source** — a
handful of scheduled workflows read the registry, shard the active sources
across a job matrix, and each job walks its slice. Adding a collection is one
new file; removing one is a status change. If a change requires editing a
workflow to add a data source, the design is wrong.

## One file per source

`registry/<source_id>.yml` — one file per source, not one big file, so
additions are reviewable and never merge-conflict.

```yaml
source_id: hf.models.text-generation   # publisher.domain.series — IMMUTABLE, never reused
status: active                          # active | paused | auto_disabled | retired
cadence: daily                          # hourly | daily | weekly | monthly
schema_id: adoption.v1                  # picks the parser at derive time

publisher: Hugging Face
publisher_tier: first_party             # first_party | primary | redistribution
destroys_own_history: true              # false → do not capture; publisher archives it
licence: "…"                            # terms the captured data is under
personal_data: none                     # none | parties_only | present

storage: git                            # git | object
endpoints:
  - url: https://huggingface.co/api/models?...
    delay_seconds: 2                    # per-host politeness delay
    timeout_seconds: 30                 # optional

gates:
  expect_status: 200                    # an int, or a list like [200, 404]
  min_bytes: 1000
  content_type_any: [json]
  must_contain: ["downloads"]
  must_not_contain: ["Access Denied"]
  max_shrink_pct: 50
```

Optional keys: `notes`, `tags`. Anything else is rejected — at fleet scale a
typo that silently disables a gate is worse than a red build.

## Validation is a hard gate

`wss validate` fails on any problem; CI runs it on every push. Rules
beyond shape checking:

- the filename must be `<source_id>.yml` and `source_id` must be
  `publisher.domain.series` (lowercase, ≥ 3 dot-separated segments)
- `dedupe_ignore` is an optional list of regexes stripped from the body **only**
  when deciding changed vs unchanged. Use it when a publisher stamps a random
  id into every render (Drupal's `js-view-dom-id-<hash>`, build ids, nonces),
  which otherwise defeats dedupe entirely — storage grows without bound and
  `outcome: changed` stops meaning anything. Archived bytes and
  `content_sha256` are always the untouched response.
- `personal_data: present` is rejected outright — this fleet does not collect
  personal data
- `personal_data: parties_only` is a narrow exemption for public proceedings,
  and it **requires `notes`** saying who the named parties are and why the
  derived tables do not carry them. Use it only when all of these hold:
  - names appear solely as **parties to a public proceeding** — petitioners,
    applicants, respondents — not as the subject of the dataset
  - the authority publishes them as an inseparable part of that proceeding
  - no contact details, identifiers or sensitive attributes are present
  - **deleting the name column leaves the dataset's purpose intact**, and the
    derived tables therefore carry no such column

  That last line is the test. If removing the names destroys the point of the
  dataset, the dataset is about people: use `present` and do not capture it.
  A register of who holds a licence fails the test; a docket of petitions
  filed against a regulation passes it.

  Redaction is not a route back in. If the record carries a case number that
  resolves to the party on the publisher's own site, a placeholder buys no
  privacy and costs a field — declare what is true instead.
- `destroys_own_history: false` on an **active** source is rejected: the
  publisher archives its own history, so capture adds nothing. Keeping the
  entry as `paused`/`retired` is allowed (it documents the decision)
- duplicate `source_id`s across files are rejected — IDs are immutable and
  never reused

## Deterministic sharding

`shard_of(source_id, N) = sha256(source_id)[:8] mod N`. A source lands in the
same shard every run, so failures are attributable to a stable job. `plan`
emits only the non-empty shards as a JSON array (`["1/20", "7/20", …]`) —
that single indirection is what lets ten workflows drive thousands of sources.
