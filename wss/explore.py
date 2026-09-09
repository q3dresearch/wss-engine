"""Reconnaissance — case a site before writing a registry entry.

`wss doctor` checks an entry you already wrote. `wss explore` helps you write
one: it fetches politely, classifies what came back, hunts for the leads that
decide whether a source is viable at all (an underlying JSON API behind an
HTML page, a feed, conditional-request support), maps the payload onto the
observation schema, and prints a starter registry entry with gates inferred
from what it actually saw.

It writes nothing to the archive and never guesses from documentation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlsplit

from .capture import Fetcher, FetchError, contact_from_env
from .registry import Endpoint

# Keys whose names suggest they identify an entity, carry a timestamp, or
# hold a measure. Ordered: the earlier a hint matches, the better the guess.
ID_HINTS = ("id", "slug", "symbol", "ticker", "cik", "isin", "cusip", "name", "key", "code")
DATE_HINTS = ("as_of", "asof", "date", "updated", "modified", "published", "created", "time", "_at")
SKIP_METRIC_HINTS = ("id", "code", "zip", "year", "phone", "lat", "lon", "index")

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?")
API_URL_RE = re.compile(
    r"""["'(]((?:https?://[^"'()\s]+|/[^"'()\s]*)"""
    r"""(?:/api/|/v\d+/|\.json|\.csv|/graphql|format=json)[^"'()\s]*)["')]""",
    re.IGNORECASE,
)
JSON_BLOB_RE = re.compile(r"(__NEXT_DATA__|__NUXT__|__INITIAL_STATE__|window\.__[A-Z_]+)")

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/json")
MAX_LEADS = 12


@dataclass
class Report:
    url: str
    status: int | None = None
    content_type: str = ""
    size: int = 0
    etag: str = ""
    last_modified: str = ""
    kind: str = "unknown"  # json | html | csv | xml | pdf | other
    robots_allowed: bool = True
    conditional: str = "not tested"
    leads: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    entity_candidates: list[str] = field(default_factory=list)
    date_candidates: list[str] = field(default_factory=list)
    metric_candidates: list[str] = field(default_factory=list)
    record_count: int | None = None
    must_contain: str = ""


