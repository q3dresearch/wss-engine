"""Lossless re-encodings of raw, for formats that are mostly furniture.

WHY THIS EXISTS AND WHY IT IS NOT A SUMMARY. Some publishers ship a payload
whose bytes are overwhelmingly boilerplate: the same tags, the same namespace
and the same URL prefix repeated once per record. Storing that verbatim costs
real space, and worse, GZIP DESTROYS GIT'S DELTA -- two gzip streams of
near-identical input share no bytes, so a monthly roster that barely changes
still pays for a full copy every capture.

catalog.data.gov is the case that forced this. 559,462 datasets across 112
sitemap files, ~84 MB of XML per capture. Measured over twelve monthly captures
of one segment at 1% membership churn: gzipped XML 599,601 bytes against sorted
plaintext 84,427 -- 67.2 MB/yr against 9.5 MB/yr across all 112. The source was
about to be pushed to object storage over a formatting decision.

THE RULE THAT MAKES THIS SAFE: A CODEC MUST ROUND-TRIP BYTE-EXACTLY, AND
CAPTURE PROVES IT EVERY TIME. This is not "we kept the important fields". Pack
then unpack must reproduce the publisher's exact bytes, and `capture` checks the
sha256 before it writes. If the round trip fails the codec is abandoned for that
fetch and the raw is stored unchanged. So `raw` still means what the publisher
sent -- it is merely written down in fewer bytes, exactly as gzip already does
one level below.

A codec that cannot promise that does not belong here. Dropping a field you
cannot reconstruct is a summary, and summaries belong in `derived`.
"""

from __future__ import annotations

import re

_CODECS: dict[str, tuple] = {}


def register(name: str, pack, unpack) -> None:
    _CODECS[name] = (pack, unpack)


def get(name: str):
    return _CODECS.get(name)


def names() -> tuple[str, ...]:
    return tuple(sorted(_CODECS))


# --- sitemap.v1 -------------------------------------------------------------
#
# A sitemaps.org <urlset>. The only tags the standard puts inside <url> are
# loc, lastmod, changefreq and priority; catalog.data.gov emits just loc and
# lastmod, with one constant namespace and no attributes anywhere.
#
# The packed form is a header line holding the parts every record shares --
# the XML declaration, the urlset open tag and the common URL prefix -- then
# one tab-separated line per record. Sorted? NO. Record ORDER is part of the
# bytes, and this codec's whole claim is that it reproduces them, so order is
# preserved even though sorting would compress better. Sorting is derive's
# business, not storage's.

_URLSET = re.compile(
    rb'^(<\?xml[^>]*\?>\n)(<urlset[^>]*>\n)(.*)(</urlset>)(\n?)$', re.S)
_ENTRY = re.compile(
    rb'  <url>\n    <loc>([^<]*)</loc>\n'
    rb'(?:    <lastmod>([^<]*)</lastmod>\n)?  </url>\n')
_SEP = b"\x00"


def _common_prefix(locs: list[bytes]) -> bytes:
    if not locs:
        return b""
    p = locs[0]
    for l in locs[1:]:
        while not l.startswith(p):
            p = p[:-1]
            if not p:
                return b""
    return p


def sitemap_pack(data: bytes) -> bytes | None:
    """Return the packed form, or None if this payload is not a plain urlset.

    None means "not mine" and the caller stores the raw untouched. Anything
    unexpected -- an attribute, a changefreq, whitespace that differs -- makes
    the round trip fail, so refusing early is cheaper than being caught by the
    verification step.
    """
    m = _URLSET.match(data)
    if not m:
        return None
    decl, open_tag, body, close, tail = m.groups()
    entries = _ENTRY.findall(body)
    # Every byte of the body must be accounted for, or the round trip cannot
    # reproduce it. Rebuilding and comparing here is the cheap version of the
    # check capture does again on the whole file.
    rebuilt = b"".join(
        b"  <url>\n    <loc>" + loc + b"</loc>\n"
        + (b"    <lastmod>" + lm + b"</lastmod>\n" if lm else b"")
        + b"  </url>\n" for loc, lm in entries)
    if rebuilt != body:
        return None
    prefix = _common_prefix([loc for loc, _ in entries])
    # THE TAIL IS A TOKEN, NOT THE BYTE ITSELF. It is the file's optional
    # trailing newline, and putting a raw b"\n" in the header made the header
    # line contain a newline -- so unpack's partition(b"\n") split inside it and
    # the round trip silently lost the last byte. Every header field must be
    # newline-free by construction.
    head = _SEP.join((b"sitemap.v1", decl.strip(), open_tag.strip(), prefix,
                      b"nl" if tail else b""))
    rows = b"\n".join(loc[len(prefix):] + b"\t" + lm for loc, lm in entries)
    return head + b"\n" + rows


def sitemap_unpack(data: bytes) -> bytes:
    head, _, rows = data.partition(b"\n")
    tag, decl, open_tag, prefix, tail_tok = head.split(_SEP)
    if tag != b"sitemap.v1":
        raise ValueError(f"not a sitemap.v1 payload: {tag!r}")
    tail = b"\n" if tail_tok == b"nl" else b""
    out = [decl, b"\n", open_tag, b"\n"]
    if rows:
        for line in rows.split(b"\n"):
            loc, _, lm = line.partition(b"\t")
            out += [b"  <url>\n    <loc>", prefix, loc, b"</loc>\n"]
            if lm:
                out += [b"    <lastmod>", lm, b"</lastmod>\n"]
            out.append(b"  </url>\n")
    out += [b"</urlset>", tail]
    return b"".join(out)


register("sitemap.v1", sitemap_pack, sitemap_unpack)
