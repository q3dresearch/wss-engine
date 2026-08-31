"""Capture kernel: fetch → gate → hash → dedupe → write → manifest.

The contract:
  1. Raw response bytes are archived verbatim. Nothing is parsed here.
  2. Every fetch appends a manifest row, including unchanged ones.
  3. A failed gate quarantines the response; it never enters the archive.
  4. Failures are loud: any error/quarantined outcome makes the run red.
  5. Identifiable user-agent (SNAPSHOTTER_CONTACT), robots.txt honoured,
     per-host delay, 3 retries with exponential backoff.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.robotparser
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from . import __version__, manifest, storage
from .gates import run_gates
from .registry import Endpoint, Source, load_registry, parse_shard, select, shard_members

MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
ROBOTS_AGENT = "snapshotter"


class ContactMissing(RuntimeError):
    pass


class FetchError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def contact_from_env() -> str:
    contact = os.environ.get("SNAPSHOTTER_CONTACT", "").strip()
    if not contact:
        raise ContactMissing(
            "SNAPSHOTTER_CONTACT is not set. Captures run with an identifiable "
            "user-agent; set it to an email a publisher can reach you at."
        )
    return contact


def user_agent(contact: str) -> str:
    return f"snapshotter/{__version__} (contact: {contact})"


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass
class FetchResult:
    status: int
    body: bytes
    content_type: str
    etag: str
    last_modified: str


class Fetcher:
    """Polite HTTP client: per-host delay, robots cache, retries with backoff."""

    def __init__(self, contact: str, session: requests.Session | None = None, retry_base: float | None = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent(contact)
        if retry_base is None:
            retry_base = float(os.environ.get("SNAPSHOTTER_RETRY_BASE", "2"))
        self.retry_base = retry_base
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, tuple[urllib.robotparser.RobotFileParser | None, str]] = {}

    def _polite_wait(self, host: str, delay: float) -> None:
        last = self._last_hit.get(host)
        if last is not None and delay > 0:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)

    def _mark(self, host: str) -> None:
        self._last_hit[host] = time.monotonic()

    def robots_allows(self, url: str, delay: float) -> tuple[bool, str]:
        """(allowed, warning). Unreachable robots.txt allows with a warning."""
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc
        if host not in self._robots:
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                self._polite_wait(host, delay)
                resp = self.session.get(robots_url, timeout=10)
                self._mark(host)
                if resp.status_code == 200:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(resp.text.splitlines())
                    self._robots[host] = (rp, "")
                elif 400 <= resp.status_code < 500:
                    self._robots[host] = (None, "")  # no robots.txt → allowed
                else:
                    self._robots[host] = (None, "robots_fetch_failed")
            except requests.RequestException:
                self._robots[host] = (None, "robots_fetch_failed")
        rp, warning = self._robots[host]
        if rp is None:
            return True, warning
        return rp.can_fetch(ROBOTS_AGENT, url), warning

    def fetch(self, endpoint: Endpoint, etag: str = "", last_modified: str = "") -> FetchResult:
        host = urllib.parse.urlsplit(endpoint.url).netloc
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        last_reason = "fetch_failed_unknown"
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                time.sleep(self.retry_base * (2 ** (attempt - 1)))
            self._polite_wait(host, endpoint.delay_seconds)
            try:
                resp = self.session.get(endpoint.url, headers=headers, timeout=endpoint.timeout_seconds)
            except requests.RequestException as exc:
                self._mark(host)
                last_reason = f"fetch_failed_{type(exc).__name__}"
                continue
            self._mark(host)
            if resp.status_code in RETRYABLE_STATUS:
                last_reason = f"retries_exhausted_status_{resp.status_code}"
                continue
            return FetchResult(
                status=resp.status_code,
                body=resp.content,
                content_type=resp.headers.get("Content-Type", ""),
                etag=resp.headers.get("ETag", ""),
                last_modified=resp.headers.get("Last-Modified", ""),
            )
        raise FetchError(last_reason)


def _empty_row(source_id: str, url: str, fetched_at: str) -> dict:
    return {col: "" for col in manifest.COLUMNS} | {
        "source_id": source_id,
        "url": url,
        "fetched_at": fetched_at,
    }


def capture_source(
    root: Path | str,
    source: Source,
    fetcher: Fetcher,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[dict]:
    """Capture every endpoint of one source; append one manifest row each."""
    rows: list[dict] = []
    store = storage.store_for(source, root)
    local = storage.LocalGitStore(root)  # quarantine is always local triage material

    for endpoint in source.endpoints:
        fetched_dt = now_fn()
        fetched_at = iso_z(fetched_dt)
        row = _empty_row(source.source_id, endpoint.url, fetched_at)
        prev = manifest.last_capture(root, source.source_id, endpoint.url)
        warnings: list[str] = []

        allowed, robots_warning = fetcher.robots_allows(endpoint.url, endpoint.delay_seconds)
        if robots_warning:
            warnings.append(robots_warning)
        if not allowed:
            row |= {"outcome": "skipped", "reason": "robots_disallowed", "warnings": ";".join(warnings)}
            manifest.append_row(root, row)
            rows.append(row)
            continue

        try:
            res = fetcher.fetch(
                endpoint,
                etag=(prev or {}).get("etag", ""),
                last_modified=(prev or {}).get("last_modified", ""),
            )
        except FetchError as exc:
            row |= {"outcome": "error", "reason": exc.reason, "warnings": ";".join(warnings)}
            manifest.append_row(root, row)
            rows.append(row)
            continue

        if res.status == 304:
            if prev is None:
                row |= {"http_status": "304", "outcome": "error", "reason": "unexpected_304"}
            else:
                row |= {
                    "http_status": "304",
                    "content_type": prev["content_type"],
                    "content_length": prev["content_length"],
                    "content_sha256": prev["content_sha256"],
                    "etag": prev["etag"] or res.etag,
                    "last_modified": prev["last_modified"] or res.last_modified,
                    "outcome": "unchanged",
                    "raw_ref": prev["raw_ref"],
                    "reason": "not_modified",
                }
            row["warnings"] = ";".join(warnings)
            manifest.append_row(root, row)
            rows.append(row)
            continue

        sha = hashlib.sha256(res.body).hexdigest()
        content_type = (res.content_type or "").split(";")[0].strip().lower()
        ext = storage.ext_for(res.content_type)
        prev_length = int(prev["content_length"]) if prev and prev.get("content_length") else None
        gate = run_gates(
            status_code=res.status,
            content_type=res.content_type,
            body=res.body,
            gates=source.gates,
            prev_content_length=prev_length,
        )
        warnings.extend(gate.warnings)
        row |= {
            "http_status": str(res.status),
            "content_type": content_type,
            "content_length": str(len(res.body)),
            "content_sha256": sha,
            "etag": res.etag,
            "last_modified": res.last_modified,
            "warnings": ";".join(warnings),
        }

        if not gate.ok:
            ref = storage.quarantine_path(source.source_id, fetched_dt, sha, ext)
            local.write(ref, res.body)
            row |= {"outcome": "quarantined", "raw_ref": ref, "reason": gate.reason}
        elif prev is not None and prev.get("content_sha256") == sha:
            row |= {"outcome": "unchanged", "raw_ref": prev["raw_ref"]}
        else:
            ref = storage.raw_path(source.source_id, fetched_dt, sha, ext)
            if not store.exists(ref):
                store.write(ref, res.body)
            row |= {"outcome": "changed" if prev is not None else "first_capture", "raw_ref": ref}

        manifest.append_row(root, row)
        rows.append(row)
    return rows


def write_heartbeat(root: Path | str, payload: dict) -> Path:
    """state/last_run.json — written every run, even when nothing changed.

    It proves the cron is alive, and the resulting repo activity stops GitHub
    from disabling scheduled workflows after 60 quiet days.
    """
    state_dir = Path(root) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "last_run.json"
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


@dataclass
class CaptureReport:
    rows: list[dict] = field(default_factory=list)
    sources: int = 0
    counts: Counter = field(default_factory=Counter)

    @property
    def ok(self) -> bool:
        return self.counts["error"] == 0 and self.counts["quarantined"] == 0


def run_capture(
    root: Path | str,
    cadence: str,
    shard_spec: str = "1/1",
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log: Callable[[str], None] = lambda s: None,
) -> CaptureReport:
    contact = contact_from_env()
    index0, count = parse_shard(shard_spec)
    sources = load_registry(root)
    mine = shard_members(select(sources, cadence=cadence), index0, count)
    fetcher = Fetcher(contact)
    report = CaptureReport(sources=len(mine))

    for source in sorted(mine, key=lambda s: s.source_id):
        for row in capture_source(root, source, fetcher, now_fn):
            report.rows.append(row)
            report.counts[row["outcome"]] += 1
            note = f" ({row['reason']})" if row["reason"] else ""
            log(f"{row['outcome']:<13} {source.source_id}{note}")

    write_heartbeat(
        root,
        {
            "cadence": cadence,
            "shard": shard_spec,
            "sources": report.sources,
            "outcomes": dict(sorted(report.counts.items())),
            "completed_at": iso_z(now_fn()),
        },
    )
    return report


def doctor(root: Path | str, source_id: str, log: Callable[[str], None] = print) -> int:
    """Dry-run one source: fetch, print the raw response, run gates. No writes."""
    contact = contact_from_env()
    sources = load_registry(root)
    matches = [s for s in sources if s.source_id == source_id]
    if not matches:
        log(f"unknown source_id: {source_id}")
        return 1
    source = matches[0]
    fetcher = Fetcher(contact)
    log(f"source_id : {source.source_id}")
    log(f"status    : {source.status}   cadence: {source.cadence}   storage: {source.storage}")
    log(f"schema_id : {source.schema_id}   publisher: {source.publisher} ({source.publisher_tier})")
    ok_all = True
    for endpoint in source.endpoints:
        log("")
        log(f"GET {endpoint.url}")
        allowed, robots_warning = fetcher.robots_allows(endpoint.url, endpoint.delay_seconds)
        if robots_warning:
            log(f"  robots   : warning — {robots_warning} (proceeding)")
        if not allowed:
            log("  robots   : DISALLOWED — capture would record outcome=skipped")
            ok_all = False
            continue
        try:
            res = fetcher.fetch(endpoint)
        except FetchError as exc:
            log(f"  fetch    : FAILED — {exc.reason}")
            ok_all = False
            continue
        sha = hashlib.sha256(res.body).hexdigest()
        log(f"  status   : {res.status}")
        log(f"  type     : {res.content_type or '(none)'}")
        log(f"  bytes    : {len(res.body)}")
        log(f"  sha256   : {sha[:12]}")
        if res.etag:
            log(f"  etag     : {res.etag}")
        if res.last_modified:
            log(f"  modified : {res.last_modified}")
        preview = _preview(res.body)
        log("  ── raw response (head) " + "─" * 40)
        for line in preview.splitlines():
            log(f"  {line}")
        log("  " + "─" * 63)
        prev = manifest.last_capture(root, source.source_id, endpoint.url)
        prev_length = int(prev["content_length"]) if prev and prev.get("content_length") else None
        gate = run_gates(
            status_code=res.status,
            content_type=res.content_type,
            body=res.body,
            gates=source.gates,
            prev_content_length=prev_length,
        )
        log(f"  gates    : {'PASS' if gate.ok else 'FAIL — ' + gate.reason}")
        ok_all = ok_all and gate.ok
    return 0 if ok_all else 1


def _preview(body: bytes, limit: int = 1200) -> str:
    try:
        parsed = json.loads(body)
        if isinstance(parsed, list):
            head = json.dumps(parsed[:2], indent=2)
            return head[:limit] + (f"\n… ({len(parsed)} items total)" if len(parsed) > 2 else "")
        return json.dumps(parsed, indent=2)[:limit]
    except (ValueError, UnicodeDecodeError):
        return body[:limit].decode("utf-8", errors="replace")