class _HTMLScan(HTMLParser):
    """Collect the handful of tags that reveal how a page is really fed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.feeds: list[tuple[str, str]] = []
        self.next_links: list[str] = []
        self.forms: list[str] = []
        self.times: list[str] = []
        self.script_srcs: list[str] = []
        self.text_len = 0
        self.script_count = 0
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link":
            rel = (a.get("rel") or "").lower()
            if "alternate" in rel and a.get("type", "") in FEED_TYPES and a.get("href"):
                self.feeds.append((a.get("type", ""), a["href"]))
            if "next" in rel and a.get("href"):
                self.next_links.append(a["href"])
        elif tag == "a" and "next" in (a.get("rel") or "").lower() and a.get("href"):
            self.next_links.append(a["href"])
        elif tag == "form":
            self.forms.append((a.get("method") or "get").upper())
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        elif tag == "script":
            self.script_count += 1
            self._in_script = True
            if a.get("src"):
                self.script_srcs.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if not self._in_script:
            self.text_len += len(data.strip())


def _walk_records(data) -> tuple[list[dict], int | None]:
    """Find the list of record dicts in a JSON payload, however it is wrapped."""
    if isinstance(data, list):
        records = [r for r in data if isinstance(r, dict)]
        return records[:3], len(data)
    if isinstance(data, dict):
        for key in ("items", "results", "data", "records", "rows", "features", "value"):
            if isinstance(data.get(key), list):
                inner = [r for r in data[key] if isinstance(r, dict)]
                return inner[:3], len(data[key])
        for value in data.values():  # any list of dicts will do
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[:3], len(value)
        return [data], None
    return [], None


def _flatten(record: dict, prefix: str = "") -> dict:
    flat: dict = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and len(flat) < 60:
            flat.update(_flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat


def _rank(keys: list[str], hints: tuple[str, ...]) -> list[str]:
    def score(key: str) -> tuple[int, int]:
        low = key.lower()
        for i, hint in enumerate(hints):
            if low == hint or low.endswith(hint) or hint in low:
                return (i, len(key))
        return (len(hints), len(key))

    return sorted([k for k in keys if score(k)[0] < len(hints)], key=score)


def _classify_json(body: bytes, report: Report) -> None:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        report.warnings.append("declared JSON but did not parse")
        return
    records, count = _walk_records(data)
    report.record_count = count
    if not records:
        report.warnings.append("no record-shaped objects found in the payload")
        return
    flat = _flatten(records[0])
    keys = list(flat)

    report.entity_candidates = _rank(keys, ID_HINTS)[:5]
    dated = _rank(keys, DATE_HINTS)
    dated += [
        k for k, v in flat.items()
        if k not in dated and isinstance(v, str) and ISO_DATE_RE.match(v)
    ]
    report.date_candidates = list(dict.fromkeys(dated))[:5]
    report.metric_candidates = [
        k for k, v in flat.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
        and not any(h in k.lower() for h in SKIP_METRIC_HINTS)
    ][:8]
    if report.metric_candidates:
        report.must_contain = report.metric_candidates[0].split(".")[-1]
    elif keys:
        report.must_contain = keys[0].split(".")[-1]


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _classify_xml(body: bytes, report: Report) -> None:
    """Atom/RSS/plain XML: find the repeating record element and its fields."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        report.warnings.append(f"XML did not parse: {exc}")
        return

    counts: dict[str, list] = {}
    for parent in root.iter():
        for child in parent:
            counts.setdefault(_strip_ns(child.tag), []).append(child)
    if not counts:
        report.warnings.append("no repeating elements found")
        return

    # A record is a *container*: leaves like <link> repeat more often but
    # carry no fields, so require sub-elements before counting popularity.
    containers = {
        tag: els for tag, els in counts.items() if any(len(list(e)) >= 2 for e in els)
    }
    record_tag, records = max((containers or counts).items(), key=lambda kv: len(kv[1]))
    report.record_count = len(records)
    report.leads.append(f"repeating element <{record_tag}> × {len(records)} — the record shape")

    fields: dict[str, str] = {}
    for field_el in records[0].iter():
        name = _strip_ns(field_el.tag)
        if name == record_tag:
            continue
        text = (field_el.text or "").strip()
        if text:
            fields.setdefault(name, text)
        for attr, value in field_el.attrib.items():
            fields.setdefault(f"{name}@{_strip_ns(attr)}", value)

    keys = list(fields)
    report.entity_candidates = _rank(keys, ID_HINTS)[:5]
    dated = _rank(keys, DATE_HINTS)
    dated += [k for k, v in fields.items() if k not in dated and ISO_DATE_RE.match(v)]
    report.date_candidates = list(dict.fromkeys(dated))[:5]
    report.metric_candidates = [
        k for k, v in fields.items()
        if re.fullmatch(r"-?\d+(\.\d+)?", v) and not any(h in k.lower() for h in SKIP_METRIC_HINTS)
    ][:8]
    report.must_contain = f"<{record_tag}"
    if not report.metric_candidates:
        report.warnings.append(
            "no numeric fields — this looks like a document/event feed. "
            "Consider schema_id: archive.v1 (capture + change detection, no metrics)"
        )


def _classify_html(body: bytes, url: str, report: Report) -> None:
    text = body.decode("utf-8", errors="replace")
    scan = _HTMLScan()
    try:
        scan.feed(text)
    except Exception:  # malformed markup is common; take what we parsed
        pass

    for ctype, href in scan.feeds[:3]:
        report.leads.append(f"feed ({ctype}): {urljoin(url, href)}")
    for href in scan.next_links[:2]:
        report.leads.append(f"pagination rel=next: {urljoin(url, href)}")

    seen: set[str] = set()
    for match in API_URL_RE.finditer(text):
        candidate = urljoin(url, match.group(1))
        if candidate not in seen and len(seen) < MAX_LEADS:
            seen.add(candidate)
            report.leads.append(f"data URL in page source: {candidate}")

    if JSON_BLOB_RE.search(text):
        report.leads.append("embedded state blob (__NEXT_DATA__ / __INITIAL_STATE__) — often the same JSON the page renders from")
    if scan.times:
        report.leads.append(f"<time datetime> present, e.g. {scan.times[0]} — candidate observed_at")
    if any(m == "POST" for m in scan.forms):
        report.warnings.append("page has POST form(s) — the engine only issues GET")
    if scan.text_len < 500 and scan.script_count > 3:
        report.warnings.append(
            f"little server-rendered text ({scan.text_len} chars) with {scan.script_count} scripts — "
            "likely JavaScript-rendered; find the API it calls, the engine does not run JS"
        )


