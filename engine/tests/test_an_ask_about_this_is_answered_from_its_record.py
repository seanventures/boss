"""An "Ask about this" turn is answered from the thing's own record, inside a budget.

THE INCIDENT (boss-1, 2026-10-02, workflow run wfr_9c78b289633348c68246, thread
athr_a424f934552e4865a7f4). A Preview step failed:

    the preview it started finished as 'failed' — app did not serve HTTP within timeout.
    error: ImportError: attempted relative import with no known parent package
    log tail: ... lead_triage_core/approvals.py ...

The operator pressed "Ask the Qa". The console sent a paraphrase of the step (error cut at 400
characters, "Check the app's code and the host before answering." stapled on) scoped to the
BUILD. The server framed it as a question about a completed build's PRD; the role-grounding
re-ask and the continuation both ran WITHOUT the operator's question in context; the word "host"
sent the continuation down the deployment chain; its answer replaced the first one; and after
276 seconds — progress written to one thread, polled on another — the operator read "Lead Triage
Inbox has never been deployed". The answer was in the question: a package module was launched
as a script, and the fix is to run the app's real entry point (or `python -m`).

These pin the design that replaces that, for every kind of thing a door can ask about:
  * every subject kind the console can send resolves to evidence (read from the console source);
  * the incident's step yields a context whose first block is its error, under an answer-this
    contract;
  * every agent call the router makes carries the operator's literal question;
  * a re-ask cannot replace an answer with one about a different object;
  * the turn has one budget — a scripted model that keeps calling tools is made to answer;
  * progress names the tool and its object, and re-asks report it too;
  * the thread the dock polls is the thread the turn lands in.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text

from boss_engine.services import intent_router as ir
from boss_engine.services.turn_budget import TurnBudget
from boss_engine.services.turn_subject import (
    RESOLVERS, SUBJECT_KINDS, SubjectRef, cites_the_record, parse_subject, resolve_subject,
    stays_on_subject, strays_from_subject,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
WEB_SUBJECTS = REPO / "apps" / "web" / "lib" / "dock-subject.ts"

INCIDENT_ERROR = (
    "the preview it started finished as 'failed' — app did not serve HTTP within timeout. "
    "error: ImportError: attempted relative import with no known parent package "
    "log tail: Traceback (most recent call last):\n"
    '  File "/tmp/boss-previews/prev_3f87d37f96b540198e28-hli9ptot/lead_triage_core/'
    'approvals.py", line 3, in <module>\n'
    "    from .models import Approval\n"
    "ImportError: attempted relative import with no known parent package"
)
QUESTION = "Why did the Preview step fail, and what is the fix?"
STEP_REF = "workflow_step:wfr_9c78b289633348c68246:preview"


# --------------------------------------------------------------------------------------------
# A database holding one of everything a door can ask about.
# --------------------------------------------------------------------------------------------
@pytest.fixture()
def db(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    log = tmp_path / "_preview.log"
    log.write_text("Starting preview...\n" + INCIDENT_ERROR.split("log tail: ", 1)[1])
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
            "CREATE TABLE discovery_runs (id TEXT, project_id TEXT, title TEXT, status TEXT,"
            " phase TEXT, seed TEXT, error TEXT)",
            "CREATE TABLE deployment_targets (id TEXT, project_id TEXT, app_name TEXT,"
            " environment TEXT, status TEXT, target_kind TEXT, target_ref TEXT, domains TEXT)",
            "CREATE TABLE deployment_runs (id TEXT, project_id TEXT, deployment_target_id TEXT,"
            " app_name TEXT, environment TEXT, status TEXT, failure_reason TEXT,"
            " target_ref TEXT, commit_ref TEXT, created_at TEXT)",
            "CREATE TABLE decisions (id TEXT, project_id TEXT, question TEXT, status TEXT,"
            " recommendation TEXT, options TEXT, risk_tier INTEGER, default_action TEXT,"
            " external_ref TEXT)",
            "CREATE TABLE deployment_health_samples (id TEXT, project_id TEXT,"
            " deployment_target_id TEXT, checked_at TEXT, ok BOOLEAN, status_code INTEGER,"
            " latency_ms INTEGER, error TEXT, observed_version TEXT)",
        ):
            c.execute(text(ddl))
        c.execute(text(
            "INSERT INTO workflow_runs VALUES ('wfr_9c78b289633348c68246','boss-dogfood',"
            "'failed',:i,NULL,NULL,'2026-10-02')"),
            {"i": json.dumps({"title": "Lead Triage Inbox"})})
        c.execute(text(
            "INSERT INTO workflow_step_runs VALUES ('wfs_1','boss-dogfood',"
            "'wfr_9c78b289633348c68246','preview','preview','failed','{}',:o,:e,1,0,NULL,"
            "'2026-10-02')"),
            # THE REAL SHAPE, read from the VM's row: the preview's result is NESTED under
            # "preview", and last_error is the runtime's summary — cut. The first fixture was
            # flat, which is why the resolver's flat reads passed here and missed on the VM.
            {"e": INCIDENT_ERROR[:160], "o": json.dumps({
                "tool": "start_preview", "status": "awaiting", "capability": "preview",
                "await": {"id": "prev_3f87d37f96b540198e28", "kind": "preview"},
                "preview_id": "prev_3f87d37f96b540198e28",
                "preview": {"id": "prev_3f87d37f96b540198e28", "status": "failed",
                            "error": INCIDENT_ERROR, "failure_kind": "port_timeout",
                            "suggested_action": "Inspect startup logs, ensure HOST/PORT are "
                                                "honored, or retry in Docker mode."}})})
        c.execute(text(
            "INSERT INTO app_previews VALUES ('prev_3f87d37f96b540198e28','boss-dogfood',"
            "'crun_3e370c114b95425d8a7c','paramg/boss-sandbox','boss/crun_3e37','',NULL,NULL,"
            "'process','failed','port_timeout',:e,:l,'2026-10-02')"),
            {"e": INCIDENT_ERROR, "l": str(log)})
        c.execute(text(
            "INSERT INTO code_task_runs VALUES ('crun_3e370c114b95425d8a7c','boss-dogfood',"
            "NULL,'Build a lead triage inbox','completed','done',NULL,"
            "'https://github.com/paramg/boss-sandbox/pull/218','{}','2026-10-02')"))
        c.execute(text(
            "INSERT INTO discovery_runs VALUES ('drun_1','boss-dogfood','Inbox research',"
            "'failed','synthesize','triage inbox',"
            "'synthesis failed: model returned no findings')"))
        c.execute(text(
            "INSERT INTO deployment_targets VALUES ('deptgt_1','boss-dogfood','Change Calendar',"
            "'staging','active','ssh','host-1','[\"192.0.2.202:3013\"]')"))
        c.execute(text(
            "INSERT INTO deployment_runs VALUES ('deprun_1','boss-dogfood','deptgt_1',"
            "'Change Calendar','staging','failed','health check: connection refused',"
            "'host-1','abc123','2026-10-02')"))
        c.execute(text(
            "INSERT INTO deployment_targets VALUES ('deptgt_2','boss-dogfood','HookWatch',"
            "'staging','active','ssh','host-1','[]')"))
        c.execute(text(
            "INSERT INTO deployment_runs VALUES ('deprun_2','boss-dogfood','deptgt_2',"
            "'HookWatch','staging','succeeded',NULL,'host-1','def456','2026-09-20')"))
        c.execute(text(
            "INSERT INTO deployment_health_samples VALUES ('hs_1','boss-dogfood','deptgt_2',"
            "'2026-10-02T08:00:00',0,NULL,NULL,'GET http://192.0.2.202:3020/health: "
            "ECONNREFUSED',NULL)"))
        c.execute(text(
            "INSERT INTO decisions VALUES ('dec_1','boss-dogfood','Merge PR #186','pending',"
            "'{}','[]',2,'approve','crun_3e370c114b95425d8a7c')"))
    with engine.begin() as conn:
        yield conn


#: One real id per kind, all in the fixture above.
EXAMPLES = {
    "workflow_step": SubjectRef("workflow_step", "wfr_9c78b289633348c68246", "preview"),
    "workflow_run": SubjectRef("workflow_run", "wfr_9c78b289633348c68246"),
    "code_task": SubjectRef("code_task", "crun_3e370c114b95425d8a7c"),
    "preview": SubjectRef("preview", "prev_3f87d37f96b540198e28"),
    "discovery": SubjectRef("discovery", "drun_1"),
    "deployment": SubjectRef("deployment", "deptgt_1"),
    "decision": SubjectRef("decision", "dec_1"),
    # Infra's Ask BOSS: the fleet is the project's (its machines and targets are read in
    # test_the_fleet_is_a_subject.py against the real schema).
    "fleet": SubjectRef("fleet", "boss-dogfood"),
    # The Project page's captain drawer: the whole project (read against the real schema in
    # apps/api/tests/test_a_project_page_reads_its_whole_story.py).
    "project": SubjectRef("project", "boss-dogfood"),
}


def _console_kinds() -> set[str]:
    """The kinds the console can send, read from the console's own source."""
    src = WEB_SUBJECTS.read_text(encoding="utf-8")
    block = src[src.index("export const SUBJECT_KINDS"):src.index("] as const")]
    declared = set(re.findall(r'"([a-z_]+)"', block))
    used: set[str] = set()
    for path in (REPO / "apps" / "web" / "app").rglob("*.tsx"):
        used |= set(re.findall(r'kind:\s*"([a-z_]+)",\s*id:', path.read_text(encoding="utf-8")))
    return declared | used


