"""Registry: load, validate, select active sources, deterministic sharding.

One file per source under ``registry/<source_id>.yml``. The registry drives
the fleet: scheduled workflows never name a source, they shard whatever is
active. Validation is strict — an invalid registry fails the build.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Weekly is the floor and monthly the default: sample from how long a state
# persists, not from how often the source republishes. Nothing this fleet
# captures has ever been decided by a sub-weekly reading, so "daily" and
# "hourly" are not offered -- a registry asking for one fails validation.
CADENCE_HOURS = {"weekly": 168, "monthly": 720, "quarterly": 2160}
STATUSES = ("active", "paused", "auto_disabled", "retired")
PUBLISHER_TIERS = ("first_party", "primary", "redistribution")
PERSONAL_DATA = ("none", "parties_only", "present")
STORAGE_BACKENDS = ("git", "object")
# Does re-stating an unchanged snapshot carry signal?
#   every_capture  yes -- "this drug was STILL short on the 7th" is the finding
#   on_change      no  -- a slow register re-stating 30,483 rows a run is 35%
#                        of the partition saying nothing new
RESTATE_MODES = ("every_capture", "on_change")

GATE_KEYS = (
    "expect_status",
    "min_bytes",
    "content_type_any",
    "must_contain",
    "must_not_contain",
    "max_shrink_pct",
)
ENDPOINT_KEYS = ("url", "delay_seconds", "timeout_seconds", "method", "body", "encoding")
REQUIRED_KEYS = (
    "source_id",
    "status",
    "cadence",
    "schema_id",
    "publisher",
    "publisher_tier",
    "destroys_own_history",
    "licence",
    "personal_data",
    "storage",
    "endpoints",
    "gates",
)
OPTIONAL_KEYS = ("notes", "tags", "auth", "dedupe_ignore", "restate")

AUTH_KEYS = ("bearer_env", "scheme", "headers")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Credentials belong in a header, never in a URL: the manifest records every
# URL verbatim and forever, so a key in a query string is a permanent leak.
SECRET_IN_URL_RE = re.compile(
    r"[?&](api[-_]?key|apikey|access[-_]?token|auth[-_]?token|token|secret|password|key)=",
    re.IGNORECASE,
)

# publisher.domain.series — at least three lowercase dot-separated segments.
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*){2,}$")


class RegistryError(ValueError):
    """Raised when the registry does not validate. Loud by design."""


@dataclass(frozen=True)
class Endpoint:
    url: str
    delay_seconds: float = 1.0
    timeout_seconds: float = 30.0
    # A POST endpoint's body is part of its identity: the same URL with two
    # different bodies is two different fetches, and the manifest keys on URL
    # alone. body_key() below is what keeps them apart.
    method: str = "GET"
    body: dict | None = None
    # Publishers disagree on how a POST body is spelled. FDA's Data Dashboard
    # takes JSON; FDA's own iRES takes form-urlencoded with the whole query as
    # a single `payload` string. Same agency, same week, two encodings.
    encoding: str = "json"

    def body_key(self) -> str:
        """Stable short hash of the request body, or '' for a plain GET.

        Appended to the manifest's url column so paged or parameterised POSTs
        cannot overwrite each other's history under one shared URL.
        """
        if not self.body:
            return ""
        import hashlib, json as _json
        blob = _json.dumps(self.body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def identity(self) -> str:
        k = self.body_key()
        return f"{self.url}#body={k}" if k else self.url


@dataclass(frozen=True)
class Source:
    source_id: str
    status: str
    cadence: str
    schema_id: str
    publisher: str
    publisher_tier: str
    destroys_own_history: bool
    licence: str
    personal_data: str
    storage: str
    endpoints: tuple[Endpoint, ...]
    # every_capture (default) | on_change -- see RESTATE_MODES
    restate: str = "every_capture"
    gates: dict = field(default_factory=dict)
    auth: dict = field(default_factory=dict)
    notes: str = ""
    # Regexes stripped from the body *only* to decide changed vs unchanged.
    # The stored bytes and content_sha256 stay verbatim.
    dedupe_ignore: tuple[str, ...] = ()
    path: Path | None = None


def registry_dir(root: Path | str) -> Path:
    return Path(root) / "registry"


def _validate_gates(gates: object, problems: list[str], where: str) -> None:
    if not isinstance(gates, dict):
        problems.append(f"{where}: gates must be a mapping")
        return
    for key in gates:
        if key not in GATE_KEYS:
            problems.append(f"{where}: unknown gate {key!r} (allowed: {', '.join(GATE_KEYS)})")
    expect = gates.get("expect_status")
    if expect is not None:
        codes = expect if isinstance(expect, list) else [expect]
        if not codes or not all(isinstance(c, int) and 100 <= c <= 599 for c in codes):
            problems.append(f"{where}: expect_status must be an HTTP status code or list of codes")
    min_bytes = gates.get("min_bytes")
    if min_bytes is not None and not (isinstance(min_bytes, int) and min_bytes >= 0):
        problems.append(f"{where}: min_bytes must be a non-negative integer")
    for key in ("content_type_any", "must_contain", "must_not_contain"):
        val = gates.get(key)
        if val is not None and not (
            isinstance(val, list) and val and all(isinstance(v, str) and v for v in val)
        ):
            problems.append(f"{where}: {key} must be a non-empty list of strings")
    shrink = gates.get("max_shrink_pct")
    if shrink is not None and not (isinstance(shrink, (int, float)) and 0 <= shrink <= 100):
        problems.append(f"{where}: max_shrink_pct must be a number between 0 and 100")


def _validate_auth(auth: object, problems: list[str], where: str) -> None:
    """`auth: {bearer_env: VAR, scheme: Bearer}` — a NAME, never a secret.

    `scheme` is the Authorization prefix and defaults to Bearer. It exists
    because publishers disagree: PeeringDB requires `Api-Key`, and sending
    Bearer there returns 400, which reads as a broken request rather than as
    the wrong auth scheme it actually is.
    """
    if auth is None:
        return
    if not isinstance(auth, dict) or not auth:
        problems.append(f"{where}: auth must be a mapping, e.g. auth: {{bearer_env: WSS_SOME_KEY}}")
        return
    for key in auth:
        if key not in AUTH_KEYS:
            problems.append(f"{where}: unknown auth key {key!r} (allowed: {', '.join(AUTH_KEYS)})")
    name = auth.get("bearer_env")
    if name is not None:
        if not isinstance(name, str) or not ENV_NAME_RE.match(name):
            problems.append(
                f"{where}: bearer_env must be an environment variable NAME "
                f"(upper snake case), not a credential"
            )
    _validate_scheme(auth, problems, where)
    hdrs = auth.get("headers")
    if hdrs is not None:
        if not isinstance(hdrs, dict) or not hdrs:
            problems.append(f"{where}: auth.headers must be a non-empty mapping of Header-Name: ENV_VAR")
        else:
            for hname, envname in hdrs.items():
                if not isinstance(hname, str) or not hname or " " in hname:
                    problems.append(f"{where}: auth.headers key {hname!r} is not a header name")
                if not isinstance(envname, str) or not ENV_NAME_RE.match(envname or ""):
                    problems.append(
                        f"{where}: auth.headers[{hname!r}] must be an environment variable NAME "
                        f"(upper snake case), not a credential"
                    )


def _validate_scheme(auth: dict, problems: list[str], where: str) -> None:
    scheme = auth.get("scheme")
    if scheme is None:
        return
    if not isinstance(scheme, str) or not scheme.isascii() or " " in scheme or not scheme:
        problems.append(f"{where}: scheme must be a single Authorization prefix, e.g. Api-Key")
    if "scheme" in auth and "bearer_env" not in auth:
        problems.append(f"{where}: scheme has no effect without bearer_env")


def _validate_endpoints(endpoints: object, problems: list[str], where: str) -> list[Endpoint]:
    parsed: list[Endpoint] = []
    if not isinstance(endpoints, list) or not endpoints:
        problems.append(f"{where}: endpoints must be a non-empty list")
        return parsed
    for i, ep in enumerate(endpoints):
        ep_where = f"{where}: endpoints[{i}]"
        if not isinstance(ep, dict):
            problems.append(f"{ep_where}: must be a mapping with a url")
            continue
        for key in ep:
            if key not in ENDPOINT_KEYS:
                problems.append(f"{ep_where}: unknown key {key!r}")
        url = ep.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(f"{ep_where}: url must start with http:// or https://")
            continue
        if SECRET_IN_URL_RE.search(url):
            problems.append(
                f"{ep_where}: url looks like it carries a credential in a query parameter. "
                f"The manifest records every URL permanently — use `auth: {{bearer_env: VAR}}` instead"
            )
            continue
        delay = ep.get("delay_seconds", 1.0)
        if not isinstance(delay, (int, float)) or delay < 0:
            problems.append(f"{ep_where}: delay_seconds must be a non-negative number")
            delay = 1.0
        timeout = ep.get("timeout_seconds", 30.0)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            problems.append(f"{ep_where}: timeout_seconds must be a positive number")
            timeout = 30.0
        method = str(ep.get("method", "GET")).upper()
        if method not in ("GET", "POST"):
            problems.append(f"{ep_where}: method must be GET or POST")
            method = "GET"
        body = ep.get("body")
        if body is not None and not isinstance(body, dict):
            problems.append(f"{ep_where}: body must be a mapping (it is sent as JSON)")
            body = None
        if body is not None and method != "POST":
            problems.append(f"{ep_where}: body is only sent on POST; set method: POST")
        # A credential belongs in a header, never in a body the manifest may
        # later be asked to explain. Same rule already enforced for URLs.
        if body is not None and SECRET_IN_URL_RE.search(json.dumps(body)):
            problems.append(
                f"{ep_where}: body looks like it carries a credential. "
                f"Use `auth: {{headers: {{Header-Name: ENV_VAR}}}}` instead"
            )
        encoding = str(ep.get("encoding", "json")).lower()
        if encoding not in ("json", "form"):
            problems.append(f"{ep_where}: encoding must be json or form")
            encoding = "json"
        if ep.get("encoding") is not None and method != "POST":
            problems.append(f"{ep_where}: encoding only applies to a POST body")
        parsed.append(Endpoint(url=url, delay_seconds=float(delay), timeout_seconds=float(timeout),
                               method=method, body=body, encoding=encoding))
    return parsed


def validate_entry(data: object, path: Path) -> tuple[Source | None, list[str]]:
    """Validate one registry document. Returns (Source or None, problems)."""
    where = path.name
    problems: list[str] = []
    if not isinstance(data, dict):
        return None, [f"{where}: top level must be a mapping"]

    for key in data:
        if key not in REQUIRED_KEYS and key not in OPTIONAL_KEYS:
            problems.append(f"{where}: unknown key {key!r}")
    for key in REQUIRED_KEYS:
        if key not in data:
            problems.append(f"{where}: missing required key {key!r}")
    if problems and any("missing required" in p for p in problems):
        return None, problems

    source_id = data["source_id"]
    if not isinstance(source_id, str) or not SOURCE_ID_RE.match(source_id):
        problems.append(
            f"{where}: source_id must match publisher.domain.series "
            f"(lowercase, digits, hyphens; at least three dot-separated segments)"
        )
    elif path.stem != source_id:
        problems.append(f"{where}: filename must be <source_id>.yml (got stem {path.stem!r})")

    def check_enum(key: str, allowed: tuple) -> None:
        if data[key] not in allowed:
            problems.append(f"{where}: {key} must be one of {', '.join(allowed)} (got {data[key]!r})")

    check_enum("status", STATUSES)
    if data["cadence"] not in CADENCE_HOURS:
        problems.append(f"{where}: cadence must be one of {', '.join(CADENCE_HOURS)}")
    check_enum("publisher_tier", PUBLISHER_TIERS)
    check_enum("personal_data", PERSONAL_DATA)
    check_enum("storage", STORAGE_BACKENDS)
    if "restate" in data:
        check_enum("restate", RESTATE_MODES)

    for key in ("schema_id", "publisher", "licence"):
        if not isinstance(data[key], str) or not data[key].strip():
            problems.append(f"{where}: {key} must be a non-empty string")

    if not isinstance(data["destroys_own_history"], bool):
        problems.append(f"{where}: destroys_own_history must be true or false")
    elif data["destroys_own_history"] is False and data.get("status") == "active":
        problems.append(
            f"{where}: destroys_own_history is false — the publisher archives its own "
            f"history, so there is nothing to capture; keep status paused/retired or remove the entry"
        )

    if data["personal_data"] == "present":
        problems.append(f"{where}: personal_data 'present' is rejected — this fleet does not collect personal data")
    elif data["personal_data"] == "parties_only" and not str(data.get("notes") or "").strip():
        # The narrow exemption is only meaningful if the reasoning is recorded:
        # who the parties are, and why deleting their names leaves the dataset
        # intact. Without that, this is 'present' with a friendlier label.
        problems.append(
            f"{where}: personal_data 'parties_only' requires notes stating who the "
            f"named parties are and why the derived tables do not carry them"
        )

    ignore = data.get("dedupe_ignore")
    if ignore is not None:
        if not isinstance(ignore, list) or not all(isinstance(x, str) for x in ignore):
            problems.append(f"{where}: dedupe_ignore must be a list of regex strings")
        else:
            for pat in ignore:
                try:
                    re.compile(pat)
                except re.error as exc:
                    problems.append(f"{where}: dedupe_ignore pattern {pat!r} is not a "
                                    f"valid regex — {exc}")

    endpoints = _validate_endpoints(data["endpoints"], problems, where)
    _validate_gates(data["gates"], problems, where)
    _validate_auth(data.get("auth"), problems, where)

    if problems:
        return None, problems
    return (
        Source(
            source_id=source_id,
            status=data["status"],
            cadence=data["cadence"],
            schema_id=data["schema_id"],
            publisher=data["publisher"],
            publisher_tier=data["publisher_tier"],
            destroys_own_history=data["destroys_own_history"],
            licence=data["licence"],
            personal_data=data["personal_data"],
            storage=data["storage"],
            restate=data.get("restate", "every_capture"),
            endpoints=tuple(endpoints),
            gates=dict(data["gates"]),
            auth=dict(data.get("auth") or {}),
            notes=str(data.get("notes") or "").strip(),
            dedupe_ignore=tuple(data.get("dedupe_ignore") or ()),
            path=path,
        ),
        [],
    )


def load_registry(root: Path | str) -> list[Source]:
    """Load and strictly validate every registry entry. Raises RegistryError."""
    sources, problems = _load(root)
    if problems:
        raise RegistryError("\n".join(problems))
    return sources


def validate_registry(root: Path | str) -> list[str]:
    """Return the full list of problems (empty means valid)."""
    return _load(root)[1]


def _load(root: Path | str) -> tuple[list[Source], list[str]]:
    reg_dir = registry_dir(root)
    problems: list[str] = []
    sources: list[Source] = []
    if not reg_dir.is_dir():
        return [], [f"registry directory not found: {reg_dir}"]
    files = sorted(p for p in reg_dir.iterdir() if p.suffix in (".yml", ".yaml"))
    if not files:
        problems.append(f"registry directory is empty: {reg_dir}")
    seen: dict[str, Path] = {}
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: YAML parse error: {exc}")
            continue
        source, entry_problems = validate_entry(data, path)
        problems.extend(entry_problems)
        if source is not None:
            if source.source_id in seen:
                problems.append(
                    f"{path.name}: duplicate source_id {source.source_id!r} (also in {seen[source.source_id].name})"
                )
                continue
            seen[source.source_id] = path
            sources.append(source)
    sources.sort(key=lambda s: s.source_id)
    return sources, problems


def select(
    sources: list[Source],
    cadence: str | None = None,
    statuses: tuple[str, ...] = ("active",),
) -> list[Source]:
    out = [s for s in sources if s.status in statuses]
    if cadence is not None:
        out = [s for s in out if s.cadence == cadence]
    return out


def shard_of(source_id: str, shard_count: int) -> int:
    """Deterministic 0-based shard: sha256(source_id) mod shard_count.

    Stable across processes and Python versions so a source always lands in
    the same shard and failures stay attributable.
    """
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")
    digest = hashlib.sha256(source_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def parse_shard(spec: str) -> tuple[int, int]:
    """Parse '3/20' into (index0=2, count=20)."""
    m = re.fullmatch(r"(\d+)/(\d+)", spec.strip())
    if not m:
        raise ValueError(f"shard must look like '3/20', got {spec!r}")
    index, count = int(m.group(1)), int(m.group(2))
    if count < 1 or not (1 <= index <= count):
        raise ValueError(f"shard index out of range: {spec!r}")
    return index - 1, count


def shard_members(sources: list[Source], index0: int, count: int) -> list[Source]:
    return [s for s in sources if shard_of(s.source_id, count) == index0]


def plan(sources: list[Source], cadence: str, shard_count: int) -> list[str]:
    """Non-empty shards as '<i>/<count>' strings — the Actions matrix input."""
    active = select(sources, cadence=cadence)
    occupied = sorted({shard_of(s.source_id, shard_count) for s in active})
    return [f"{i + 1}/{shard_count}" for i in occupied]


def plan_json(sources: list[Source], cadence: str, shard_count: int) -> str:
    return json.dumps(plan(sources, cadence, shard_count), separators=(",", ":"))
