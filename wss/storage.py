"""Storage backends. One interface, one path convention, two backends.

    raw/<source_id>/<YYYY>/<MM>/<YYYYMMDDTHHMMSSZ>-<sha256[:12]>.<ext>

Identical paths in git and object storage make migration a copy, not a
rewrite. Year/month partitioning keeps every directory under GitHub's
3,000-entry cap. The manifest always stays in git regardless of backend.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

RAW_EXT_BY_TYPE = {
    "application/json": "json",
    "text/json": "json",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/csv": "csv",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "txt",
}


def ext_for(content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    return RAW_EXT_BY_TYPE.get(ct, "bin")


def _timestamped_path(prefix: str, source_id: str, fetched_at: datetime, sha256_hex: str, ext: str) -> str:
    ts = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    return (
        f"{prefix}/{source_id}/{fetched_at:%Y}/{fetched_at:%m}/{ts}-{sha256_hex[:12]}.{ext}"
    )


def raw_path(source_id: str, fetched_at: datetime, sha256_hex: str, ext: str) -> str:
    return _timestamped_path("raw", source_id, fetched_at, sha256_hex, ext)


def quarantine_path(source_id: str, fetched_at: datetime, sha256_hex: str, ext: str) -> str:
    return _timestamped_path("quarantine", source_id, fetched_at, sha256_hex, ext)


class Store(ABC):
    @abstractmethod
    def write(self, rel_path: str, data: bytes) -> None: ...

    @abstractmethod
    def exists(self, rel_path: str) -> bool: ...

    @abstractmethod
    def read(self, rel_path: str) -> bytes: ...


class LocalGitStore(Store):
    """Files under the data root; the domain repo's git history is the archive."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, rel_path: str) -> Path:
        return self.root / rel_path

    def write(self, rel_path: str, data: bytes) -> None:
        path = self._path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def exists(self, rel_path: str) -> bool:
        return self._path(rel_path).is_file()

    def read(self, rel_path: str) -> bytes:
        return self._path(rel_path).read_bytes()


class ObjectStore(Store):
    """S3-compatible object storage (target: Cloudflare R2 for zero egress).

    Same keys as LocalGitStore paths. boto3 is imported lazily so the engine
    has no hard dependency on it; install with `pip install wss[object]`.
    """

    def __init__(self, bucket: str, endpoint_url: str | None = None, prefix: str = "", client=None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3  # deferred: only object-backend users need it

            client = boto3.client("s3", endpoint_url=endpoint_url)
        self.client = client

    def _key(self, rel_path: str) -> str:
        return f"{self.prefix}/{rel_path}" if self.prefix else rel_path

    def write(self, rel_path: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(rel_path), Body=data)

    def exists(self, rel_path: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(rel_path))
            return True
        except Exception:
            return False

    def read(self, rel_path: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(rel_path))["Body"].read()


def store_for(source, root: Path | str) -> Store:
    """Pick the backend the source declares. Object config comes from env."""
    if source.storage == "git":
        return LocalGitStore(root)
    bucket = os.environ.get("WSS_OBJECT_BUCKET", "")
    if not bucket:
        raise RuntimeError(
            f"{source.source_id} declares storage: object but WSS_OBJECT_BUCKET is not set"
        )
    return ObjectStore(
        bucket=bucket,
        endpoint_url=os.environ.get("WSS_OBJECT_ENDPOINT") or None,
        prefix=os.environ.get("WSS_OBJECT_PREFIX", ""),
    )