# --------------------------------------------------------------------------------------------
# Every kind resolves.
# --------------------------------------------------------------------------------------------
def test_every_kind_the_console_can_send_has_a_resolver():
    """A door with a kind the engine cannot resolve opens onto nothing — the model would be told
    about a subject and handed no record of it."""
    kinds = _console_kinds()
    assert len(kinds) >= 6, "the console's subject list could not be read"
    assert kinds <= set(SUBJECT_KINDS), f"console kinds the engine does not know: {kinds - set(SUBJECT_KINDS)}"
    assert set(SUBJECT_KINDS) <= set(RESOLVERS), "a declared kind has no resolver"
    assert set(EXAMPLES) == set(SUBJECT_KINDS), "every kind needs an example below"


@pytest.mark.parametrize("kind", SUBJECT_KINDS)
def test_every_kind_resolves_to_its_evidence(db, kind):
    ev = resolve_subject(db, "boss-dogfood", EXAMPLES[kind])
    assert ev.found, f"{kind} did not resolve"
    block = ev.render(QUESTION)
    assert EXAMPLES[kind].id in block
    assert ev.status, f"{kind} resolved with no status"


@pytest.mark.parametrize("kind", SUBJECT_KINDS)
def test_a_record_in_another_project_is_not_evidence(db, kind):
    """The tenant fence: the subject's id alone must never reach another project's row."""
    assert not resolve_subject(db, "someone-else", EXAMPLES[kind]).found


