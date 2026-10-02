"""The disposable stack is an installed BOSS in every way that decides whether a screen works.

The incident: on boss-1 the console key is bound to principal `ceo` and the worker key to
`system`, with BOSS_REQUIRE_API_AUTH=true. The agent wizard posted as `vp_agents` and got 403;
"Advance now" sent the console key to a worker-only route and got 403. Every in-process suite
runs with transport auth off, so none of them could see either. A harness that quietly ran the
same way would be the same blind spot with a longer runtime.

Also pinned: the stack never inherits a developer's credentials (an OPENAI_API_KEY exported in a
shell would turn a scripted run into a paid one), the only `claude` it can find is the scripted
one, and every outbound call goes through the fakes' proxy.
"""
from __future__ import annotations

import os

import pytest

from e2e.harness import stack as stack_mod
from e2e.harness.target import TargetError, live_target_from_env


def _state(tmp_path, **overrides) -> stack_mod.StackState:
    values = dict(run_dir=str(tmp_path), pg_container="c", pg_port=1, db_password="pw",
                  api_port=2, model_port=3, fakes_port=4, smtp_port=5, web_port=6,
                  owner_key="owner-k", worker_key="worker-k", console_password="p" * 32,
                  session_secret="s" * 32)
    values.update(overrides)
    return stack_mod.StackState(**values)


def test_transport_auth_is_on_with_the_owner_and_worker_keys_bound_as_on_boss_1(tmp_path):
    env = stack_mod.service_env(_state(tmp_path))
    assert env["BOSS_REQUIRE_API_AUTH"] == "true"
    assert set(env["BOSS_API_KEYS"].split(",")) == {"owner-k", "worker-k"}
    assert env["BOSS_API_KEY_PRINCIPALS"] == "owner-k:ceo,worker-k:system"
    assert env["BOSS_WORKER_PRINCIPAL"] == "system"
    assert env["BOSS_EDITION"] == "community"


def test_the_database_is_never_the_operators(tmp_path):
    env = stack_mod.service_env(_state(tmp_path))
    assert env["DATABASE_URL"].endswith("/boss_e2e")
    assert stack_mod.DB_NAME != "boss"


def test_nothing_is_inherited_from_the_developers_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-and-billable")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "real-subscription")
    env = stack_mod.service_env(_state(tmp_path))
    assert env["OPENAI_API_KEY"] == "e2e-stub-key"
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["HOME"].startswith(str(tmp_path))


def test_the_only_claude_on_path_is_the_scripted_one(tmp_path):
    env = stack_mod.service_env(_state(tmp_path))
    assert env["PATH"].split(os.pathsep)[0] == str(stack_mod.STUB_BIN)
    assert env["BOSS_CLAUDE_CLI_PATH"] == str(stack_mod.STUB_BIN / "claude")
    for name in ("claude", "ssh", "codex"):
        assert os.access(stack_mod.STUB_BIN / name, os.X_OK), name
    assert "BOSS_E2E_MODEL_STUB_URL" in env["BOSS_AGENT_ENV_ALLOW"]
    assert (tmp_path / "home" / ".boss-e2e-model-stub-url").read_text() == "http://127.0.0.1:3"


def test_every_outbound_call_goes_through_the_fakes_and_trusts_only_this_runs_ca(tmp_path):
    env = stack_mod.service_env(_state(tmp_path))
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        assert env[name] == "http://127.0.0.1:4"
    assert "127.0.0.1" in env["NO_PROXY"]
    ca = str(tmp_path / "fakes" / "tls" / "ca.pem")
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS"):
        assert env[name] == ca
    # macOS's keychain helper hung every clone ("git clone timed out") until this was set.
    assert "helper =" in (tmp_path / "home" / ".gitconfig").read_text()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_a_live_target_says_exactly_what_it_is_missing():
    with pytest.raises(TargetError) as raised:
        live_target_from_env({})
    assert "BOSS_E2E_API_URL" in str(raised.value) and "BOSS_E2E_OWNER_KEY" in str(raised.value)
    target = live_target_from_env({"BOSS_E2E_API_URL": "http://boss-1:8000/",
                                   "BOSS_E2E_OWNER_KEY": "k"})
    assert target.api_url == "http://boss-1:8000" and target.real_models
    assert target.worker_key is None and target.notes


def test_a_live_target_with_real_models_is_refused_without_consent(tmp_path, capsys):
    from e2e.harness.drive import run_all

    target = live_target_from_env({"BOSS_E2E_API_URL": "http://127.0.0.1:9",
                                   "BOSS_E2E_OWNER_KEY": "k"})
    assert run_all(target, tmp_path) == 2
    assert "--real-models" in capsys.readouterr().err
