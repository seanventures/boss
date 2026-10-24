"""An investigation runs on whatever Anthropic credential BOSS has configured, and a failed one
says so in the conversation that asked for it.

Found on the laptop 2026-10-24 (inv_495a3c51886f4f8b): the install's working model source was
its Anthropic API key, saved through Repo and models as `setup-anthropic`; the subscription was
disconnected and the console's own `anthropic_api` source was only declared. The governed
investigation booked its call on `anthropic_api` (claude_credential mapped every stored key
there), the router refused it — `no_eligible_source`, in 0.4 s — and nothing was posted back,
because the investigation had been started by a button press that did not carry the thread.

These tests mirror that install's sources: subscription revoked, `anthropic_api` declared with
no key, a connected `setup-anthropic` holding the key. Only the Claude CLI and the sandbox are
faked; the router, the budget, the leases and the receipts are real.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from boss_engine.services import claude_credential, code_agent, debug_session, sandbox
from boss_engine.services.entitlements import EntitlementBrokerService
from boss_engine.services.jobs import JobLeaseService

KEY_SOURCE = "setup-anthropic"
VERDICT = ("Found it.\n```json\n{\"cause\": \"propose_action only accepts registry tools\", "
           "\"evidence\": [\"builtin.py: _DISCUSS_OPERATIONS_TOOLS\"]}\n```")


class _Agent:
    seen: list = []

    def __init__(self, settings=None) -> None:
        self.settings = settings

    def _invoke(self, prompt, cwd, **kwargs):
        _Agent.seen.append(str(getattr(self.settings, "claude_credential_kind", "") or ""))
        return {"result": VERDICT, "num_turns": 3, "usage": {"input_tokens": 10}}

    @staticmethod
    def _usage(data, settings):
        return {"total_tokens": 10}, 1_200


class _Sandbox:
    def __init__(self, settings, task_id):
        self.path = Path("/tmp") / f"inv-key-{task_id}"

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        return False

    def provision(self, *a, **k):
        raise RuntimeError("no repository in this test")


@pytest.fixture
def laptop(client, engine, monkeypatch):
    """A project whose sources are the laptop's: the key lives on `setup-anthropic`."""
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    p = "invkey_" + uuid.uuid4().hex[:10]
    env_name = f"BOSS_TEST_ANTHROPIC_{uuid.uuid4().hex[:8].upper()}"
    monkeypatch.setenv(env_name, "sk-ant-test-only-not-a-key")
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": p})
        c.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": p})
        c.execute(text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)"),
                  {"p": p})
        c.execute(text("INSERT INTO budget_policies(id,scope_type,scope_id,daily_cap_usd_micros,"
                       "monthly_cap_usd_micros,burst_cap_usd_micros_per_min) "
                       "VALUES(:p,'project',:p,10000000,10000000,0)"), {"p": p})
        broker = EntitlementBrokerService(get_settings())
        broker.seed_default_sources(c, p, "test")
        c.execute(text("UPDATE entitlement_sources SET connection_status='revoked', "
                       "status='missing_credentials' WHERE project_id=:p "
                       "AND source_id='anthropic_claude_code'"), {"p": p})
        broker.upsert_source(c, {
            "project_id": p, "provider": "anthropic", "source_id": KEY_SOURCE,
            "source_kind": "api", "entitlement_kind": "api_key", "owner_type": "project",
            "owner_id": p, "cost_mode": "usage_based_api", "auth_method": "api_key",
            "status": "active", "connection_status": "connected", "runtime_supported": True,
            "observed_capacity": {"credential_present": True},
            "supported_models": ["claude-sonnet-4-5"],
            "connection_metadata": {"base_url": "https://api.anthropic.com",
                                    "model": "claude-sonnet-4-5"}},
            actor="test", emit_events=False)
    ref = "sref_" + uuid.uuid4().hex[:12]
    with engine.begin() as c:
        # The key, as Setup binds it: an environment reference on the setup source.
        c.execute(text("INSERT INTO secret_refs (id, project_id, scope_id, ref_kind, ref_key, "
                       "provider, secret_kind, storage_backend, status, created_by) VALUES "
                       "(:i, :p, :p, 'env', :k, 'anthropic', 'api_token', 'environment', "
                       "'active', 'test')"), {"i": ref, "p": p, "k": env_name})
        c.execute(text("UPDATE entitlement_sources SET credential_secret_ref_id = :r "
                       "WHERE project_id = :p AND source_id = :s"),
                  {"r": ref, "p": p, "s": KEY_SOURCE})
    monkeypatch.setattr(code_agent, "ClaudeCodeAgent", _Agent)
    monkeypatch.setattr(sandbox, "EphemeralSandbox", _Sandbox)
    monkeypatch.setattr(debug_session, "_repo_for", lambda *a, **k: ("", ""))
    _Agent.seen = []
    return p


