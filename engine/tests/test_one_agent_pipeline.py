"""ONE AGENT PIPELINE (BOSSAPP-759, docs/product/ONE_AGENT_PIPELINE.md).

Operator, 2026-10-23: "There must be ONE agent pipeline, and all agents go through it … console,
API, CLI, and agent-to-agent too." The pipeline is AgentService.chat → IntentRouter.route_turn
(agent_decision.decide picks the turn's action; EngineAgent runs the tools; offers are pressed
through agent_action).

This test fails when a module NOT listed below starts making agent decisions of its own: imports
the decision model (`agent_decision`), builds or calls the router, calls a model directly, or runs
its own tool loop. Every listed module says why it is allowed. A worker (a coding engine, a panel
seat writing a PRD, a reviewer) calls a model to PRODUCE something; it does not choose what BOSS
does next. Entries marked MIGRATE are conversational agents still outside the pipeline; the
doc's table says what is left for each. An entry that no longer needs its reason fails too, so
the list only shrinks."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("engine/boss_engine", "apps/api/app", "apps/worker/boss_worker")

#: What making an agent decision looks like in code.
SIGNS = {
    "agent_decision": "imports the decision model",
    "route_turn": "calls the router",
    "IntentRouter": "builds a router",
    "create_chat_completion": "calls a model (chat / tools)",
    "create_text_response": "calls a model (text)",
    "run_agent_loop": "runs its own tool loop",
}

PIPELINE = "the one pipeline"
TRANSPORT = "model transport, chooses nothing"
WORKER = "worker: produces an artifact, chooses no BOSS action"
MIGRATE = "MIGRATE: a conversational agent still outside the pipeline (see the doc)"

ALLOWED: dict[str, tuple[str, str]] = {
    # ── the pipeline ────────────────────────────────────────────────────────────────────────
    "engine/boss_engine/services/agent_service.py": (PIPELINE, "AgentService.chat: the one turn"),
    "engine/boss_engine/services/intent_router.py": (PIPELINE, "route_turn: the one decision"),
    "engine/boss_engine/services/agent_decision.py": (PIPELINE, "the decision model itself"),
    "engine/boss_engine/agent/engine_agent.py": (PIPELINE, "the router's answering tool loop"),
    "engine/boss_engine/services/subject_answer.py": (PIPELINE, "route_turn's one-call answer"),
    "apps/api/app/main.py": (PIPELINE, "agent_turn; POST /v1/intent/route is route_turn without "
                                       "a thread (the same decision code)"),
    # ── transport ───────────────────────────────────────────────────────────────────────────
    "engine/boss_engine/services/claude_cli_adapter.py": (TRANSPORT, "a model adapter"),
    "engine/boss_engine/services/model_router/store.py": (TRANSPORT, "model routing probe"),
    "engine/boss_engine/services/monitoring_pack.py": (TRANSPORT, "uses agent_decision._routed "
                                                       "to pick a model, not an action"),
    # ── workers ─────────────────────────────────────────────────────────────────────────────
    "engine/boss_engine/services/code_agent.py": (WORKER, "a coding engine inside a build step"),
    "engine/boss_engine/services/github_pr_review_agent.py": (WORKER, "writes a PR review"),
    "engine/boss_engine/multirepo/change_sets.py": (WORKER, "plans a multi-repo change set"),
    "engine/boss_engine/services/architecture_spend.py": (WORKER, "writes architecture docs"),
    "engine/boss_engine/services/meeting_service.py": (WORKER, "panel seats and PRD synthesis"),
    "engine/boss_engine/services/meeting_actions.py": (WORKER, "extracts a meeting's action list; "
                                                       "a separate step creates the effects"),
    "engine/boss_engine/services/prd_decomposition.py": (WORKER, "splits a PRD into tasks"),
    "engine/boss_engine/services/skill_executors.py": (WORKER, "model.generate_* skills"),
    "engine/boss_engine/services/video_planner.py": (WORKER, "writes a validated edit plan"),
    "engine/boss_engine/services/workflow_drafting.py": (WORKER, "drafts a workflow for review"),
    "engine/boss_engine/services/typed_decisions.py": (WORKER, "answers a workflow author's "
                                                       "typed question for its branch"),
    "engine/boss_engine/services/workflow_precheck_agent.py": (WORKER, "advisory read-only "
                                                               "pre-check predictions"),
    "engine/boss_engine/services/sre_ask.py": (WORKER, "plans a read-only query; never executes"),
    "engine/boss_engine/services/software_factory.py": (WORKER, "writes the factory plan artifact"),
    "engine/boss_engine/services/conversation_spend.py": (MIGRATE, "transport of agent_converse "
                                                          "talk / plan / room"),
    # ── still outside the pipeline ──────────────────────────────────────────────────────────
    "engine/boss_engine/services/agent_loop.py": (MIGRATE, "workflow agent step's own tool loop"),
    "engine/boss_engine/services/agent_steps.py": (MIGRATE, "workflow agent step: picks a role, "
                                                   "then agent_loop"),
    "engine/boss_engine/services/step_recovery.py": (MIGRATE, "a failed step's recovery agent"),
    "engine/boss_engine/services/app_alerts.py": (MIGRATE, "alert triage and flap tuning"),
}

#: These may never decide on their own again: Metatron's router and the captain's service were
#: the second decision path BOSSAPP-759 removed.
NEVER = {"apps/api/app/routers/metatron_captain.py",
         "engine/boss_engine/services/metatron_captain.py",
         "engine/boss_engine/tools/captain_tools.py",
         "engine/boss_engine/services/focused_agents.py",
         "engine/boss_engine/services/metatron_command.py"}


def _signs(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names += (node.module or "").split(".") + [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            names += [part for a in node.names for part in a.name.split(".")]
        found.update(n for n in names if n in SIGNS)
    return found


def _deciders() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.py")):
            signs = _signs(path)
            if signs:
                out[str(path.relative_to(ROOT))] = signs
    return out


def test_no_module_outside_the_pipeline_makes_an_agent_decision():
    deciders = _deciders()
    unlisted = {path: sorted(signs) for path, signs in deciders.items() if path not in ALLOWED}
    assert not unlisted, (
        "These modules make an agent decision outside the one pipeline. Route them through "
        "AgentService.chat (or agent_action for a press); a real worker goes in ALLOWED with "
        f"its reason: {unlisted}")
    assert not NEVER & set(deciders), sorted(NEVER & set(deciders))


def test_the_allow_list_only_shrinks():
    deciders = _deciders()
    stale = sorted(path for path in ALLOWED if path not in deciders)
    assert not stale, f"no longer decides on its own — remove from ALLOWED: {stale}"


def test_the_engine_cli_turn_is_an_ask_boss_turn(monkeypatch, capsys):
    """`boss-engine ask` with a database goes through AgentService.chat, the one turn."""
    import types

    from boss_engine import __main__ as cli
    from boss_engine.services import agent_service

    seen: list[dict] = []

    class Ctx:
        durable_workspace_configured = True
        settings = object()

        def begin(self):
            import contextlib

            return contextlib.nullcontext("conn")

    def chat(self, connection, **kwargs):
        seen.append(kwargs)
        return {"thread_id": "athr_cli", "reply": "I can't create projects from here.",
                "proposed_actions": [{"id": "pa", "tool": "watch", "args": {}, "label": "Watch"}]}

    monkeypatch.setattr(cli, "_ctx", lambda: Ctx())
    monkeypatch.setattr(agent_service.AgentService, "__init__", lambda self, settings: None)
    monkeypatch.setattr(agent_service.AgentService, "chat", chat)
    assert cli._cmd_ask(types.SimpleNamespace(project="boss-dogfood", message="set up projects",
                                              allow_writes=False)) == 0
    out = capsys.readouterr()
    assert seen == [{"project_id": "boss-dogfood", "actor": "cli", "message": "set up projects",
                     "thread_id": None}]
    assert "can't create projects" in out.out and "Watch [watch]" in out.err


def test_the_doc_accounts_for_every_entry_left_to_migrate():
    doc = (ROOT / "docs" / "product" / "ONE_AGENT_PIPELINE.md").read_text()
    for path, (kind, _why) in ALLOWED.items():
        if kind == MIGRATE:
            assert Path(path).name in doc, path


# ── Domain agents take their turn through the pipeline (AgentService.decide) ─────────────────

#: Entry points that used to decide on their own and now take a turn through the pipeline. Each
#: must call `AgentService…decide` — the router's one decision — and nothing on SIGNS.
MIGRATED = {
    "engine/boss_engine/services/investigation.py": "incident verdict",
    "engine/boss_engine/services/comms.py": "inbox / Slack / email decision replies",
    "engine/boss_engine/services/github_triage_agent.py": "GitHub issue triage",
}


def _calls_the_domain_turn(path: str) -> bool:
    tree = ast.parse((ROOT / path).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    decides = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "decide" for n in ast.walk(tree))
    return "AgentService" in names and decides


def test_every_migrated_entry_point_takes_its_turn_through_the_pipeline():
    deciders = _deciders()
    for path, what in MIGRATED.items():
        assert path not in deciders, (what, sorted(deciders[path]))
        assert path not in ALLOWED, what
        assert _calls_the_domain_turn(path), f"{what} ({path}) does not call AgentService.decide"


class _Model:
    """A scripted decision model (no paid call) answering agent_decision's JSON contract."""

    def __init__(self, action: str, confidence: float = 0.9):
        self.action, self.confidence, self.prompts = action, confidence, []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        import json

        self.prompts.append((system_prompt, prompt))
        return "r", json.dumps({"action": self.action, "confidence": self.confidence,
                                "reason": "scripted", "question": ""}), {"model": "scripted"}


