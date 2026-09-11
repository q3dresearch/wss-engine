"""A scaffolded domain repo must be usable the moment it is written."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from wss import __version__, cli, derive, init


@pytest.fixture
def scaffolded(tmp_path):
    target = tmp_path / "wss-example"
    init.scaffold(target, owner="someone")
    return target


def test_scaffold_validates_out_of_the_box(scaffolded, capsys):
    # The whole point: a fresh repo passes the CI gate with zero edits.
    assert cli.main(["--root", str(scaffolded), "validate"]) == 0
    out = capsys.readouterr().out
    assert "1 source(s), 0 active" in out  # example entry ships paused


def test_dotfiles_are_renamed(scaffolded):
    assert (scaffolded / ".gitattributes").is_file()
    assert (scaffolded / ".gitignore").is_file()
    assert (scaffolded / ".github" / "workflows" / "capture-weekly.yml").is_file()
    assert not (scaffolded / "github").exists()
    assert not (scaffolded / "gitattributes").exists()


def test_csv_conventions_land_before_any_data(scaffolded):
    # .gitattributes must exist before the first CSV is ever committed.
    assert "*.csv text eol=lf" in (scaffolded / ".gitattributes").read_text()
    assert "quarantine/" in (scaffolded / ".gitignore").read_text()


def test_two_separate_licence_files(scaffolded):
    code = (scaffolded / "LICENSE").read_text()
    data = (scaffolded / "LICENSE-DATA").read_text()
    assert "MIT License" in code and "someone" in code
    assert "Attribution 4.0 International" in data
    assert "CC-BY-4.0" in (scaffolded / "CITATION.cff").read_text()


def test_workflows_pin_the_generating_engine_version(scaffolded):
    for name in ("capture-weekly", "health", "derive", "validate"):
        text = (scaffolded / ".github" / "workflows" / f"{name}.yml").read_text()
        assert f"wss-engine.git@v{__version__}" in text
        # GitHub's own ${{ }} expressions must survive templating untouched
        assert "${{ github.repository_owner }}" in text
        assert "{{" not in text.replace("${{", "")  # no unrendered placeholders
        yaml.safe_load(text)  # parses as YAML
    assert f"wss-engine.git@v{__version__}" in (scaffolded / "requirements.txt").read_text()


def test_derive_workflow_needs_no_parser_wiring(scaffolded):
    text = (scaffolded / ".github" / "workflows" / "derive.yml").read_text()
    # the engine discovers parsers/*.py itself, so the workflow just says "derive"
    assert "run: wss derive" in text
    assert "--parsers" not in text  # adding a parser needs no workflow edit
    assert "git diff --exit-code -- derived" in text  # byte-identical gate on PRs


def test_example_source_is_paused_and_documented(scaffolded):
    entry = yaml.safe_load((scaffolded / "registry" / "example.web.stats.yml").read_text())
    assert entry["status"] == "paused"  # nothing captures until a human says so
    assert entry["personal_data"] == "none"
    assert entry["destroys_own_history"] is True


def test_example_parser_registers_and_parses(scaffolded, monkeypatch):
    import sys

    monkeypatch.syspath_prepend(str(scaffolded))
    sys.modules.pop("parsers", None)
    sys.modules.pop("parsers.example_v1", None)
    import parsers.example_v1  # noqa: F401  (registers on import)

    assert "example.v1" in derive.registered()
    fn, version = derive._PARSERS["example.v1"]
    ctx = derive.ParseContext(source=None, url="u", raw_ref="r")
    body = json.dumps({"items": [{"id": "a", "count": 7}]}).encode()
    obs = list(fn(body, ctx))
    assert [(o.entity_id, o.metric, o.value) for o in obs] == [("a", "count", 7)]
    assert version == "1"


def test_refuses_to_overwrite_unless_forced(tmp_path):
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "precious.txt").write_text("do not clobber")
    with pytest.raises(init.InitError, match="not empty"):
        init.scaffold(target, owner="someone")
    init.scaffold(target, owner="someone", force=True)
    assert (target / "precious.txt").read_text() == "do not clobber"
    assert (target / "README.md").is_file()


def test_rejects_a_bad_repo_name(tmp_path):
    with pytest.raises(init.InitError, match="repo name"):
        init.scaffold(tmp_path / "x", owner="o", name="../escape")


def test_title_defaults_from_the_name(tmp_path):
    init.scaffold(tmp_path / "wss-arxiv", owner="someone")
    readme = (tmp_path / "wss-arxiv" / "README.md").read_text()
    assert "# wss-arxiv — Arxiv History" in readme


def test_scaffold_teaches_credential_hygiene(scaffolded):
    """A fork must not have to invent secret handling for itself."""
    gitignore = (scaffolded / ".gitignore").read_text()
    assert ".env.local" in gitignore and ".env\n" in gitignore
    example = (scaffolded / ".env.example").read_text()
    assert "WSS_CONTACT=" in example
    assert "NEVER be committed" in example
    assert not (scaffolded / ".env.local").exists()  # the user creates it
    workflow = (scaffolded / ".github" / "workflows" / "capture-weekly.yml").read_text()
    assert "bearer_env" in workflow  # tells you where to add a credential


def test_no_workflow_declares_an_empty_env(scaffolded):
    # `env:` with nothing under it is valid YAML (it loads as None) and is
    # rejected by GitHub at dispatch: "Unexpected value ''". Deleting the last
    # variable out of an env block and leaving the comments behind therefore
    # passes yaml.safe_load and breaks every workflow in the repo. Assert the
    # thing the parser won't.
    for path in sorted((scaffolded / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        scopes = [("workflow", doc)] + list((doc.get("jobs") or {}).items())
        for scope, obj in scopes:
            assert "env" not in obj or obj["env"], f"{path.name} [{scope}]: empty env:"


def test_capture_carries_a_contact_in_every_job(scaffolded):
    # `wss plan` fails fast without a contact, and plan is a separate job from
    # capture, so the variable has to sit at workflow scope to reach both.
    doc = yaml.safe_load((scaffolded / ".github" / "workflows" / "capture-weekly.yml").read_text())
    assert "WSS_CONTACT" in (doc.get("env") or {})


def test_scaffold_wires_object_storage_secrets(tmp_path):
    """A repo that later declares `storage: object` must not need hand-wiring.

    `wss validate` passes with storage: object and says nothing about
    credentials, so the failure surfaces only on a runner — which is how
    wss-drug-scarcity stayed red for three days. The secrets are harmless when
    every source is storage: git (the variables resolve empty), so they ship.
    """
    from wss.init import scaffold
    scaffold(tmp_path, owner="q3dresearch")
    for name in ("capture-weekly.yml", "derive.yml"):
        text = (tmp_path / ".github" / "workflows" / name).read_text()
        for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                    "R2_BUCKET_NAME", "WSS_OBJECT_PREFIX"):
            assert var in text, f"{name} does not pass {var}"
