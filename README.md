# wss — web snapshots

The engine behind `wss-*` data repos: **scheduled GitHub Actions that scrape
or parse any observable web page or API, every day, forever.**

It archives responses **verbatim**, keeps an append-only manifest as the
provenance record, and derives point-in-time observation tables from that
archive — never from the live web. ("Snapshots" as in captured bytes, not
screenshots: HTML, JSON, CSV, whatever the page returns.)

The engine holds **no data, ever**. A *domain repo* (`wss-hugging-face`,
`wss-arxiv`, …) holds a registry of sources, runs this CLI from a handful of
scheduled workflows, and commits what comes back. The design target is 1,000+
concurrent collections managed by one person, so the binding constraint is
human attention — every design decision serves that.

```bash
pip install "wss @ git+https://github.com/<owner>/wss-engine.git@v0.3.0"
wss init ../wss-yoursite --owner <owner>    # a new data repo, ready to run
```

## The rule everything hangs on

**There is never a workflow per source.** A registry drives the fleet: a few
scheduled workflows read it, shard the active sources across a job matrix,
and each job walks its slice.

- Adding a collection is **one new file** (`registry/<source_id>.yml`).
- Removing one is a status change.
- No infrastructure is touched either way.

If a change requires editing a workflow to add a data source, the design is
wrong.

## Install

```
pip install "wss @ git+https://github.com/neldivad/wss-engine.git@v0.3.0"
# object-storage backend (Cloudflare R2 / S3):
pip install "wss[object] @ git+https://github.com/neldivad/wss-engine.git@v0.3.0"
```

## CLI — this is the whole interface

```
wss init ../wss-arxiv --owner me      # scaffold a new domain repo
wss validate                          # registry schema check; CI gate
wss plan --cadence daily --shards 20  # JSON shard array for the Actions matrix
wss capture --cadence daily --shard 3/20
wss health                            # rebuild health table, apply auto-disable
wss derive --since 2026-08            # raw → observation tables
wss doctor <source_id>                # dry-run one source, print raw response
```

All commands take `--root` (default: current directory) pointing at the data
root — the domain repo checkout.

## The capture contract — non-negotiable

1. Raw response bytes archived verbatim; nothing parsed at capture time.
2. Every fetch appends a manifest row, including unchanged ones — dedupe
   skips the file write, never the observation.
3. Failed gate → quarantine; bad responses never enter the archive.
4. Failures are loud: non-zero exit, red build.
5. Identifiable user-agent (`WSS_CONTACT`), robots.txt honoured,
   per-host delay, 3 retries with exponential backoff.

Details: [docs/capture.md](docs/capture.md).

## Layout

```
wss/
├── registry.py    load, validate, select active, deterministic sharding
├── capture.py     fetch → gate → hash → dedupe → write → manifest; doctor
├── gates.py       validation rules
├── storage.py     LocalGitStore | ObjectStore — one interface, same paths
├── manifest.py    append-only fetch log; the provenance record
├── health.py      health table from manifest; auto-disable
├── cohort.py      frozen cohort selection (generic, not publisher-specific)
├── derive.py      raw → long-format observation tables
├── csvio.py       deterministic CSV conventions
├── init.py        scaffold a domain repo from templates/
└── cli.py
```

Docs: [new domain repo](docs/new-domain.md) · [registry](docs/registry.md) ·
[capture](docs/capture.md) · [storage](docs/storage.md) ·
[health](docs/health.md) · [derive](docs/derive.md) ·
[cohort](docs/cohort.md) · [fleet workflows](docs/fleet.md)

## Starting a new domain repo

`wss init <dir> --owner <gh-owner>` writes a complete, immediately
valid domain repo — workflows, licences, `.gitattributes` (before any CSV
exists), an example registry entry and parser. **Never fork an existing
domain repo**; forks inherit the wrong parsers and drift from the template.
See [docs/new-domain.md](docs/new-domain.md).

Examples: a [registry entry](examples/registry/example.web.stats.yml), a
[parser](examples/parsers/example_parser.py), and the canonical
[capture workflow](examples/workflows/capture-daily.yml) a domain repo copies.

## Sandbox

`python sandbox/run.py` generates ~4 months of synthetic captures in the real
archive shape with ground truth planted — an incumbent decaying, a challenger
accelerating, a plateau, one faded and dead — then derives, runs
`sandbox/analysis.sql`, and **asserts the analysis recovers the planted
truth**. Analysis gets built and tested before a single real byte exists.

## Tests

```
pip install -e ".[dev]"
pytest
```

The self-test (`tests/test_selftest.py`) drives every outcome —
first_capture, unchanged, changed, quarantined, error, skipped, plus the
heartbeat — against a local fixture server on an OS-assigned port, and proves
the fleet path (plan → deterministic shards → capture) with three dummy
registry entries. No network leaves the machine.

## Prior art this borrows from

Singer taps/targets and Meltano (config-as-fleet), dbt (derived layer), DCAT
(catalog vocabulary, for the later serving layer), SCD Type 2 (the formal
name for the bitemporal history pattern).

## Licence

MIT (engine code). Domain repos license their *data* separately —
see the two-file pattern (`LICENSE` + `LICENSE-DATA`) in any domain repo.