def test_a_domain_turn_is_the_routers_one_decision(monkeypatch):
    """AgentService.decide → IntentRouter.decide_among → agent_decision.decide: the same seam the
    console's turn decides through, with the caller's choices, record and hints as its input."""
    from boss_engine.services import agent_decision
    from boss_engine.services.agent_service import AgentService, Choice

    seen: list[dict] = []
    real = agent_decision.decide

    def spy(connection, settings, project_id, **kwargs):
        seen.append(kwargs)
        return real(connection, settings, project_id, **kwargs)

    monkeypatch.setattr(agent_decision, "decide", spy)
    model = _Model("noise")
    got = AgentService(None).decide(
        None, project_id="p", actor="boss.on_call", role="sre", agent="the on-call agent",
        external_ref="alert:a1", situation="An alert fired.", message="Triage it.",
        choices=[Choice("noise", "mark it noise"), Choice("page_now", "page someone")],
        context={"signals": ["x"]}, hints=["a guess"], adapter=model, min_confidence=0.5)
    assert got.decided and got.action == "noise"
    assert [c.key for c in seen[0]["candidates"]] == ["noise", "page_now"]
    assert seen[0]["context"] == {"signals": ["x"]} and seen[0]["hints"] == ["a guess"]
    assert seen[0]["agent"] == "the on-call agent" and seen[0]["ref"] == "alert:a1"
    assert "the on-call agent" in model.prompts[0][0]


