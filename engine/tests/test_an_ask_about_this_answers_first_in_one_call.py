"""A question about a record is answered FIRST, in one tool-less call, inside a hard budget.

THE INCIDENT (boss-1, after deploying 1547f72). "Ask the Qa" on a failed Preview step
(workflow_step:wfr_475bf68eb18f485b8c52:preview — Vite: Could not resolve entry module
"index.html") took 347 s and answered "Budget spent; answering from evidence gathered…". The one
before it took 276 s. The owner: "these things should be quick … so pathetic and time consuming …
it really times out after spending 240s there? very annoying."

The answer was in the step's record both times. The time went on the multi-step operate loop —
each round-trip a CLI call with every tool schema, the operate persona and Claude Code's own
system prompt. Measured locally on the recorded rows with the real model after this change:
one call, 6.5-9.0 s, ~2.7k tokens, an answer naming the cause, the fix and what it could not
confirm (the loop's first round-trip alone was 70-140k tokens and produced only tool calls).

These pin the design (services/subject_answer.py):
  * the first call has no tools, no operate persona, and carries the record and the question;
  * nothing — a hung model, a failed one — makes it exceed its budget, and the operator still
    gets the cause as recorded and what was not checked;
  * the loop runs only on "Investigate further" / "Do the fix" (depth="investigate") or an
    instruction, and the general chat still runs it with its tools;
  * the reply offers "Investigate further" and "Do the fix".
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import call_deadline
from boss_engine.services import intent_router as ir
from boss_engine.services import subject_answer as sa
from boss_engine.services.turn_subject import parse_subject, resolve_subject

#: The recorded boss-1 row for the 347 s incident, as the step's output nests it.
VITE_ERROR = (
    'app did not serve HTTP within timeout. error: x Build failed in 10ms | error during build: '
    '| Could not resolve entry module "index.html". log tail: error during build: Could not '
    'resolve entry module "index.html".     at getRollupError (file:///tmp/boss-previews/'
    'prev_a2529d5d17bc48fca548-ydzbvpm7/docs-freshness-board/node_modules/rollup/dist/es/'
    'shared/parseAst.js:319:41)')
STEP_REF = "workflow_step:wfr_475bf68eb18f485b8c52:preview"
QUESTION = "Why is it in this state, and what is the fix?"


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as c:
        for ddl in (
            "CREATE TABLE workflow_runs (id TEXT, project_id TEXT, status TEXT, input TEXT,"
            " error TEXT, blocked_reason TEXT, created_at TEXT)",
            "CREATE TABLE workflow_step_runs (id TEXT, project_id TEXT, workflow_run_id TEXT,"
            " step_id TEXT, step_kind TEXT, status TEXT, input TEXT, output TEXT,"
            " last_error TEXT, attempt INTEGER, iteration INTEGER, decision_id TEXT,"
            " created_at TEXT)",
            "CREATE TABLE app_previews (id TEXT, project_id TEXT, run_id TEXT, repo TEXT,"
            " branch TEXT, command TEXT, port INTEGER, url TEXT, mode TEXT, status TEXT,"
            " failure_kind TEXT, error TEXT, log_path TEXT, created_at TEXT)",
            "CREATE TABLE code_task_runs (id TEXT, project_id TEXT, task_id TEXT, title TEXT,"
            " status TEXT, phase TEXT, error TEXT, pull_request_url TEXT, state TEXT,"
            " created_at TEXT)",
        ):
            c.execute(text(ddl))
        c.execute(text("INSERT INTO workflow_runs VALUES ('wfr_475bf68eb18f485b8c52',"
                       "'boss-dogfood','failed',:i,:e,NULL,'2026-10-02')"),
                  {"i": json.dumps({"title": "Build a documentation freshness board."}),
                   "e": VITE_ERROR[:300]})
        c.execute(text(
            "INSERT INTO workflow_step_runs VALUES ('wfs_77a4b98c52a24413b1f1','boss-dogfood',"
            "'wfr_475bf68eb18f485b8c52','preview','capability','failed','{}',:o,:e,1,0,NULL,"
            "'2026-10-02')"),
            {"e": VITE_ERROR[:300], "o": json.dumps({
                "tool": "start_preview", "status": "awaiting", "capability": "preview",
                "await": {"id": "prev_a2529d5d17bc48fca548", "kind": "preview"},
                "preview_id": "prev_a2529d5d17bc48fca548",
                "preview": {"id": "prev_a2529d5d17bc48fca548", "status": "failed",
                            "error": VITE_ERROR, "failure_kind": "port_timeout",
                            "suggested_action": "Inspect startup logs, ensure HOST/PORT are "
                                                "honored, or retry in Docker mode."}})})
        c.execute(text(
            "INSERT INTO app_previews VALUES ('prev_a2529d5d17bc48fca548','boss-dogfood',"
            "'crun_fc2d5de15a064c128bc7','paramg/boss-sandbox',"
            "'boss/code/crun_fc2d5de15a064c128bc7',NULL,NULL,NULL,'process','failed',"
            "'port_timeout',:e,'/nonexistent/_preview.log','2026-10-02')"), {"e": VITE_ERROR})
        c.execute(text(
            "INSERT INTO code_task_runs VALUES ('crun_fc2d5de15a064c128bc7','boss-dogfood',NULL,"
            "'Build a documentation freshness board.','completed','done',NULL,"
            "'https://github.com/paramg/boss-sandbox/pull/219','{}','2026-10-02')"))
    with engine.begin() as conn:
        yield conn


class _Budget:
    def reserve(self, *a, **kw):
        return {"status": "reserved", "reservation_id": "res_1"}

    def release(self, *a, **kw):
        return None


class _Loop:
    """The investigating agent. Every call recorded; it must not be reached by a first answer."""

    def __init__(self, reply="The loop's answer."):
        self.reply = reply
        self.calls: list[dict[str, Any]] = []
        self.ambient: dict = {}

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return type("T", (), {"reply": self.reply, "trace": []})()


class _Model:
    """A chat backend: records each completion, answers, hangs or fails as told."""

    def __init__(self, reply="Cause: Vite found no index.html.\nFix: add it.\nNot confirmed: "
                             "where index.html lives.", *, sleep=0.0, error=None):
        self.reply, self.sleep, self.error = reply, sleep, error
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        if self.sleep:
            time.sleep(self.sleep)        # ignores the deadline, like a wedged backend
        if self.error:
            raise self.error
        return {"content": self.reply, "tool_calls": None,
                "raw": {"role": "assistant", "content": self.reply}}, {}


def _router(monkeypatch, model, loop=None, **settings):
    router = ir.IntentRouter.__new__(ir.IntentRouter)
    router.budget = _Budget()
    router.settings = type("S", (), {"agent_router_budget_usd_micros": 1000, **settings})()
    loop = loop or _Loop()
    monkeypatch.setattr(router, "_answer_agent", lambda: loop, raising=False)
    monkeypatch.setattr(router, "_first_answer_adapter", lambda: model, raising=False)
    monkeypatch.setattr(ir, "emit_event", lambda *a, **kw: None)
    progress: list[str] = []
    monkeypatch.setattr(ir, "_progress_reporter", lambda project_id, thread_id: progress.append)
    return router, loop, progress


def _ask(router, db, question=QUESTION, *, external_ref=STEP_REF, depth=None, history=()):
    messages = [*history, {"role": "user", "content": question}]
    return router._answer(db, "boss-dogfood", messages, actor="executive_console",
                          external_ref=external_ref, role="Qa", thread_id="athr_x",
                          depth=depth)


# --------------------------------------------------------------------------------------------
# One call, no tools, the record and the question.
# --------------------------------------------------------------------------------------------
def test_the_first_call_has_no_tools_and_carries_the_evidence(db, monkeypatch):
    model = _Model()
    router, loop, progress = _router(monkeypatch, model)
    turn = _ask(router, db)
    assert len(model.calls) == 1, "a question about a record took more than one model call"
    assert loop.calls == [], "the investigating loop ran without the operator asking for it"
    call = model.calls[0]
    assert call["tools"] is None, "the first call was handed tools"
    system, user = call["messages"][0]["content"], call["messages"][1]["content"]
    # The record, whole, and the operator's literal words.
    assert 'Could not resolve entry module "index.html"' in user
    assert "prev_a2529d5d17bc48fca548" in user and "pull/219" in user
    assert QUESTION in user
    # What the record shows that its own label gets wrong — for a failed BUILD too.
    assert "build failed before anything could serve" in user
    # Not the operate persona, not the tool belt: a short answer-from-the-evidence prompt.
    assert len(system) < 2000
    assert "AVAILABLE TOOLS" not in system + user
    for part in ("Cause:", "Fix:", "Not confirmed:"):
        assert part in system
    assert turn.reply.startswith("Cause: Vite found no index.html.")
    assert any("reading the record for workflow step" in p for p in progress)


def test_the_reply_offers_investigate_further_and_do_the_fix(db, monkeypatch):
    router, _, _ = _router(monkeypatch, _Model())
    turn = _ask(router, db)
    labels = [s["label"] for s in turn.next_steps]
    assert labels == ["Investigate further", "Do the fix"]
    assert all(s["depth"] == sa.INVESTIGATE and s["message"] for s in turn.next_steps)


def test_a_follow_up_sees_the_previous_answer(db, monkeypatch):
    model = _Model()
    router, loop, _ = _router(monkeypatch, model)
    _ask(router, db, "why does it say port_timeout then?", history=[
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": "Cause: the Vite build found no index.html."}])
    assert loop.calls == []
    assert "the Vite build found no index.html" in model.calls[0]["messages"][1]["content"]


def test_a_label_only_answer_gets_the_records_cause_without_a_second_call(db, monkeypatch):
    """The ImportError incident's wrong answer repeated the checker's HOST/PORT advice. The loop
    re-asks that; the first answer cannot afford a second call, so the record's finding is
    appended instead."""
    model = _Model(reply="Cause: the app did not serve HTTP within timeout.\nFix: ensure "
                         "HOST/PORT are honored, or retry in Docker mode.")
    router, _, _ = _router(monkeypatch, model)
    turn = _ask(router, db)
    assert len(model.calls) == 1
    assert "What the record itself shows: The app's build failed" in turn.reply


# --------------------------------------------------------------------------------------------
# The budget is a wall, not a hope.
# --------------------------------------------------------------------------------------------
def test_a_hung_model_is_abandoned_at_the_budget_and_the_record_still_answers(db, monkeypatch):
    model = _Model(sleep=8.0)
    router, loop, _ = _router(monkeypatch, model, chat_subject_answer_seconds=1.0)
    started = time.monotonic()
    turn = _ask(router, db)
    assert time.monotonic() - started < 4.0, "the first answer ran past its budget"
    assert loop.calls == [], "running out of time started the long loop anyway"
    # The best answer so far — the cause as recorded — and what was not checked.
    assert "did not answer within 1s" in turn.reply
    assert 'Could not resolve entry module "index.html"' in turn.reply
    assert "Not checked:" in turn.reply and "read_preview_log" in turn.reply
    assert "not verified" in turn.reply, "the checker's suggestion was passed off as the fix"
    assert "still working" not in turn.reply.lower()
    assert [s["label"] for s in turn.next_steps][0] == "Investigate further"


def test_a_failed_model_still_answers_from_the_record_and_says_what_failed(db, monkeypatch):
    model = _Model(error=RuntimeError("claude CLI exited 1: Not logged in"))
    router, _, _ = _router(monkeypatch, model)
    turn = _ask(router, db)
    assert "Not logged in" in turn.reply
    assert "build failed before anything could serve" in turn.reply
    assert "Not checked:" in turn.reply


def test_the_default_budget_meets_the_owners_target():
    """First answer under 60 s: the budget plus the abandonment grace stays inside it."""
    assert sa.answer_seconds(object()) + 1.0 < 60
    assert sa.answer_seconds(type("S", (), {"chat_subject_answer_seconds": 20})()) == 20


def test_the_cli_first_call_drops_claude_codes_prompt_and_is_cut_at_the_budget(
        db, tmp_path, monkeypatch):
    """The real adapter: its own short system prompt, no tool list, no MCP — and a CLI that
    never answers is cut at the first answer's budget, not the CLI's 240 s."""
    from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter

    argv_file = tmp_path / "argv.json"
    hung = tmp_path / "claude"
    hung.write_text(f"#!{sys.executable}\nimport json, sys, time\n"
                    f"json.dump(sys.argv, open({str(argv_file)!r}, 'w'))\ntime.sleep(60)\n")
    hung.chmod(0o755)
    adapter = ClaudeCliAdapter.__new__(ClaudeCliAdapter)
    adapter.settings = Settings().model_copy(update={"claude_cli_timeout": 240,
                                                     "repo_root": tmp_path})
    adapter.model_override = None
    monkeypatch.setattr(adapter, "_claude_bin", lambda: str(hung), raising=False)
    monkeypatch.setattr(adapter, "_child_env", lambda: {}, raising=False)
    monkeypatch.setattr(call_deadline, "MIN_CALL_SECONDS", 0.5)
    ev = resolve_subject(db, "boss-dogfood", parse_subject(STEP_REF))
    assert ev.found
    started = time.monotonic()
    quick = sa.answer_first(adapter, ev, QUESTION, seconds=2.0)
    assert time.monotonic() - started < 6.0, "the CLI ran on its own timeout"
    assert not quick.by_model and "time ran out" in quick.reason
    argv = json.loads(argv_file.read_text())
    assert "--system-prompt" in argv, "the first call carried Claude Code's own system prompt"
    system = argv[argv.index("--system-prompt") + 1]
    assert "Cause:" in system and len(system) < 2000
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    prompt = argv[argv.index("-p") + 1]
    assert "AVAILABLE TOOLS" not in prompt and QUESTION in prompt


# --------------------------------------------------------------------------------------------
# The loop runs when asked for — and the general chat keeps its tools.
# --------------------------------------------------------------------------------------------
def test_investigate_further_runs_the_loop_not_the_first_answer(db, monkeypatch):
    model = _Model()
    router, loop, _ = _router(monkeypatch, model)
    step = sa.NEXT_STEPS[0]
    turn = _ask(router, db, step["message"], depth=step["depth"])
    assert model.calls == [], "Investigate further was answered by the one-call path"
    assert len(loop.calls) >= 1
    # The loop still opens with the record and the contract (turn_subject).
    assert loop.calls[0]["system"].startswith("SUBJECT OF THIS TURN: workflow step")
    assert turn.next_steps == []


@pytest.mark.parametrize("instruction", ["Do the fix you recommended.", "retry it",
                                         "can you fix it?"])
def test_an_instruction_goes_to_the_loop_that_can_act(db, monkeypatch, instruction):
    model = _Model()
    router, loop, _ = _router(monkeypatch, model)
    _ask(router, db, instruction)
    assert model.calls == [] and len(loop.calls) >= 1


def test_a_thread_with_no_subject_still_runs_the_loop(db, monkeypatch):
    model = _Model()
    router, loop, _ = _router(monkeypatch, model)
    _ask(router, db, "why is the queue slow?", external_ref=None)
    assert model.calls == [] and len(loop.calls) >= 1


def test_the_general_chat_agent_still_calls_the_model_with_its_tools():
    """The loop itself is untouched: its first round-trip carries the tool schemas."""
    from boss_engine.agent.engine_agent import EngineAgent
    from boss_engine.tools import build_discuss_read_registry

    model = _Model(reply="Nothing is queued.")
    agent = EngineAgent(type("Ctx", (), {"settings": Settings()})(),
                        registry=build_discuss_read_registry(edition="community"),
                        adapter=model)
    turn = agent.chat(project_id="boss-dogfood", message="why is the queue slow?",
                      max_steps=4)
    assert turn.reply == "Nothing is queued."
    tools = model.calls[0]["tools"] or []
    assert len(tools) > 10, "the general chat lost its tools"


def test_a_screenshot_goes_to_the_loop():
    ev = type("E", (), {"found": True})()
    assert not sa.wants_first_answer(QUESTION, ev, None, has_images=True)
    assert sa.wants_first_answer(QUESTION, ev, None)
    assert not sa.wants_first_answer(QUESTION, type("E", (), {"found": False})(), None)
