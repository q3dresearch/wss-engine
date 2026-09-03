# Casing a site

What to do *before* a registry entry exists. `wss doctor` checks an entry you
already wrote; this is how you decide whether to write one at all.

```bash
export WSS_CONTACT="you@example.com"
wss explore "https://example.gov/some/listing" --source-id example.gov.listing
```

`explore` writes nothing. It fetches once (politely, honouring robots.txt),
classifies what came back, maps the payload onto the observation schema,
hunts for leads, and prints a starter registry entry with gates inferred from
what it actually saw.

## The six questions, in order

### 1. Is it a document or a state?

This is the question that decides whether the source is worth capturing at
all, and it is sharper than "do they have an archive".

- **Document** — immutable once issued: a filing, an opinion, a press
  release. Institutions archive these well. **Cite theirs; don't recapture.**
- **State** — the current value of something mutable: holdings, a
  registration status, a docket posture, a job posting, a price. **Almost
  nothing archives state**, including institutions that consider themselves
  archives.

The practical test: *can you ask "what did this look like on 14 March 2024"
and get an answer?* If not, the history is being destroyed and is yours to
preserve — that is `destroys_own_history: true`. If yes, the registry will
reject an active entry, and it is right to.

The subtle case: a publisher can archive its documents perfectly while
destroying the *index* of them. A company's filings live forever on EDGAR;
what its investor-relations page listed on a given Tuesday does not. Capture
the index, not the documents.

One nuance for documents: "immutable" is sometimes a lie. Slip opinions get
revised after publication without notice. If detecting that is the point,
capture with `schema_id: archive.v1` — see below.

### 2. Can a plain GET reach it?

The engine issues plain HTTP GETs. No JavaScript, no browser, no login, no
form posts. `explore` warns when a page looks JavaScript-rendered ("little
server-rendered text with N scripts") or carries POST forms.

That is rarely the end of the road: a JS-rendered page is fetching its data
from somewhere, and `explore` reports the API URLs and embedded state blobs
(`__NEXT_DATA__`, `__INITIAL_STATE__`) it finds in the page source. Point the
registry entry at that endpoint instead — it is usually cleaner JSON than the
HTML would have been. Your browser's network tab finds the rest.

### 3. What is the natural cadence?

**Match the decision cycle, not the data's volatility.** If the series informs
a quarterly decision, weekly gives twelve observations per decision — plenty,
and a tenth of the data to manage. Daily is for trade-grade signals, which are
a different game (zero-sum, latency-sensitive) than decision-grade ones.

The one thing a slow cadence costs is *transitions*: sample faster than the
average lifespan of the thing you are watching, or you will see states without
the changes between them. If births and deaths are the point, that constraint
binds.

`explore` reports whether the server supports revalidation (ETag /
Last-Modified). When it does, an unchanged fetch costs a 304 and no body —
polling is cheap for both sides.

### 4. Does the payload carry its own "as of" date?

If yes, the parser should set `observed_at` from it. This matters most where
the publication lag is real: an ETF holdings file is usually as-of T-1, and a
filing has a period, a filing date, and possibly an amendment that supersedes
it. Without the payload's own date you can never answer "what was known on
date X", which is the only question point-in-time data exists to answer.

`explore` lists the date-shaped fields it found under `observed_at ←`.

### 5. What does a *broken* response look like?

This is where the real thought goes — gates are more important than parsers.
A parser turns JSON into rows and is mechanical; gates encode your judgment
about what failure looks like, and they protect the archive for years.

- `min_bytes` — an error page is small. `explore` suggests roughly half of
  what it observed.
- `must_contain` — a token that is always present when the response is real
  (a column header, a key name).
- `must_not_contain` — the block page ("Access Denied", "rate limit").
- `max_shrink_pct` — the subtle one. A fund's holdings do not halve
  overnight; if the bytes did, that is an outage wearing a `200 OK`.
- `expect_status: [200, 404]` — when a 404 is *data* (a tracked entity died)
  rather than a failure.

### 6. Is someone already selling it?

Ask this **before** probing, not after. It is cheap and it is the question
that has killed the most candidates.

Perishable state that is commercially valuable is usually already held by
someone whose access beats an HTTP request — merchant feeds, an affiliate
API, a data partnership. You will not out-collect them by being diligent.

| candidate | occupant | the advantage you cannot match |
| --- | --- | --- |
| datacenter GPU pricing | SemiAnalysis | proprietary channel checks |
| browser extension listings | Chrome-Stats | daily snapshots, removed listings, CSV export |
| PC component pricing | PCPartPicker | affiliate/merchant feeds, a decade of accumulation |

Three in a row, all the same shape, so state it as a rule:

**If the perishable state is a price, assume it is taken.** Check before you
probe. Prices are the most commercially valuable state there is, therefore
the most contested, therefore the worst ground for an unfunded fleet.

The counter-rule is where the wins came from: **capture what is valuable but
not yet monetisable.** Nobody sells "which arXiv papers ship into production"
or "what share of proposed generating capacity dies before it is built",
which is exactly why those were available.

When a price is genuinely the thing you want, look for its **physical
antecedent** instead — the permit, the licence, the quota, the berth, the
claim. Authorities publish those, rarely archive them, and have no commercial
interest in the series.

## Capture-only sources

Some sources exist to be archived and watched, not measured — court opinions,
IR decks, terms of service. Use the built-in schema:

```yaml
schema_id: archive.v1
```

No parser needed. The raw bytes are archived verbatim, the manifest records
`changed` vs `unchanged` on every look (so a silent revision is caught and
both versions are kept), and derive emits one `content_bytes` observation per
capture so the document stays queryable in the observation table — a size
shift between two dates is a revision, with both versions already on disk.

## Then

1. Save the suggested entry as `registry/<source_id>.yml`, leaving it
   `paused`, and fill in the TODOs (publisher, licence, notes).
2. `wss doctor <source_id>` — **read the raw response**. Never write a parser
   from documentation.
3. Write the parser, or set `archive.v1`.
4. Flip `status: active`, commit, add a Coverage row to the repo's README.

If this is a new publisher rather than a new source for an existing repo,
start with `wss init` — see [new-domain.md](new-domain.md).