def _suggest_min_bytes(size: int) -> int:
    half = max(1, size // 2)
    for unit in (100_000, 10_000, 1_000, 100, 10):
        if half >= unit:
            return half - (half % unit)
    return 1


def _suggest_entry(report: Report, source_id: str) -> str:
    """A starter entry, but ONLY from a fetch that actually succeeded.

    Gates are inferred from the observed body, so inferring them from an error
    page produces an entry that locks the error in. Probing NYISO's queue from
    a GitHub runner returned `202` with a zero-length body -- a bot-mitigation
    challenge -- and this function cheerfully suggested `min_bytes: 1` with
    `content_type_any: [html]`, which validates (1 >= 1) and would have captured
    the challenge page monthly, forever, reporting success.

    `status: paused` was the only thing standing between that and a live source,
    and a paused entry is one flip away from active.
    """
    if report.status != 200:
        return (
            f"# NO STARTER ENTRY: the probe returned {report.status or 'no response'}, not 200.\n"
            f"#\n"
            f"# Gates are inferred from the body that came back, so an entry built on\n"
            f"# this one would encode the error page as the expected payload. Read the\n"
            f"# raw response first and find out what the publisher is actually saying:\n"
            f"#\n"
            f"#   202 / 204 with an empty body -> bot-mitigation challenge; the runner is\n"
            f"#                                   being asked to run JavaScript\n"
            f"#   403                          -> bot wall or missing credential\n"
            f"#   404 under a valid key        -> the block is on the IP, not the auth\n"
            f"#                                   (accessdata.fda.gov does exactly this)\n"
            f"#   429 / 503                    -> throttle. Retry with a delay; this is\n"
            f"#                                   NOT evidence the source is unusable\n"
            f"#\n"
            f"# If it answers from your laptop and not here, that is a fact about WHERE\n"
            f"# the request comes from. Capture it locally or not at all."
        )
    ext_type = {"json": "json", "csv": "csv", "html": "html", "xml": "xml"}.get(report.kind, report.kind)
    lines = [
        f"source_id: {source_id}",
        "status: paused          # doctor it, read the raw response, then flip to active",
        "cadence: weekly         # match the decision cycle, not the data's volatility",
        f"schema_id: {source_id.split('.')[0]}.v1",
        "",
        "publisher: TODO",
        "publisher_tier: first_party",
        "# TRUE only if you cannot reconstruct this as of an arbitrary past date.",
        "# Documents (filings, opinions) are usually archived — cite those instead.",
        "# State (holdings, statuses, listings, prices) is almost never archived.",
        "destroys_own_history: true",
        'licence: "TODO — the publisher\'s terms"',
        "personal_data: none",
        "storage: git",
        "",
        "endpoints:",
        f'  - url: "{report.url}"',
        "    delay_seconds: 2",
        "",
        "gates:",
        "  expect_status: 200",
        f"  min_bytes: {_suggest_min_bytes(report.size)}",
        f"  content_type_any: [{ext_type}]",
    ]
    if report.must_contain:
        lines.append(f'  must_contain: ["{report.must_contain}"]')
    lines.append('  must_not_contain: ["Access Denied", "rate limit"]')
    lines.append("  max_shrink_pct: 50")
    return "\n".join(lines)


def explore(
    url: str,
    source_id: str = "publisher.domain.series",
    log: Callable[[str], None] = print,
) -> int:
    """Case one URL. Returns 0 if it looks capturable, 1 if blocked."""
    contact = contact_from_env()
    fetcher = Fetcher(contact)
    report = Report(url=url)
    endpoint = Endpoint(url=url, delay_seconds=1.0)

    allowed, robots_warning = fetcher.robots_allows(url, endpoint.delay_seconds)
    report.robots_allowed = allowed
    if robots_warning:
        report.warnings.append(f"robots.txt: {robots_warning} (proceeding)")
    if not allowed:
        log(f"robots.txt DISALLOWS {url}\nThis source cannot be captured. Stop here.")
        return 1

    try:
        res = fetcher.fetch(endpoint)
    except FetchError as exc:
        log(f"fetch failed: {exc.reason}")
        return 1

    report.status = res.status
    report.content_type = (res.content_type or "").split(";")[0].strip().lower()
    report.size = len(res.body)
    report.etag = res.etag
    report.last_modified = res.last_modified

    ct = report.content_type
    body_head = res.body[:200].lstrip()
    if "json" in ct or body_head[:1] in (b"{", b"["):
        report.kind = "json"
    elif "csv" in ct:
        report.kind = "csv"
    elif "xml" in ct:
        report.kind = "xml"
    elif "html" in ct:
        report.kind = "html"
    elif "pdf" in ct:
        report.kind = "pdf"
    else:
        report.kind = ct or "other"

    if report.kind == "json":
        _classify_json(res.body, report)
    elif report.kind == "xml":
        _classify_xml(res.body, report)
    elif report.kind == "html":
        _classify_html(res.body, url, report)
    elif report.kind == "csv":
        first = res.body[:2000].decode("utf-8", errors="replace").splitlines()
        if first:
            cols = [c.strip().strip('"') for c in first[0].split(",")][:12]
            report.entity_candidates = _rank(cols, ID_HINTS)[:5]
            report.date_candidates = _rank(cols, DATE_HINTS)[:5]
            report.metric_candidates = [c for c in cols if c not in report.entity_candidates][:8]
            report.must_contain = cols[0] if cols else ""
            report.leads.append(f"CSV header: {', '.join(cols)}")
    elif report.kind == "pdf":
        report.warnings.append("PDF — archive it and detect silent revisions; parsing to observations is a separate problem")

    # Conditional requests decide whether daily polling is cheap for both sides.
    if res.etag or res.last_modified:
        try:
            again = fetcher.fetch(endpoint, etag=res.etag, last_modified=res.last_modified)
            report.conditional = "yes — 304 on revalidation" if again.status == 304 else f"no — {again.status} with a full body"
        except FetchError:
            report.conditional = "unknown (revalidation request failed)"
    else:
        report.conditional = "no ETag/Last-Modified offered"

    # sitemap presence hints at whether the publisher exposes an archive
    parts = urlsplit(url)
    sitemap = f"{parts.scheme}://{parts.netloc}/sitemap.xml"
    try:
        sm = fetcher.session.get(sitemap, timeout=10)
        if sm.status_code == 200 and b"<" in sm.content[:200]:
            report.leads.append(f"sitemap.xml exists: {sitemap} — check whether it lists dated archive pages")
    except Exception:
        pass

    _print(report, source_id, log)
    return 0


def _print(report: Report, source_id: str, log: Callable[[str], None]) -> None:
    def section(title: str) -> None:
        log("")
        log(title)
        log("─" * len(title))

    section(f"RESPONSE  {report.url}")
    log(f"  status       : {report.status}")
    log(f"  content-type : {report.content_type or '(none)'}  → treated as {report.kind}")
    log(f"  size         : {report.size:,} bytes")
    log(f"  revalidation : {report.conditional}")
    if report.record_count is not None:
        log(f"  records      : {report.record_count:,}")

    if report.entity_candidates or report.date_candidates or report.metric_candidates:
        section("MAPS ONTO THE OBSERVATION SCHEMA AS")
        log(f"  entity_id  ← {', '.join(report.entity_candidates) or '(none obvious — look at the raw payload)'}")
        log(f"  observed_at ← {', '.join(report.date_candidates) or '(none — capture time will be used)'}")
        log(f"  metric/value ← {', '.join(report.metric_candidates) or '(none numeric — may be an archive-only source)'}")

    if report.leads:
        section("LEADS")
        for lead in report.leads:
            log(f"  • {lead}")

    if report.warnings:
        section("WARNINGS")
        for warning in report.warnings:
            log(f"  ! {warning}")

    section("THE QUESTION ONLY YOU CAN ANSWER")
    log("  Can you ask this publisher 'what did this look like on <a past date>'")
    log("  and get an answer?")
    log("    yes → they archive it. Do not capture; cite them.")
    log("    no  → the history is being destroyed. Capture it.")

    section("STARTER REGISTRY ENTRY")
    for line in _suggest_entry(report, source_id).splitlines():
        log(f"  {line}")
    log("")
    log(f"  Save as registry/{source_id}.yml, then: wss doctor {source_id}")
