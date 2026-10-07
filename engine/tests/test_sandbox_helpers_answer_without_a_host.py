"""The helper catalogue answers on an install with no micro-VM host (round 3, R3-STACK).

The Community compose stack has no sandbox-service, and GET /v1/sandboxes/helpers answered 503 to
every caller allowed to read it — the API sweep's last expected failure. Like list_sandboxes, an
absent or unreachable host is an answer: an empty catalogue that says why.
"""
from types import SimpleNamespace

from boss_engine.services import sandbox_console


def test_no_host_configured_is_an_empty_catalogue_that_says_why(monkeypatch):
    monkeypatch.setattr(sandbox_console, "_base", lambda s: "")
    out = sandbox_console.list_helpers(SimpleNamespace(sandbox_token=""))
    assert out["commands"] == [] and out["categories"] == {} and "not configured" in out["unavailable"]


def test_an_unreachable_host_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(sandbox_console, "_base", lambda s: "http://127.0.0.1:9")
    out = sandbox_console.list_helpers(SimpleNamespace(sandbox_token=""))
    assert out["commands"] == [] and out["unavailable"]
