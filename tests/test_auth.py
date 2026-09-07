"""Authenticated sources — and the guarantee that credentials never land on disk.

An API key is often a *billing* credential (OpenRouter's Data API takes the
same key used for paid inference), so leaking one costs real money. These
tests assert the leak paths are closed, not merely that auth works.
"""

from __future__ import annotations

import json

import pytest

from wss import capture, cli, manifest, registry
from tests.conftest import write_source_yaml
from tests.fixture_server import FixtureServer

SECRET = "sk-or-v1-totallysecrettokenvalue000000"
BODY = json.dumps({"data": [{"id": "x", "n": 1}], "pad": "y" * 40})


def write_authed_source(root, url, *, bearer_env="WSS_DEMO_KEY", source_id="demo.api.thing"):
    path = write_source_yaml(root, source_id, url)
    path.write_text(path.read_text() + f"\nauth:\n  bearer_env: {bearer_env}\n")
    return path


@pytest.mark.usefixtures("contact_env")
def test_bearer_token_is_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("WSS_DEMO_KEY", SECRET)
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/api", BODY)
        write_authed_source(tmp_path, server.url + "/api")
        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 0
        sent = [h for path, h in server.requests if path == "/api"]
    assert sent and sent[0].get("Authorization") == f"Bearer {SECRET}"


@pytest.mark.usefixtures("contact_env")
def test_credential_never_reaches_disk(tmp_path, monkeypatch):
    """The archive, the manifest and every state file must be secret-free."""
    monkeypatch.setenv("WSS_DEMO_KEY", SECRET)
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/api", BODY)
        write_authed_source(tmp_path, server.url + "/api")
        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 0

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written, "capture wrote nothing"
    for path in written:
        assert SECRET not in path.read_text(encoding="utf-8", errors="replace"), f"secret leaked into {path}"


