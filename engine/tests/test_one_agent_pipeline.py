"""ONE AGENT PIPELINE (BOSSAPP-759, docs/product/ONE_AGENT_PIPELINE.md).

Operator, 2026-10-23: "There must be ONE agent pipeline, and all agents go through it … console,
API, CLI, and agent-to-agent too." The pipeline is AgentService.chat → IntentRouter.route_turn
(agent_decision.decide picks the turn's action; EngineAgent runs the tools; offers are pressed
through agent_action).

This test fails when a module NOT listed below starts making agent decisions of its own: imports
the decision model (`agent_decision`), builds or calls the router, calls a model directly, runs
its own tool loop, drives a coding engine (`run_role` / `run_research` / `make_code_agent` /
a CodeAgent constructor), builds a decision model (`TypeSafeDecider`), or runs the harness
kernel (`kernel.run`). Every listed module says why it is allowed. A worker (a coding engine, a
panel seat writing a PRD, a reviewer) calls a model to PRODUCE something; it does not choose
what BOSS does next. Entries marked MIGRATE are conversational agents still outside the
pipeline; the doc's table says what is left for each. An entry that no longer needs its reason
fails too, so the list only shrinks.

THE KERNEL (BOSSAPP-961). `boss_engine/kernel` is the one shared loop the old loops move onto
(K2-K6, K11). KERNEL entries are its own modules; once the moves land (K14), a module outside
the kernel that calls `create_chat_completion` with tools joins the refused class here — the
"six loops" problem, enforced."""
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
    "stream_text_response": "calls a model (streamed text)",
    "create_plain_answer": "calls a model (text)",
    "run_agent_loop": "runs its own tool loop",
    "run_role": "drives a coding engine",
    "run_research": "drives a research engine",
    "make_code_agent": "builds a coding engine",
    "TypeSafeDecider": "builds a decision model",
}

#: Classes only `make_code_agent` (and the modules that define or register them) may construct.
CODE_AGENT_CONSTRUCTORS = frozenset({
    "ClaudeCodeAgent", "NativeCodeAgent", "CodexCodeAgent", "GenericCliCodeAgent",
    "PacedCodeAgent",
})

PIPELINE = "the one pipeline"
TRANSPORT = "model transport, chooses nothing"
WORKER = "worker: produces an artifact, chooses no BOSS action"
KERNEL = "the harness kernel: runs through the one loop"
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
    "engine/boss_engine/services/agent_loop.py": (
        WORKER, "carries out a workflow agent step's instruction (and the factory's plan) with "
                "only the step agent's granted skills and servers, each call re-checked by "
                "SkillRuntimeService.invoke; the runner, not the loop, decides what the run "
                "does next"),
    "engine/boss_engine/services/agent_steps.py": (
        WORKER, "runs a workflow agent step's instruction through agent_loop (the worker); "
                "WHICH agent runs it is a turn through the pipeline (choose_agent → "
                "AgentService.decide)"),
    # ── the coding family (visible since the signs widened for BOSSAPP-961 §8.3) ────────────
    "engine/boss_engine/services/governed_code_task_service.py": (
        WORKER, "the governed build pipeline: every stage is a run_role on the contracted "
                "engine make_code_agent built"),
    "engine/boss_engine/services/code_task_service.py": (
        WORKER, "builds the run's coding engine (make_code_agent) for the legacy lane, "
                "retries and guest runs"),
    "engine/boss_engine/services/engine_contract.py": (
        TRANSPORT, "the coding contract itself: ContractedAgent wraps every engine's run_role "
                   "with the capability fallbacks"),
    "engine/boss_engine/services/cli_harness.py": (
        TRANSPORT, "defines and drives the generic CLI harnesses (grok/cursor/aider) and "
                   "their login/catalog probes"),
    "engine/boss_engine/services/codex_cli_adapter.py": (
        TRANSPORT, "the Codex CLI as a raw completion backend, on the generic harness"),
    "engine/boss_engine/services/scripted_engine.py": (
        TRANSPORT, "the scripted conformance engine: plays a script, calls no model"),
    "engine/boss_engine/services/workspace_perms.py": (
        TRANSPORT, "the workspace-permission decorator wrapped around every engine's run_role"),
    "engine/boss_engine/services/setup_service.py": (
        WORKER, "Repo and models' CLI probe: a login check and one hello run, never a build"),
    "engine/boss_engine/services/engine_startup.py": (
        WORKER, "the start door's probe: can the engine routing picked actually start"),
    "engine/boss_engine/services/parallel_build.py": (
        WORKER, "the build split's work packages, each one a run_role"),
    "engine/boss_engine/services/pr_review_again.py": (
        WORKER, "re-runs the adversarial review on a pull request's real diff"),
    "engine/boss_engine/services/self_fix_build.py": (
        WORKER, "builds a self-fix through the coding contract (BOSSAPP-571)"),
    "engine/boss_engine/services/docs_agent.py": (
        WORKER, "writes documentation through the coding contract"),
    "engine/boss_engine/services/discovery_service.py": (
        WORKER, "runs web research sessions on a coding engine (run_research)"),
    "engine/boss_engine/services/verifier_debate.py": (
        WORKER, "the verifier debate (endpoint-only today)"),
    "engine/boss_engine/services/verifier_meta_eval.py": (
        WORKER, "scores the verifiers (endpoint-only today)"),
    "engine/boss_engine/services/debug_session.py": (
        MIGRATE, "a debug session on a coding engine; constructs ClaudeCodeAgent directly "
                 "instead of make_code_agent (the constructor check lists it too)"),
    "engine/boss_engine/services/typed_decision_spend.py": (
        TRANSPORT, "reserves, leases and settles a typed decision's spend; decides nothing"),
    "engine/boss_engine/services/knowledge/rulebook_extract.py": (
        WORKER, "extracts a rulebook's rules (the analyst's sessions on the coding contract)"),
    "engine/boss_engine/services/knowledge/rule_scan.py": (
        WORKER, "scans a repository for a rule through the coding contract"),
    "engine/boss_engine/services/knowledge/regulatory_trace.py": (
        WORKER, "traces a regulation to the code that implements it"),
    "engine/boss_engine/services/knowledge/banking_regulatory_pipeline.py": (
        WORKER, "the banking regulatory pipeline's sessions on the coding contract"),
    # ── the kernel (BOSSAPP-961 K1) ─────────────────────────────────────────────────────────
    "engine/boss_engine/kernel/engine.py": (
        KERNEL, "the kernel's engine seam: the ONE place a kernel run calls a model"),
    "engine/boss_engine/kernel/presets.py": (
        KERNEL, "each old loop's rules as kernel policies; imports the loops' own prompt "
                "constants so the copies cannot drift"),
}