def test_the_subject_travels_as_the_threads_scope():
    assert parse_subject(STEP_REF) == EXAMPLES["workflow_step"]
    assert EXAMPLES["workflow_step"].external_ref == STEP_REF
    # Every build conversation has been keyed on the bare run id; that stays a subject.
    assert parse_subject("crun_3e370c114b95425d8a7c") == EXAMPLES["code_task"]
    # A role thread is not about a record, and must not be given one.
    assert parse_subject("role:Qa") is None
    assert parse_subject(None) is None


# --------------------------------------------------------------------------------------------
# The incident, end to end through the router.
# --------------------------------------------------------------------------------------------
class _Budget:
    def reserve(self, *a, **kw):
        return {"status": "reserved", "reservation_id": "res_1"}

    def release(self, *a, **kw):
        return None


class _Turn:
    def __init__(self, reply, trace=None):
        self.reply = reply
        self.trace = trace or []


class _Scripted:
    """The answer agent, scripted: one reply per call, every call recorded."""

    def __init__(self, *replies, spend: int = 1):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.spend = spend

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        budget = kwargs.get("budget")
        for _ in range(self.spend):
            if budget is not None:
                budget.spend_call()
        reply = self.replies.pop(0) if self.replies else ""
        return _Turn(reply)


def _router(monkeypatch, answerer, **settings):
    router = ir.IntentRouter.__new__(ir.IntentRouter)
    router.budget = _Budget()
    router.settings = type("S", (), {"agent_router_budget_usd_micros": 1000, **settings})()
    monkeypatch.setattr(router, "_answer_agent", lambda: answerer, raising=False)
    monkeypatch.setattr(ir, "emit_event", lambda *a, **kw: None)
    progress: list[str] = []
    monkeypatch.setattr(ir, "_progress_reporter",
                        lambda project_id, thread_id: progress.append)
    return router, progress


def _ask(router, db, *, external_ref=STEP_REF, role="Qa", question=QUESTION,
         depth="investigate"):
    # THE INVESTIGATING LOOP. Since boss-1's 347 s "Ask the Qa", a question about a record is
    # answered first in one tool-less call (subject_answer.py, pinned by
    # test_an_ask_about_this_answers_first_in_one_call.py) and this loop runs when the operator
    # presses "Investigate further" / "Do the fix" — depth="investigate". Every guarantee below
    # is about that loop, and still holds for it.
    return router._answer(db, "boss-dogfood", [{"role": "user", "content": question}],
                          actor="executive_console", external_ref=external_ref, role=role,
                          thread_id="athr_x", depth=depth)


ANSWER = ("The preview crashed on start with ImportError: lead_triage_core/approvals.py uses a "
          "relative import and was launched as a script. Run the app's server entry, or "
          "`python -m lead_triage_core.<entry>`, instead of the module file.")


def test_the_incidents_context_opens_with_the_steps_error_and_an_answer_contract(
        db, monkeypatch):
    agent = _Scripted(ANSWER)
    router, _ = _router(monkeypatch, agent)
    _ask(router, db)
    system = agent.calls[0]["system"]
    # FIRST, above the persona — the persona's examples are redeploys.
    assert system.startswith("SUBJECT OF THIS TURN: workflow step wfr_9c78b289633348c68246")
    first_block = system[:system.index("You are BOSS")]
    assert f'THE OPERATOR ASKED: "{QUESTION}"' in first_block
    # The error, whole: the console's copy was cut at 400 characters, mid-word.
    assert "RECORDED ERROR" in first_block
    assert first_block.index("RECORDED ERROR") < first_block.index("RECORDED FACTS")
    assert "attempted relative import with no known parent package" in first_block
    assert "lead_triage_core/approvals.py" in first_block
    # What the record shows that its own label gets wrong.
    assert "crashed before serving" in first_block
    assert "port_timeout" in first_block
    # The links the console never sent: step -> preview -> build -> PR.
    assert "prev_3f87d37f96b540198e28" in first_block
    assert "pull/218" in first_block
    assert "launch command: (not recorded)" in first_block
    # And the contract that it be answered.
    assert "Your FIRST SENTENCE answers the operator's question above" in first_block
    assert "Do not answer about a different object" in first_block
    # Not the old framing that sent it to the PRD.
    assert "Call read_spec with this run id FIRST" not in system


def test_an_answer_from_the_subjects_record_is_not_re_asked(db, monkeypatch):
    """boss-1's first pass used no tools — the evidence was in hand — and the role-grounding
    re-ask threw its answer away. An answer from the subject's record is grounded."""
    agent = _Scripted(ANSWER, "Lead Triage Inbox has never been deployed.")
    router, _ = _router(monkeypatch, agent)
    turn = _ask(router, db)
    assert len(agent.calls) == 1, "a grounded subject answer was re-asked"
    assert "ImportError" in turn.reply
    assert "never been deployed" not in turn.reply


