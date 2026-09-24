"""Robots evaluation that matches what real crawlers do: LONGEST MATCH WINS.

WHY NOT urllib.robotparser. The standard library returns the FIRST matching
rule, and the de-facto standard -- what Google, Bing and the RFC 9309 draft all
implement -- is that the LONGEST matching path wins, with Allow beating Disallow
on a tie. The difference is not academic:

    User-agent: *
    Allow: /
    Disallow: /c/portal/

urllib.robotparser permits /c/portal/anything, because `Allow: /` is listed
first and matches. Every real crawler refuses it, because `/c/portal/` is the
longer match. op.europa.eu ships exactly that file, and a first-match reading
turns an explicit refusal into a permission.

WHY NOT grep EITHER. A substring test for "Disallow: /venues/" matches
"Disallow: /venues/*/edit" and closes a whole section that was never closed.
That mistake was made on songkick.com on 2026-09-17 and caught only because the
result looked implausible.

So: parse the groups, pick the one that binds us, and evaluate by length.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse


def _groups(text: str) -> list[tuple[list[str], list[tuple[bool, str]]]]:
    """[(user-agents, [(allowed, path), ...]), ...] in file order.

    Consecutive `User-agent:` lines share one group, which is what the standard
    says and what publishers actually write.
    """
    out: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    starting = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not starting and agents:
                out.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            starting = True
        elif field in ("allow", "disallow"):
            starting = False
            if value or field == "disallow":
                # An empty `Disallow:` means allow everything; represent it as
                # a zero-length rule so it never beats a real one.
                rules.append((field == "allow", value))
        elif field == "crawl-delay":
            starting = False
            rules.append((None, value))  # carried separately by crawl_delay()
    if agents:
        out.append((agents, rules))
    return out


def _binding(groups, ua: str):
    """The group whose agent token appears in our UA, else the `*` group."""
    ua = ua.lower()
    best = None
    for agents, rules in groups:
        for a in agents:
            if a != "*" and a in ua:
                return rules
            if a == "*":
                best = rules if best is None else best
    return best


def _match_len(pattern: str, path: str) -> int:
    """Length of `pattern` if it matches `path`, else -1. Handles * and $."""
    if not pattern:
        return -1
    rx = re.escape(pattern).replace(r"\*", ".*")
    rx = rx[:-2] + "$" if rx.endswith(r"\$") else rx + ".*"
    return len(pattern) if re.match(rx, path) else -1


def can_fetch(robots_txt: str, ua: str, url: str) -> bool:
    """Longest match wins; Allow beats Disallow on equal length."""
    rules = _binding(_groups(robots_txt), ua)
    if rules is None:
        return True                      # no group binds us
    p = urlparse(url)
    path = unquote(p.path or "/") + (f"?{p.query}" if p.query else "")
    best_len, best_allow = -1, True
    for allowed, pattern in rules:
        if allowed is None:              # crawl-delay
            continue
        n = _match_len(pattern, path)
        if n > best_len or (n == best_len and allowed):
            best_len, best_allow = n, allowed
    return best_allow if best_len >= 0 else True


def crawl_delay(robots_txt: str, ua: str):
    rules = _binding(_groups(robots_txt), ua)
    for allowed, value in rules or []:
        if allowed is None:
            try:
                return float(value)
            except ValueError:
                return None
    return None
