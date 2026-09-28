"""The script that moves a subscription token to a deployment, and what it refuses to do.

It exists because the instruction it replaced was "open the CLI's credential file and copy the
access_token field", which failed on its first human run: the doc's placeholder went in
verbatim, the secret store answered `{"stored": true}`, and the deployment held a
seven-character string instead of a credential.

So the properties under test are mostly about failing loudly. A script that handles a live
credential has exactly two ways to be wrong — send the wrong thing, or show the right thing —
and both are silent at the moment they happen.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "store_subscription_token.py"


@pytest.fixture(scope="module")
def mod():
    if not SCRIPT.exists():  # pragma: no cover - engine installed standalone
        pytest.skip("scripts/ not present in this checkout")
    spec = importlib.util.spec_from_file_location("store_subscription_token", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _auth_file(tmp_path: Path, payload: dict, name: str = "auth.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize("shape", [
    {"access_token": "real-token-value"},
    {"tokens": {"access_token": "real-token-value"}},
    {"accounts": [{"tokens": {"access_token": "real-token-value"}}]},
])
def test_it_finds_the_token_however_the_file_nests_it(mod, tmp_path, shape) -> None:
    """Pinning a path like ["tokens"]["access_token"] breaks silently on a CLI update.

    The shape of these files is not contractual — it is whatever the CLI wrote last release.
    """
    path = _auth_file(tmp_path, shape)
    assert mod.read_token(path, "access_token") == "real-token-value"


def test_a_missing_file_says_to_sign_in_rather_than_stack_tracing(mod, tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        mod.read_token(tmp_path / "absent.json", "access_token")
    assert "sign in" in str(exc.value)


def test_a_missing_field_names_the_flag_that_fixes_it(mod, tmp_path) -> None:
    """The CLI is free to rename its fields; the operator needs a way through when it does."""
    with pytest.raises(SystemExit) as exc:
        mod.read_token(_auth_file(tmp_path, {"unexpected": "shape"}), "access_token")
    assert "--field" in str(exc.value)


def test_a_corrupt_file_does_not_quote_its_contents_back(mod, tmp_path) -> None:
    """json's own error carries an excerpt of the document, and the document is a credential."""
    path = tmp_path / "auth.json"
    path.write_text('{"tokens": {"access_token": "super-secret-value" ')
    with pytest.raises(SystemExit) as exc:
        mod.read_token(path, "access_token")
    assert "super-secret-value" not in str(exc.value)


def test_codex_sends_the_whole_file_not_one_field(mod, tmp_path, capsys) -> None:
    """The field route cannot work for Codex, so the script must not offer it one.

    `codex login --with-access-token` rejects access_token, id_token and refresh_token alike —
    it wants an agent identity JWT, which a consumer ChatGPT plan never mints. Restoring the
    file makes the CLI report "Logged in using ChatGPT". Storing a field would put a value in
    the deployment that cannot authorise anything, and report success doing it.
    """
    doc = {"tokens": {"access_token": "real-token-value"}, "last_refresh": "2026-09-16"}
    path = _auth_file(tmp_path, doc)

    assert mod.main(["codex", "--auth-file", str(path), "--dry-run"]) == 0

    printed = capsys.readouterr().out
    assert "CODEX_AUTH_JSON" in printed, printed
    assert "whole auth.json" in printed, printed
    compact = json.dumps(doc, separators=(",", ":"))
    assert f"{len(compact)} chars" in printed, printed
    assert "real-token-value" not in printed, "the script printed the credential"


def test_the_credential_is_sent_as_a_single_line(mod, tmp_path) -> None:
    """The secret store refuses a newline, and these files are normally pretty-printed.

    Measured: POSTing a two-line value returns HTTP 400
    `{"error": "value must be a single-line string"}`, while 4000 characters on one line is
    accepted. So sending the file verbatim fails for most operators. Re-serialising compactly
    changes the bytes and not the meaning, because the CLI parses JSON.
    """
    pretty = tmp_path / "auth.json"
    pretty.write_text(json.dumps({"tokens": {"access_token": "v"}}, indent=2))

    content = mod.read_whole_file(pretty)

    assert "\n" not in content
    assert json.loads(content) == {"tokens": {"access_token": "v"}}