def _lease(engine, p: str, inv: str) -> dict:
    with engine.begin() as c:
        job_id = c.execute(text("SELECT job_id FROM investigations WHERE id = :i"),
                           {"i": inv}).scalar_one()
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            c, project_id=p, job_id=job_id, worker_id="worker-key-test", lease_ttl_seconds=600)
    return {"job_id": job_id, "worker_id": "worker-key-test", "lease_token": lease["lease_token"]}


def _open(engine, p: str, thread_id=None) -> str:
    with engine.begin() as c:
        opened = debug_session.open_investigation(
            c, project_id=p, question="where does propose_action refuse?", actor="ceo",
            thread_id=thread_id, settings=api_main.settings)
    assert opened["ok"], opened
    return opened["investigation_id"]


def test_the_stored_key_books_on_the_source_that_holds_it(engine, laptop):
    with engine.connect() as c:
        said = claude_credential.describe(c, api_main.settings, laptop)
    assert said["active"] == claude_credential.KIND_STORED_KEY
    assert said["source_id"] == KEY_SOURCE, "not anthropic_api, which holds no key here"


def test_an_investigation_runs_on_the_key_and_books_its_source(engine, laptop):
    inv = _open(engine, laptop)
    out = debug_session.run_investigation(engine, api_main.settings, investigation_id=inv,
                                          lease=_lease(engine, laptop, inv))
    assert out["ok"] and out["status"] == "completed", out
    assert _Agent.seen == [claude_credential.KIND_STORED_KEY], "the CLI ran on the bound key"
    with engine.connect() as c:
        sources = c.execute(text(
            "SELECT DISTINCT l.source_id FROM model_gateway_leases l JOIN jobs j "
            "ON j.id = l.job_id JOIN investigations i ON i.job_id = j.id WHERE i.id = :i"),
            {"i": inv}).scalars().all()
    assert sources == [KEY_SOURCE], sources


def test_a_failed_investigation_says_why_in_its_conversation(engine, laptop):
    from boss_engine.services.agent_service import AgentService

    with engine.begin() as c:
        thread = AgentService(get_settings()).start_or_load_thread(
            c, project_id=laptop, actor="ceo")["id"]
        # The key goes away: no source can answer.
        c.execute(text("UPDATE entitlement_sources SET status = 'inactive', "
                       "connection_status = 'revoked' WHERE project_id = :p AND source_id = :s"),
                  {"p": laptop, "s": KEY_SOURCE})
    inv = _open(engine, laptop, thread_id=thread)
    out = debug_session.run_investigation(engine, api_main.settings, investigation_id=inv,
                                          lease=_lease(engine, laptop, inv))
    assert out["ok"] is False, out
    with engine.connect() as c:
        posted = c.execute(text("SELECT content FROM agent_messages WHERE thread_id = :t"),
                           {"t": thread}).scalars().all()
    assert posted and inv in posted[-1] and "did not run to a verdict" in posted[-1], posted
    assert "Traceback" not in posted[-1]


def test_a_pressed_tool_carries_its_conversation(client, monkeypatch):
    """The press path hands the tool the thread the offer was made in, as the turn does, and a
    client cannot supply BOSS's own underscore arguments."""
    seen = {}

    def fake_open(connection, **kw):
        seen.update(kw)
        return {"ok": True, "investigation_id": "inv_fake", "status": "queued"}

    monkeypatch.setattr(debug_session, "open_investigation", fake_open)
    settings = get_settings()
    from boss_engine.services.agent_service import AgentService

    with api_main.engine.begin() as c:
        thread = AgentService(settings).start_or_load_thread(
            c, project_id=settings.default_project_id, actor="ceo")["id"]
    response = client.post("/v1/engine/agent/action", json={
        "project_id": settings.default_project_id, "actor": "ceo", "thread_id": thread,
        "tool": "investigate",
        "args": {"question": "why?", "_thread_id": "athr_spoofed", "_writes_allowed": True}})
    assert response.status_code == 200, response.text
    assert seen.get("thread_id") == thread
    assert seen.get("allow_fix") is False


def test_a_self_fix_build_runs_its_engines_on_the_projects_key(engine, laptop, monkeypatch):
    """request_self_fix's build books no gateway source (its budget is its own reservation), but
    its engines must run on the same credential: bound per lane with the project's connection,
    as a build's are — not looked up in the environment, where this install has no key."""
    from boss_engine.services import self_fix_build

    built = []

    def fake_build(settings, name=None):
        built.append((name, str(getattr(settings, "claude_credential_kind", "") or "")))
        return _Agent(settings)

    monkeypatch.setattr(code_agent, "_build_code_agent", fake_build)
    monkeypatch.setattr(self_fix_build, "engine_names", lambda *a, **k: ("claude_code",
                                                                         "claude_code"))
    monkeypatch.setattr(self_fix_build, "lane_models", lambda *a, **k: (None, None, None))
    with engine.connect() as c:
        self_fix_build.build_engines(c, api_main.settings, laptop)
    assert built == [("claude_code", claude_credential.KIND_STORED_KEY)], built
