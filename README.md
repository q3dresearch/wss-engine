<h1 align="center">wss-engine</h1>

<p align="center">
  <strong>Scheduled GitHub Actions that capture any web page or API — every day, forever</strong>
</p>

<div align="center">

  <a href="https://github.com/q3dresearch/wss-engine/actions/workflows/test.yml"><img alt="tests status" src="https://img.shields.io/github/actions/workflow/status/q3dresearch/wss-engine/test.yml?label=tests&style=flat-square"></a>
  <a href="https://github.com/q3dresearch/wss-engine/blob/main/LICENSE"><img alt="licence" src="https://img.shields.io/github/license/q3dresearch/wss-engine?style=flat-square"></a>
  <a href="https://github.com/q3dresearch/wss-engine"><img alt="stars" src="https://img.shields.io/github/stars/q3dresearch/wss-engine?style=social"></a>

</div>

<p align="center">
  <sub>fleet: <strong>engine</strong> · <a href="https://github.com/q3dresearch/wss-hugging-face">hugging face</a> · <a href="https://github.com/q3dresearch/wss-openrouter">openrouter</a> · <a href="https://github.com/q3dresearch/wss-cloud-footprint">cloud footprint</a> · <a href="https://github.com/q3dresearch/wss-mining-pipeline">mining</a> · <a href="https://github.com/q3dresearch/wss-forest-harvest">forest</a> · <a href="https://github.com/q3dresearch/wss-food-trace">food</a></sub>
</p>

It archives responses **verbatim**, keeps an append-only manifest as the
provenance record, and derives point-in-time observation tables from that
archive — never from the live web. ("Snapshots" as in captured bytes, not
screenshots: HTML, JSON, CSV, whatever the page returns.)

The engine holds **no data, ever**. A *domain repo*
([wss-hugging-face](https://github.com/q3dresearch/wss-hugging-face),
[wss-openrouter](https://github.com/q3dresearch/wss-openrouter),
[wss-cloud-footprint](https://github.com/q3dresearch/wss-cloud-footprint)) holds a
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
pip install "wss @ git+https://github.com/q3dresearch/wss-engine.git@v0.6.2"

wss explore <url>                     # case a site before writing anything
wss init ../wss-yoursite --owner me   # scaffold a domain repo
wss validate                          # registry schema check; CI gate
wss plan --cadence daily --shards 20  # JSON shard array for the Actions matrix
wss capture --cadence daily --shard 3/20
wss health                            # health table, auto-disable
wss derive                            # raw → observation tables
wss doctor <source_id>                # dry-run one source, print raw bytes
wss sources                           # write SOURCES.md: every URL, licence, last capture
wss datapage                          # write docs/index.html with schema.org Dataset markup
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

## Publishing a dataset so it can be found

A repository nobody links to is invisible, and a README cannot fix it:
**GitHub sanitises `<script>` out of rendered markdown**, and Google Dataset
Search discovers datasets *only* through a `schema.org/Dataset` JSON-LD block.
`wss datapage` writes that block into `docs/index.html`, generated from
`CITATION.cff`, the registry and the manifest — so it is a build artifact, not
a page to maintain.

Two steps, once per repo.

```bash
wss datapage --repo-url https://github.com/<owner>/<repo>
git add CITATION.cff docs/ && git commit && git push
```

1. **Write the abstract.** `CITATION.cff` ships with a `TODO:` placeholder and
   `datapage` refuses to publish it, falling back to generic text. The abstract
   is the field that decides whether anyone cites you — say what is captured
   and why the history would otherwise be lost. Replace the scaffold keywords
   too: name the actual publishers and places, because retrieval grounds on
   entities and `open-data, dataset` names none.
2. **Settings → Pages → deploy from `main`, `/docs`.** A repo file cannot be
   read by a dataset index; only the served page can. `datapage` writes
   `docs/.nojekyll` so prose kept alongside the page cannot fail the build.

After that it maintains itself: `derive` regenerates the page on every
scheduled run, so the coverage dates track the manifest instead of the last
time someone remembered.

### On DOIs, and why this stops at two steps

The obvious third step is Zenodo: connect the webhook, tag a release, paste the
**concept DOI** into `CITATION.cff` as `doi:` (never the version DOI — the
concept one always resolves to the latest release), then fix the resource type
by hand, because Zenodo's GitHub integration types every record `Software` and
ignores `type: dataset` in the file.

**Done once here, deliberately not repeated.** It is a real archival service
and the DOI is real, but it answers a question these repos were not asking. A
DOI buys citability in venues that require one and a copy that outlives the
host; it buys **no discovery** that the served page does not already provide,
and it adds a release ritual to every repo. The page is the load-bearing part —
Dataset Search reads JSON-LD, not DOIs.

So: mint one if a specific venue asks for it. Otherwise the page cites the
repository and the access date, which is a citable handle that costs nothing to
keep current. Two caveats if you do it: connect the webhook **before** tagging,
because Zenodo only archives releases created after it exists; and a published
record is permanent and cannot be deleted, so check `personal_data` across the
registry first.

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
