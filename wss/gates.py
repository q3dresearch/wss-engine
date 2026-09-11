"""Validation gates. A response that fails a gate is quarantined, never archived."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _expected_statuses(gates: dict) -> tuple[int, ...]:
    expect = gates.get("expect_status", 200)
    if isinstance(expect, list):
        return tuple(expect)
    return (expect,)


def run_gates(
    *,
    status_code: int,
    content_type: str,
    body: bytes,
    gates: dict,
    prev_content_length: int | None = None,
) -> GateResult:
    """Apply gates in a fixed order; the first failure wins."""
    if status_code not in _expected_statuses(gates):
        return GateResult(False, f"bad_status_{status_code}")

    min_bytes = gates.get("min_bytes")
    if min_bytes is not None and len(body) < min_bytes:
        return GateResult(False, f"too_small_{len(body)}_bytes")

    allowed_types = gates.get("content_type_any")
    if allowed_types:
        ct = (content_type or "").split(";")[0].strip().lower()
        # Some publishers send no Content-Type at all. CloudFront serves the
        # WDPA monthly zip with Content-Length and Last-Modified and nothing
        # else, so `ct` is "" and every token misses -- which quarantined a
        # perfectly good 23 MB archive. `none` is the way to say "this
        # publisher omits it and that is expected", so the gate stays required
        # and the omission stays declared rather than silently tolerated.
        matched = any(token.lower() in ct for token in allowed_types) if ct else \
            any(token.strip().lower() == "none" for token in allowed_types)
        if not matched:
            return GateResult(False, f"content_type_{ct or 'missing'}")

    text: str | None = None
    needles = gates.get("must_contain")
    if needles:
        text = body.decode("utf-8", errors="replace")
        if not all(needle in text for needle in needles):
            return GateResult(False, "missing_required_text")

    forbidden = gates.get("must_not_contain")
    if forbidden:
        if text is None:
            text = body.decode("utf-8", errors="replace")
        if any(needle in text for needle in forbidden):
            return GateResult(False, "contains_forbidden_text")

    max_shrink = gates.get("max_shrink_pct")
    if max_shrink is not None and prev_content_length and prev_content_length > 0:
        shrink_pct = (prev_content_length - len(body)) * 100.0 / prev_content_length
        if shrink_pct > max_shrink:
            return GateResult(False, f"shrunk_{shrink_pct:.0f}_pct")

    return GateResult(True)
