"""Community setup and read tools stop ungoverned model/network effects."""
from __future__ import annotations

import socket

import pytest

from boss_engine.services import setup_service
from boss_engine.tools import build_default_registry, build_discuss_read_registry


class _Settings:
    def __init__(self, edition):
        self.boss_edition = edition


class _Ctx:
    def __init__(self, edition):
        self.settings = _Settings(edition)
        self.began = False

    def begin(self):
        self.began = True
        raise AssertionError("the gate must run before database or external work")


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
@pytest.mark.parametrize("args", [{"provider": "codex"},
                                  {"provider": "codex", "prove": True}])
def test_live_cli_proof_tool_stops_before_setup_or_model(edition, args, monkeypatch):
    def no_model(*_args, **_kwargs):
        raise AssertionError("live model proof must not run")

    monkeypatch.setattr(setup_service, "connect_coding_agent", no_model)
    monkeypatch.setattr(setup_service, "prove_coding_agent", no_model)
    ctx = _Ctx(edition)
    result = build_default_registry().invoke(
        "connect_coding_agent", ctx, project_id="project-a", actor="member", args=args,
    )
    assert result["ok"] is False
    assert "Community live CLI proof is paused" in result["error"]
    assert ctx.began is False


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
def test_setup_service_stops_default_proof_before_cli_detection(edition, monkeypatch):
    def no_model(*_args, **_kwargs):
        raise AssertionError("live model proof or detection must not run")

    monkeypatch.setattr(setup_service, "prove_coding_agent", no_model)
    from boss_engine.services import cli_harness
    monkeypatch.setattr(cli_harness, "load_specs", no_model)
    with pytest.raises(setup_service.SetupError, match="Live coding-agent proof is paused"):
        setup_service.connect_coding_agent(
            None, _Settings(edition), "project-a", "codex", actor="member",
        )


def test_explicit_no_proof_keeps_safe_subscription_registration(monkeypatch):
    from boss_engine.services import cli_harness

    class _Spec:
        require_local_session_file = False
        entitlement_source_id = None
        binary = "safe-agent"

    monkeypatch.setattr(cli_harness, "load_specs", lambda _settings: {"safe": _Spec()})
    monkeypatch.setattr(cli_harness, "detect", lambda _spec: {
        "installed": True, "authenticated": True, "version": "1.0", "auth_detail": "signed in",
    })
    monkeypatch.setattr(setup_service, "prove_coding_agent", lambda *_a, **_k: (
        pytest.fail("prove=false must not run a model")))
    result = setup_service.connect_coding_agent(
        None, _Settings("community"), "project-a", "safe", actor="member", prove=False,
    )
    assert result["ok"] is True
    assert result["proof"] == {"ok": True, "skipped": True}


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown"])
def test_host_port_discovery_stops_before_db_or_network(edition, monkeypatch):
    def no_probe(*_args, **_kwargs):
        raise AssertionError("host probe must not run")

    monkeypatch.setattr(socket, "socket", no_probe)
    ctx = _Ctx(edition)
    result = build_default_registry().invoke(
        "find_free_port", ctx, project_id="project-a", actor="member",
        args={"host": "169.254.169.254", "start": 3000},
    )
    assert result["ok"] is False
    assert "Community host-port probes" in result["error"]
    assert ctx.began is False
    assert "find_free_port" not in build_discuss_read_registry(edition=edition).names()
