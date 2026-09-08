"""The narrow personal-data exemption, and the guard that keeps it narrow."""

from __future__ import annotations

import pytest

from wss import registry
from tests.conftest import write_source_yaml


def _load(tmp_path):
    return registry.load_registry(tmp_path)


def test_present_in_a_public_repo_is_refused_even_when_paused(tmp_path):
    """The storage rule does not relax for a paused source. `paused` stops the
    fetching; it does not stop the bytes already committed from being public."""
    write_source_yaml(tmp_path, "a.b.c", "https://example.gov/x", status="paused")
    p = tmp_path / "registry" / "a.b.c.yml"
    p.write_text(p.read_text().replace("personal_data: none",
                                       "personal_data: present"))
    with pytest.raises(registry.RegistryError, match="requires storage: object"):
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


def test_present_is_allowed_when_the_raw_stays_private(tmp_path):
    """Discarding a source because it contains an email throws away every other
    column with it. What must not happen is PII in a PUBLIC repo, which is a
    storage question, not a collection one."""
    path = write_source_yaml(tmp_path, "demo.reg.estabs", "https://example.com/a")
    base = path.read_text().replace("personal_data: none", "personal_data: present")

    # present + git = refused; the raw would land in a public repository
    path.write_text(base)
    problems = registry.validate_registry(tmp_path)
    assert any("requires storage: object" in p for p in problems)

    # present + object, but no notes = still refused
    path.write_text(base.replace("storage: git", "storage: object"))
    problems = registry.validate_registry(tmp_path)
    assert any("requires notes" in p for p in problems)
    assert not any("requires storage: object" in p for p in problems)

    # present + object + notes = allowed
    path.write_text(base.replace("storage: git", "storage: object")
                    + 'notes: "Carries ESTABLISHMENT_CONTACT_NAME and _EMAIL; derive '
                      'drops both and publishes FEI, firm, address and operations only."\n')
    assert registry.validate_registry(tmp_path) == []