@pytest.mark.usefixtures("contact_env")
def test_missing_credential_fails_loudly_but_locally(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("WSS_DEMO_KEY", raising=False)
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/ok", BODY)
        server.set("/api", BODY)
        write_authed_source(tmp_path, server.url + "/api")
        write_source_yaml(tmp_path, "demo.api.open", server.url + "/ok")  # no auth
        # non-zero exit: the run is red
        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 1

    authed = list(manifest.iter_rows(tmp_path, "demo.api.thing"))
    assert [r["outcome"] for r in authed] == ["error"]
    assert authed[0]["reason"] == "missing_credential"
    assert SECRET not in json.dumps(authed)
    # the unauthenticated source in the same shard still succeeded
    assert [r["outcome"] for r in manifest.iter_rows(tmp_path, "demo.api.open")] == ["first_capture"]


@pytest.mark.usefixtures("contact_env")
def test_doctor_names_the_variable_never_the_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WSS_DEMO_KEY", SECRET)
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/api", BODY)
        write_authed_source(tmp_path, server.url + "/api")
        assert cli.main(["--root", str(tmp_path), "doctor", "demo.api.thing"]) == 0
    out = capsys.readouterr().out
    assert "$WSS_DEMO_KEY" in out and "value never printed" in out
    assert SECRET not in out


def test_env_local_is_loaded_but_real_env_wins(tmp_path, monkeypatch):
    (tmp_path / ".env.local").write_text(
        "# comment\n\nWSS_DEMO_KEY=from-file\nexport WSS_OTHER=\"quoted-value\"\nWSS_TAKEN=from-file\n"
    )
    monkeypatch.delenv("WSS_DEMO_KEY", raising=False)
    monkeypatch.delenv("WSS_OTHER", raising=False)
    monkeypatch.setenv("WSS_TAKEN", "from-environment")

    loaded = capture.load_env_file(tmp_path)

    import os

    assert os.environ["WSS_DEMO_KEY"] == "from-file"
    assert os.environ["WSS_OTHER"] == "quoted-value"
    # CI secrets must never be shadowed by a file in the checkout
    assert os.environ["WSS_TAKEN"] == "from-environment"
    assert "WSS_TAKEN" not in loaded


def test_registry_rejects_a_credential_in_the_url(tmp_path):
    write_source_yaml(tmp_path, "demo.api.leaky", "https://example.com/v1/data?api_key=sk-live-abc123")
    problems = registry.validate_registry(tmp_path)
    assert any("credential in a query parameter" in p for p in problems)
    assert any("bearer_env" in p for p in problems)


def test_registry_rejects_a_literal_secret_in_the_auth_block(tmp_path):
    path = write_source_yaml(tmp_path, "demo.api.thing", "https://example.com/v1/data")
    path.write_text(path.read_text() + f"\nauth:\n  bearer_env: {SECRET}\n")
    problems = registry.validate_registry(tmp_path)
    assert any("environment variable NAME" in p for p in problems)


def test_auth_block_is_optional_and_typo_checked(tmp_path):
    write_source_yaml(tmp_path, "demo.api.open", "https://example.com/v1/data")
    assert registry.validate_registry(tmp_path) == []  # no auth block is fine

    path = write_source_yaml(tmp_path, "demo.api.thing", "https://example.com/v1/other")
    path.write_text(path.read_text() + "\nauth:\n  bearer_token: WSS_DEMO_KEY\n")
    assert any("unknown auth key" in p for p in registry.validate_registry(tmp_path))


def _source_with_auth(tmp_path, **auth):
    path = write_source_yaml(tmp_path, "demo.api.thing", "https://example.invalid/x")
    block = "\nauth:\n" + "".join(f"  {k}: {v}\n" for k, v in auth.items())
    path.write_text(path.read_text() + block)
    return registry.load_registry(tmp_path)[0]


def test_scheme_defaults_to_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("WSS_DEMO_KEY", SECRET)
    src = _source_with_auth(tmp_path, bearer_env="WSS_DEMO_KEY")
    assert capture.auth_headers(src) == {"Authorization": f"Bearer {SECRET}"}


def test_scheme_overrides_the_prefix(tmp_path, monkeypatch):
    # PeeringDB answers Bearer with 400 and Api-Key with 401: a Bearer-only
    # client cannot authenticate there, and the 400 reads as a malformed
    # request rather than as the wrong scheme it is.
    monkeypatch.setenv("WSS_DEMO_KEY", SECRET)
    src = _source_with_auth(tmp_path, bearer_env="WSS_DEMO_KEY", scheme="Api-Key")
    assert capture.auth_headers(src) == {"Authorization": f"Api-Key {SECRET}"}


def test_scheme_without_a_credential_is_rejected(tmp_path):
    path = write_source_yaml(tmp_path, "demo.api.thing", "https://example.invalid/x")
    path.write_text(path.read_text() + "\nauth:\n  scheme: Api-Key\n")
    with pytest.raises(registry.RegistryError, match="no effect without bearer_env"):
        registry.load_registry(tmp_path)


def test_the_secret_is_still_never_in_the_scheme_error(tmp_path, monkeypatch):
    monkeypatch.delenv("WSS_DEMO_KEY", raising=False)
    src = _source_with_auth(tmp_path, bearer_env="WSS_DEMO_KEY", scheme="Api-Key")
    with pytest.raises(capture.CredentialMissing) as exc:
        capture.auth_headers(src)
    assert SECRET not in str(exc.value)


def _post_source(root, url, body, *, headers=None):
    path = write_source_yaml(root, "demo.api.post", url)
    text = path.read_text()
    text = text.replace(f"  - url: {url}\n",
                        f"  - url: {url}\n    method: POST\n    body:\n"
                        + "".join(f"      {k}: {v}\n" for k, v in body.items()))
    if headers:
        text += "\nauth:\n  headers:\n" + "".join(f"    {h}: {e}\n" for h, e in headers.items())
    path.write_text(text)
    return registry.load_registry(root)


@pytest.mark.usefixtures("contact_env")
def test_post_sends_the_body_and_content_type(tmp_path):
    with FixtureServer() as srv:
        srv.allow_all_robots()
        srv.set("/q", BODY)
        _post_source(tmp_path, f"{srv.url}/q", {"rows": 5000, "sort": "RefusalDate"})
        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 0
        assert srv.bodies, "no POST body reached the server"
        sent = json.loads(srv.bodies[-1])
        assert sent == {"rows": 5000, "sort": "RefusalDate"}
        ctypes = [h.get("Content-Type") for _, h in srv.requests if h.get("Content-Type")]
        assert "application/json" in ctypes


@pytest.mark.usefixtures("contact_env")
def test_two_headers_are_both_sent(tmp_path, monkeypatch):
    # FDA's Data Dashboard needs Authorization-User AND Authorization-Key
    # together; no single-header scheme can express that.
    monkeypatch.setenv("WSS_U", "someone@example.com")
    monkeypatch.setenv("WSS_K", SECRET)
    with FixtureServer() as srv:
        srv.allow_all_robots()
        srv.set("/q", BODY)
        _post_source(tmp_path, f"{srv.url}/q", {"rows": 1},
                     headers={"Authorization-User": "WSS_U", "Authorization-Key": "WSS_K"})
        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 0
        hdrs = [h for p, h in srv.requests if p == "/q"][-1]
        assert hdrs["Authorization-User"] == "someone@example.com"
        assert hdrs["Authorization-Key"] == SECRET


@pytest.mark.usefixtures("contact_env")
def test_post_never_sends_a_conditional_header(tmp_path):
    # A POST is not cacheable and can never answer 304; If-None-Match there is
    # noise at best and a 412 at worst.
    with FixtureServer() as srv:
        srv.allow_all_robots()
        srv.set("/q", BODY)
        _post_source(tmp_path, f"{srv.url}/q", {"rows": 1})
        for _ in range(2):  # second run is where a GET would send If-None-Match
            cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"])
        for path, h in srv.requests:
            if path == "/q":
                assert "If-None-Match" not in h and "If-Modified-Since" not in h


def test_same_url_different_bodies_are_different_fetches(tmp_path):
    from wss.registry import Endpoint
    a = Endpoint(url="https://x.invalid/q", method="POST", body={"rows": 1})
    b = Endpoint(url="https://x.invalid/q", method="POST", body={"rows": 2})
    plain = Endpoint(url="https://x.invalid/q")
    assert a.identity() != b.identity(), "paged POSTs would overwrite each other's history"
    assert plain.identity() == "https://x.invalid/q", "a GET's identity must stay the bare URL"


def test_body_requires_post(tmp_path):
    path = write_source_yaml(tmp_path, "demo.api.thing", "https://example.invalid/x")
    path.write_text(path.read_text().replace(
        "  - url: https://example.invalid/x\n",
        "  - url: https://example.invalid/x\n    body:\n      rows: 1\n"))
    with pytest.raises(registry.RegistryError, match="only sent on POST"):
        registry.load_registry(tmp_path)


@pytest.mark.usefixtures("contact_env")
def test_form_encoding_sends_urlencoded_not_json(tmp_path):
    # FDA's Data Dashboard takes JSON and FDA's own iRES takes form-urlencoded
    # with the whole query as one `payload` string. Same agency, two encodings.
    with FixtureServer() as srv:
        srv.allow_all_robots()
        srv.set("/q", BODY)
        path = write_source_yaml(tmp_path, "demo.api.form", f"{srv.url}/q")
        path.write_text(path.read_text().replace(
            f"  - url: {srv.url}/q\n",
            f"  - url: {srv.url}/q\n    method: POST\n    encoding: form\n"
            "    body:\n      payload: '{\"rows\":5}'\n"))
        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 0
        assert srv.bodies[-1] == b"payload=%7B%22rows%22%3A5%7D"
        ct = [h.get("Content-Type") for _, h in srv.requests if h.get("Content-Type")]
        assert "application/x-www-form-urlencoded" in ct
        assert "application/json" not in ct


def test_encoding_without_post_is_rejected(tmp_path):
    path = write_source_yaml(tmp_path, "demo.api.thing", "https://example.invalid/x")
    path.write_text(path.read_text().replace(
        "  - url: https://example.invalid/x\n",
        "  - url: https://example.invalid/x\n    encoding: form\n"))
    with pytest.raises(registry.RegistryError, match="only applies to a POST body"):
        registry.load_registry(tmp_path)