def test_a_credential_file_that_is_not_json_is_refused(mod, tmp_path) -> None:
    """Restoring a malformed file would leave the CLI signed out and the store reporting fine."""
    path = tmp_path / "auth.json"
    path.write_text('{"tokens": {"access_token": "super-secret-value" ')

    with pytest.raises(SystemExit) as exc:
        mod.main(["codex", "--auth-file", str(path), "--dry-run"])

    assert "not valid JSON" in str(exc.value)
    assert "super-secret-value" not in str(exc.value)


def test_a_real_run_without_a_destination_refuses_early(mod, tmp_path) -> None:
    """Better to stop than to read a credential into memory for a request nowhere to send.

    The message must name `export`. The first person to hit this had set the variable without
    it, so the shell echoed the value back to them while this process saw nothing — and the
    original message told them to do what they had just done. A child process cannot
    distinguish unexported from unset, so the text has to raise the possibility.
    """
    path = _auth_file(tmp_path, {"access_token": "real-token-value"})
    with pytest.raises(SystemExit) as exc:
        mod.main(["codex", "--auth-file", str(path), "--boss-api", "", "--store-url", ""])
    assert "--boss-api" in str(exc.value)
    assert "export" in str(exc.value)


def test_boss_is_the_default_destination(mod, tmp_path, monkeypatch, capsys) -> None:
    """The point of the whole change: the customer needs no store of their own.

    Before this, a subscription on a server meant owning the delivery — an env file you may not
    be able to reach, or a secret service you may not run, then a redeploy. BOSS already knows
    the best store this installation has, so the honest interface is "give it to BOSS".
    """
    sent = {}

    def fake(api, api_key, project_id, provider, credential, actor="ceo", timeout=30,
             accept_database_fallback=False):
        sent.update(api=api, key=api_key, project=project_id, provider=provider,
                    credential=credential, accepted=accept_database_fallback)
        return {"ok": True, "backend": "database"}

    monkeypatch.setattr(mod, "send_to_boss", fake)
    path = _auth_file(tmp_path, {"tokens": {"access_token": "real-token-value"}})

    assert mod.main(["codex", "--auth-file", str(path),
                     "--boss-api", "https://boss.example", "--boss-api-key", "k"]) == 0

    assert sent["api"] == "https://boss.example"
    assert sent["provider"] == "codex"
    assert sent["accepted"] is False, "the operator has not accepted anything by default"
    assert json.loads(sent["credential"]) == {"tokens": {"access_token": "real-token-value"}}
    out = capsys.readouterr().out
    assert "database" in out and "Nothing to restart" in out
    assert "real-token-value" not in out


def test_the_default_store_request_acts_as_a_project_member(mod, monkeypatch) -> None:
    from boss_engine.services.members import DEFAULT_MEMBER_IDS

    captured = {}

    def capture(request, timeout=None):
        captured["body"] = json.loads(request.data.decode())

        class Response:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", capture)
    assert mod.send_to_boss("http://boss", "k", "p", "codex", '{"t":1}')["ok"] is True
    assert captured["body"]["actor"] in DEFAULT_MEMBER_IDS
    assert captured["body"]["actor"] != "setup"


def test_boss_refusing_the_credential_does_not_echo_it(mod, monkeypatch) -> None:
    """BOSS's error body can quote the request, and the request carried the credential."""
    import urllib.error

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://boss/x", 422, "unprocessable", {},
            __import__("io").BytesIO(b'{"detail": "the codex credential is not valid JSON."}'))

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(SystemExit) as exc:
        mod.send_to_boss("http://boss", "k", "p", "codex", "secret-credential-value")

    assert "422" in str(exc.value)
    assert "secret-credential-value" not in str(exc.value)


