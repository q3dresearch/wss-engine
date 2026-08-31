# Capture contract — non-negotiable

1. **Raw response bytes are archived verbatim. Nothing is parsed at capture
   time.** A parser bug must always be fixable by re-parsing the archive,
   never by re-fetching a page that has since changed.
2. **Every fetch appends a manifest row, including unchanged ones.**
   Content-hash dedupe skips the *file write*, never the *observation*.
   "We looked and it was identical" is what makes a revision date defensible.
3. **Failed gate → quarantine.** Bad responses never enter the archive. The
   bytes land under `quarantine/` (same path convention as `raw/`) for triage;
   quarantine is working-tree/CI-artifact material, never committed.
4. **Failures are loud.** Any `error` or `quarantined` outcome exits non-zero
   and turns the build red.
5. Identifiable user-agent carrying `WSS_CONTACT` (capture refuses to
   run without it); `robots.txt` honoured; configured per-host delay; 3
   retries with exponential backoff on 429/5xx and connection errors.

## Manifest

One row per fetch, appended to `manifest/<source_id>/<YYYY-MM>.csv`:

```
source_id, url, fetched_at, http_status, content_type, content_length,
content_sha256, etag, last_modified, outcome, raw_ref, reason, warnings
```

Outcomes: `first_capture | changed | unchanged | quarantined | error | skipped`.

- `unchanged` rows carry the `raw_ref` of the capture they matched — one raw
  file, many dated observations.
- `skipped` means robots.txt disallowed the URL; it counts as neither success
  nor failure for health.
- The manifest **always stays in git**, whatever the storage backend — it is
  small, append-only, and its commit history is the provenance record.

## Conditional requests

When a previous capture recorded an `ETag`/`Last-Modified`, capture sends
`If-None-Match`/`If-Modified-Since`. A `304` is recorded as `unchanged`
(reason `not_modified`) without re-downloading the body.

## Heartbeat

Every capture run writes `state/last_run.json`, even when nothing changed. It
proves the cron is alive, and the resulting commit activity stops GitHub from
disabling scheduled workflows after 60 quiet days.

## Doctor

`wss doctor <source_id>` dry-runs one source: robots verdict, fetch,
headers, a preview of the raw bytes, and each gate's verdict. **Always run it
and read the raw response before flipping a new source to `active`.** It
writes nothing.