#: These may never decide on their own again: Metatron's router and the captain's service were
#: the second decision path BOSSAPP-759 removed.
NEVER = {"apps/api/app/routers/metatron_captain.py",
         "engine/boss_engine/services/metatron_captain.py",
         "engine/boss_engine/tools/captain_tools.py",
         "engine/boss_engine/services/focused_agents.py",
         "engine/boss_engine/services/metatron_command.py"}


def _called(node: ast.Call) -> str:
    fn = node.func
    return fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute)
                                                   else "")


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
            # `from boss_engine.kernel… import run / resume` runs the kernel.
            if "kernel" in (node.module or "") and \
                    {alias.name for alias in node.names} & {"run", "resume"}:
                found.add("kernel.run")
        elif isinstance(node, ast.Import):
            names += [part for a in node.names for part in a.name.split(".")]
        if isinstance(node, ast.Call):
            called = _called(node)
            if called in CODE_AGENT_CONSTRUCTORS:
                found.add(f"{called}(")
            if called in ("run", "resume") and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "kernel":
                found.add("kernel.run")
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


# ── One constructor for every coding engine (BOSSAPP-961 §8.3 item 4) ─────────────────────────

#: Where a CodeAgent class may be constructed directly, and why. Everything else goes through
#: `make_code_agent`, so routing, the contract wrapper and governance cannot be skipped by
#: building an engine by hand. The list only shrinks.
CONSTRUCTS_ITS_OWN: dict[str, str] = {
    "engine/boss_engine/services/code_agent.py":
        "defines the classes; make_code_agent and the harness registry live here",
    "engine/boss_engine/services/cli_harness.py":
        "defines GenericCliCodeAgent and builds it for its own login/catalog probes",
    "engine/boss_engine/services/codex_cli_adapter.py":
        "builds the generic harness it wraps as a chat backend",
    "engine/boss_engine/services/setup_service.py":
        "Repo and models' CLI probe builds the harness it is probing",
    "engine/boss_engine/services/debug_session.py":
        "MIGRATE: constructs ClaudeCodeAgent directly; should take make_code_agent's engine",
}


def _constructor_sites() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.py")):
            ctors = {_called(node) for node in ast.walk(ast.parse(path.read_text()))
                     if isinstance(node, ast.Call) and _called(node) in CODE_AGENT_CONSTRUCTORS}
            if ctors:
                out[str(path.relative_to(ROOT))] = ctors
    return out