def test_a_domain_turn_outside_the_choices_is_refused_not_coerced():
    from boss_engine.services.agent_service import CLARIFY, AgentService, Choice

    got = AgentService(None).decide(
        None, project_id="p", actor="x", external_ref="", situation="s", message="m",
        choices=[Choice("a", "do a")], adapter=_Model("b"))
    assert got.status == CLARIFY and got.action is None


def test_a_domain_turn_is_kept_on_its_records_thread():
    """The turn and its verdict are written on the record's own thread (surface `agent`, never
    the chat rail), and the next turn on that record reads them as its history."""
    from uuid import uuid4

    from sqlalchemy import text

    from ._obs_seed import pg_engine, project
    from boss_engine.services.agent_service import DOMAIN_SURFACE, AgentService, Choice

    eng = pg_engine()
    ref = f"incident:inc_{uuid4().hex[:8]}"
    with eng.begin() as c:
        pid = project(c)
    service = AgentService(None)
    first, second = _Model("h0"), _Model("unknown")
    choices = [Choice("h0", "the cause is the deploy"), Choice("unknown", "not known", acts=False)]
    with eng.begin() as c:
        service.decide(c, project_id=pid, actor="boss.investigation", role="sre",
                       external_ref=ref, situation="s", message="Which cause?", choices=choices,
                       adapter=first)
    with eng.begin() as c:
        service.decide(c, project_id=pid, actor="boss.investigation", role="sre",
                       external_ref=ref, situation="s", message="Which cause now?",
                       choices=choices, adapter=second)
        rows = c.execute(text(
            "SELECT t.surface, t.agent_role, m.role, m.content FROM agent_threads t "
            "JOIN agent_messages m ON m.thread_id = t.id WHERE t.project_id = :p "
            "AND t.external_ref = :r ORDER BY m.created_at, CASE m.role WHEN 'user' THEN 0 "
            "ELSE 1 END"), {"p": pid, "r": ref}).all()
    assert {r[0] for r in rows} == {DOMAIN_SURFACE} and {r[1] for r in rows} == {"sre"}
    assert [r[2] for r in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[1][3].startswith("h0 (confidence 0.9)")
    # The second turn read the first as its history.
    assert "Which cause?" in second.prompts[0][1] and "h0 (confidence" in second.prompts[0][1]
