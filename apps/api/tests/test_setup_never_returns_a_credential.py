"""No response and no log line carries a pasted token or key, or any 4+ character piece of one.

Per the operator's rule: a prefix or suffix of a short secret can be the whole secret, so the
old `last4` is gone from Repo and models entirely.
"""
import logging

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, fakes, view

TOKEN = "ghp_e2e_full"            # the fake's profile name is the token; it has no secret part
KEY = "sk-stub-ok"


def _pieces(secret: str) -> set[str]:
    return {secret[i:i + n] for n in range(4, len(secret) + 1) for i in range(len(secret) - n + 1)}


def test_nothing_echoes_a_credential(client, monkeypatch, caplog):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    caplog.set_level(logging.DEBUG)
    bodies = []
    found = client.post("/v1/setup/repository/token", headers=AUTH,
                        json={"project_id": PROJECT, "token": TOKEN})
    bodies.append(found.text)
    bodies.append(client.post("/v1/setup/repository/select", headers=AUTH, json={
        "project_id": PROJECT, "connection_id": found.json()["connection_id"],
        "full_name": "e2e/app"}).text)
    bodies.append(client.post("/v1/setup/models", headers=AUTH, json={
        "project_id": PROJECT, "provider": "openrouter", "kind": "api_key", "api_key": KEY,
        "model": "stub/small"}).text)
    bodies.append(client.post("/v1/setup/models", headers=AUTH, json={
        "project_id": PROJECT, "provider": "openrouter", "kind": "api_key",
        "api_key": "sk-stub-401", "model": "stub/small"}).text)
    bodies.append(client.post("/v1/setup/checks", headers=AUTH,
                              json={"project_id": PROJECT, "target": "all"}).text)
    bodies.append(str(view(client)))
    text_seen = "\n".join(bodies) + "\n" + caplog.text
    for secret in (TOKEN, KEY, "sk-stub-401"):
        leaked = [p for p in _pieces(secret.split("_", 2)[-1] if secret == TOKEN else secret)
                  if len(p) >= 8 and p in text_seen]
        assert not leaked, (secret, leaked[:3])
    assert "last4" not in text_seen and "token_last4" not in text_seen