def test_make_code_agent_is_the_only_code_agent_constructor():
    sites = _constructor_sites()
    unlisted = {path: sorted(ctors) for path, ctors in sites.items()
                if path not in CONSTRUCTS_ITS_OWN}
    assert not unlisted, (
        "These modules construct a coding engine directly. Build it with make_code_agent so "
        f"routing, the contract and governance apply, or list the exception with why: {unlisted}")
    stale = sorted(path for path in CONSTRUCTS_ITS_OWN if path not in sites)
    assert not stale, f"no longer constructs an engine — remove from CONSTRUCTS_ITS_OWN: {stale}"


# ── Domain agents take their turn through the pipeline (AgentService.decide) ─────────────────

#: Entry points that used to decide on their own and now take a turn through the pipeline. Each
#: must call `AgentService…decide` — the router's one decision — and nothing on SIGNS.
MIGRATED = {
    "engine/boss_engine/services/investigation.py": "incident verdict",
    "engine/boss_engine/services/comms.py": "inbox / Slack / email decision replies",
    "engine/boss_engine/services/github_triage_agent.py": "GitHub issue triage",
    "engine/boss_engine/services/app_alerts.py": "alert triage and flap tuning",
    "engine/boss_engine/services/focused_agents.py": "the focused agents' duty loop",
    "engine/boss_engine/services/step_recovery.py": "a failed step's recovery agent (arguments, "
                                                    "a bounded read phase, validate as the "
                                                    "check with one re-ask)",
    "engine/boss_engine/services/agent_converse.py": "agent-to-agent talk, plan and room: the "
                                                     "plan's start card",
    "engine/boss_engine/services/project_experience/understanding.py": (
        "Helm's understanding in the New work modal: the brief as a structured decision's "
        "arguments, checked against the code graph"),
}

#: Entry points whose agents SPEAK through the pipeline's streaming answer turn
#: (`AgentService.answer_stream`), under the caller's spend envelope. The envelope itself
#: (conversation_spend.py) governs the call and makes none: it holds no sign.
SPEAKS_THROUGH_THE_PIPELINE = {
    "engine/boss_engine/services/agent_converse.py": "every talk, plan and room seat",
}
ENVELOPES = ("engine/boss_engine/services/conversation_spend.py",)

#: Moved too, but the module still holds a worker (ALLOWED says which): its decision is the
#: pipeline's, its execution stays a worker.
MIGRATED_BESIDE_A_WORKER = {
    "engine/boss_engine/services/agent_steps.py": "which agent runs a workflow agent step",
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
    for path, what in MIGRATED_BESIDE_A_WORKER.items():
        assert ALLOWED[path][0] == WORKER, what
        # Only the worker's sign is left: no decision model, router or one-shot model call.
        assert deciders.get(path, set()) <= {"run_agent_loop"}, (what, deciders.get(path))
        assert _calls_the_domain_turn(path), f"{what} ({path}) does not call AgentService.decide"


def test_every_seat_speaks_through_the_pipelines_streaming_answer():
    deciders = _deciders()
    for path, what in SPEAKS_THROUGH_THE_PIPELINE.items():
        tree = ast.parse((ROOT / path).read_text())
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "answer_stream" for n in ast.walk(tree)), what
        assert path not in deciders and path not in ALLOWED, what
    for path in ENVELOPES:
        assert path not in deciders and path not in ALLOWED, path


class _Envelope:
    """A caller's envelope (no paid call): pins its own adapter, counts the attempts it admits,
    and settles what the pipeline's call produced."""

    def __init__(self, pinned):
        self.pinned, self.attempts, self.settled, self.contacted = pinned, 0, None, []

    def run(self, produce, *, prompt, system_prompt):
        self.attempts += 1
        answer = produce(self.pinned)
        self.contacted.append(False)
        chunks, usage = [], {}
        try:
            for piece in answer:
                self.contacted[-1] = True
                if isinstance(piece, dict):
                    usage = piece
                else:
                    chunks.append(piece)
                    yield "chunk", piece
        finally:
            answer.close()
        self.settled = ("".join(chunks), usage)
        yield "done", self.settled


class _Streaming:
    def __init__(self, words):
        self.words, self.calls = words, []

    def stream_text_response(self, prompt, system_prompt=None):
        self.calls.append((system_prompt, prompt))
        yield from self.words
        yield {"model": "scripted", "prompt_tokens": 3, "completion_tokens": len(self.words)}