def test_every_agent_call_carries_the_operators_literal_question(db, monkeypatch):
    """Both re-asks used to send an instruction with no question in context. Driven through the
    grounding re-ask AND the continuation (a general role turn that hands the problem back)."""
    agent = _Scripted(
        "I could not determine the cause. Would you like me to investigate further?",
        "Still could not determine it. Would you like me to look again?",
        "The build is fine.",
    )
    router, _ = _router(monkeypatch, agent)
    question = "is the lead triage build healthy?"
    _ask(router, db, external_ref=None, role="Qa", question=question)
    assert len(agent.calls) >= 2, "expected the re-asks to run"
    for call in agent.calls:
        carried = str(call["message"]) + json.dumps(call.get("history") or [])
        assert question in carried, f"an agent call was made without the question: {call['message'][:120]}"
        assert call.get("on_step") is not None, "a re-ask without on_step reports nothing"
        assert call.get("budget") is not None, "a pass outside the turn's budget"


def test_a_re_ask_cannot_replace_the_answer_with_one_about_something_else(db, monkeypatch):
    """The incident's last step: the continuation's "never been deployed" REPLACED an answer
    about the Preview step."""
    first = ("The preview prev_3f87d37f96b540198e28 failed with ImportError. I could not "
             "determine which file is the entry point. Would you like me to investigate further?")
    agent = _Scripted(first, "Lead Triage Inbox has never been deployed — zero hosts.")
    router, _ = _router(monkeypatch, agent)
    turn = _ask(router, db, role=None)
    assert len(agent.calls) == 2, "the continuation should have been tried"
    assert "never been deployed" not in turn.reply
    assert "ImportError" in turn.reply


def test_the_continuation_is_sent_to_the_subjects_tools_not_the_deploy_chain(db, monkeypatch):
    """`_DEPLOY_SUBJECT` matched the bare word "host" in the console's boilerplate and ordered
    describe_deployment_target → list_deployments → preflight. With a subject the chain is the
    subject's own open points."""
    agent = _Scripted("I could not determine it. Would you like me to investigate further?",
                      ANSWER)
    router, _ = _router(monkeypatch, agent)
    # The operator's own words mention the host; the subject decides the chain regardless.
    _ask(router, db, role=None, question=QUESTION + " Check the host too.")
    instruction = str(agent.calls[1]["message"])
    assert "describe_deployment_target" not in instruction


def test_a_reply_strays_only_when_it_names_something_else(db):
    """The incident's continuation is refused; an answer that cites the step is not."""
    ev = resolve_subject(db, "boss-dogfood", EXAMPLES["workflow_step"])
    assert stays_on_subject(ANSWER, ev)
    assert stays_on_subject("The Preview step failed because prev_3f87d37f96b540198e28 "
                            "crashed.", ev)
    # The build the step links to is on subject; it is IN the record.
    assert stays_on_subject("Build crun_3e370c114b95425d8a7c shipped approvals.py as a "
                            "package module.", ev)
    assert not stays_on_subject("Lead Triage Inbox has never been deployed — not failed, never "
                                "shipped. list_deployment_hosts returns zero hosts.", ev)
    # An object of a kind the record does not link to.
    assert "deployment" in strays_from_subject(
        "The real problem is deprun_77aa11bb22, which failed.", ev)


def test_on_a_build_thread_an_on_topic_answer_without_the_run_id_is_on_subject(db):
    """Review of 20b0abf: the positive-match test refused every good build-thread answer,
    because builds rarely carry an error and nobody quotes a run id."""
    ev = resolve_subject(db, "boss-dogfood", EXAMPLES["code_task"])
    assert ev.found and not ev.error
    assert stays_on_subject("Three acceptance criteria are untested: the decline disclaimer, "
                            "the audit export and the keyboard path.", ev)


def test_the_operators_own_question_decides_whether_deploy_talk_is_on_subject(db):
    ev = resolve_subject(db, "boss-dogfood", EXAMPLES["code_task"])
    reply = "It has never been deployed: list_deployments returns nothing for this app."
    assert not stays_on_subject(reply, ev, "Which criteria are untested?")
    assert stays_on_subject(reply, ev, "Was this build ever deployed?")


def test_on_a_build_thread_an_on_topic_continuation_replaces_a_hand_back(db, monkeypatch):
    """The reviewer's replay, as a test: before the fix CALLS 2 and the hand-back stood."""
    better = ("Three acceptance criteria are untested: the decline disclaimer, the audit "
              "export and the keyboard path.")
    agent = _Scripted("I could not determine which criteria are untested. Would you like me "
                      "to investigate further?", better)
    router, _ = _router(monkeypatch, agent)
    turn = _ask(router, db, external_ref="crun_3e370c114b95425d8a7c", role=None,
                question="Which criteria are untested?")
    assert len(agent.calls) == 2
    assert turn.reply == better


def test_a_found_record_without_the_cause_does_not_switch_off_the_lookup_floor(db, monkeypatch):
    """Review of 20b0abf: `grounded = record found` turned the lookup floor off for every build
    thread and briefing door. A build with no recorded error, asked an operational question and
    answered with zero tools, is still re-asked to look."""
    agent = _Scripted("It is still working; nothing looks stuck.",
                      "diagnose_run shows the build is in its test phase, last activity 2 min ago.")
    router, _ = _router(monkeypatch, agent)
    _ask(router, db, external_ref="crun_3e370c114b95425d8a7c", role=None,
         question="Is this build still working or stuck?")
    assert len(agent.calls) == 2, "the lookup floor did not fire"
    assert "diagnose_run" in str(agent.calls[1]["message"])


