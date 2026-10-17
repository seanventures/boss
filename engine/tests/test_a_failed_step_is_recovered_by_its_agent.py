"""The recovery agent's judgement, without a database (services/step_recovery.py).

What a failed step's agent may decide, and what BOSS refuses of it: the cause signatures of the
real failure chain, the validation guardrails (never another app's project, evidence or
nothing, never the same action twice for the same cause, one question), BOSS's coded triage
when no model answers, the same behaviour whichever engine the router returns, the host-side
guard on a clean, and the words the run page shows."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from boss_engine.services import boss_recommendations, model_routing, stack_deploy
from boss_engine.services import step_recovery as sr

REAL_ROUTED_ADAPTER = sr.routed_adapter  # the conftest stands it in with "no model"

NPM = {"phase": "plan", "cause": "app", "step": "preflight",
       "headline": "The target machine 203.0.113.10 has no `npm` (it has: docker); this "
                   "manifest runs commands on the machine directly"}
EAI = {"phase": "apply", "cause": "host", "step": "health",
       "headline": "The app could not reach res-db, the service BOSS provisioned for it on "
                   "203.0.113.10, so it exited at start.",
       "action": "This is BOSS's network wiring, not the app. Update BOSS and deploy again.",
       "log_tail": "Error: getaddrinfo EAI_AGAIN res-db"}
PORT = {"phase": "apply", "cause": "host", "step": "install: install 1",
        "headline": "Port 8000 on 203.0.113.10 is already held by another container",
        "log_tail": "Error response from daemon: Bind for 0.0.0.0:8000 failed: port is "
                    "already allocated"}
OWN_HOST = {"read": True, "address": "203.0.113.10", "own_projects": ["helpdesk-ai"],
            "port_holders": [{"name": "helpdesk-ai-app-1", "project": "helpdesk-ai",
                              "own": True, "own_project": True}]}
FOREIGN_HOST = {"read": True, "address": "203.0.113.10", "own_projects": [],
                "port_holders": [{"name": "other-web-1", "project": "other-app", "own": False,
                                  "own_project": False}]}


def _ev(failure: dict, host: dict | None = None) -> dict:
    return {"failure": failure, "host_state": host or {},
            "own_projects": list((host or {}).get("own_projects") or [])}


# ── the cause ─────────────────────────────────────────────────────────────────────────────────

def test_the_chains_causes_have_stable_signatures():
    assert sr.cause_signature("deploy", NPM)[0] == "deploy:missing_tool:npm"
    assert sr.cause_signature("deploy", EAI)[0] == "deploy:unwired:res-db"
    assert sr.cause_signature("deploy", PORT) == ("deploy:port_held:8000",
                                                 {"pattern": "port_held", "port": 8000})
    assert sr.cause_signature("build", {"why": "secret scan found a literal"})[0] == \
        "build:secret_scan"
    assert sr.cause_signature("build", {"why": "pom.xml at the repository root"})[0] \
        .startswith("build:boss:")
    a = sr.cause_signature("deploy", {"headline": "step x failed (exit 3) for sdep_abcdef1234"})
    b = sr.cause_signature("deploy", {"headline": "step x failed (exit 7) for sdep_99887766ff"})
    assert a == b, "ids and numbers do not make a new cause"


def test_a_held_port_is_the_hosts_not_the_apps():
    said = stack_deploy.explain_failure("install: install 1", 1, PORT["log_tail"],
                                        host="deploy@203.0.113.10")
    assert said["cause"] == "host" and "Port 8000" in said["headline"]
    assert stack_deploy.explain_failure(
        "install: up", 1, "listen tcp 0.0.0.0:3000: bind: address already in use")["cause"] \
        == "host"


# ── validation: what BOSS refuses of the agent ────────────────────────────────────────────────

def _validate(raw: dict, *, evidence: dict | None = None, last: dict | None = None,
              signature: str = "deploy:port_held:8000"):
    evidence = evidence or _ev(PORT, OWN_HOST)
    return sr.validate(raw, allowed=sr.KINDS["deploy"], evidence=evidence,
                       corpus=json.dumps(evidence), last=last or {}, signature=signature)


CLEAN = {"action": "clean_and_retry", "projects": ["helpdesk-ai"],
         "evidence": ["Bind for 0.0.0.0:8000 failed: port is already allocated"]}


def test_a_clean_of_this_apps_own_release_is_accepted():
    decision, why = _validate(CLEAN)
    assert why == "" and decision["projects"] == ["helpdesk-ai"]


def test_another_apps_project_is_refused():
    decision, why = _validate({**CLEAN, "projects": ["other-app"]},
                              evidence=_ev(PORT, FOREIGN_HOST))
    assert decision is None and "not this app's own" in why


def test_evidence_must_be_what_the_agent_was_shown():
    decision, why = _validate({**CLEAN, "evidence": ["the moon was in the wrong phase today"]})
    assert decision is None and "cite evidence" in why


def test_the_same_cause_twice_with_the_same_action_is_refused():
    decision, why = _validate(CLEAN, last={"signature": "deploy:port_held:8000",
                                           "action": "clean_and_retry"})
    assert decision is None and "same cause" in why


@pytest.mark.parametrize("raw, said", [
    ({"action": "fix_app", "evidence": ["port is already allocated"], "note": "fix"},
     "precise note"),
    ({"action": "ask_person", "question": ""}, "ONE specific question"),
    ({"action": "wait_for_boss", "evidence": ["port is already allocated"]}, "boss_fix.title"),
    ({"action": "delete_everything"}, "is not one of"),
])
def test_each_action_needs_what_makes_it_actionable(raw, said):
    decision, why = _validate(raw)
    assert decision is None and said in why


def test_a_build_cannot_clean_a_host():
    decision, why = sr.validate(CLEAN, allowed=sr.KINDS["build"], evidence=_ev(PORT, OWN_HOST),
                                corpus=json.dumps(_ev(PORT, OWN_HOST)), last={},
                                signature="build:x")
    assert decision is None and "not allowed for this step" in why


# ── BOSS's coded triage ───────────────────────────────────────────────────────────────────────

def _coded(failure: dict, host: dict | None = None, kind: str = "deploy", last: dict | None = None):
    signature, pattern = sr.cause_signature(kind, failure)
    return sr.coded_decision(kind, _ev(failure, host), signature=signature, pattern=pattern,
                             last=last or {}, allowed=sr.KINDS[kind])


def test_the_coded_triage_places_the_chain():
    assert _coded(NPM)["action"] == "fix_app"
    assert _coded(EAI)["action"] == "wait_for_boss"
    clean = _coded(PORT, OWN_HOST)
    assert clean["action"] == "clean_and_retry" and clean["projects"] == ["helpdesk-ai"]
    asked = _coded(PORT, FOREIGN_HOST)
    assert asked["action"] == "ask_person" and "other-web-1" in asked["question"]
    assert asked["question"].count("?") == 1
    assert _coded({"why": "worker lost: connection reset by peer"}, kind="build")["action"] \
        == "retry"


def test_the_coded_triage_never_repeats_an_action_for_the_same_cause():
    again = _coded(NPM, last={"signature": "deploy:missing_tool:npm", "action": "fix_app"})
    assert again["action"] == "ask_person"
    assert again["question"].startswith("BOSS already tried that for this same cause")


# ── the model, through BOSS's router, whichever engine ───────────────────────────────────────

class Stub:
    def __init__(self, answer: dict, engine: str):
        self.answer = answer
        self.seen: list = []
        self._boss_choice = model_routing.ModelChoice(engine, "connected", f"setup-{engine}")
        self._boss_route = None

    def create_chat_completion(self, messages, tools=None, **_):
        self.seen.append(list(messages))
        body = json.dumps(self.answer)
        return ({"content": body, "raw": {"role": "assistant", "content": body}},
                {"model": "stub-model", "cost_usd_micros": 3_000})


@pytest.mark.parametrize("engine", ["claude_agent_sdk", "claude_code", "codex", "openrouter",
                                    "native"])
def test_the_decision_is_the_same_whichever_engine_the_router_returns(monkeypatch, engine):
    seen: dict[str, Any] = {}

    def fake_route(connection, settings, project_id, *, purpose=None):
        seen["purpose"] = purpose
        return Stub(CLEAN, engine)

    def fake_governed(adapter, connection, settings, project_id, *, skill_id, ref):
        seen["governed"] = (skill_id, ref)
        return adapter
    monkeypatch.setattr(model_routing, "project_chat_adapter", fake_route)
    monkeypatch.setattr(model_routing, "governed", fake_governed)
    adapter, raw = REAL_ROUTED_ADAPTER(object(), object(), "p1", ref="recovery:wfr_1:deploy")
    # A tier (Balanced), never a model name; every turn governed and metered.
    assert seen["purpose"].tier == "balanced" and seen["purpose"].step == "step_recovery"
    assert seen["governed"] == ("model.step_recovery", "recovery:wfr_1:deploy")
    out = sr.ask_model(adapter, raw_adapter=raw, agent="Harbor", evidence=_ev(PORT, OWN_HOST),
                       allowed=sr.KINDS["deploy"], last={}, signature="deploy:port_held:8000")
    assert out["decision"]["action"] == "clean_and_retry"
    assert out["decision"]["projects"] == ["helpdesk-ai"]
    assert out["by"]["engine"] == engine and out["cost_usd_micros"] == 3_000
    assert "You are Harbor" in adapter.seen[0][0]["content"]


def test_a_refused_answer_is_asked_once_more_then_left_to_boss():
    stub = Stub({**CLEAN, "projects": ["other-app"]}, "native")
    out = sr.ask_model(stub, agent="Harbor", evidence=_ev(PORT, FOREIGN_HOST),
                       allowed=sr.KINDS["deploy"], last={}, signature="deploy:port_held:8000")
    assert out["decision"] is None and "not this app's own" in out["refused"]
    assert len(stub.seen) == 2
    assert stub.seen[1][-1]["content"].startswith("Refused:")


def test_a_model_that_errors_is_a_result_not_a_crash():
    class Broken:
        def create_chat_completion(self, *a, **k):
            raise RuntimeError("upstream 500")
    out = sr.ask_model(Broken(), agent="Harbor", evidence=_ev(PORT), allowed=sr.ACTIONS,
                       last={}, signature="s")
    assert out["decision"] is None and "upstream 500" in out["error"]


def test_no_vendor_sdk_is_imported():
    source = Path(sr.__file__).read_text()
    assert not re.search(r"^\s*(?:import|from)\s+(?:anthropic|openai|claude_agent_sdk)\b",
                         source, re.M)


# ── the clean, guarded on the host ────────────────────────────────────────────────────────────

def test_a_clean_is_guarded_on_the_host_and_keeps_volumes():
    rendered = {"dir": "/opt/boss-apps/helpdesk", "install": [{"name": "up", "run": "x",
                                                               "timeout_seconds": 60}],
                "update": []}
    out = stack_deploy.retire_own_projects(rendered, ["helpdesk-ai", "Bad Name; rm -rf /"])
    steps = [s for s in out["install"] if s["name"].startswith(stack_deploy.RETIRE_OWN_STEP)]
    assert [s["name"] for s in steps] == [f"{stack_deploy.RETIRE_OWN_STEP} (helpdesk-ai)"]
    run = steps[0]["run"]
    assert "com.docker.compose.project.working_dir" in run and "/opt/boss-apps/helpdesk/" in run
    assert "BOSS_NOT_THIS_APPS" in run and "exit 3" in run
    assert "down --remove-orphans" in run and " -v" not in run.split("down", 1)[1]
    assert out["install"][-1]["name"] == "up" and out["update"] == steps
    assert stack_deploy.retire_own_projects(rendered, []) is rendered
    said = stack_deploy.explain_failure("install: x", 3, "BOSS_NOT_THIS_APPS other")
    assert said["cause"] == "host" and "nothing was removed" in said["headline"]


# ── budget, words, records ────────────────────────────────────────────────────────────────────

def test_the_window_has_a_clock():
    class NoDb:
        def begin_nested(self):
            raise RuntimeError("no database")
    state = sr.open_window({})
    ok, _ = sr.budget_left(NoDb(), state, {"budget_usd_micros": 10, "max_seconds": 60})
    assert ok
    state["started_at"] = "2020-01-01T00:00:00+00:00"
    ok, why = sr.budget_left(NoDb(), state, {"budget_usd_micros": 10, "max_seconds": 60})
    assert not ok and "minutes" in why
    state = {**sr.open_window({}), "spent_usd_micros": 10}
    ok, why = sr.budget_left(NoDb(), state, {"budget_usd_micros": 10, "max_seconds": 60})
    assert not ok and "recovery budget" in why
    reopened = sr.open_window({**state, "decisions": [{"action": "fix_app", "signature": "s"}]},
                              why="a person retried")
    assert reopened["spent_usd_micros"] == 0 and sr.last_decision(reopened) == {}


def test_the_run_page_reads_what_the_agent_did():
    entry = {"action": "clean_and_retry", "cause": "port 8000 held by this app's earlier "
             "release (helpdesk-ai)"}
    assert sr.sentence("Harbor", entry) == (
        "Harbor: port 8000 held by this app's earlier release (helpdesk-ai) → cleaning it up "
        "and deploying again")
    assert sr.sentence("Harbor", {"action": "retry", "cause": "a blip",
                                  "backoff_seconds": 60}).endswith("trying again in 60s")


def test_the_recommendations_markdown_has_what_a_developer_needs():
    md = boss_recommendations.markdown([{
        "id": "brec_1", "title": "Template gate plans from the shared repository root",
        "status": "open", "signature": "build:boss:repository_root", "occurrences": 2,
        "created_at": "2026-10-16T10:00:00", "boss_version": "1.0.0rc10",
        "symptom": "needs Java", "evidence": ["pom.xml found at the repository root"],
        "root_cause": "the planner reads the root", "proposed_change": "read the app folder",
        "files": ["engine/boss_engine/templates/gate.py"],
        "refs": [{"run_id": "wfr_1", "step_id": "build", "build_id": "crun_1",
                  "at": "2026-10-16T10:00:00"}]}])
    for part in ("## Template gate plans", "**Status:** open", "2026-10-16", "needs Java",
                 "pom.xml found", "**Root cause (hypothesis).**", "**Proposed change.**",
                 "`engine/boss_engine/templates/gate.py`", "wfr_1", "crun_1", "seen 2 time"):
        assert part in md, part
