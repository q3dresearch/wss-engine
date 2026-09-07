from __future__ import annotations

import hashlib

import pytest

from wss import cli, registry
from tests.conftest import write_source_yaml


def test_valid_entry_loads(tmp_path):
    write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/api")
    sources = registry.load_registry(tmp_path)
    assert len(sources) == 1
    s = sources[0]
    assert s.source_id == "fixture.demo.alpha"
    assert s.cadence == "weekly"
    assert s.endpoints[0].url == "https://example.com/api"
    assert s.endpoints[0].delay_seconds == 0
    assert s.gates["expect_status"] == 200


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"cadence": "fortnightly"}, "cadence"),
        ({"status": "enabled"}, "status"),
        ({"personal_data": "present"}, "personal_data 'present' is rejected"),
        ({"publisher_tier": "secondary"}, "publisher_tier"),
        ({"storage": "ftp"}, "storage"),
        ({"source_id": "TooShort"}, "source_id"),
        ({"schema_id": ""}, "schema_id"),
    ],
)
def test_invalid_field_values(tmp_path, mutation, expected):
    path = write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/api")
    text = path.read_text()
    for key, value in mutation.items():
        lines = [
            f"{key}: {value!r}" if line.startswith(f"{key}:") else line
            for line in text.splitlines()
        ]
        text = "\n".join(lines)
    path.write_text(text)
    problems = registry.validate_registry(tmp_path)
    assert problems and any(expected in p for p in problems)


def test_active_source_must_destroy_history(tmp_path):
    path = write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/api")
    path.write_text(path.read_text().replace("destroys_own_history: true", "destroys_own_history: false"))
    problems = registry.validate_registry(tmp_path)
    assert any("publisher archives its own history" in p for p in problems)
    # paused entries with the same flag are fine (documentation of a decision)
    path.write_text(path.read_text().replace("status: active", "status: paused"))
    assert registry.validate_registry(tmp_path) == []


def test_filename_must_match_source_id(tmp_path):
    write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/api", filename="wrong-name.yml")
    problems = registry.validate_registry(tmp_path)
    assert any("filename must be" in p for p in problems)


def test_unknown_keys_rejected(tmp_path):
    path = write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/api")
    path.write_text(path.read_text() + "  surprise_gate: 1\n")
    problems = registry.validate_registry(tmp_path)
    assert any("unknown gate 'surprise_gate'" in p for p in problems)
    path.write_text(path.read_text().replace("  surprise_gate: 1\n", "") + "shiny_new_key: yes\n")
    problems = registry.validate_registry(tmp_path)
    assert any("unknown key 'shiny_new_key'" in p for p in problems)


def test_endpoint_validation(tmp_path):
    path = write_source_yaml(tmp_path, "fixture.demo.alpha", "ftp://example.com/api")
    assert any("url must start with http" in p for p in registry.validate_registry(tmp_path))
    path.write_text(
        "\n".join(
            line for line in path.read_text().splitlines() if not line.strip().startswith(("- url:", "delay_seconds:"))
        )
    )
    assert any("endpoints must be a non-empty list" in p for p in registry.validate_registry(tmp_path))


def test_duplicate_source_id(tmp_path):
    write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/a")
    write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/b", filename="fixture.demo.alpha.yaml")
    problems = registry.validate_registry(tmp_path)
    assert any("duplicate source_id" in p for p in problems)


def test_validate_cli_fails_build_on_malformed_entry(tmp_path, capsys):
    path = write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/api")
    assert cli.main(["--root", str(tmp_path), "validate"]) == 0
    path.write_text(path.read_text().replace("cadence: weekly", "cadence: sometimes"))
    assert cli.main(["--root", str(tmp_path), "validate"]) == 1
    assert "INVALID" in capsys.readouterr().err


def test_shard_is_deterministic_sha256_mod():
    # Pinned to the documented formula: sha256(source_id)[:8] as big-endian mod N.
    # Any change to the algorithm re-shuffles the fleet and must trip this test.
    for source_id in ("hf.models.text-generation", "fixture.demo.alpha", "a.b.c"):
        for count in (1, 3, 20, 256):
            expected = int.from_bytes(hashlib.sha256(source_id.encode()).digest()[:8], "big") % count
            assert registry.shard_of(source_id, count) == expected
    assert registry.shard_of("hf.models.text-generation", 20) == registry.shard_of("hf.models.text-generation", 20)


def test_parse_shard():
    assert registry.parse_shard("3/20") == (2, 20)
    assert registry.parse_shard("1/1") == (0, 1)
    for bad in ("0/20", "21/20", "x/20", "3", "3/0"):
        with pytest.raises(ValueError):
            registry.parse_shard(bad)


def test_plan_only_lists_occupied_shards(tmp_path):
    write_source_yaml(tmp_path, "fixture.demo.alpha", "https://example.com/api")
    sources = registry.load_registry(tmp_path)
    shards = registry.plan(sources, "weekly", 20)
    expected_index = registry.shard_of("fixture.demo.alpha", 20)
    assert shards == [f"{expected_index + 1}/20"]
    # a cadence the fixture does NOT declare must plan nothing
    assert registry.plan(sources, "monthly", 20) == []