def test_grounded_means_the_answer_used_the_recorded_failure(db):
    step = resolve_subject(db, "boss-dogfood", EXAMPLES["workflow_step"])
    assert cites_the_record(ANSWER, step)
    assert not cites_the_record("The preview failed; I will look into it.", step)
    build = resolve_subject(db, "boss-dogfood", EXAMPLES["code_task"])
    assert not cites_the_record("The build is fine and the PR is open.", build), (
        "a record with no failure grounds nothing")


def test_a_deployments_record_carries_its_latest_health_check(db):
    """The Monitoring door asks why an app is not answering; the outage is in the health
    samples, not in a run that succeeded weeks ago."""
    ev = resolve_subject(db, "boss-dogfood", SubjectRef("deployment", "deptgt_2"))
    assert ev.found
    assert "ECONNREFUSED" in ev.error
    assert "FAILING" in str(ev.facts.get("latest health check"))
    assert cites_the_record("It refuses connections (ECONNREFUSED on /health).", ev)


def test_the_real_step_shape_yields_the_label_finding_and_the_whole_error(db):
    """The VM row nests the preview's result under "preview" and cuts last_error."""
    ev = resolve_subject(db, "boss-dogfood", EXAMPLES["workflow_step"])
    assert ev.error == INCIDENT_ERROR, "the cut last_error was preferred over the whole text"
    assert ev.facts["failure_kind"] == "port_timeout"
    assert "HOST/PORT" in str(ev.facts["suggested_action (the checker's, not verified)"])
    assert any("checker's label" in f for f in ev.findings)
    assert not any(k == "output.preview" for k in ev.facts), "the nested record dumped raw"