def test_a_rejected_write_does_not_echo_the_request_body(mod, monkeypatch) -> None:
    """The store's error body can quote the request back, and the request carried the token."""
    import urllib.error

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://store/secret", 403, 'forbidden: {"value": "real-token-value"}', {}, None)

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(SystemExit) as exc:
        mod.store("http://store", "ops", "CODEX_ACCESS_TOKEN", "real-token-value")
    assert "real-token-value" not in str(exc.value)
    assert "403" in str(exc.value)


def test_only_providers_that_can_actually_travel_are_offered(mod) -> None:
    """Cursor has no token route, so offering it would promise something that cannot work."""
    with pytest.raises(SystemExit):
        mod.main(["cursor", "--dry-run"])


# --- writing straight into an env file --------------------------------------
#
# The route for a deployment with no HTTP secret store, which is most of them. Without it,
# Step 2 of the setup doc had no command at all for the common case — only prose telling the
# reader to "use whatever your platform provides", which is not an instruction.


def test_it_writes_the_credential_into_an_env_file(mod, tmp_path, capsys) -> None:
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n")
    auth = _auth_file(tmp_path, {"tokens": {"access_token": "real-token-value"}})

    assert mod.main(["codex", "--auth-file", str(auth), "--write-env", str(env)]) == 0

    lines = env.read_text().splitlines()
    assert "EXISTING=1" in lines
    assert any(ln.startswith("CODEX_AUTH_JSON=") for ln in lines)
    assert "real-token-value" not in capsys.readouterr().out


def test_rewriting_replaces_rather_than_appends(mod, tmp_path) -> None:
    """Two assignments of one name leave the winner up to whatever reads the file.

    Appending blindly means a stale credential can outrank the new one, and the operator sees
    an expired session after doing exactly what they were told.
    """
    env = tmp_path / ".env"
    env.write_text("CODEX_AUTH_JSON=stale\nAFTER=2\n")
    auth = _auth_file(tmp_path, {"tokens": {"access_token": "fresh"}})

    mod.main(["codex", "--auth-file", str(auth), "--write-env", str(env)])

    assignments = [ln for ln in env.read_text().splitlines()
                   if ln.startswith("CODEX_AUTH_JSON=")]
    assert len(assignments) == 1
    assert "stale" not in assignments[0]
    assert "AFTER=2" in env.read_text(), "unrelated settings must survive"


def test_an_export_prefixed_duplicate_is_replaced_too(mod, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("export CODEX_AUTH_JSON=stale\n")
    auth = _auth_file(tmp_path, {"tokens": {"access_token": "fresh"}})

    mod.main(["codex", "--auth-file", str(auth), "--write-env", str(env)])

    assert "stale" not in env.read_text()


def test_a_new_env_file_is_not_world_readable(mod, tmp_path) -> None:
    import stat

    env = tmp_path / "fresh.env"
    auth = _auth_file(tmp_path, {"tokens": {"access_token": "v"}})

    mod.main(["codex", "--auth-file", str(auth), "--write-env", str(env)])

    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


def test_the_written_value_is_one_line_and_parses_back(mod, tmp_path) -> None:
    """An env file cannot carry a newline, and these auth files are pretty-printed."""
    env = tmp_path / ".env"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "v"}}, indent=2))

    mod.main(["codex", "--auth-file", str(auth), "--write-env", str(env)])

    line = [ln for ln in env.read_text().splitlines()
            if ln.startswith("CODEX_AUTH_JSON=")][0]
    assert json.loads(line.split("=", 1)[1]) == {"tokens": {"access_token": "v"}}


def test_a_dry_run_names_the_env_file_as_the_destination(mod, tmp_path, capsys) -> None:
    auth = _auth_file(tmp_path, {"tokens": {"access_token": "v"}})

    mod.main(["codex", "--auth-file", str(auth), "--write-env", "/tmp/x.env", "--dry-run"])

    assert "/tmp/x.env" in capsys.readouterr().out


