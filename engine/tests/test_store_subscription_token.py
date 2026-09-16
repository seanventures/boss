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

    The message must name `export`. The first person to hit this had set OPS_BASE_URL without
    it, so the shell echoed the value back to them while this process saw nothing — and the
    original message, "or set OPS_BASE_URL", told them to do what they had just done. A child
    process cannot distinguish unexported from unset, so the text has to raise the possibility.
    """
    path = _auth_file(tmp_path, {"access_token": "real-token-value"})
    with pytest.raises(SystemExit) as exc:
        mod.main(["codex", "--auth-file", str(path), "--store-url", ""])
    assert "--store-url" in str(exc.value)
    assert "export" in str(exc.value)


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
