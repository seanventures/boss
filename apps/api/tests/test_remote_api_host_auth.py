"""A non-loopback Host does not replace the API key.

The Community API defaults to a loopback bind. Publishing that port is an
operator choice. These requests never enter the TestClient context manager:
doing so runs the API lifespan, which opens Postgres.
"""

from fastapi.testclient import TestClient

from apps.api.app import main as api_main

_PATH = "/v1/auth/session"
_DETAIL = "Valid BOSS API key or OIDC bearer token required."


def _client(host: str, address: str) -> TestClient:
    return TestClient(
        api_main.app,
        base_url=f"http://{host}",
        client=(address, 44321),
        headers={"Host": host},
    )


def _rejected(client: TestClient, key: str | None) -> tuple[int, str]:
    headers = {}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    response = client.get(_PATH, headers=headers)
    assert response.status_code == 401, response.text
    return response.status_code, response.json()["detail"]


def test_remote_host_still_requires_the_api_key(monkeypatch):
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "oidc_enabled", False)
    monkeypatch.setattr(api_main.settings, "enterprise_saml_enabled", False)
    monkeypatch.setattr(api_main.settings, "api_keys", "community-test-key")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals", "community-test-key:ceo",
    )

    loopback = _client("127.0.0.1", "127.0.0.1")
    remote = _client("boss.example", "203.0.113.10")

    missing_loopback = _rejected(loopback, None)
    wrong_loopback = _rejected(loopback, "not-the-key")
    missing_remote = _rejected(remote, None)
    wrong_remote = _rejected(remote, "not-the-key")

    assert missing_loopback == (401, _DETAIL)
    assert wrong_loopback == (401, _DETAIL)
    assert missing_remote == missing_loopback
    assert wrong_remote == wrong_loopback
