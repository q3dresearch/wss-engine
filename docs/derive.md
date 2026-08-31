# Derive — raw → observation tables

Long format, not wide. Wide is nicer to query but cannot be uniform across
heterogeneous domains; build pivot views downstream instead.

```
series_id, entity_id, observed_at, captured_at,
metric, value, unit, source_id, raw_ref, parser_version
```

`observed_at` = when the fact was true. `captured_at` = when we saw it. That
separation *is* point-in-time integrity (formally: bitemporal modelling /
SCD Type 2). An `unchanged` manifest row still yields observations — dated by
its own fetch time, backed by the same `raw_ref`.

## Parsers are plugins

A parser is registered per `schema_id`:

```python
from wss import derive

PARSER_VERSION = "1"

def parse(body: bytes, ctx: derive.ParseContext):
    for item in json.loads(body):
        yield derive.Observation(entity_id=item["id"], metric="downloads_30d",
                                 value=item["downloads"], unit="count")

derive.register("adoption.v1", parse, PARSER_VERSION)
```

Run with `wss derive --parsers parsers.adoption_v1` (repeatable; the
data root is put on `sys.path`, so domain repos keep parsers in a `parsers/`
package). A schema with manifest rows but no registered parser fails loudly.

Rules:

- A parser must be a **pure function of the response bytes**. A parser bug is
  fixed by bumping `PARSER_VERSION`, re-running derive, and re-parsing the
  archive — never by re-fetching.
- Leave `Observation.observed_at` as `None` unless the payload itself carries
  the observation time; derive fills it with each manifest row's fetch time.
- `series_id` defaults to the `source_id`.

## Determinism

Output partitions are `derived/observations/<YYYY-MM>.csv` (by `observed_at`
month): sorted rows, stable column order, LF endings, trailing newline,
locale-free values. **Rebuilding from the same archive is byte-identical**,
and CI enforces it — a diff means a hand-edited CSV or a nondeterministic
parser, both of which should turn the build red. Never hand-edit a derived
CSV; fix the parser and rebuild.

`--since 2026-08` limits the rebuild to partitions from that month on. A full
rebuild (no `--since`) also prunes stale partitions.