# --------------------------------------------------------------------------------------------
# The turn budget.
# --------------------------------------------------------------------------------------------
class _ToolHappyAdapter:
    """A model that never stops calling tools while it has any."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, messages, tools=None):
        self.calls.append({"tools": tools, "last": messages[-1]})
        names = [t["function"]["name"] for t in (tools or [])]
        if tools and names != ["propose_action"]:
            return ({"raw": {"role": "assistant", "content": ""},
                     "tool_calls": [{"id": f"t{len(self.calls)}", "name": "read_preview_log",
                                     "arguments": json.dumps(
                                         {"preview_id": "prev_3f87d37f96b540198e28"})}]}, {})
        return ({"raw": {"role": "assistant", "content": "answer"}, "content":
                 "It crashed on an ImportError; I could not read the full log."}, {})


def _agent(adapter):
    from boss_engine.agent.engine_agent import EngineAgent

    agent = EngineAgent.__new__(EngineAgent)
    agent._adapter = adapter
    agent._notes = []
    agent.ambient = {}
    # propose_action IS IN THE REAL REGISTRY, ALWAYS. The budget tests ran without it, so the
    # closing offer — which only exists when propose_action does — never ran under them, and a
    # turn that overran its budget by one call and two round-trips passed (review of 374ba07).
    agent.registry = type("R", (), {"openai_schema": lambda self: [
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("read_preview_log", "propose_action")]})()
    agent._run_tool = lambda name, args, **kw: {"ok": True, "log": "…"}
    return agent


@pytest.mark.parametrize("cap", [2, 3, 4, 7])
def test_a_model_that_keeps_calling_tools_is_made_to_answer_within_the_turns_budget(cap):
    adapter = _ToolHappyAdapter()
    budget = TurnBudget(seconds=600, model_calls=cap)
    turn = _agent(adapter).chat(project_id="p", message=QUESTION, max_steps=14, budget=budget)
    assert len(adapter.calls) <= cap, "the turn ran past its model-call budget"
    assert budget.used <= cap
    # The last call is the answer: no tools but the button it may attach.
    assert [t["function"]["name"] for t in (adapter.calls[-1]["tools"] or [])] \
        in ([], ["propose_action"])
    final_prompt = str(adapter.calls[-1]["last"]["content"])
    assert "budget is spent" in final_prompt
    assert "could NOT check" in final_prompt
    assert "ImportError" in turn.reply


@pytest.mark.parametrize("seconds,round_trip", [(90, 40), (180, 60), (180, 157)])
def test_the_wall_clock_ends_the_turn_too(seconds, round_trip):
    """Within the turn's seconds plus ONE round-trip — the forced answer — and never two: the
    closing offer used to add a whole round-trip of its own (300 s on a 180 s budget at 60 s a
    call)."""
    now = [0.0]
    adapter = _ToolHappyAdapter()
    budget = TurnBudget(seconds=seconds, model_calls=50, clock=lambda: now[0])

    original = adapter.create_chat_completion

    def slow(messages, tools=None):
        now[0] += round_trip
        return original(messages, tools)

    adapter.create_chat_completion = slow
    _agent(adapter).chat(project_id="p", message=QUESTION, max_steps=14, budget=budget)
    assert now[0] <= seconds + round_trip, f"the turn ran {now[0]} s on a {seconds} s budget"
    assert [t["function"]["name"] for t in (adapter.calls[-1]["tools"] or [])] \
        in ([], ["propose_action"])


def test_a_spent_turn_can_still_attach_its_fix_as_a_button():
    """Review of 20b0abf: past the turn's budget the next call had no tools, so a read → read →
    propose chain ended as prose with nothing to press. The forced answer may make ONE
    propose_action call, and only that.

    REWRITTEN (review of 374ba07): the offer was a round-trip of its own, then the answer
    another — model_calls + 1 calls. The offer now rides on the answer: ONE call, inside the
    budget, whose proposal runs and whose text is the reply."""
    seen: list = []

    class _Offerer:
        calls: list = []

        def create_chat_completion(self, messages, tools=None):
            self.calls.append(tools)
            names = [t["function"]["name"] for t in (tools or [])]
            if names == ["propose_action"]:
                return ({"raw": {"role": "assistant", "content": "x"},
                         "content": "It crashed on ImportError; the retry is one click away.",
                         "tool_calls": [
                    {"id": "p1", "name": "propose_action",
                     "arguments": json.dumps({"tool": "retry_workflow_step"})},
                    {"id": "p2", "name": "redeploy", "arguments": "{}"}]}, {})
            if tools:
                return ({"raw": {"role": "assistant", "content": ""}, "tool_calls": [
                    {"id": f"t{len(self.calls)}", "name": "read_preview_log",
                     "arguments": "{}"}]}, {})
            return ({"raw": {"role": "assistant", "content": "x"},
                     "content": "It crashed on ImportError; the retry is one click away."}, {})

    adapter = _Offerer()
    agent = _agent(adapter)
    agent.registry = type("R", (), {"openai_schema": lambda self: [
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("read_preview_log", "propose_action", "redeploy")]})()
    agent._run_tool = lambda name, args, **kw: seen.append(name) or {"ok": True}
    budget = TurnBudget(seconds=600, model_calls=3)
    turn = agent.chat(project_id="p", message=QUESTION, max_steps=14, budget=budget)
    assert "propose_action" in seen, "the closing offer was not allowed"
    assert "redeploy" not in seen, "the closing step ran a tool other than propose_action"
    assert len(adapter.calls) == 3 and budget.used == 3, "the offer cost a call of its own"
    assert "ImportError" in turn.reply


def test_a_spent_budget_starts_no_re_ask(db, monkeypatch):
    """The first pass used the turn; the reply hands the problem back; nothing more runs."""
    agent = _Scripted("I could not determine it. Would you like me to investigate further?",
                      "second pass", spend=16)
    # The turn's own budget, whichever size it was given (turn_budget.for_turn).
    router, _ = _router(monkeypatch, agent, chat_turn_model_calls=16,
                        chat_deep_turn_model_calls=16)
    _ask(router, db, external_ref=None, role=None, question="why is the queue slow?")
    assert len(agent.calls) == 1


def test_the_budget_is_configured_not_hardcoded():
    from boss_engine.config import Settings

    s = Settings()
    assert 30 <= s.chat_turn_seconds <= 240, "the turn must answer before the console's stall"
    assert s.chat_turn_model_calls >= 4


# --------------------------------------------------------------------------------------------
# Progress names the work.
# --------------------------------------------------------------------------------------------
def test_progress_names_the_tool_and_what_it_is_reading():
    from boss_engine.agent.engine_agent import _step_label

    assert _step_label("read_preview_log", {"preview_id": "prev_3f87d37f96b540198e28"}) == (
        "reading the preview's start-up log — prev_3f87d37f96b540198e28")
    assert _step_label("describe_workflow_run", {"run_id": "wfr_9c78"}).startswith(
        "reading the workflow run")


def test_the_phase_line_names_the_subject_rather_than_chaining_tools():
    adapter = _ToolHappyAdapter()
    seen: list[str] = []
    _agent(adapter).chat(project_id="p", message=QUESTION, max_steps=3, on_step=seen.append,
                         budget=TurnBudget(seconds=600, model_calls=3),
                         focus="workflow step wfr_9c78 / preview")
    assert seen[0] == "reading workflow step wfr_9c78 / preview and deciding what is still open"
    assert "reading the preview's start-up log — prev_3f87d37f96b540198e28" in seen
    assert seen[-1].startswith("writing the answer")


# --------------------------------------------------------------------------------------------
# The dock polls the thread the turn lands in.
# --------------------------------------------------------------------------------------------
@pytest.fixture()
def threads(monkeypatch):
    from boss_engine.services import agent_service

    monkeypatch.setattr(agent_service, "emit_event", lambda *a, **kw: None)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE agent_threads (id TEXT PRIMARY KEY, project_id TEXT, actor TEXT,"
            " surface TEXT, external_ref TEXT, status TEXT DEFAULT 'active',"
            " agent_role TEXT DEFAULT '', pending_plan_id TEXT, pending_plan_summary TEXT,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
    with engine.begin() as conn:
        svc = agent_service.AgentService.__new__(agent_service.AgentService)
        yield svc, conn


def test_ensure_and_chat_resolve_the_same_thread(threads):
    svc, conn = threads
    # The console's pre-turn ensure, now carrying the turn's own key...
    ensured = svc.start_or_load_thread(conn, project_id="boss-dogfood",
                                       actor="executive_console", external_ref=STEP_REF,
                                       agent_role="Qa")
    # ...and the turn, holding that id.
    chat = svc.start_or_load_thread(conn, project_id="boss-dogfood", actor="executive_console",
                                    thread_id=ensured["id"], external_ref=STEP_REF,
                                    agent_role="Qa")
    assert chat["id"] == ensured["id"]


def test_a_held_thread_for_another_scope_is_not_where_the_turn_lands(threads):
    """The dock held a role thread while the turn was about a step. Answering there would have
    read `role:Qa` as the subject and dropped the step."""
    svc, conn = threads
    role_thread = svc.start_or_load_thread(conn, project_id="boss-dogfood",
                                           actor="executive_console", external_ref="role:Qa",
                                           agent_role="Qa")
    turn = svc.start_or_load_thread(conn, project_id="boss-dogfood", actor="executive_console",
                                    thread_id=role_thread["id"], external_ref=STEP_REF,
                                    agent_role="Qa")
    assert turn["id"] != role_thread["id"]
    assert turn["external_ref"] == STEP_REF


def test_the_ensure_endpoint_passes_the_role_the_turn_will_use():
    src = (REPO / "apps" / "api" / "app" / "main.py").read_text(encoding="utf-8")
    body = src[src.index("def ensure_agent_thread_endpoint"):]
    body = body[:body.index("\n@app.")]
    assert 'agent_role=str(payload.get("role") or "")' in body
    route = (REPO / "apps" / "web" / "app" / "api" / "agent-thread" / "route.ts").read_text(
        encoding="utf-8")
    assert "external_ref:" in route and "role:" in route
    ui = (REPO / "apps" / "web" / "app" / "agent-ui.tsx").read_text(encoding="utf-8")
    assert "external_ref: scopeRef || undefined," in ui
    assert "role: roleName || undefined" in ui


# --------------------------------------------------------------------------------------------
# Review of 374ba07: grounded means the CAUSE, and on-topic means not about something else.
# --------------------------------------------------------------------------------------------
def _checker_sentences():
    """Every sentence the preview checker itself writes — its labels and its advice."""
    from boss_engine.services.preview_service import CHECKER_LABELS, START_FAILURE_ADVICE

    return [*CHECKER_LABELS, *START_FAILURE_ADVICE.values()]


@pytest.mark.parametrize("sentence", _checker_sentences())
def test_repeating_the_checkers_own_words_does_not_ground_an_answer(db, sentence):
    """The incident's misdiagnosis WAS the checker's label: "app did not serve HTTP within
    timeout … ensure HOST/PORT are honored". Every label and every piece of advice the checker
    writes is enumerated here; none of them, repeated, counts as using the recorded failure."""
    step = resolve_subject(db, "boss-dogfood", EXAMPLES["workflow_step"])
    assert not cites_the_record(sentence, step), sentence
    assert not cites_the_record(f"The preview failed: {sentence}", step), sentence


@pytest.mark.parametrize("reply", [
    "The app did not serve HTTP within timeout. Ensure HOST/PORT are honored on the host and "
    "retry in Docker mode.",
    "The preview started but nothing answered; the port is probably blocked on the host.",
    "The preview it started finished as failed — see the traceback, most recent call last.",
])
def test_the_reviewers_ungrounded_replies_are_not_grounded(db, reply):
    step = resolve_subject(db, "boss-dogfood", EXAMPLES["workflow_step"])
    assert not cites_the_record(reply, step)


def test_an_answer_that_names_the_cause_still_is(db):
    step = resolve_subject(db, "boss-dogfood", EXAMPLES["workflow_step"])
    assert cites_the_record(ANSWER, step)
    assert cites_the_record("approvals.py does a relative import, so it cannot run as a "
                            "script.", step)
    # A plain-words failure keeps its plain-words mark once the checker's words are gone.
    deploy = resolve_subject(db, "boss-dogfood", EXAMPLES["deployment"])
    assert cites_the_record("The host refused the connection — connection refused on 3013.",
                            deploy)


def test_the_incidents_wrong_answer_is_re_asked_not_accepted(db, monkeypatch):
    """Through the router, with role=Qa: the label-only answer used to make exactly ONE call —
    no lookup floor, no role re-ask — and the port_timeout misdiagnosis stood."""
    wrong = ("The app did not serve HTTP within timeout. Ensure HOST/PORT are honored on the "
             "host and retry in Docker mode.")
    agent = _Scripted(wrong, ANSWER, ANSWER)
    router, _ = _router(monkeypatch, agent)
    _ask(router, db)
    assert len(agent.calls) >= 2, "the checker's label was taken as a grounded answer"


#: On-topic continuations, per subject kind, that the reviewer's probes refused: code
#: identifiers that merely start like an id prefix, and liveness answers to liveness questions.
ON_TOPIC = [
    ("code_task", "Which criteria are untested?",
     "The `dec_rate` field in pricing.py is never validated, and the audit export has no test."),
    ("code_task", "Which criteria are untested?",
     "The `dept_id` column has no index, and nothing tests the keyboard path."),
    ("code_task", "Is it live yet?",
     "It is not deployed yet: the PR is open and waiting for review."),
    ("code_task", "Can customers use it?",
     "Not yet — it has never been deployed; the PR is open and the preview is the only copy."),
    ("workflow_step", QUESTION,
     "approvals.py does a relative import. Set `dec_threshold` aside — the fix is to run "
     "`python -m lead_triage_core.server` so the ImportError goes away."),
    ("workflow_step", QUESTION,
     "The ImportError is in approvals.py; `prev_state` in the reducer is unrelated."),
    ("workflow_run", "Is this live?",
     "Not yet: the run stopped at Preview, so it has not been deployed."),
    ("deployment", "Why is it not answering?",
     "The health check gets connection refused on 3013; the service is not listening."),
]


@pytest.mark.parametrize("kind,question,reply", ON_TOPIC,
                         ids=[f"{k}-{i}" for i, (k, _q, _r) in enumerate(ON_TOPIC)])
def test_an_on_topic_continuation_never_strays(db, kind, question, reply):
    ev = resolve_subject(db, "boss-dogfood", EXAMPLES[kind])
    assert strays_from_subject(reply, ev, question) == "", reply


def test_the_incidents_off_subject_answer_still_strays(db):
    """The fix must not reopen the incident: on the Preview step, "never been deployed … zero
    hosts" is still about something else."""
    ev = resolve_subject(db, "boss-dogfood", EXAMPLES["workflow_step"])
    assert strays_from_subject("Lead Triage Inbox has never been deployed — not failed, never "
                               "shipped. list_deployment_hosts returns zero hosts.", ev, QUESTION)
    assert strays_from_subject("Lead Triage Inbox has never been deployed.", ev, QUESTION)


