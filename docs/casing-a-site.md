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

## The questions, in order

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

### 1b. Your prior is wrong in a known direction

Measured over 28 candidates this umbrella has screened (`catalogue.csv`):

| predicted | count | turned out to be that |
| --- | --- | --- |
| **wss** | 21 | **8 — 38%** |
| larder / sift | 7 | **7 — 100%** |

Every miss runs one way. Nothing predicted larder or sift has ever turned out to
be wss; thirteen things predicted wss were not. **We over-call wss**, and the
question that flips it is almost always the same one: *does the publisher keep
its own history?*

Two recent examples, both of which looked perishable and were not:

- **NRC reactor status** ships as `PowerReactorStatusForLast365Days.txt`. The
  filename says perishable. NRC publishes per-year archives back to 2005.
- **CAISO's interconnection queue** publishes 1,771 *withdrawn* projects — the
  DECRS shape exactly — until you read the column list and find `WITHDRAWNDATE`
  and `WITHDRAW_REASON` already in it.

So: **treat wss as the conclusion of last resort, not the hypothesis.** Spend the
first five minutes trying to find the archive rather than confirming its absence,
and check the *column list*, not a grep over it — a search summary claimed DECRS
carried NDC and it does not; a 60-character grep said CAISO had no withdrawal
date and it does.

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

**Default monthly. Weekly is the floor, quarterly for slow or meta-level
processes, and daily essentially never.**

Match the decision cycle, not the data's volatility — those come apart, and
volatility is the tempting one. `wss-hugging-face` captures daily and finds
changed content **88% of the time**, so the data really does move every day.
Nobody makes a daily decision on it: every recipe reading it asks a monthly or
quarterly question. What the daily cadence buys is 4.7 MB/day of storage and a
1 GB repo in seven months; at weekly the same series is ~0.7 MB/day and four
years.

A missed wss capture is unrecoverable, so the instinct is to sample fast. But
the loss from sampling slowly is a *transition you did not see*, and that is
bounded by the lifespan of the thing being watched — not by how often its bytes
change. Pick the cadence from how long a state persists, not from how often the
page differs.

If the series informs
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

### 3b. What is the base rate, and did most of it already happen?

Cheap, decisive, and the one most often skipped: **before designing a capture,
measure how often the thing you would record actually changes — and whether the
change you care about has already finished.**

Most sources expose enough to answer this from a *single* fetch. A field like
`last_updated` gives you the whole distribution at once, with no archive and no
waiting.

The worked example that killed a candidate. Public open-data catalogues expose
`data_updated_at` per dataset, and the plan was to watch for schema drift and
abandonment. One sample of 4,000 datasets:

| | |
|---|---|
| no data update in over 3 years | **46.4%** |
| updated in the last 30 days | 20.5% |
| median staleness | **2.2 years** |

Half the catalogue was already abandoned, at a median of over two years. The
process was not *upcoming*, it was **largely complete** — an archive begun that
day would record the tail and miss the event.

Two rules come out of it:

- **Arriving late is a real failure mode, and it is invisible from the source
  itself.** A page that looks live and returns 200 tells you nothing about
  whether the interesting transitions already occurred.
- **If one snapshot answers the question, it is `larder/`, not `wss/`.**
  "Half of open data is abandoned" needs no history at all. History buys only
  two things: **disappearance**, and **the date a change happened**. If neither
  is the question, stop here.

### 3c. Is it a counted event or a survey of institutions?

3b measures staleness that already happened. This one predicts staleness that
has not happened yet, from the shape of the thing being measured.

> **A counted event is a durable capture target. A survey of institutions is
> not.**

Counts arrive as a by-product of operations — deaths get registered, cases get
notified, doses get administered — so they keep arriving whether or not anyone
is funding a data programme. An inventory of what institutions *have* requires
someone to commission a round of asking, and when that funding stops the
indicator freezes at whatever the last round said. It stays in the catalogue
looking live.

Measured on WHO's Global Health Observatory, 48 active indicators against 199
untouched for over four years:

| shape | active | dormant >4y |
|---|---|---|
| counted / measured | **56%** | 19% |
| policy inventory | 4% | **17%** |

The dormant names say it plainly — *"Existence of operational policy/strategy/
action plan for hearing health"*, *"National treatment policy for alcohol use
disorders"*, *"Health warning labels on alcohol containers"*. Each needs 194
governments asked about their own laws. The active ones are *estimated malaria
cases*, *TB treatment coverage*, *adolescent birth rate*, *pharmacists per
10,000*.

**Three ways an indicator dies, and only the first is about the subject:**