def test_a_seat_speaks_through_the_pipelines_streaming_answer_inside_the_callers_envelope():
    """AgentService.answer_stream: a role-addressed turn whose answer (engine_agent.stream_answer)
    streams word by word inside the caller's envelope, which pinned the model and settles it."""
    from boss_engine.services.agent_service import AgentService, SpendEnvelope

    model = _Streaming(["I would ", "test the ", "login first."])
    envelope = _Envelope(model)
    stream = AgentService(None).answer_stream(
        role="qa", system_prompt="You are Vera, the QA engineer.", prompt="What first?",
        envelope=SpendEnvelope(envelope.run))
    first = next(stream)
    assert first == ("chunk", "I would ") and model.calls == [
        ("You are Vera, the QA engineer.", "What first?")]
    rest = list(stream)
    assert [v for k, v in rest if k == "chunk"] == ["test the ", "login first."]
    assert rest[-1] == ("done", ("I would test the login first.",
                                 {"model": "scripted", "prompt_tokens": 3,
                                  "completion_tokens": 3}))
    assert envelope.attempts == 1 and envelope.settled == rest[-1][1]
    import pytest

    with pytest.raises(ValueError):
        next(AgentService(None).answer_stream(role="qa", system_prompt="s", prompt="p",
                                              adapter=model, mode="operate"))


class _Reading:
    """A scripted chat model: reads with a tool first, then answers each turn from a script."""

    def __init__(self, answers):
        import json

        self.answers = [json.dumps(a) for a in answers]
        self.seen = []

    def create_chat_completion(self, messages, tools=None, **_):
        self.seen.append((list(messages), tools))
        if len(self.seen) == 1:
            return ({"content": "", "tool_calls": [{"id": "t1", "name": "read_logs",
                                                    "arguments": '{"run": "wfr_1"}'}]},
                    {"model": "scripted"})
        return {"content": self.answers.pop(0)}, {"model": "scripted"}


def test_a_structured_decision_reads_first_checks_its_evidence_and_asks_once_more():
    """The step-recovery shape, through the router's one decision: a bounded read phase, the
    chosen action's arguments validated against its schema, and the caller's check against the
    corpus (context + reads) with ONE re-ask when it refuses."""
    from boss_engine.services.agent_service import (AgentService, Choice, DECIDED, ReadPhase)

    reads: list = []

    def run(name, args):
        reads.append((name, args))
        return {"ok": True, "lines": ["Bind for 0.0.0.0:8000 failed: port is already allocated"]}

    def check(answer, corpus):
        cited = " ".join(str(e) for e in answer.get("evidence") or [])
        return "" if cited and cited in corpus else "cite evidence you were shown"

    model = _Reading([
        {"action": "clean", "confidence": 0.9, "reason": "r", "arguments": {"projects": ["a"]},
         "evidence": ["the moon was in the wrong phase"]},
        {"action": "clean", "confidence": 0.9, "reason": "port held by our release",
         "arguments": {"projects": ["helpdesk-ai"]},
         "evidence": ["Bind for 0.0.0.0:8000 failed: port is already allocated"]}])
    schema = {"properties": {"projects": {"type": "array", "description": "what to take down"}},
              "required": ["projects"]}
    got = AgentService(None).decide(
        None, project_id="p", actor="boss recovery agent", agent="Harbor", external_ref="",
        situation="A deploy failed.", message="Recover it.", brief="You are Harbor.",
        choices=[Choice("clean", "take our old release down", arguments=schema),
                 Choice("ask", "ask a person", acts=False)],
        context={"failure": "deploy failed"}, check=check, adapter=model, min_confidence=0.5,
        read=ReadPhase(tools=[{"type": "function", "function": {"name": "read_logs"}}],
                       run=run, max_turns=4))
    assert got.status == DECIDED and got.action == "clean"
    assert got.arguments == {"projects": ["helpdesk-ai"]} and got.turns == 3
    assert reads == [("read_logs", {"run": "wfr_1"})]
    assert model.seen[0][1] and "You are Harbor." in model.seen[0][0][0]["content"]
    assert model.seen[2][0][-1]["content"].startswith("Refused: cite evidence")


def test_a_decision_under_an_envelope_is_one_attempt_and_never_asked_twice():
    """Under a caller's envelope the decision's call is the envelope's one attempt: an answer
    its arguments refuse is not re-asked, and nothing is decided."""
    import json

    from boss_engine.services.agent_service import NO_MODEL, AgentService, Choice, SpendEnvelope

    class Whole:
        def create_text_response(self, prompt, system_prompt=None):
            return "r", json.dumps({"action": "start_build", "arguments": {"inputs": "x"}}), {}

    envelope = _Envelope(Whole())
    got = AgentService(None).decide(
        None, project_id="p", actor="pm", external_ref="", situation="s", message="Propose.",
        choices=[Choice("start_build", "start a build", acts=False,
                        arguments={"properties": {"inputs": {"type": "object"}}})],
        envelope=SpendEnvelope(envelope.run))
    assert envelope.attempts == 1 and got.status == NO_MODEL
    assert "must be a JSON object" in got.refused


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
