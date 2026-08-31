from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from snapshotter import derive


@pytest.fixture(autouse=True)
def clean_parser_registry():
    derive.clear_parsers()
    yield
    derive.clear_parsers()


@pytest.fixture
def contact_env(monkeypatch):
    monkeypatch.setenv("SNAPSHOTTER_CONTACT", "fleet-test@example.com")
    monkeypatch.setenv("SNAPSHOTTER_RETRY_BASE", "0")


def write_source_yaml(
    root: Path,
    source_id: str,
    url: str,
    *,
    status: str = "active",
    cadence: str = "daily",
    schema_id: str = "test.v1",
    gates: str | None = None,
    filename: str | None = None,
) -> Path:
    gates = gates or textwrap.dedent(
        """\
        gates:
          expect_status: 200
          min_bytes: 10
          content_type_any: [json]
          must_not_contain: ["Access Denied"]
        """
    )
    text = textwrap.dedent(
        f"""\
        source_id: {source_id}
        status: {status}
        cadence: {cadence}
        schema_id: {schema_id}
        publisher: Test Fixtures Inc
        publisher_tier: first_party
        destroys_own_history: true
        licence: "test data"
        personal_data: none
        storage: git
        endpoints:
          - url: {url}
            delay_seconds: 0
        """
    ) + gates
    reg = root / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    path = reg / (filename or f"{source_id}.yml")
    path.write_text(text, encoding="utf-8")
    return path
