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
    "engine/boss_engine/services/comms.py": (MIGRATE, "inbox / Slack / email decision replies"),
    "engine/boss_engine/services/github_triage_agent.py": (MIGRATE, "GitHub issue triage"),
    "engine/boss_engine/services/app_alerts.py": (MIGRATE, "alert triage and flap tuning"),
    "engine/boss_engine/services/investigation.py": (MIGRATE, "incident root-cause verdict "
                                                     "(recorded, acts on nothing)"),
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