- **Survey burden** — nobody funded the next round. The common case.
- **Model obsolescence** — attributable-burden figures (*"Deaths attributable
  to the environment"*) need a methodology refresh, which is a research project
  rather than a pipeline.
- **Succession** — it moved custodian. *"Age-standardized death rates, colon and
  rectum cancers"* is dormant 13 years at WHO because cancer registration went
  to IARC. It did not die, it emigrated — and a capture pointed at the old home
  archives a fossil while the live series runs elsewhere.

**So check the shape before the cadence.** A source can pass 3b today — recently
updated, healthy — and still be an inventory whose next round is unfunded. The
question is not when it last moved, but whether anything makes it move again.

### 3d. Does it work from a runner, or only from your laptop?

The variable that decides whether a capture works is usually **where the request
comes from**, not what it carries. Publishers rate-limit and bot-score by IP,
and a GitHub runner's IP is shared with an enormous number of other users, so it
arrives pre-judged in a way your home connection never does.

Two sources in this fleet make the point from opposite directions:

- `peeringdb.facilities.geo` failed four times with
  `retries_exhausted_status_429` while anonymous. A key was added, and the next
  CI run captured cleanly. **The credential was the fix**, because it moves you
  off the shared anonymous per-IP quota and onto your own.
- `accessdata.fda.gov` answers GitHub runners with a 404 apology page **under a
  valid API key**. The credential buys nothing, because the block is on the
  runner, not on the request.

So the test that settles it is not "does this URL answer?" — from your laptop it
almost always does. It is **"does this answer from CI?"**, and the only way to
know is to run it there once before trusting the entry. A source that works
locally and fails in Actions is the normal case, not the surprising one.

A credential is a real obligation — it expires, it has to be rotated, and its
absence surfaces as `missing_credential` rather than as anything self-healing.
Of this fleet's 14 recorded failures on keyed sources, seven are exactly that:
the secret had not been added yet. That is a one-time onboarding cost, paid
during casing. Whether it becomes a recurring cost depends on expiry policy, and
this fleet is too young to have measured that — do not assume either way.

What to write down while casing: which of the two situations you are in. "Needs
a key, verified it captures from CI" and "needs a key and *still* cannot be
captured from CI" look identical in the registry and could not be more
different in what they cost.

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

`min_bytes` and `content_type_any` are **required** on every source, and the
engine refuses a registry entry that omits either. The reason is the soft-404:
a publisher that answers a missing file with `200 OK` and an HTML error page
defeats every other gate at once — the status is fine, the body is real bytes,
the parser may even survive it, and the capture lands in the manifest as a
success.

`eia.gov` does this. A missing month of EIA-860M returns **55,723 bytes of
HTML — byte-identical across two different missing months** — where the real
workbook is about 14 MB. Either gate catches it; a source that set neither
would have archived the error page monthly and reported green.

The tell generalises: **identical byte counts across URLs that should differ**
means one shell is answering for all of them. It is the same observation as
the SPA test, seen from the other end — there, unrelated paths return the same
size because the app shell is the response; here, missing months do.

### 5b. Does the same request return the same bytes?

Fetch the identical URL three times and compare hashes. If they differ while
the data has not changed, **dedup is broken and you will not notice**: every
capture writes a fresh copy of the archive, storage grows without bound, and
`outcome: changed` stops meaning anything.

The usual cause is row order. Two ArcGIS services, same query shape, opposite
behaviour:

| service | three identical fetches |
| --- | --- |
| CAL FIRE (hosted feature service) | one hash — stable by luck, not by contract |
| Skogsstyrelsen (ArcGIS Server) | **three different hashes**, identical byte count |

**Rendered HTML has a second version of this.** Drupal stamps a random
`js-view-dom-id-<hash>` into every view container on every request, and other
CMSs emit build ids, nonces or render timestamps the same way. Identical data,
identical byte count, different sha — and it will not show up in a diff of the
*visible* text, only in a diff of the markup. FDA's animal-feed consultation
page does exactly this, four times per render.

For that case use `dedupe_ignore` in the registry: a list of regexes stripped
from the body **only** to decide changed vs unchanged. The archived bytes and
`content_sha256` stay verbatim, so provenance is untouched.

```yaml
dedupe_ignore:
  - "js-view-dom-id-[0-9a-f]+"
```

The fix for ordering is to make the order explicit — `orderByFields` on ArcGIS, `ORDER BY`
or a sort parameter elsewhere — and to sort on a **business key** rather than a
surrogate id, for the same reason partitions do.

Stability by luck is worth writing down as luck. A publisher can change its
default ordering without announcing anything, and nothing else in the pipeline
would flag it.

### 5c. Is the short list just page one?

A view showing only recent rows is indistinguishable from a rolling window
until you check. FDA's final-rules inventory returns **51 rows back to 2014**
and describes itself as covering everything "since 1994" — the gap reads as
evidence of deletion. Add `showAll=true` and it returns **609 rows back to
1975**. Nothing perishes; it paginates.

Look for a pagination or show-all parameter before concluding a source
destroys its history. The same test against the petitions queue on the same
host returns 32 rows either way, which is what confirms *that* one is genuinely
only the open queue. Two sources, one server, identical appearance, opposite
verdicts.

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

The questions above decide whether a source *can* be captured. They do not
decide whether it is **worth** capturing, and that is a separate loop: probe the
data shape, write the research questions down *before* charting anything, try to
answer them, and note what the data cannot reach. Most candidates die at that
third step, which is the cheapest place for them to die.

A source that passes every question here and answers no interesting question is
still a source you should not build.

Once it has earned a build:


1. Save the suggested entry as `registry/<source_id>.yml`, leaving it
   `paused`, and fill in the TODOs (publisher, licence, notes).
2. `wss doctor <source_id>` — **read the raw response**. Never write a parser
   from documentation.
3. Write the parser, or set `archive.v1`.
4. Flip `status: active`, commit, add a Coverage row to the repo's README.

If this is a new publisher rather than a new source for an existing repo,
start with `wss init` — see [new-domain.md](new-domain.md).
