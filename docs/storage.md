# Storage

One interface, two backends, **identical path convention**, so migrating a
source from git to object storage is a copy, not a rewrite:

```
raw/<source_id>/<YYYY>/<MM>/<YYYYMMDDTHHMMSSZ>-<sha256[:12]>.<ext>
```

Year/month partitioning is required: GitHub caps a directory at 3,000
entries. The extension comes from the response content-type (`json`, `html`,
`csv`, `xml`, `txt`, else `bin`).

## `storage: git` (LocalGitStore)

Raw files live in the domain repo and are committed by the workflow's final
job. Right for small, low-cadence sources.

## `storage: object` (ObjectStore)

S3-compatible object storage, same keys. Target Cloudflare R2 for zero
egress. Configure via environment:

| variable | meaning |
| --- | --- |
| `WSS_OBJECT_BUCKET` | bucket name (required) |
| `WSS_OBJECT_ENDPOINT` | endpoint URL (R2: `https://<account>.r2.cloudflarestorage.com`) |
| `WSS_OBJECT_PREFIX` | optional key prefix |

Credentials use the standard AWS variables (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`). Install the extra: `pip install wss[object]`.

**The manifest always stays in git** regardless of backend — it is the
provenance record, and `raw_ref` keys resolve in either backend because the
paths are identical.

## Setting up object storage — the runbook

Written down because the alternative is rediscovering it on a new machine.
There is no separate tool to install and nothing to maintain on a schedule:
the backend is in this engine, retention is an R2 lifecycle rule, and
credentials live in GitHub Actions secrets.

### Once, for the fleet

1. **Create one bucket**, not one per repo — `wss-archive`. Leave public access
   **disabled** and attach no custom domain; R2 buckets are private by default
   and that default is the whole point.
2. **Create an API token** scoped to that bucket, Object Read & Write.
3. **Separate repos by prefix**, not by bucket, so an eighth repo costs a
   prefix rather than a credential:

   ```
   wss-archive/
     cloud-footprint/raw/<source_id>/2026/09/...
     forest-harvest/raw/...
   ```

4. **Lifecycle rules, if ever needed**, are configured in the R2 dashboard.
   No cron, no cleanup job. Dedupe already prevents identical bytes being
   stored twice.

### Per repo

Add four **secrets** — Settings → Secrets and variables → Actions. They must be
secrets, not plain `env:` values: secrets are masked in workflow logs, plain
values are printed.

| secret | example |
| --- | --- |
| `WSS_OBJECT_BUCKET` | `wss-archive` |
| `WSS_OBJECT_ENDPOINT` | `https://<account>.r2.cloudflarestorage.com` |
| `WSS_OBJECT_PREFIX` | `cloud-footprint` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | from the API token |

Then flip the source: `storage: git` -> `storage: object`. The key convention
is identical, so migrating an existing source is a copy, not a rewrite.

### What stays public, and why that is the point

**The manifest never moves.** It stays in git for every backend, carrying
`source_id`, `url`, `fetched_at`, `http_status`, `content_sha256` and
`raw_ref` — metadata only, no content.

So a reader of the public repo can verify **what** you captured, **when**, and
its hash, but cannot fetch the bytes without credentials. That is worth having
deliberately rather than accepting as a compromise: point-in-time integrity is
provable to anyone doing diligence, while the content stays private.

Nothing else leaks. `capture.py` contains no `print()` calls; the CLI emits
only `outcome source_id (reason)`, and reasons are symbolic tokens
(`fetch_failed_TimeoutError`, `robots_disallowed`, `not_modified`) rather than
response bodies.

### When to bother

Only when size forces it. Measured on this fleet, marginal growth after the
first-day backfill:

| repo | MB/day | reaches 1 GB |
| --- | --- | --- |
| cloud-footprint | 12.9 | **~10 weeks** |
| forest-harvest | 8.7 | ~4 months |
| hugging-face | 4.7 | ~7 months |
| openrouter | 1.5 | ~2 years |
| drug-scarcity | 0.2 | ~17 years |
| mining-pipeline, food-trace | ~0 | never |

A source holding personal data is *not* on its own a reason to migrate — the
derived tables carry no names either way, and a few MB a month sits fine in
git. Migrate when the repo is outgrowing GitHub, and move sensitive sources
across at the same time for free.
