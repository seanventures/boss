"""The official Grok CLI can use a local OAuth session without API-key fallback."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import cli_harness, setup_service


SPEC = cli_harness.BUILTIN_CLI_HARNESSES["grok"]


def _session(tmp_path: Path) -> Path:
    home = tmp_path / ".grok"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text("{}")
    auth.chmod(0o600)
    return home


def test_grok_declares_only_the_official_owner_local_route() -> None:
    assert SPEC.entitlement_source_id == "xai_grok_build"
    assert SPEC.entitlement_provider == "xai"
    assert SPEC.require_local_session_file
    assert not SPEC.token_env and not SPEC.auth_file_secret
    assert SPEC.supports_read_only() and not SPEC.verified
    assert SPEC.fail_on_nonzero
    assert "grok login --device-auth" in SPEC.signin_hint
    assert "XAI_API_KEY" in SPEC.env_exclude
    assert setup_service.PROVIDERS["grok"]["kind"] == "subscription"


def test_grok_is_a_selectable_code_harness() -> None:
    from boss_engine.services.harness_registry import get_harness

    registered = get_harness("grok")
    agent = registered.build(SimpleNamespace())

    assert registered.kind == "cli" and not registered.verified
    assert isinstance(agent, cli_harness.GenericCliCodeAgent)
    assert agent.engine == "grok"


def test_signed_out_grok_models_exit_zero_does_not_claim_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _session(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _: "/usr/local/bin/grok")
    calls = []

    def probe(_binary, args, **kwargs):
        calls.append((args, kwargs))
        if args == ("--version",):
            return 0, "grok 1.0.40"
        return 0, "You are not authenticated.\nAvailable models:\n  grok-code-fast"

    monkeypatch.setattr(cli_harness, "_run_probe", probe)
    found = cli_harness.detect(SPEC)

    assert found["installed"] and not found["authenticated"] and not found["ready"]
    assert found["portable"] is False
    assert found["credential_kind"] == ""
    assert "device-auth" in found["next_step"]
    assert "XAI_API_KEY" in calls[-1][1]["exclude_env"]


def test_grok_requires_private_session_of_this_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _session(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _: "/usr/local/bin/grok")
    calls = []
    monkeypatch.setattr(cli_harness, "_run_probe",
                        lambda _binary, args, **_kw: (calls.append(args) or (0, "grok 1.0.40")))
    (home / "auth.json").chmod(0o644)

    found = cli_harness.detect(SPEC)

    assert found["installed"] and not found["ready"]
    assert calls == [("--version",)]  # no auth probe when credential is exposed
    assert "0600" in found["auth_detail"]


def test_grok_positive_probe_is_only_a_preliminary_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _session(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _: "/usr/local/bin/grok")
    monkeypatch.setattr(cli_harness, "_run_probe", lambda _binary, args, **_kw: (
        (0, "grok 1.0.40") if args == ("--version",)
        else (0, "Available models:\n  grok-code-fast")))

    found = cli_harness.detect(SPEC)

    assert found["ready"] and found["authenticated"]
    assert found["verified_harness"] is False


@pytest.mark.parametrize("setting", (
    'api_key = "fake-key"',
    'env_key = "XAI_API_KEY"',
    'base_url = "https://api.x.ai/v1"',
    'extra_headers = { "x-api-key" = "fake-key" }',
    'api_backend = "responses"',
))
def test_grok_refuses_api_model_configuration_before_auth_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str,
) -> None:
    home = _session(tmp_path)
    (home / "config.toml").write_text(f'[model."grok-build"]\n{setting}\n')
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _: "/usr/local/bin/grok")
    calls = []
    monkeypatch.setattr(cli_harness, "_run_probe", lambda _binary, args, **_kw: (
        calls.append(args) or (0, "grok 1.0.41")))

    found = cli_harness.detect(SPEC)

    assert not found["ready"] and not found["authenticated"]
    assert "API billing" in found["auth_detail"]
    assert "fake-key" not in str(found)
    assert calls == [("--version",)]


def test_grok_refuses_managed_override_and_unreadable_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    system = tmp_path / "etc"
    home.mkdir()
    system.mkdir()
    (system / "requirements.toml").write_text(
        '[[version_overrides]]\n[version_overrides.model.grok-build]\n'
        'env_key = "XAI_API_KEY"\n')
    assert "API billing" in cli_harness._grok_subscription_config_problem(home, system)
    (system / "requirements.toml").write_text("[broken\n")
    assert "cannot be verified" in cli_harness._grok_subscription_config_problem(home, system)


def test_grok_refuses_direct_model_api_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    system = tmp_path / "etc"
    home.mkdir()
    system.mkdir()
    (home / "config.toml").write_text('[model]\napi_key = "fake-key"\n')
    assert "API billing" in cli_harness._grok_subscription_config_problem(home, system)


def test_grok_allows_non_billing_preferences(tmp_path: Path) -> None:
    home = tmp_path / "home"
    system = tmp_path / "etc"
    home.mkdir()
    system.mkdir()
    (home / "config.toml").write_text('[models]\ndefault = "grok-build"\n')
    assert cli_harness._grok_subscription_config_problem(home, system) == ""


def test_a_nonzero_auth_probe_is_never_ready_even_with_cached_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _session(tmp_path)
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _: "/usr/local/bin/grok")
    monkeypatch.setattr(cli_harness, "_run_probe", lambda _binary, args, **_kw: (
        (0, "grok 1.0.40") if args == ("--version",)
        else (1, "Available models:\n  grok-code-fast")))

    assert cli_harness.detect(SPEC)["ready"] is False


def test_grok_argv_and_environment_keep_the_session_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _: "/usr/local/bin/grok")
    monkeypatch.setenv("XAI_API_KEY", "must-not-reach-grok")
    monkeypatch.setenv("GROK_HOME", "/home/boss/.grok")
    monkeypatch.setattr("boss_engine.services.agent_env.agent_environment",
                        lambda environ: dict(environ))
    agent = cli_harness.GenericCliCodeAgent(SimpleNamespace(), SPEC)

    write = agent._argv("-fix this", read_only=False, model=None)
    review = agent._argv("review", read_only=True, model=None)
    env = agent._env()

    assert write[-1] == "--single=-fix this"
    assert "--always-approve" in write and "workspace" in write
    assert "--always-approve" not in review
    assert "read-only" in review and "read_file,grep,list_dir" in review
    assert "--no-subagents" in write and "--no-auto-update" in write
    assert "XAI_API_KEY" not in env
    assert env["GROK_HOME"] == "/home/boss/.grok"


def test_a_signed_out_turn_is_refused_before_any_agent_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_harness, "detect", lambda *_: {
        "ready": False, "next_step": "grok login --device-auth"})
    monkeypatch.setattr(cli_harness.subprocess, "run",
                        lambda *_a, **_k: pytest.fail("signed-out Grok was invoked"))
    agent = cli_harness.GenericCliCodeAgent(SimpleNamespace(), SPEC)

    with pytest.raises(cli_harness.CliHarnessUnavailable, match="device-auth"):
        agent.run_role("hello", tmp_path)


def test_a_failed_grok_process_stops_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_harness, "detect", lambda *_a: {"ready": True})
    monkeypatch.setattr(cli_harness.shutil, "which", lambda _: "/usr/local/bin/grok")
    monkeypatch.setattr(cli_harness.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=1, stdout="", stderr="session expired"))
    monkeypatch.setattr("boss_engine.services.llm_telemetry.record_llm_call",
                        lambda **_kw: None)
    agent = cli_harness.GenericCliCodeAgent(SimpleNamespace(), SPEC)

    with pytest.raises(cli_harness.CliHarnessUnavailable, match="session expired"):
        agent.run_role("hello", tmp_path)


def test_connect_records_owner_only_entitlement_and_code_engine_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = {"installed": True, "authenticated": True, "version": "grok 1.0.40",
             "auth_detail": "Available models", "ready": True}
    monkeypatch.setattr(cli_harness, "detect", lambda *_a, **_kw: found)
    monkeypatch.setattr(setup_service, "prove_coding_agent",
                        lambda *_a, **_kw: {"ok": True, "exit_code": 0})
    payloads = []
    monkeypatch.setattr("boss_engine.services.entitlements.EntitlementBrokerService",
                        lambda _settings: SimpleNamespace(upsert_source=(
                            lambda _conn, payload, **_kw: payloads.append(payload))))

    out = setup_service.connect_coding_agent(
        None, SimpleNamespace(boss_edition="enterprise"), "project", "grok")

    assert out["ok"] and out["use_it_with"] == "BOSS_CODE_AGENT_ENGINE=grok"
    assert out["entitlement_recorded"]
    assert payloads[0]["source_id"] == "xai_grok_build"
    assert payloads[0]["multi_tenant_allowed"] is False


def test_connect_refuses_grok_without_the_worker_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_harness, "detect", lambda *_a, **_kw: {
        "installed": True, "authenticated": False,
        "next_step": "sign in with: grok login --device-auth"})
    monkeypatch.setattr(cli_harness.GenericCliCodeAgent, "ensure_authorised",
                        lambda *_a: pytest.fail("cannot import another user's session"))

    with pytest.raises(setup_service.SetupError, match="device-auth"):
        setup_service.connect_coding_agent(
            None, SimpleNamespace(boss_edition="enterprise"), "project", "grok")


def test_grok_entitlement_cannot_skip_the_live_proof() -> None:
    with pytest.raises(setup_service.SetupError, match="requires a live read-only proof"):
        setup_service.connect_coding_agent(
            None, SimpleNamespace(boss_edition="enterprise"), "project", "grok",
            prove=False)
