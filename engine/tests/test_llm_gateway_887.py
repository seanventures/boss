"""BOSSAPP-887: the coding CLIs go through an operator-named LLM gateway, never holding its key.

A gateway row (protocol, base URL, key in the secret store) per project or org -> the launchers
route the harness that speaks its protocol through the model proxy with the gateway as upstream ->
the CLI's own login is out of that run -> the sandbox holds only a run token -> what reaches the
gateway is its own key on a model-API path -> no gateway changes nothing -> a key that cannot be
read refuses the run -> Setup's live test says ok/status, never the key.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import code_agent as ca
from boss_engine.services import llm_gateway as gw
from boss_engine.services import model_proxy as mp

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)
GW_KEY = "sk-gw-" + "g" * 40                 # shaped like a key; not a real one
LOGIN = "sk-ant-oat01-" + "t" * 48


class MemoryStore:
    def __init__(self) -> None:
        self.held: dict[str, str] = {}

    def get(self, key: str) -> str:
        if key not in self.held:
            from boss_engine.services.secret_backends import SecretNotFound

            raise SecretNotFound(key)
        return self.held[key]

    def put(self, key: str, value: str) -> None:
        self.held[key] = value

    def delete(self, key: str) -> None:
        self.held.pop(key, None)


class StubUpstream:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers)})
        yield 200, {"content-type": "application/json"}, iter([b'{"usage": {}}'])


@pytest.fixture()
def store(monkeypatch):
    held = MemoryStore()
    monkeypatch.setattr(gw, "_store", lambda settings: held)
    gw.reset_cache()
    yield held
    gw.reset_cache()


@pytest.fixture()
def upstream():
    stub = StubUpstream()
    proxy = mp.ModelProxy(upstream=stub, poll_interval=0.02)
    mp.set_proxy(proxy)
    yield stub, proxy
    mp.set_proxy(None)
    proxy.stop()


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM llm_gateways LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'w4o1 887', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        org = c.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w "
                             "ON w.id = p.workspace_id WHERE p.id = :p"), {"p": pid}).scalar()
        c.execute(text("DELETE FROM llm_gateways WHERE project_id = :p OR (project_id IS NULL "
                       "AND org_id = :o AND created_by = :tag)"),
                  {"p": pid, "o": org, "tag": f"test:{pid}"})
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _make(c, pid, *, protocol="anthropic", url="https://llm-gw.example.com/v1", **kw):
    return gw.create_gateway(c, Settings(), pid, actor=kw.pop("actor", "operator"), name="LiteLLM",
                             protocol=protocol, base_url=url, secret=GW_KEY, **kw)


def test_the_key_goes_to_the_store_and_never_comes_back(eng, project, store):
    with eng.begin() as c:
        made = _make(c, project)
        listed = gw.list_gateways(c, project)
        events = c.execute(text("SELECT data::text FROM events WHERE project_id = :p"),
                           {"p": project}).scalars().all()
    assert made["base_url"] == "https://llm-gw.example.com"          # /v1 is the CLI's to add
    assert made["secret_set"] is True and made["routes"] == ["claude_code"]
    assert store.held[gw.secret_key_for(made["id"])] == GW_KEY
    assert GW_KEY not in json.dumps(listed) and all(GW_KEY not in e for e in events)


def test_urls_and_protocols_are_checked(eng, project, store):
    with eng.begin() as c:
        with pytest.raises(gw.GatewayError, match="secret field"):
            _make(c, project, url="https://user:pw@gw.example.com")
        with pytest.raises(gw.GatewayError, match="http"):
            _make(c, project, url="ftp://gw.example.com")
        with pytest.raises(gw.GatewayError, match="cannot be routed"):
            _make(c, project, protocol="openai", harnesses=["claude_code"])
        _make(c, project)
        with pytest.raises(gw.GatewayError, match="already has an active"):
            _make(c, project)
    assert len(store.held) == 1, "a refused gateway leaves no key behind"


def test_the_project_gateway_wins_and_a_disabled_one_routes_nothing(eng, project, store):
    with eng.begin() as c:
        org_gw = _make(c, project, scope="org", actor=f"test:{project}")
        assert gw.for_harness("claude_code", project_id=project, connection=c)["id"] == org_gw["id"]
        gw.reset_cache()
        mine = _make(c, project, url="https://team-gw.example.com")
        assert gw.for_harness("claude_code", project_id=project, connection=c)["id"] == mine["id"]
        assert gw.for_harness("codex", project_id=project, connection=c) is None
        assert gw.for_harness("aider", project_id=project, connection=c) is None
        gw.update_gateway(c, Settings(), project, mine["id"], actor="operator", status="disabled")
        gw.update_gateway(c, Settings(), project, org_gw["id"], actor="operator",
                          status="disabled")
        assert gw.for_harness("claude_code", project_id=project, connection=c) is None


def test_no_project_means_no_gateway():
    assert gw.for_harness("claude_code", project_id=None) is None


def test_claude_code_goes_through_the_gateway_without_its_login(upstream, store, monkeypatch):
    stub, proxy = upstream
    gateway = {"id": "llmgw_x", "name": "LiteLLM", "protocol": "anthropic",
               "base_url": "https://llm-gw.example.com"}
    store.put(gw.secret_key_for("llmgw_x"), GW_KEY)
    monkeypatch.setattr(gw, "for_harness", lambda harness, settings=None, **kw:
                        gateway if harness == "claude_code" else None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", LOGIN)
    env = ca.ClaudeCodeAgent(Settings())._child_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env, "a gateway-routed run cannot use the login"
    assert env["ANTHROPIC_API_KEY"].startswith(mp.TOKEN_PREFIX)
    assert env["ANTHROPIC_BASE_URL"] == proxy.base_url("anthropic")
    assert mp.holds_provider_key(env) == [] and GW_KEY not in json.dumps(env)
    # What the gateway receives: its own key, on the model path, at its own URL.
    conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=10)
    conn.request("POST", "/anthropic/v1/messages", body=b"{}",
                 headers={"x-api-key": env["ANTHROPIC_API_KEY"],
                          "content-type": "application/json"})
    assert conn.getresponse().status == 200
    conn.close()
    assert stub.calls[0]["url"] == "https://llm-gw.example.com/v1/messages"
    assert stub.calls[0]["headers"].get("x-api-key") == GW_KEY


def test_codex_goes_through_the_gateway_on_a_home_without_the_login(upstream, store, monkeypatch,
                                                                     tmp_path):
    from boss_engine.services.cli_harness import GenericCliCodeAgent, load_specs

    _stub, proxy = upstream
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    (home / "config.toml").write_text("model = 'x'\n")
    monkeypatch.setenv("CODEX_HOME", str(home))
    gateway = {"id": "llmgw_o", "name": "Portkey", "protocol": "openai",
               "base_url": "https://portkey.example.com"}
    store.put(gw.secret_key_for("llmgw_o"), GW_KEY)
    monkeypatch.setattr(gw, "for_harness", lambda harness, settings=None, **kw:
                        gateway if harness == "codex" else None)
    settings = Settings()
    env = GenericCliCodeAgent(settings, load_specs(settings)["codex"])._env()
    assert env["CODEX_HOME"].endswith("-apikey")
    assert not os.path.exists(os.path.join(env["CODEX_HOME"], "auth.json"))
    assert env["OPENAI_API_KEY"].startswith(mp.TOKEN_PREFIX)
    assert env["OPENAI_BASE_URL"] == proxy.base_url("openai") + "/v1"
    assert GW_KEY not in json.dumps(env)


def test_an_unreadable_key_refuses_rather_than_going_direct(store, monkeypatch):
    gateway = {"id": "llmgw_gone", "name": "LiteLLM", "protocol": "anthropic",
               "base_url": "https://llm-gw.example.com"}
    with pytest.raises(gw.GatewayUnavailable, match="disable the gateway to go direct"):
        gw.apply({}, gateway, harness="claude_code", settings=Settings())


def test_without_a_gateway_the_launch_is_unchanged(upstream, monkeypatch):
    monkeypatch.setattr(gw, "for_harness", lambda *a, **k: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", LOGIN)
    monkeypatch.setattr("boss_engine.services.cli_harness.stored_credential",
                        lambda settings, name, *, strict=False, include_env=True:
                        LOGIN if name == "CLAUDE_CODE_OAUTH_TOKEN" else None)
    env = ca.ClaudeCodeAgent(Settings().model_copy(
        update={"claude_code_oauth_token": "", "anthropic_api_key": None}))._child_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == LOGIN and "ANTHROPIC_BASE_URL" not in env


def test_the_live_test_says_ok_or_why_never_the_key(eng, project, store):
    seen: list[dict] = []

    def ok(method, url, headers):
        seen.append({"url": url, "headers": headers})
        return 200, {"data": [{"id": "a"}, {"id": "b"}]}

    with eng.begin() as c:
        made = _make(c, project, protocol="openai")
        out = gw.live_test(c, Settings(), project, made["id"], actor="operator", probe=ok)
        assert out["ok"] and "2 models" in out["detail"]
        assert seen[0]["url"] == "https://llm-gw.example.com/v1/models"
        assert seen[0]["headers"]["authorization"] == f"Bearer {GW_KEY}"
        refused = gw.live_test(c, Settings(), project, made["id"], actor="operator",
                               probe=lambda m, u, h: (401, {"error": GW_KEY}))
        assert refused == {**refused, "ok": False, "status": 401}
        assert "refused the key" in refused["detail"] and GW_KEY not in json.dumps(refused)
        stored = gw.list_gateways(c, project)[0]["last_test"]
        assert stored["status"] == 401