def test_a_minted_id_the_project_does_not_have_is_not_another_subject(db):
    """Before an id counts against a reply, it must be a real object in this project."""
    from boss_engine.services.turn_subject import id_exists

    ev = resolve_subject(db, "boss-dogfood", EXAMPLES["workflow_step"])
    reply = "Compare it with deprun_0000aaaa1111bbbb2222, which shipped the same module."
    assert strays_from_subject(reply, ev, QUESTION)          # no check: counted
    assert strays_from_subject(reply, ev, QUESTION,
                               exists=lambda t: id_exists(db, "boss-dogfood", t)) == ""


def test_a_deployment_links_the_build_that_shipped_it(db):
    """A deployment's continuation naming the build behind the outage was refused: deployment
    records never linked a crun id. The release packet names it."""
    db.execute(text("ALTER TABLE deployment_runs ADD COLUMN release_packet TEXT"))
    db.execute(text(
        "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
        "environment, status, failure_reason, target_ref, commit_ref, created_at, release_packet)"
        " VALUES ('deprun_3','boss-dogfood','deptgt_2','HookWatch','staging','succeeded',NULL,"
        "'host-1','fed789','2026-10-01',:p)"),
        {"p": json.dumps({"code_task_run_id": "crun_99aa88bb77cc66dd55ee"})})
    ev = resolve_subject(db, "boss-dogfood", SubjectRef("deployment", "deptgt_2"))
    assert any("crun_99aa88bb77cc66dd55ee" in line for line in ev.linked)
    assert strays_from_subject("Build crun_99aa88bb77cc66dd55ee changed the port to 3021, so "
                               "the health check misses it.", ev, "Why is it down?") == ""


