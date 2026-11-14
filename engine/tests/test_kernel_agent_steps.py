"""K4 (AP-P1b): workflow agent steps (agent_loop.run_agent_loop) run on the harness kernel.

BOSS_KERNEL_STEPS is ON by default. A run is stamped with the loop it starts on
(finalize_ctx["loop"]) and resumes there; BOSS_KERNEL_STEPS=0 starts new runs on the old loop.
Both loops drive ONE toolset (agent_loop.AgentStepToolSet) assembled from the agent's grants —
the loop's private schema list and its name → permission map are gone: each native tool's
permission is declared next to the tool and catalogued in `native_tool_registry`.

THE SHADOW COMPARISON. Each scenario below is played twice with the same script on the scripted
conformance engine — once on the old loop, once on the kernel — over the same in-memory
agent_runs row and the same fakes, and the two runs must agree on: what the model was sent
(messages and the tools offered, request by request), which tools actually RAN and with what
arguments, which Decisions were raised, how the run ended, and what it cost per call (one
llm_calls row per model call, priced by the scripted engine). The cost table is printed
(`pytest -s`) and asserted: the kernel never spends more per run or per call.

Scenarios: a native read then the answer; the step cap's forced answer; a held skill running;
a skill over the risk threshold held on a Decision, approved, run ONCE, and the run finishing;
an MCP tool on a reviewed server, and one held because its tier needs a person; a fetched page
whose text a later write would carry (held); a busy per-minute budget parking the run.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from boss_engine.kernel.scripted import Call, Calls, Say, ScriptedEngine
from boss_engine.services import agent_loop

COST = 1_200  # what each scripted model call reports it cost, in micro-dollars
SETTINGS = SimpleNamespace(boss_edition="community", agent_max_steps=6,
                           agent_autonomous_risk_max=2, tool_result_screen="patterns")
READ = {"type": "object", "properties": {"run": {"type": "string"}}, "required": ["run"],
        "additionalProperties": False}
PAGE_TEXT = "The quarterly numbers on this page are not for sharing with anyone outside, ever."

#: The deleted name → permission map (agent_loop.AGENT_TOOL_PERMISSIONS on main before K4),
#: pinned: the registry that replaced it must declare exactly these.
LEGACY_PERMISSIONS = {
    "describe_schema": "system:read", "diagnose_deployment": "deployment:read",
    "diagnose_preview": "preview:read", "get_budget_and_costs": "budget:read",
    "get_project_status": "project:read", "inspect_app": "artifact:read",
    "list_apps": "artifact:read", "list_repo_files": "repository:read",
    "read_repo_file": "repository:read", "run_shell": "sandbox:exec",
    "change_existing_app": "code_task:create", "create_followup_task": "work_item:manage",
    "open_incident": "reliability:respond", "publish_document": "github:sync",
    "ask_human": "work_item:manage",
}


# ════ The private map and toolset are gone ════════════════════════════════════════════════════

def test_the_native_registry_declares_exactly_the_permissions_the_deleted_map_did():
    registry = agent_loop.native_tool_registry()
    assert {name: registry.get(name).permission for name in LEGACY_PERMISSIONS} \
        == LEGACY_PERMISSIONS
    assert sorted(registry.names()) == sorted(LEGACY_PERMISSIONS)


def test_the_private_permission_map_is_deleted():
    assert not hasattr(agent_loop, "AGENT_TOOL_PERMISSIONS")
    source = open(agent_loop.__file__).read()
    assert "AGENT_TOOL_PERMISSIONS" not in source


def test_both_loops_build_their_tools_in_one_place():
    import inspect

    legacy = inspect.getsource(agent_loop._drive_run_legacy)
    kernel_side = inspect.getsource(agent_loop._drive_run_on_kernel)
    for source in (legacy, kernel_side):
        assert "AgentStepToolSet(connection, settings, run, run_id)" in source
        assert "_native_tool_schemas(" not in source and "list_mcp_servers(" not in source


# ════ The flag and the stamp ══════════════════════════════════════════════════════════════════

def test_the_flag_is_on_by_default_and_an_off_word_turns_it_off(monkeypatch):
    monkeypatch.delenv("BOSS_KERNEL_STEPS", raising=False)
    assert agent_loop.kernel_steps()
    for word in ("0", "false", "off"):
        monkeypatch.setenv("BOSS_KERNEL_STEPS", word)
        assert not agent_loop.kernel_steps()


def test_a_run_resumes_on_the_loop_it_started_on(monkeypatch):
    fresh = {"status": "running", "step": 0, "transcript": [], "finalize_ctx": {}}
    under_way = {"status": "running", "step": 3, "transcript": [{"tool": "x"}],
                 "finalize_ctx": {}}
    monkeypatch.setenv("BOSS_KERNEL_STEPS", "1")
    assert agent_loop._on_kernel(fresh)
    assert not agent_loop._on_kernel(under_way), "an old run moved loops mid-way"
    assert not agent_loop._on_kernel({**under_way, "finalize_ctx": {"loop": "legacy"}})
    monkeypatch.setenv("BOSS_KERNEL_STEPS", "0")
    assert not agent_loop._on_kernel(fresh)
    assert agent_loop._on_kernel({**under_way, "finalize_ctx": {"loop": "kernel"}})


# ════ The shadow harness ══════════════════════════════════════════════════════════════════════

class World:
    """One in-memory install: an agent_runs table, decisions, and every effect recorded."""

    def __init__(self):
        self.rows: dict = {}
        self.decisions: dict = {}
        self.ran: list = []
        self.events: list = []

    # agent_loop's own store
    def save(self, _c, run_id, project_id, task_id, status, messages, transcript, usage, fctx,
             *, step=0, pending=None, output_text=None):
        self.rows[run_id] = copy.deepcopy({
            "id": run_id, "project_id": project_id, "task_id": task_id, "status": status,
            "messages": messages, "transcript": transcript, "usage": usage,
            "finalize_ctx": fctx, "step": step, "pending": pending, "output_text": output_text})

    def load(self, _c, run_id):
        row = self.rows.get(run_id)
        return copy.deepcopy(row) if row else None

    def decide(self, _c, project_id, task_id, question, recommendation, *_a, **_k):
        decision_id = f"dec_{len(self.decisions) + 1}"
        self.decisions[decision_id] = {"question": question,
                                       "recommendation": dict(recommendation)}
        return decision_id

    # the connection resume_agent_run reads the decision through
    def execute(self, _sql, params=None):
        decision_id = str((params or {}).get("id") or "")
        decision = self.decisions.get(decision_id)
        row = None if decision is None else {
            "id": decision_id, "status": "approved", "question": decision["question"],
            "recommendation": decision["recommendation"],
            "answer": {"choice": "staging"}, "answered_by": "usr_ops", "approved_by": "usr_ops",
            "resolution_note": ""}
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))

    def begin_nested(self):
        return SimpleNamespace(commit=lambda: None, rollback=lambda: None,
                               __enter__=lambda *_: None, __exit__=lambda *_: False)


class Conn:
    """The connection the loop is handed: decisions are read through it; nested savepoints
    are no-ops."""

    engine = None

    def __init__(self, world):
        self.world = world

    def execute(self, sql, params=None):
        return self.world.execute(sql, params)

    def begin_nested(self):
        class _Nested:
            def commit(self):
                pass

            def rollback(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return _Nested()


SKILL_TRIM = {"id": "media.trim", "kind": "builtin", "description": "Trim a clip.",
              "risk_tier": 1, "input_schema": {"clip": "string"}}
SKILL_PUBLISH = {"id": "media.publish", "kind": "builtin", "description": "Publish a clip.",
                 "risk_tier": 3, "input_schema": {"clip": "string"}}


def install(monkeypatch, world, *, loop, adapter, skills=(), mcp=(), budget_busy=False,
            bounded=False):
    """agent_loop wired to `world`, the scripted `adapter`, these skills and MCP servers."""
    monkeypatch.setenv("BOSS_KERNEL_STEPS", "1" if loop == "kernel" else "0")
    monkeypatch.setattr(agent_loop, "load_agent_run", world.load)
    monkeypatch.setattr(agent_loop, "_save_run", world.save)
    monkeypatch.setattr(agent_loop, "project_chat_adapter", lambda *_a, **_k: adapter)
    monkeypatch.setattr(agent_loop, "governed", lambda a, *_a, **_k: a)
    monkeypatch.setattr(agent_loop, "_agent_purpose", lambda *_a, **_k: None)
    monkeypatch.setattr(agent_loop, "emit_event",
                        lambda _c, _p, kind, *_a, **_k: world.events.append(kind))
    monkeypatch.setattr(agent_loop, "create_blocking_decision", world.decide)
    monkeypatch.setattr(agent_loop, "_skill_policy", lambda *_a, **_k: None)
    monkeypatch.setattr(agent_loop, "_skill_resolution_for_run", lambda *_a, **_k: {
        "eager": [dict(s) for s in skills], "on_demand": [], "role_key": "editor"})
    servers = [dict(s) for s in mcp]
    monkeypatch.setattr(agent_loop, "list_mcp_servers", lambda *_a, **_k: servers)
    monkeypatch.setattr(agent_loop, "_granted_mcp_servers", lambda *_a, **_k: servers)
    monkeypatch.setattr(agent_loop, "auth_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(agent_loop, "known_unreachable", lambda *_a, **_k: False,
                        raising=False)

    class Mcp:
        def __init__(self, _settings):
            pass

        def list_tools(self, url, headers=None):
            return [{"name": "fetch_page", "description": "Fetch a page",
                     "inputSchema": {"type": "object",
                                     "properties": {"url": {"type": "string"}}}},
                    {"name": "delete_repo", "description": "Delete a repository",
                     "inputSchema": {"type": "object",
                                     "properties": {"name": {"type": "string"}}}}]

    monkeypatch.setattr(agent_loop, "MCPClient", Mcp)

    def invoke_mcp(_c, _s, _p, server, tool, args, **kw):
        world.ran.append(("mcp", tool.get("name"), dict(args), kw.get("approved_by")))
        return {"content": PAGE_TEXT}

    def invoke_skill(_c, _s, _p, skill_id, caller, args, **kw):
        world.ran.append(("skill", skill_id, dict(args), kw.get("approved_by")))
        return {"status": "succeeded", "skill_id": skill_id, "output": {"clip": "c2.mp4"}}

    def execute(_c, _p, _s, tool, args, actor="system", task_id=None):
        world.ran.append(("write", tool, dict(args), actor))
        return {"ok": True, "filed": True}

    monkeypatch.setattr(agent_loop, "_invoke_mcp_skill", invoke_mcp)
    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", invoke_skill)
    monkeypatch.setattr(agent_loop, "execute_agent_tool", execute)
    monkeypatch.setattr(agent_loop, "_native_tool_schemas", lambda settings: [
        {"type": "function", "function": {"name": "read_logs",
                                          "description": "Read a run's logs.",
                                          "parameters": READ}}])
    def dispatch(_c, _p, _s, name, args):
        if name != "read_logs":  # agent_loop._dispatch's own answer to a name it lacks
            return {"error": f"unknown tool: {name}"}
        world.ran.append(("native", name, dict(args), None))
        return {"ok": True, "lines": [f"run {args.get('run')}: 3 errors"]}

    monkeypatch.setattr(agent_loop, "_dispatch", dispatch)
    if budget_busy:
        def busy(self, call):
            world.ran.append(("busy", None, {}, None))
            return ({"status": "skipped", "note": agent_loop.BUDGET_BUSY_MESSAGE},
                    {"retry_after_seconds": 30, "refusal": "per-minute cap"})

        monkeypatch.setattr(agent_loop.BudgetWaiter, "invoke", busy)


def start(world, *, loop, bounded, run_id="arun_shadow"):
    fctx = {"loop": loop}
    if bounded:
        fctx["agent_step"] = {"role_key": "editor", "skill_ids": ["media.trim"],
                              "mcp_server_ids": [], "actor": "workflow"}
    world.save(None, run_id, "p", None, "running",
               [{"role": "system", "content": "You are the editor."},
                {"role": "user", "content": "Trim the clip and publish it."}],
               [], agent_loop._zero_usage(), fctx, step=0)
    return run_id


def play(monkeypatch, script, *, loop, bounded=False, approve=False, **kw):
    """One scenario on one loop: (outcome[, resumed outcome], world, engine)."""
    world = World()
    engine = ScriptedEngine(list(script), cost_usd_micros=COST)
    install(monkeypatch, world, loop=loop, adapter=engine, bounded=bounded, **kw)
    run_id = start(world, loop=loop, bounded=bounded)
    conn = Conn(world)
    outcome = agent_loop._drive_run(conn, SETTINGS, run_id, None)
    outcomes = [outcome]
    if approve and outcome.get("status") == "suspended":
        outcomes.append(agent_loop.resume_agent_run(conn, SETTINGS, run_id))
    return outcomes, world, engine


def requests(engine):
    return [{"tools": r["tools"],
             "messages": [(m.get("role"), m.get("content")) for m in r["messages"]]}
            for r in engine.requests]


COSTS: dict = {}


def shadow(monkeypatch, name, script, **kw):
    """THE SHADOW COMPARISON: the same script on both loops must agree."""
    old, old_world, old_engine = play(monkeypatch, script, loop="legacy", **kw)
    new, new_world, new_engine = play(monkeypatch, script, loop="kernel", **kw)
    assert requests(new_engine) == requests(old_engine), name
    assert new_world.ran == old_world.ran, (name, old_world.ran, new_world.ran)
    assert [d["question"] for d in new_world.decisions.values()] == \
        [d["question"] for d in old_world.decisions.values()], name
    for a, b in zip(old, new):
        assert a["status"] == b["status"], (name, a, b)
        assert (a.get("output_text") or "") == (b.get("output_text") or ""), name
        assert bool(a.get("decision_id")) == bool(b.get("decision_id")), name
        assert [(t["tool"], t["args"]) for t in a.get("transcript") or []] == \
            [(t["tool"], t["args"]) for t in b.get("transcript") or []], name
    # COST PER CALL, from the calls each loop made (one llm_calls row each).
    old_cost = sum(int((o.get("usage") or {}).get("cost_usd_micros") or 0) for o in old)
    new_cost = sum(int((o.get("usage") or {}).get("cost_usd_micros") or 0) for o in new)
    old_calls, new_calls = old_engine.calls, new_engine.calls
    COSTS[name] = (old_calls, old_cost, new_calls, new_cost)
    assert new_calls <= old_calls and new_cost <= old_cost, COSTS[name]
    if new_calls:
        assert new_cost / new_calls <= (old_cost / old_calls if old_calls else new_cost)
    return old, new, old_world, new_world


# ════ The scenarios ═══════════════════════════════════════════════════════════════════════════

def test_shadow_a_native_read_then_the_answer(monkeypatch):
    old, new, *_ = shadow(monkeypatch, "native read",
                          [Call("read_logs", {"run": "r1"}), Say("r1 fails on auth.")])
    assert new[0]["status"] == "completed" and new[0]["output_text"] == "r1 fails on auth."


def test_shadow_the_step_cap_forces_the_answer(monkeypatch):
    monkeypatch.setattr(SETTINGS, "agent_max_steps", 2)
    old, new, *_ = shadow(monkeypatch, "step cap",
                          [Call("read_logs", {"run": "r1"}), Call("read_logs", {"run": "r2"}),
                           Say("Both runs are red.")])
    assert new[0]["output_text"] == "Both runs are red."


def test_shadow_a_held_skill_runs(monkeypatch):
    old, new, _, world = shadow(monkeypatch, "held skill",
                                [Call("skill_media_trim", {"clip": "c1.mp4"}), Say("Trimmed.")],
                                bounded=True, skills=[SKILL_TRIM])
    assert world.ran == [("skill", "media.trim", {"clip": "c1.mp4"}, None)]


def test_shadow_a_skill_over_the_threshold_waits_for_a_person_and_runs_once(monkeypatch):
    old, new, _, world = shadow(
        monkeypatch, "approval round trip",
        [Calls(Call("skill_media_publish", {"clip": "c2.mp4"}),
               Call("skill_media_trim", {"clip": "c3.mp4"})),
         Say("Published.")],
        bounded=True, skills=[SKILL_TRIM, SKILL_PUBLISH], approve=True)
    assert [o["status"] for o in new] == ["suspended", "completed"]
    # Held, its sibling skipped, then run ONCE under the approval.
    assert world.ran == [("skill", "media.publish", {"clip": "c2.mp4"}, "dec_1")]
    assert list(world.decisions) == ["dec_1"]
    assert new[1]["output_text"] == "Published."


def test_shadow_an_mcp_tool_runs_and_a_tier_that_needs_a_person_is_held(monkeypatch):
    reviewed = {"id": "srv_web", "name": "web", "url": "http://web.invalid/mcp",
                "risk_tier": 0, "enabled": True, "provenance": {"trusted": True}}
    old, new, _, world = shadow(monkeypatch, "mcp", [
        Call("mcp_web_fetch_page", {"url": "https://example.invalid"}),
        Say("Read it.")], mcp=[reviewed])
    assert world.ran == [("mcp", "fetch_page", {"url": "https://example.invalid"}, None)]
    unreviewed = {**reviewed, "provenance": {}}
    old, new, _, world = shadow(monkeypatch, "mcp held", [
        Call("mcp_web_delete_repo", {"name": "boss"}), Say("never")], mcp=[unreviewed])
    assert new[0]["status"] == "suspended" and world.ran == []
    assert "not reviewed" in list(world.decisions.values())[0]["question"]


def test_shadow_a_fetched_page_does_not_leave_through_a_write(monkeypatch):
    reviewed = {"id": "srv_web", "name": "web", "url": "http://web.invalid/mcp",
                "risk_tier": 0, "enabled": True, "provenance": {"trusted": True}}
    monkeypatch.setattr(agent_loop, "_WRITE_FNS", {
        "create_followup_task": (lambda *_a: {"ok": True}, 1)})
    monkeypatch.setattr(agent_loop, "_WRITE_SCHEMAS", [
        {"type": "function", "function": {
            "name": "create_followup_task", "description": "File a follow-up.",
            "parameters": {"type": "object",
                           "properties": {"title": {"type": "string"},
                                          "description": {"type": "string"}}}}}])
    old, new, _, world = shadow(monkeypatch, "untrusted write", [
        Call("mcp_web_fetch_page", {"url": "https://example.invalid"}),
        Call("create_followup_task", {"title": "note", "description": PAGE_TEXT}),
        Say("never")], mcp=[reviewed])
    assert new[0]["status"] == "suspended"
    assert [r[0] for r in world.ran] == ["mcp"], "the page left through a write"


def test_shadow_a_busy_minute_parks_the_step(monkeypatch):
    old, new, *_ = shadow(monkeypatch, "budget wait",
                          [Call("skill_media_trim", {"clip": "c1.mp4"}), Say("never")],
                          bounded=True, skills=[SKILL_TRIM], budget_busy=True)
    assert new[0]["status"] == old[0]["status"] == agent_loop.BUDGET_WAIT
    assert new[0]["retry_after_seconds"] == 30


def test_shadow_a_typed_question_parks_and_its_answer_resumes_the_run(monkeypatch):
    from boss_engine.services import typed_forms

    asked: list = []

    def ask_human(_c, *, project_id, question, form, asker, run_id=None, **_k):
        asked.append(question)
        return {"decision_id": f"dec_{len(asked)}"}

    monkeypatch.setattr(typed_forms, "ask_human", ask_human)
    monkeypatch.setattr(typed_forms, "emit_event", lambda *_a, **_k: None)

    def with_question(monkeypatch_, world):
        monkeypatch_.setattr(agent_loop, "_native_tool_schemas", lambda settings: [
            typed_forms.ASK_HUMAN_SCHEMA])
        world.decisions["dec_1"] = {"question": "Which environment?", "recommendation": {}}

    old, new = [], []
    for loop, out in (("legacy", old), ("kernel", new)):
        asked.clear()
        world = World()
        engine = ScriptedEngine([Call("ask_human", {"question": "Which environment?", "fields": [
                                     {"type": "select", "options": ["staging", "prod"]}]}),
                                 Say("Deploying to staging.")], cost_usd_micros=COST)
        install(monkeypatch, world, loop=loop, adapter=engine)
        with_question(monkeypatch, world)
        run_id = start(world, loop=loop, bounded=False)
        conn = Conn(world)
        out.append(agent_loop._drive_run(conn, SETTINGS, run_id, None))
        out.append(agent_loop.resume_agent_run(conn, SETTINGS, run_id))
        out.append(requests(engine))
    assert [o["status"] for o in new[:2]] == [o["status"] for o in old[:2]] \
        == ["suspended", "completed"]
    assert new[1]["output_text"] == old[1]["output_text"] == "Deploying to staging."
    assert new[2] == old[2], "the model read the answer differently"
    assert "HUMAN ANSWER" in str(new[2][-1]["messages"][-1][1])


def test_zz_the_cost_per_call_table(capsys):
    """Printed with -s; every row asserted in `shadow` (kernel ≤ old, per run and per call)."""
    assert COSTS, "run with the scenarios above"
    lines = ["scenario                 old calls  old $µ   kernel calls  kernel $µ  Δ$µ/call"]
    for name, (oc, ocost, nc, ncost) in sorted(COSTS.items()):
        per_old = ocost / oc if oc else 0
        per_new = ncost / nc if nc else 0
        lines.append(f"{name:24s} {oc:9d} {ocost:7d} {nc:13d} {ncost:10d} {per_new - per_old:9.1f}")
    with capsys.disabled():
        print("\n" + "\n".join(lines))


# ════ The kernel run's row is the old loop's row ══════════════════════════════════════════════

def test_the_kernel_runs_row_reads_like_the_old_loops(monkeypatch):
    (outcome,), world, _ = play(monkeypatch, [Call("read_logs", {"run": "r1"}), Say("done")],
                                loop="kernel")
    row = world.rows["arun_shadow"]
    assert row["status"] == "completed" and row["output_text"] == "done"
    assert row["messages"][0] == {"role": "system", "content": "You are the editor."}
    assert row["transcript"][0]["tool"] == "read_logs" and row["transcript"][0]["step"] == 0
    assert row["finalize_ctx"]["loop"] == "kernel" and "kernel" in row["finalize_ctx"]
    assert row["step"] == 2 and row["usage"]["cost_usd_micros"] == 2 * COST
    json.dumps(row)  # the row is JSON, whatever a tool returned


def test_a_fatal_model_failure_still_raises_out_of_the_run(monkeypatch):
    from boss_engine.kernel.scripted import Fail

    with pytest.raises(RuntimeError, match="invalid_request"):
        play(monkeypatch, [Fail(ValueError("400 invalid_request: bad model"))], loop="kernel")
