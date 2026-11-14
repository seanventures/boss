"""AP-P7 acceptance (d): revoking the parent's Passport cascades to EVERY child run within ONE
transaction (docs/design/AGENT_PLATFORM.md §5 Phase 7; IDENTITY.md §7.11).

A parent agent run (Atlas) holds a delegated run token. Its sub-agents get EXCHANGED tokens through
the kernel's `subagents` middleware (services/subagent_runs.OboExchange → identity/runs.spawn_child):
narrower than the parent's (a reader gets only `:read` permissions), bound to their own keys,
nested under the parent's run id. Two synchronous children wait on a person (so they are live), a
background child runs on its agent_runs row. One `passports.revoke` of the parent then — inside
that one transaction, before it commits — revokes every child token and halts the background
child's run.

Needs a throwaway Postgres holding the identity block (BOSS_IDENTITY_TEST_DATABASE_URL, as every
S3 identity test).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.interrupts import MemoryInterrupts, interrupt
from boss_engine.kernel.scripted import Call, Calls, Reply, Say, ScriptedEngine
from boss_engine.kernel.subagents import Coordination, SubagentDef, SubagentRequest, Subagents
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.types import ToolSchema

from ._identity_agents_support import BUILDER, build_env, session_ctx, start, verify

PARENT_RUN = "run_ag:arun_lead"
OBJ = {"type": "object", "properties": {"q": {"type": "string"}}, "additionalProperties": True}


def _forget_identity_caches() -> None:
    """build_env points the identity caches at ITS keys and database; a later test in the same
    process must not sign or hash with them (they outlive monkeypatch's restore)."""
    from boss_engine.identity import audit, authn_support, grants, signing_keys, vault

    vault.reset_master_key_cache()
    signing_keys.clear_cache()
    grants.clear_guard_cache()
    audit.reset_chain_key_cache()
    authn_support.reset_server_key_for_tests()


@pytest.fixture()
def env(monkeypatch):
    env = build_env(monkeypatch)
    yield env
    monkeypatch.undo()          # the environment first, then the caches built from it
    _forget_identity_caches()


@pytest.fixture(autouse=True)
def _fresh_identity_caches():
    yield
    _forget_identity_caches()


def _setup_tables(env):
    with env.db() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS agent_runs (id TEXT PRIMARY KEY, status TEXT "
                       "NOT NULL, updated_at TIMESTAMPTZ)"))
        c.execute(text("TRUNCATE agent_runs"))
        for key, agent in (("atlas", env.atlas), ("forge", env.forge)):
            c.execute(text("INSERT INTO roles (id, role_key, agent_id, project_id) "
                           "VALUES (:i, :k, :a, :p)"),
                      {"i": f"role_{key}", "k": key, "a": agent, "p": env.project})


def _parent_context(env):
    from boss_engine.identity import tokens

    _setup_tables(env)
    # Spawning a sub-agent is running an agent: the parent's token must carry agent:invoke.
    env.world.grant(env.alice, {"agent:invoke"}, env.project)
    with env.db() as c:
        c.execute(text("INSERT INTO identity_agent_ceilings VALUES (:a, 'agent:invoke') "
                       "ON CONFLICT DO NOTHING"), {"a": env.atlas})
    g = env.grant(agents=(env.atlas,), children=(env.forge,),
                  permissions=BUILDER + ("agent:invoke",))
    shim = start(env, g.id, agent=env.atlas, run=PARENT_RUN, scope=BUILDER + ("agent:invoke",))
    with env.db() as c:
        ctx = tokens.context_for(verify(env, shim), db=c)
    return g, shim, ctx