def test_a_stale_ops_base_url_does_not_hijack_the_default_route(mod, tmp_path, monkeypatch,
                                                                capsys) -> None:
    """THE trap. `--store-url` used to default from OPS_BASE_URL and outrank `--boss-api`.

    Somebody exported OPS_BASE_URL for an earlier step, ran the documented command hours later,
    and their credential went to their own store while the page said BOSS would hold it. The
    run SUCCEEDED, so nothing pointed at the discrepancy — they only noticed because the
    command did not ask for the BOSS variables the doc said it needed.

    An advanced route has to be asked for explicitly.
    """
    monkeypatch.setenv("OPS_BASE_URL", "http://stale.example:8099")
    monkeypatch.setenv("BOSS_API", "https://boss.example")
    monkeypatch.setenv("BOSS_API_KEY", "k")
    sent = {}
    monkeypatch.setattr(mod, "send_to_boss",
                        lambda *a, **k: sent.update(api=a[0]) or {"ok": True, "backend": "database"})
    path = _auth_file(tmp_path, {"tokens": {"access_token": "v"}})

    assert mod.main(["codex", "--auth-file", str(path)]) == 0

    assert sent.get("api") == "https://boss.example", "a stale export outranked the default"
    assert "stale.example" not in capsys.readouterr().out


def test_each_route_says_where_the_credential_actually_went(mod, tmp_path, monkeypatch,
                                                            capsys) -> None:
    """Two destinations that both print "stored" are indistinguishable at a glance."""
    monkeypatch.setattr(mod, "store", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(mod, "resolve_ops_token", lambda *a, **k: "t")
    path = _auth_file(tmp_path, {"tokens": {"access_token": "v"}})

    mod.main(["codex", "--auth-file", str(path), "--store-url", "http://mine.example"])

    out = capsys.readouterr().out
    assert "http://mine.example" in out
    assert "YOUR store, not BOSS's" in out


def test_a_fallback_refusal_is_relayed_not_retried(mod, tmp_path, monkeypatch) -> None:
    """BOSS asks whether Postgres is acceptable; the script must not answer for the operator.

    Re-sending with the flag set on their behalf would defeat the one exchange that exists to
    make the choice explicit — and they would learn where their credential went by reading the
    source.
    """
    import io
    import urllib.error

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://boss/x", 409, "conflict", {},
            io.BytesIO(json.dumps({"detail": {
                "error": "secret_store_fallback_not_accepted",
                "message": "no secret store configured; anyone with the database has it",
                "backend": "database",
            }}).encode()))

    monkeypatch.setattr("urllib.request.urlopen", refuse)

    with pytest.raises(SystemExit) as exc:
        mod.send_to_boss("http://boss", "k", "p", "codex", "cred")

    assert "anyone with the database has it" in str(exc.value)
    assert "--accept-database-fallback" in str(exc.value)


def test_the_flag_travels_when_the_operator_sets_it(mod, tmp_path, monkeypatch) -> None:
    sent = {}

    def capture(request, timeout=None):
        sent.update(json.loads(request.data.decode()))

        class R:
            def read(self_inner):
                return b'{"ok": true, "backend": "database", "fallback_accepted": true}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_):
                return False
        return R()

    monkeypatch.setattr("urllib.request.urlopen", capture)
    path = _auth_file(tmp_path, {"tokens": {"access_token": "v"}})

    assert mod.main(["codex", "--auth-file", str(path), "--boss-api", "http://boss",
                     "--boss-api-key", "k", "--accept-database-fallback"]) == 0
    assert sent["accept_database_fallback"] is True


def test_storing_in_the_database_says_so_afterwards(mod, tmp_path, monkeypatch, capsys) -> None:
    """A success message that does not mention it leaves the operator thinking it went to a vault."""
    monkeypatch.setattr(mod, "send_to_boss",
                        lambda *a, **k: {"ok": True, "backend": "database",
                                         "fallback_accepted": True})
    path = _auth_file(tmp_path, {"tokens": {"access_token": "v"}})

    mod.main(["codex", "--auth-file", str(path), "--boss-api", "http://boss",
              "--boss-api-key", "k", "--accept-database-fallback"])

    out = capsys.readouterr().out
    assert "BOSS's own database" in out
    assert "bootstrap_infisical" in out
