"""Infisical as a secret backend, without talking to one.

The operator's own platform runs Infisical, so an installation should be able to keep its
secrets there rather than in the store BOSS would otherwise fall back to — Postgres, which
`secret_backends` itself says is not a good place for a token.

Two properties carry most of the risk, and neither is visible in a happy-path call:

  * the access token EXPIRES (two hours by default). A backend that fetched one and kept it
    would work all afternoon and fail in the middle of unrelated work that evening.
  * every request carries either the client secret or the secret value, so an error that
    quotes the response body quotes a credential.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from boss_engine.services.secret_backends import InfisicalBackend, SecretUnavailable


class FakeInfisical:
    """Records requests and answers them, standing in for the server."""

    def __init__(self, *, expires_in: int = 7200, secrets: Optional[dict] = None):
        self.calls: list[tuple[str, str, Optional[dict]]] = []
        self.logins = 0
        self.expires_in = expires_in
        self.secrets = dict(secrets or {})
        self.fail_create_for: set[str] = set()

    def __call__(self, request, timeout=None):  # urlopen signature
        method, url = request.get_method(), request.full_url
        payload = json.loads(request.data.decode()) if request.data else None
        self.calls.append((method, url, payload))
        if "auth/universal-auth/login" in url:
            self.logins += 1
            return _Response({"accessToken": f"tok-{self.logins}",
                              "expiresIn": self.expires_in})
        name = url.split("/api/v4/secrets/")[-1].split("?")[0] if "/api/v4/secrets/" in url else ""
        if method == "GET" and name:
            if name not in self.secrets:
                raise _http_error(url, 404, '{"message": "not found"}')
            return _Response({"secret": {"secretValue": self.secrets[name]}})
        if method == "GET":
            return _Response({"secrets": [{"secretKey": k} for k in self.secrets]})
        if method == "POST":
            if name in self.fail_create_for:
                raise _http_error(url, 400, '{"message":"secret already exists"}')
            self.secrets[name] = payload["secretValue"]
            return _Response({"ok": True})
        if method == "PATCH":
            self.secrets[name] = payload["secretValue"]
            return _Response({"ok": True})
        if method == "DELETE":
            self.secrets.pop(name, None)
            return _Response({"ok": True})
        raise AssertionError(f"unexpected {method} {url}")


class _Response:
    def __init__(self, body: Any):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _http_error(url: str, code: int, body: str):
    import io
    import urllib.error

    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(body.encode()))


@pytest.fixture
def server(monkeypatch):
    fake = FakeInfisical(secrets={"CODEX_AUTH_JSON": "{}"})
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def _backend(**kw) -> InfisicalBackend:
    return InfisicalBackend("https://infisical.example", "cid", "csecret", "proj", **kw)


def test_it_reads_a_secret(server) -> None:
    assert _backend().get("CODEX_AUTH_JSON") == "{}"


def test_a_missing_secret_is_an_answer_not_a_crash(server) -> None:
    with pytest.raises(SecretUnavailable):
        _backend().get("NOT_THERE")


def test_the_access_token_is_reused_rather_than_refetched(server) -> None:
    """A login per call would triple the request count and rate-limit under load."""
    backend = _backend()
    backend.get("CODEX_AUTH_JSON")
    backend.get("CODEX_AUTH_JSON")
    backend.list_keys()

    assert server.logins == 1


def test_an_expiring_token_is_refetched_before_it_lapses(server, monkeypatch) -> None:
    """THE failure that would not show up in testing: it works, then stops hours later."""
    clock = {"now": 1_000.0}
    monkeypatch.setattr("boss_engine.services.secret_backends.time.time",
                        lambda: clock["now"])
    server.expires_in = 7200
    backend = _backend()

    backend.get("CODEX_AUTH_JSON")
    assert server.logins == 1

    clock["now"] += 7200          # past expiry
    backend.get("CODEX_AUTH_JSON")
    assert server.logins == 2, "a stale token would have been sent"


def test_the_refresh_happens_before_expiry_not_after(server, monkeypatch) -> None:
    """A call that starts just inside the window must not finish just outside it."""
    clock = {"now": 1_000.0}
    monkeypatch.setattr("boss_engine.services.secret_backends.time.time",
                        lambda: clock["now"])
    backend = _backend()
    backend.get("CODEX_AUTH_JSON")

    clock["now"] += 7200 - 60     # inside the margin, not yet expired
    backend.get("CODEX_AUTH_JSON")

    assert server.logins == 2


def test_writing_an_existing_secret_updates_it(server) -> None:
    """Infisical separates create from update, so an upsert is two calls.

    A rotated credential takes this branch every time, which makes it the common path rather
    than an error path.
    """
    server.fail_create_for = {"CODEX_AUTH_JSON"}

    _backend().put("CODEX_AUTH_JSON", "new-value")

    assert server.secrets["CODEX_AUTH_JSON"] == "new-value"
    assert [m for m, _, _ in server.calls if m in ("POST", "PATCH")][-1] == "PATCH"


def test_writing_a_new_secret_creates_it(server) -> None:
    _backend().put("FRESH", "value")

    assert server.secrets["FRESH"] == "value"
    assert not any(m == "PATCH" for m, _, _ in server.calls)


def test_delete_and_list(server) -> None:
    backend = _backend()
    backend.put("SECOND", "v")

    assert backend.list_keys() == ["CODEX_AUTH_JSON", "SECOND"]

    backend.delete("SECOND")
    assert backend.list_keys() == ["CODEX_AUTH_JSON"]


def test_the_scope_travels_on_every_call(server) -> None:
    """A secret is addressed by project+environment+path, not by name alone."""
    backend = _backend(environment="staging", secret_path="/boss")
    backend.put("K", "v")

    _, url, payload = [c for c in server.calls if c[0] == "POST"][-1]
    assert payload["projectId"] == "proj"
    assert payload["environment"] == "staging"
    assert payload["secretPath"] == "/boss"


def test_an_error_never_carries_the_body_it_came_from(server) -> None:
    """Every request holds either the client secret or the secret value."""
    def explode(request, timeout=None):
        raise _http_error(request.full_url, 403,
                          '{"clientSecret": "csecret", "secretValue": "leaked-value"}')

    server_calls = explode
    import urllib.request
    original = urllib.request.urlopen
    urllib.request.urlopen = server_calls
    try:
        with pytest.raises(SecretUnavailable) as exc:
            _backend().get("K")
    finally:
        urllib.request.urlopen = original

    assert "403" in str(exc.value)
    assert "csecret" not in str(exc.value)
    assert "leaked-value" not in str(exc.value)


def test_health_reports_reachability(server) -> None:
    assert _backend().health()["ok"] is True


def test_health_says_no_rather_than_raising(monkeypatch) -> None:
    def unreachable(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", unreachable)

    result = _backend().health()
    assert result["ok"] is False
    assert "unreachable" in result["error"]


# --- selection, and not breaking installations that have no Infisical ---------


def test_an_unconfigured_infisical_names_every_setting_it_needs() -> None:
    from boss_engine.config import Settings
    from boss_engine.services.secret_backends import backend_for

    with pytest.raises(SecretUnavailable) as exc:
        backend_for("infisical", Settings())

    message = str(exc.value)
    for setting in ("BOSS_INFISICAL_URL", "BOSS_INFISICAL_CLIENT_ID",
                    "BOSS_INFISICAL_CLIENT_SECRET", "BOSS_INFISICAL_PROJECT_ID"):
        assert setting in message


def test_it_is_tried_first_but_skipped_when_absent(monkeypatch) -> None:
    """Adding a backend at the head of the chain must not change what anyone else gets.

    `default_secret_backend` walks the preference order and takes the first that answers, so an
    entry that raises "not configured" has to fall through silently — otherwise every
    installation without Infisical would break on upgrade.
    """
    from boss_engine.config import Settings
    from boss_engine.services.secret_backends import _BACKEND_PREFERENCE, default_secret_backend

    assert _BACKEND_PREFERENCE[0] == "infisical"
    monkeypatch.delenv("BOSS_INFISICAL_URL", raising=False)

    assert default_secret_backend(Settings()) != "infisical"


def test_the_client_secret_can_stay_out_of_the_environment(tmp_path, monkeypatch) -> None:
    """One long-lived credential is the floor; keep it off the process environment."""
    from boss_engine.config import Settings
    from boss_engine.services.secret_backends import backend_for

    secret_file = tmp_path / "infisical-client-secret"
    secret_file.write_text("csecret\n")
    monkeypatch.setenv("BOSS_INFISICAL_URL", "https://infisical.example")
    monkeypatch.setenv("BOSS_INFISICAL_CLIENT_ID", "cid")
    monkeypatch.setenv("BOSS_INFISICAL_PROJECT_ID", "proj")
    monkeypatch.delenv("BOSS_INFISICAL_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("BOSS_INFISICAL_CLIENT_SECRET_FILE", str(secret_file))

    assert backend_for("infisical", Settings()).kind == "infisical"


# --- the bundled instance, which has a token and no client secret ------------


def test_a_bootstrap_token_is_used_without_an_exchange(server) -> None:
    """`infisical bootstrap` returns a machine-identity TOKEN, not a client id/secret.

    A backend that could only exchange credentials would be unable to authenticate against the
    instance BOSS provisioned itself — the bundled case, which is the whole point of Phase 2.
    """
    backend = InfisicalBackend("https://infisical.example", "", "", "proj", token="boot-token")

    assert backend.get("CODEX_AUTH_JSON") == "{}"
    assert server.logins == 0, "it should not have tried to log in"
    sent = [c for c in server.calls if "/api/v4/secrets/" in c[1]][0]
    assert sent[0] == "GET"


def test_a_token_is_enough_to_be_considered_configured(monkeypatch) -> None:
    from boss_engine.config import Settings
    from boss_engine.services.secret_backends import backend_for

    monkeypatch.setenv("BOSS_INFISICAL_URL", "https://infisical.example")
    monkeypatch.setenv("BOSS_INFISICAL_PROJECT_ID", "proj")
    monkeypatch.setenv("BOSS_INFISICAL_TOKEN", "boot-token")
    for absent in ("BOSS_INFISICAL_CLIENT_ID", "BOSS_INFISICAL_CLIENT_SECRET"):
        monkeypatch.delenv(absent, raising=False)

    assert backend_for("infisical", Settings()).kind == "infisical"


def test_half_a_machine_identity_is_not_configured(monkeypatch) -> None:
    """A client id with no secret would fail at the first call, far from the cause."""
    from boss_engine.config import Settings
    from boss_engine.services.secret_backends import backend_for

    monkeypatch.setenv("BOSS_INFISICAL_URL", "https://infisical.example")
    monkeypatch.setenv("BOSS_INFISICAL_PROJECT_ID", "proj")
    monkeypatch.setenv("BOSS_INFISICAL_CLIENT_ID", "cid")
    for absent in ("BOSS_INFISICAL_TOKEN", "BOSS_INFISICAL_CLIENT_SECRET",
                   "BOSS_INFISICAL_CLIENT_SECRET_FILE"):
        monkeypatch.delenv(absent, raising=False)

    with pytest.raises(SecretUnavailable):
        backend_for("infisical", Settings())