def test_a_question_that_names_its_run_opens_with_that_runs_record(db, monkeypatch):
    """The owner, 2026-10-02: "it takes so long to find the issue". Asked in the GENERAL chat
    about a run by id, the model spent its first round-trips finding and reading the run the
    question had already named; the "Ask about this" door answered the same question in one
    call because it sent the record first. An id in the question is that door."""
    agent = _Scripted(ANSWER)
    router, _ = _router(monkeypatch, agent)
    _ask(router, db, external_ref=None, role=None,
         question="Why did wfr_9c78b289633348c68246 fail, and what is the fix?")
    system = agent.calls[0]["system"]
    assert system.startswith("SUBJECT OF THIS TURN: workflow run wfr_9c78b289633348c68246")
    assert "attempted relative import" in system[:system.index("You are BOSS")]
    assert agent.calls[0]["focus"] == "workflow run wfr_9c78b289633348c68246"


def test_an_id_this_project_does_not_have_leaves_the_question_general(db, monkeypatch):
    """Keep what worked: a made-up or foreign id is not turned into "no record of …"."""
    agent = _Scripted(ANSWER)
    router, _ = _router(monkeypatch, agent)
    _ask(router, db, external_ref=None, role=None,
         question="Why did crun_ffffffffffff fail?")
    assert not agent.calls[0]["system"].startswith("SUBJECT OF THIS TURN")


# --------------------------------------------------------------------------------------------
# A turn sized by its decision, and one that stops short ends with a press that resumes it.
# --------------------------------------------------------------------------------------------
class _ExhaustedTurn(_Turn):
    def __init__(self, reply, exhausted):
        super().__init__(reply)
        self.exhausted = exhausted


def test_a_deep_turn_gets_a_deep_budget_and_a_question_a_light_one(db, monkeypatch):
    from boss_engine.config import Settings

    s = Settings()
    deep = _Scripted(ANSWER)
    router, _ = _router(monkeypatch, deep)
    _ask(router, db, depth="investigate")
    assert deep.calls[0]["budget"].model_calls == s.chat_deep_turn_model_calls
    assert deep.calls[0]["max_steps"] == s.chat_deep_max_steps
    light = _Scripted(ANSWER)
    router, _ = _router(monkeypatch, light)
    router._turn_action = "answer"
    _ask(router, db, external_ref=None, role=None, depth=None)
    assert light.calls[0]["budget"].model_calls == s.chat_turn_model_calls


def test_a_turn_that_ran_out_ends_with_one_press_to_continue(db, monkeypatch):
    class RanOut(_Scripted):
        def chat(self, **kwargs):
            super().chat(**kwargs)
            return _ExhaustedTurn(ANSWER, "steps")

    router, _ = _router(monkeypatch, RanOut())
    turn = _ask(router, db)
    assert [s["id"] for s in turn.next_steps] == ["continue"]
    assert turn.next_steps[0]["depth"] == "investigate"
    finished, _ = _router(monkeypatch, _Scripted(ANSWER))
    assert _ask(finished, db).next_steps == []
