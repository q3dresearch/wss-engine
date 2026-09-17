"""A codec is a promise that unpack(pack(x)) == x. These pin the promise.

The danger a codec introduces is not that it compresses badly -- it is that it
looks like it worked while quietly losing a byte, because `raw` is the evidence
the whole archive rests on. So the tests that matter are the ones where the
payload is slightly wrong and the codec must REFUSE rather than mangle.
"""
import gzip

import pytest

from wss import codecs, storage

DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n'
OPEN = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'


def urlset(entries, tail=b"") -> bytes:
    body = b"".join(
        b"  <url>\n    <loc>" + loc + b"</loc>\n"
        + (b"    <lastmod>" + lm + b"</lastmod>\n" if lm else b"")
        + b"  </url>\n" for loc, lm in entries)
    return DECL + OPEN + body + b"</urlset>" + tail


ENTRIES = [
    (b"https://example.gov/dataset/alpha", b"2026-01-02"),
    (b"https://example.gov/dataset/beta", b"2026-01-03"),
    (b"https://example.gov/", b""),            # no lastmod, and off the prefix
]


@pytest.mark.parametrize("tail", [b"", b"\n"])
def test_round_trip_is_byte_exact(tail):
    raw = urlset(ENTRIES, tail)
    packed = codecs.sitemap_pack(raw)
    assert packed is not None
    assert codecs.sitemap_unpack(packed) == raw


def test_packing_actually_saves_something():
    raw = urlset([(b"https://example.gov/dataset/thing-number-%d" % i,
                   b"2026-01-02") for i in range(500)])
    assert len(codecs.sitemap_pack(raw)) < len(raw) / 2


def test_empty_urlset_round_trips():
    raw = DECL + OPEN + b"</urlset>"
    assert codecs.sitemap_unpack(codecs.sitemap_pack(raw)) == raw


@pytest.mark.parametrize("payload", [
    b'{"not": "xml"}',
    b'<?xml version="1.0"?>\n<other/>',
    # changefreq and priority are legal sitemap tags this codec does NOT model.
    # Refusing is correct; silently dropping them would lose bytes.
    DECL + OPEN + b"  <url>\n    <loc>https://x/</loc>\n    <changefreq>daily</changefreq>\n  </url>\n</urlset>",
    # attributes on <url> are likewise unmodelled
    DECL + OPEN + b'  <url foo="1">\n    <loc>https://x/</loc>\n  </url>\n</urlset>',
    # different indentation is a different byte sequence
    DECL + OPEN + b"<url><loc>https://x/</loc></url>\n</urlset>",
])
def test_refuses_what_it_cannot_reproduce(payload):
    assert codecs.sitemap_pack(payload) is None


def test_storage_round_trips_through_the_path_extension(tmp_path):
    raw = urlset(ENTRIES)
    ext = storage.ext_for("application/xml", "sitemap.v1")
    assert ext == "xml.sitemap-v1"
    ref = f"raw/s/2026/09/20260917T000000Z-abc123456789.{ext}"
    store = storage.LocalGitStore(tmp_path)
    store.write(ref, raw)
    assert store.read(ref) == raw               # parsers see the original bytes
    on_disk = (tmp_path / ref).read_bytes()
    assert on_disk != raw
    # The urlset tag is stored ONCE in the header rather than per record --
    # that repetition is exactly what the codec removes.
    assert on_disk.count(b"<urlset") == 1
    assert b"<url>" not in on_disk and b"<loc>" not in on_disk


def test_codec_is_not_also_gzipped(tmp_path):
    """The saving comes from git's delta, which gzip would destroy."""
    raw = urlset([(b"https://example.gov/dataset/x-%d" % i, b"2026-01-02")
                  for i in range(2000)])
    ref = f"raw/s/2026/09/x.{storage.ext_for('application/xml', 'sitemap.v1')}"
    store = storage.LocalGitStore(tmp_path)
    store.write(ref, raw)
    on_disk = (tmp_path / ref).read_bytes()
    assert not on_disk.startswith(b"\x1f\x8b"), "packed raw must not be gzipped"
    assert store.read(ref) == raw


def test_gz_paths_are_untouched_by_codec_logic(tmp_path):
    """Every existing source still stores exactly as before."""
    raw = b'{"hello": "world"}'
    ref = "raw/s/2026/09/x.json.gz"
    store = storage.LocalGitStore(tmp_path)
    store.write(ref, raw)
    assert store.read(ref) == raw
    assert (tmp_path / ref).read_bytes().startswith(b"\x1f\x8b")
    assert storage.codec_of(ref) == ""
