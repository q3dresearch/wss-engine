from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wss import storage
from wss.registry import Endpoint, Source

TS = datetime(2026, 8, 31, 22, 10, 3, tzinfo=timezone.utc)
SHA = "deadbeefcafe" + "0" * 52


def test_path_convention_is_exact():
    # Identical in both backends: migration is a copy, not a rewrite.
    assert (
        storage.raw_path("hf.models.text-generation", TS, SHA, "json")
        == "raw/hf.models.text-generation/2026/08/20260831T221003Z-deadbeefcafe.json"
    )
    assert storage.quarantine_path("a.b.c", TS, SHA, "html").startswith("quarantine/a.b.c/2026/08/")


def test_ext_for():
    # Raw is stored gzipped, so the extension carries `.gz`.
    assert storage.ext_for("application/json; charset=utf-8") == "json.gz"
    assert storage.ext_for("text/html") == "html.gz"
    assert storage.ext_for("application/octet-stream") == "bin.gz"
    assert storage.ext_for("") == "bin.gz"


def test_compression_is_transparent_and_actually_compresses(tmp_path):
    """A parser sees the original bytes; the disk sees far fewer of them."""
    store = storage.LocalGitStore(tmp_path)
    body = (b'{"value":' + b'1234567890,' * 4000 + b'0}')
    rel = storage.raw_path("a.b.c", TS, SHA, storage.ext_for("application/json"))
    store.write(rel, body)
    assert store.read(rel) == body
    on_disk = (tmp_path / rel).stat().st_size
    assert on_disk < len(body) // 5


def test_uncompressed_refs_still_resolve(tmp_path):
    """Every raw_ref already committed as plain `.json` must keep working, or
    the manifests that point at them stop being honest."""
    store = storage.LocalGitStore(tmp_path)
    rel = storage.raw_path("a.b.c", TS, SHA, "json")      # no .gz
    store.write(rel, b'{"x": 1}')
    assert store.read(rel) == b'{"x": 1}'
    assert (tmp_path / rel).read_bytes() == b'{"x": 1}'   # written as-is


def test_identical_content_gives_identical_bytes(tmp_path):
    """gzip stamps mtime by default, which would defeat dedupe by content hash:
    the same payload would produce different bytes on every fetch."""
    store = storage.LocalGitStore(tmp_path)
    rel_a = storage.raw_path("a.b.c", TS, SHA, "json.gz")
    rel_b = storage.raw_path("a.b.d", TS, SHA, "json.gz")
    store.write(rel_a, b'{"same": true}')
    store.write(rel_b, b'{"same": true}')
    assert (tmp_path / rel_a).read_bytes() == (tmp_path / rel_b).read_bytes()


def test_local_git_store_roundtrip(tmp_path):
    store = storage.LocalGitStore(tmp_path)
    rel = storage.raw_path("a.b.c", TS, SHA, "json")
    assert not store.exists(rel)
    store.write(rel, b'{"x": 1}')
    assert store.exists(rel)
    assert store.read(rel) == b'{"x": 1}'
    assert (tmp_path / rel).is_file()


class StubS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)

    def get_object(self, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def test_object_store_same_paths(tmp_path):
    client = StubS3()
    store = storage.ObjectStore(bucket="archive", prefix="fleet", client=client)
    rel = storage.raw_path("a.b.c", TS, SHA, "json")
    store.write(rel, b"{}")
    assert ("archive", f"fleet/{rel}") in client.objects
    assert store.exists(rel)
    assert store.read(rel) == b"{}"


def _source(backend: str) -> Source:
    return Source(
        source_id="a.b.c",
        status="active",
        cadence="daily",
        schema_id="s.v1",
        publisher="P",
        publisher_tier="primary",
        destroys_own_history=True,
        licence="L",
        personal_data="none",
        storage=backend,
        endpoints=(Endpoint(url="https://example.com"),),
        gates={},
    )


def test_store_for_object_requires_bucket(tmp_path, monkeypatch):
    monkeypatch.delenv("WSS_OBJECT_BUCKET", raising=False)
    assert isinstance(storage.store_for(_source("git"), tmp_path), storage.LocalGitStore)
    with pytest.raises(RuntimeError, match="WSS_OBJECT_BUCKET"):
        storage.store_for(_source("object"), tmp_path)
