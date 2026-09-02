# wss — web snapshots

**Scheduled GitHub Actions that capture any observable web page or API, every
day, forever.** The engine behind `wss-*` data repos.

It archives responses **verbatim**, keeps an append-only manifest as the
provenance record, and derives point-in-time observation tables from that
archive — never from the live web. ("Snapshots" as in captured bytes, not
screenshots: HTML, JSON, CSV, whatever the page returns.)

The engine holds **no data, ever**. A *domain repo*
([wss-hugging-face](https://github.com/neldivad/wss-hugging-face),
[wss-openrouter](https://github.com/neldivad/wss-openrouter),
[wss-cloud-footprint](https://github.com/neldivad/wss-cloud-footprint)) holds a
registry of sources, runs this CLI from a few scheduled workflows, and commits
what comes back. The design target is 1,000+ collections managed by one
person, so the binding constraint is human attention.

## The rule everything hangs on

**There is never a workflow per source.** A registry drives the fleet: a few
scheduled workflows read it, shard the active sources across a job matrix, and
each job walks its slice.

- Adding a collection is **one new file** (`registry/<source_id>.yml`).
- Removing one is a status change.
- No infrastructure is touched either way.

If a change requires editing a workflow to add a data source, the design is
wrong.

## The capture contract — non-negotiable

1. Raw bytes archived verbatim; **nothing parsed at capture time**, so a
   parser bug is fixed by re-parsing history, never by re-fetching.
2. Every fetch appends a manifest row, **including unchanged ones** — dedupe
   skips the file write, never the observation.
3. Failed gate → quarantine; bad responses never enter the archive.
4. Failures are loud: non-zero exit, red build.
5. Identifiable user-agent, robots.txt honoured, per-host delay, 3 retries.

## Using it

```bash
pip install "wss @ git+https://github.com/neldivad/wss-engine.git@v0.5.3"

wss explore <url>                     # case a site before writing anything
wss init ../wss-yoursite --owner me   # scaffold a domain repo
wss validate                          # registry schema check; CI gate
wss plan --cadence daily --shards 20  # JSON shard array for the Actions matrix
wss capture --cadence daily --shard 3/20
wss health                            # health table, auto-disable
wss derive                            # raw → observation tables
wss doctor <source_id>                # dry-run one source, print raw bytes
```

Adding a source, start to finish:

```bash
wss explore "https://example.gov/listing"   # is this even capturable?
# save the suggested entry as registry/<source_id>.yml, still paused
wss doctor <source_id>                      # read the raw response yourself
# flip status: active
```

`explore` honours robots.txt, classifies the response, maps the payload onto
the observation schema, finds the JSON API behind a JavaScript page, and
prints a starter entry with gates inferred from what it saw. The judgment it
cannot make for you — *document or state?* — is in
[docs/casing-a-site.md](docs/casing-a-site.md). Sources that exist to be
archived and watched rather than measured use the built-in
`schema_id: archive.v1` and need no parser.

**Never fork a domain repo** to start a new one; `wss init` generates a clean
one pinned to the engine version that made it.

## Docs

[casing a site](docs/casing-a-site.md) · [new domain repo](docs/new-domain.md)
· [registry](docs/registry.md) · [capture](docs/capture.md) ·
[credentials](docs/credentials.md) · [contact](docs/contact.md) ·
[storage](docs/storage.md) · [health](docs/health.md) ·
[derive](docs/derive.md) · [cohort](docs/cohort.md) ·
[fleet workflows](docs/fleet.md)

## Tests

```bash
pip install -e ".[dev]" && pytest
python sandbox/run.py
```

The self-test drives every outcome — first_capture, unchanged, changed,
quarantined, error, skipped, plus the heartbeat — against a local fixture
server on an OS-assigned port, and proves the fleet path with three dummy
registry entries. No network leaves the machine. The sandbox generates four
months of synthetic captures with ground truth planted (an incumbent decaying,
a challenger accelerating, a plateau, one dead) and **asserts the analysis
recovers it**, so analysis is testable before real data exists.

Prior art: Singer/Meltano (config-as-fleet), dbt (derived layer), DCAT
(catalog vocabulary), SCD Type 2 (the bitemporal pattern).

## Licence

MIT. Domain repos license their *data* separately — see the two-file pattern
(`LICENSE` + `LICENSE-DATA`) in any of them.
