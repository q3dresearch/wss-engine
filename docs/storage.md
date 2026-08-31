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
