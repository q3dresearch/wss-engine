"""The narrow personal-data exemption, and the guard that keeps it narrow."""

from __future__ import annotations

import pytest

from wss import registry
from tests.conftest import write_source_yaml


def _load(tmp_path):
    return registry.load_registry(tmp_path)


def test_present_is_rejected_even_when_paused(tmp_path):
    write_source_yaml(tmp_path, "a.b.c", "https://example.gov/x", status="paused")
    p = tmp_path / "registry" / "a.b.c.yml"
    p.write_text(p.read_text().replace("personal_data: none",
                                       "personal_data: present"))
    with pytest.raises(registry.RegistryError, match="rejected"):
        _load(tmp_path)


def test_parties_only_requires_notes(tmp_path):
    write_source_yaml(tmp_path, "a.b.c", "https://example.gov/x")
    p = tmp_path / "registry" / "a.b.c.yml"
    p.write_text(p.read_text().replace("personal_data: none",
                                       "personal_data: parties_only"))
    with pytest.raises(registry.RegistryError, match="requires notes"):
        _load(tmp_path)


def test_parties_only_passes_with_notes(tmp_path):
    write_source_yaml(tmp_path, "a.b.c", "https://example.gov/x")
    p = tmp_path / "registry" / "a.b.c.yml"
    p.write_text(p.read_text().replace(
        "personal_data: none",
        'personal_data: parties_only\n'
        'notes: "One of 32 petitioners is a private citizen. Derived tables '
        'carry no petitioner column."'))
    sources = _load(tmp_path)
    assert sources[0].personal_data == "parties_only"


def test_none_still_needs_no_notes(tmp_path):
    write_source_yaml(tmp_path, "a.b.c", "https://example.gov/x")
    assert _load(tmp_path)[0].personal_data == "none"


def test_unknown_value_is_rejected(tmp_path):
    write_source_yaml(tmp_path, "a.b.c", "https://example.gov/x")
    p = tmp_path / "registry" / "a.b.c.yml"
    p.write_text(p.read_text().replace("personal_data: none",
                                       "personal_data: anonymised"))
    with pytest.raises(registry.RegistryError, match="must be one of"):
        _load(tmp_path)