def test_d_revoking_the_parents_passport_halts_every_child_in_one_transaction(env, monkeypatch):
    from boss_engine.identity import lineage, passports
    from boss_engine.services.subagent_runs import OboExchange

    env.world.grant(env.alice, {"agent_passport:revoke"})
    _grant, shim, ctx = _parent_context(env)
    parent_scope = set(shim.tokens["boss:api"].scope)

    def ask(args):  # a child that waits on a person stays live, holding its token
        return interrupt("question", {"question": f"which {args.get('q')}?"},
                         schema={"fields": [{"key": "a", "type": "text"}]})

    tools = FunctionToolSet([Tool(ToolSchema("ask", "ask a person", OBJ), ask)])

    def brain(messages, offered):
        if offered and "task" in offered:
            return Calls(Call("task", {"subagent": "forge", "brief": "first"}),
                         Call("task", {"subagent": "forge", "brief": "second"}))
        return Call("ask", {"q": messages[-1]["content"]})

    identity = OboExchange(env.engine, project_id=env.project, base_url="https://boss.test")
    mw = Subagents(Coordination(enabled=True, max_tokens=50_000, general_purpose=False),
                   defs={"forge": SubagentDef(ref="forge")}, identity=identity)
    with lineage.acting(ctx), env.engine.connect() as db:
        held = kernel.run(kernel.RunSpec(
            engine=ScriptedEngine([], after=Reply(brain)), tools=tools,
            policies=presets.agent_loop(4), project_id=env.project, system="lead",
            messages=[{"role": "user", "content": "go"}], middleware=(mw,), db=db,
            interrupts=MemoryInterrupts(), parallel_interrupts=True, run_id="arun_lead"))
        db.commit()
        assert held.status == "suspended", (held.error, held.trace)
        # A background child: its exchange at start, its run on a worker (an agent_runs row).
        background = SubagentRequest(
            target="forge", brief="long work", mode="isolated", child_run_id="arun_bg1",
            parent_run_id="arun_lead", project_id=env.project, call_id="c9", depth=1,
            report_tokens=500, token_budget=1000, usd_micros_budget=None, messages=[])
        with identity.child(background):
            pass
    with env.db() as c:
        c.execute(text("INSERT INTO agent_runs (id, status) VALUES ('arun_bg1', 'running')"))

    with env.db() as c:
        children = c.execute(text(
            "SELECT run_id, scope, revoked_at FROM identity_run_credentials "
            "WHERE run_id LIKE :c ORDER BY run_id"), {"c": PARENT_RUN + "/%"}).mappings().all()
    child_runs = sorted({r["run_id"] for r in children})
    assert len(child_runs) == 3, child_runs
    assert all(r["revoked_at"] is None for r in children), "all three children are live"
    for row in children:
        scope = set(row["scope"] if isinstance(row["scope"], list) else [])
        assert scope and scope < parent_scope, "each child holds LESS than the parent"
        assert all(p.endswith(":read") for p in scope), "a read-only child holds only reads"
    assert {r for r in child_runs} >= {PARENT_RUN + "/ag:arun_bg1"}

    # ONE TRANSACTION: revoke the parent's Passport and look before it commits.
    with env.db() as c:
        report = passports.revoke(session_ctx(env.alice), env.atlas, reason="compromised", db=c)
        live = c.execute(text(
            "SELECT run_id FROM identity_run_credentials WHERE (run_id = :p OR run_id LIKE :c) "
            "AND revoked_at IS NULL"), {"p": PARENT_RUN, "c": PARENT_RUN + "/%"}).all()
        assert live == [], "the parent's and every child's token died in this transaction"
        assert c.execute(text("SELECT status FROM agent_runs WHERE id = 'arun_bg1'")
                         ).scalar() == "paused", "the background child's run is halted"
        assert set(child_runs) <= set(report.runs_halted)
    assert report.run_tokens_revoked >= 4  # the parent's and the three children's


def test_a_reader_child_cannot_widen_and_an_exchange_refusal_refuses_the_spawn(env):
    """A child agent the grant does not allow as a child: the exchange is refused, and so is the
    spawn — the parent reads why; nothing runs under a token it could not get."""
    from boss_engine.identity import lineage
    from boss_engine.services.subagent_runs import OboExchange

    with env.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id, project_id) VALUES "
                       "('role_rook', 'rook', :a, :p)"), {"a": env.rook, "p": env.project})
    _grant, _shim, ctx = _parent_context(env)
    engine = ScriptedEngine([Call("task", {"subagent": "rook", "brief": "x"}), Say("told")],
                            after=Say("must not run"))
    mw = Subagents(Coordination(enabled=True, max_tokens=50_000, general_purpose=False),
                   defs={"rook": SubagentDef(ref="rook")},
                   identity=OboExchange(env.engine, project_id=env.project,
                                        base_url="https://boss.test"))
    with lineage.acting(ctx), env.engine.connect() as db:
        result = kernel.run(kernel.RunSpec(
            engine=engine, tools=FunctionToolSet([]), policies=presets.agent_loop(4),
            project_id=env.project, system="lead", messages=[{"role": "user", "content": "go"}],
            middleware=(mw,), run_id="arun_lead", db=db))
    entry = result.trace[0]
    assert entry["status"] == "refused" and entry["result"]["error"] == "exchange_refused"
    assert engine.calls == 2, "the child never made a model call"
    assert BUILDER  # the support module's builder permissions are the parent's ceiling
