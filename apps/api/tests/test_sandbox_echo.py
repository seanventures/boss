"""Freeze-safe echo exercise is mounted and does not claim coding GA."""


def test_sandbox_echo_route_is_mounted_when_host_missing(client, monkeypatch):
    from boss_engine.services import sandbox_provider as sp

    monkeypatch.setattr(sp, "sandbox_host_base", lambda _s: "")
    response = client.post("/v1/diag/sandbox-echo")
    # Empty host is 503 fail-closed, never 404 (route exists) and never 500.
    assert response.status_code in (503, 422)
    assert response.status_code != 404
