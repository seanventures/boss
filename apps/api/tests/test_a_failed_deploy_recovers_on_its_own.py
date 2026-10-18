"""A failed deploy is recovered by the deploy step's own agent — no operator Retry, ever.

THE CHAIN (HelpDesk run wfr_fb4fb5ded8c84a4dab12, 2026-10-16), each link of which needed the
operator's Retry that day:
  1. `npm` missing on a Docker host (the app's manifest)            → fix_app (a repair build);
  2. the app exited on `getaddrinfo EAI_AGAIN res-db` (BOSS wiring) → wait_for_boss, recorded for
     BOSS's developers, resumed by itself when BOSS's build changes;
  3. `Bind for 0.0.0.0:8000 failed: port is already allocated`, held by THIS app's earlier
     compose project                                                 → clean_and_retry;
then healthy. The only person in the loop is the one approving each new deployment — that is
governance (each deployment asks under its own approval rule), not recovery.

Around it: another app's container is never touched (the agent's clean of it is refused, and the
host never receives a `down` for it); a spent budget asks ONE question; the same cause twice with
the same action is refused for a different one; every decision is on the step and as an event.

The host is a fake that answers like a Docker machine; GitHub is a fake; the repair build is a
stub; the MODEL is a stub routed as BOSS's router routes any engine. Everything BOSS decides —
evidence, validation, guardrails, execution — is real.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import pytest
from sqlalchemy import text

from boss_engine.services import boss_recommendations, model_routing, step_recovery

from apps.api.tests.test_a_stack_deploys_only_through_an_approved_changeset import (  # noqa: F401 - fixture
    HOME,
    _approve_and_execute,
    _build,
    stack,
)
from apps.api.tests.test_a_deploy_that_fails_for_the_app_is_fixed_and_redeployed import (  # noqa: F401 - fixtures
    FIXED_HEAD,
    NPM_MANIFEST,
    SCRIPT_MANIFEST,
    _deploy_run,
    _deployments,
    _events,
    _finish,
    _step,
    _tick,
    docker_host,
    repairs,
)

TARGET_DIR = "/opt/boss-apps/helpdesk-fix"
OWN_PROJECT = "helpdesk-ai"
EAI = "Error: getaddrinfo EAI_AGAIN res-db\n    at GetAddrInfoReqWrap.onlookup"
PORT = "Error response from daemon: Bind for 0.0.0.0:8000 failed: port is already allocated"


def _msg(content: str) -> dict[str, Any]:
    return {"content": content, "tool_calls": None,
            "raw": {"role": "assistant", "content": content}}


class StubModel:
    """A model behind BOSS's router: reads the evidence it is given and answers per cause.
    `answers` maps a cause-signature prefix to the decision (dict) or a callable(evidence)."""

    def __init__(self, answers: dict[str, Any], *, engine: str = "native"):
        self.answers = answers
        self.calls: list[str] = []
        self._boss_choice = model_routing.ModelChoice(engine, "connected", f"setup-{engine}")
        self._boss_route = None

    def create_chat_completion(self, messages, tools=None, **_):
        pack = next(m["content"] for m in messages if m["role"] == "user")
        signature = re.search(r'"cause_signature": "([^"]+)"', pack).group(1)
        self.calls.append(signature)
        if any(m["role"] == "user" and m["content"].startswith("Refused:") for m in messages):
            again = self.answers.get("refused")
            if again is not None:
                return _msg(json.dumps(again)), {"model": "stub", "cost_usd_micros": 2_000}
        for prefix, answer in self.answers.items():
            if signature.startswith(prefix):
                body = answer(pack) if callable(answer) else answer
                return _msg(json.dumps(body)), {"model": "stub", "cost_usd_micros": 2_000}
        return _msg(json.dumps({"action": "ask_person", "question": "What should BOSS do?",
                                "evidence": []})), {"model": "stub", "cost_usd_micros": 2_000}


CHAIN = {
    "deploy:missing_tool:npm": {
        "action": "fix_app", "cause": "the manifest runs npm on a machine that has no npm",
        "reasoning": "The host has Docker; the app should ship as a compose app.",
        "evidence": ["has no `npm`"],
        "note": "Switch boss.deploy.yaml to kind: compose with a Dockerfile; the host has no "
                "node or npm."},
    "deploy:unwired:res-db": {
        "action": "wait_for_boss", "cause": "the app cannot resolve res-db, BOSS's database",
        "reasoning": "res-db is the service BOSS provisioned; its network is BOSS's wiring.",
        "evidence": ["getaddrinfo EAI_AGAIN res-db"],
        "boss_fix": {"title": "Compose app does not share the resources' network",
                     "root_cause": "the app's compose project is not the resources' project",
                     "proposed_change": "join_resource_project for every compose app with "
                                        "resources", "files": ["stack_deploy.py"]}},
    "deploy:port_held:8000": {
        "action": "clean_and_retry", "cause": "port 8000 is held by this app's earlier release",
        "reasoning": "helpdesk-ai-app-1 runs from this target's own directory.",
        "evidence": ["Bind for 0.0.0.0:8000 failed: port is already allocated"],
        "projects": [OWN_PROJECT]},
}


@pytest.fixture()
def model(monkeypatch):
    holder: dict[str, StubModel] = {}

    def install(answers: dict[str, Any], engine: str = "native") -> StubModel:
        stub = StubModel(answers, engine=engine)
        holder["model"] = stub
        monkeypatch.setattr(step_recovery, "routed_adapter",
                            lambda connection, settings, project_id, *, ref: (stub, stub))
        return stub
    return install


@pytest.fixture()
def host(docker_host):
    """The docker host, also answering the recovery agent's read-only probe, failing the app's
    start with a chosen output, and its health with EAI_AGAIN when told to."""
    fake = docker_host["host"]
    answer = fake.stack_exec
    fake.containers = [
        f"helpdesk-ai-app-1\t{OWN_PROJECT}\t{TARGET_DIR}/releases/{'e' * 40}\trunning\t"
        "0.0.0.0:8000->8000/tcp",
        "other-web-1\tother-app\t/opt/boss-apps/other-app/releases/abc\trunning\t"
        "0.0.0.0:9000->9000/tcp",
    ]
    fake.unhealthy_with = None

    def stack_exec(**kwargs):
        command = kwargs["command"]
        if "BOSS_RECOVERY_PROBE" in command:
            fake.calls.append(kwargs)
            assert kwargs["write"] is False, "the recovery probe must be read-only"
            return subprocess.CompletedProcess(
                [], 0, "BOSS_RECOVERY_PROBE\n" + "\n".join(fake.containers) + "\n", "")
        if fake.unhealthy_with and "BOSS_HEALTHY" in command:
            fake.calls.append(kwargs)
            return subprocess.CompletedProcess([], 1, f"BOSS_UNHEALTHY 000\n{fake.unhealthy_with}",
                                               "")
        return answer(**kwargs)

    fake.stack_exec = stack_exec
    return docker_host


def _recovery(engine, run_id: str) -> dict:
    return (_step(engine, run_id)["output"] or {}).get("recovery") or {}


def _actions(engine, run_id: str) -> list[str]:
    return [d["action"] for d in _recovery(engine, run_id).get("decisions") or []
            if d.get("action") in step_recovery.ACTIONS]


def _deploy_and_approve(client, engine, stack) -> dict:
    """The newest deployment of the target, approved and executed (the governance step)."""
    planned = _deployments(engine, stack["target"]["id"])[-1]
    deployment = client.get(f"/v1/deployments/stack/{planned['id']}").json()["deployment"]
    done, _ = _approve_and_execute(client, engine, deployment)
    return done["deployment"]


def _operator_retries(engine, run_id: str) -> int:
    with engine.begin() as c:
        return int(c.execute(text("SELECT count(*) FROM events WHERE entity_id = :r AND "
                                  "event_type = 'workflow.step_retried' AND actor IN "
                                  "('ceo', 'operator')"), {"r": run_id}).scalar())


def test_the_real_chain_recovers_with_no_operator_retry(client, engine, host, repairs, model,
                                                        monkeypatch):
    stack = host
    stub = model(CHAIN)
    build = _build(engine, '{"owner": "acme", "repo": "shop"}')
    run_id = _deploy_run(engine, build)

    # 1. npm missing → the agent fixes the app.
    _tick(engine)
    assert len(repairs) == 1 and "kind: compose with a Dockerfile" in repairs[0]["note"]
    assert "THE DEPLOY AGENT'S DIAGNOSIS" in repairs[0]["note"]
    assert _actions(engine, run_id) == ["fix_app"]
    step = _step(engine, run_id)
    assert step["status"] == "waiting" and step["output"]["await"]["kind"] == "code_task"

    # The repaired app deploys; this time it exits at start: EAI_AGAIN res-db (BOSS's wiring).
    stack["manifest"] = {"*": SCRIPT_MANIFEST}
    stack["github"].refs["pull/7"] = FIXED_HEAD
    stack["host"].unhealthy_with = EAI
    _finish(engine, repairs[0]["run_id"])
    _tick(engine)
    _tick(engine)
    failed = _deploy_and_approve(client, engine, stack)
    assert failed["status"] == "failed" and failed["error"]["cause"] == "host"
    _tick(engine)

    # 2. → wait_for_boss: recorded for BOSS's developers, the step parked on a BOSS update.
    assert _actions(engine, run_id) == ["fix_app", "wait_for_boss"]
    step = _step(engine, run_id)
    assert step["status"] == "waiting", step
    assert step["output"]["await"]["kind"] == "recovery"
    assert step["output"]["await"]["then"] == "boss_update"
    assert "recorded for BOSS's developers" in step["output"]["note"]
    with engine.begin() as c:
        recs = boss_recommendations.list_recommendations(c, HOME)
    assert [r["signature"] for r in recs] == ["deploy:unwired:res-db"]
    assert recs[0]["refs"][0]["run_id"] == run_id and recs[0]["status"] == "open"
    assert "join_resource_project" in recs[0]["proposed_change"]
    assert "getaddrinfo EAI_AGAIN res-db" in "\n".join(recs[0]["evidence"])
    md = client.get("/v1/boss-recommendations.md", params={"project_id": HOME})
    assert md.status_code == 200 and "Compose app does not share" in md.text
    _tick(engine)
    assert _step(engine, run_id)["status"] == "waiting", "nothing moves before BOSS changes"
    assert len(_deployments(engine, stack["target"]["id"])) == 1

    # BOSS is updated (its build changes): the step resumes by itself and deploys again.
    monkeypatch.setattr(step_recovery, "boss_build", lambda: "1.0.0+updated")
    stack["host"].unhealthy_with = None
    stack["host"].install_fails_with = (1, PORT + "\n")
    _tick(engine)
    _tick(engine)
    assert _events(engine, run_id, step_recovery.RESUMED_EVENT)
    failed = _deploy_and_approve(client, engine, stack)
    assert failed["status"] == "failed", failed
    assert "Port 8000" in failed["error"]["headline"]
    _tick(engine)

    # 3. port held by this app's own earlier release → clean_and_retry, then healthy.
    assert _actions(engine, run_id) == ["fix_app", "wait_for_boss", "clean_and_retry"]
    assert "cleaning it up" in _step(engine, run_id)["output"]["note"]
    stack["host"].install_fails_with = None
    _tick(engine)
    _tick(engine)
    deployments = _deployments(engine, stack["target"]["id"])
    assert len(deployments) == 3
    shipped = _deploy_and_approve(client, engine, stack)
    assert shipped["status"] == "healthy", shipped
    names = [s["name"] for s in shipped["steps"]]
    assert any(n.endswith(f"retire this app's earlier release ({OWN_PROJECT})") for n in names)
    downs = [c["command"] for c in stack["host"].writes() if "compose -p" in c["command"]
             and " down" in c["command"]]
    assert downs and all(OWN_PROJECT in d and "other-app" not in d for d in downs)
    assert all(" -v" not in d.split("down", 1)[1] for d in downs), "volumes are never removed"
    _tick(engine)

    assert _step(engine, run_id)["status"] == "succeeded"
    assert _operator_retries(engine, run_id) == 0
    assert len(repairs) == 1
    # Every decision is said: on the step, and as events the run page shows.
    decided = _events(engine, run_id, step_recovery.DECIDED_EVENT)
    assert [e["data"]["action"] for e in decided] == ["fix_app", "wait_for_boss",
                                                     "clean_and_retry"]
    assert all(e["data"]["sentence"].startswith("Release Manager:") or ":" in
               e["data"]["sentence"] for e in decided)
    assert all(e["data"]["by"]["engine"] == "native" for e in decided)
    assert stub.calls[:3] == ["deploy:missing_tool:npm", "deploy:unwired:res-db",
                              "deploy:port_held:8000"]


def test_another_apps_container_is_never_touched(client, engine, host, repairs, model):
    stack = host
    # Port 8000 is held by ANOTHER app's container; the model tries to clean it anyway.
    stack["host"].containers = [
        "other-web-1\tother-app\t/opt/boss-apps/other-app/releases/abc\trunning\t"
        "0.0.0.0:8000->8000/tcp"]
    model({"deploy:port_held:8000": {**CHAIN["deploy:port_held:8000"], "projects": ["other-app"]},
           "refused": {**CHAIN["deploy:port_held:8000"], "projects": ["other-app"]}})
    stack["manifest"] = {"*": SCRIPT_MANIFEST}
    stack["host"].install_fails_with = (1, PORT + "\n")
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    failed = _deploy_and_approve(client, engine, stack)
    assert failed["status"] == "failed"
    _tick(engine)

    step = _step(engine, run_id)
    assert step["status"] == "failed", step
    decision = _recovery(engine, run_id)["decisions"][-1]
    assert decision["action"] == "ask_person"
    assert "not this app's own" in decision["by"]["model_said"]
    assert "other-web-1" in step["last_error"] and "Port 8000" in step["last_error"]
    assert step["output"]["recovery_question"].count("?") == 1, "ONE question"
    assert not [c for c in stack["host"].writes() if "other-app" in c["command"]
                or " down" in c["command"]]
    # The step's Ask reads what its agent decided and the one question.
    from boss_engine.services import turn_subject

    with engine.begin() as c:
        evidence = turn_subject.resolve_subject(
            c, HOME, turn_subject.SubjectRef("workflow_step", run_id, "deploy"))
    said = evidence.render("why did it stop?")
    assert "recovery decisions" in said and "asking you: Port 8000" in said
    assert "recovery question for a person" in said


def test_a_spent_budget_asks_one_question(client, engine, host, repairs, model, monkeypatch):
    stack = host
    model(CHAIN)
    monkeypatch.setattr(step_recovery, "limits",
                        lambda *a, **k: {"budget_usd_micros": 0, "max_seconds": 3600})
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)

    assert repairs == [], "nothing is started once the budget is spent"
    step = _step(engine, run_id)
    assert step["status"] == "deadlettered", step
    decision = _recovery(engine, run_id)["decisions"][-1]
    assert decision["action"] == "ask_person" and decision["by"]["source"] == "budget"
    assert "recovery budget" in step["last_error"]
    assert step["output"]["recovery_question"].count("?") == 1


def test_the_same_cause_twice_needs_a_different_action(client, engine, host, repairs, model):
    stack = host
    model({"deploy:missing_tool:npm": CHAIN["deploy:missing_tool:npm"],
           "refused": CHAIN["deploy:missing_tool:npm"]})
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    assert len(repairs) == 1
    # The repair did not fix it: the manifest still runs npm. Same cause, and the model says
    # fix_app again — refused; BOSS's triage would also fix → it asks a person instead.
    stack["github"].refs["pull/7"] = FIXED_HEAD
    _finish(engine, repairs[0]["run_id"])
    _tick(engine)
    _tick(engine)

    assert len(repairs) == 1, "the same action is never repeated for the same cause"
    assert _actions(engine, run_id) == ["fix_app", "ask_person"]
    last = _recovery(engine, run_id)["decisions"][-1]
    assert "same cause" in last["by"]["model_said"]
    step = _step(engine, run_id)
    assert step["status"] == "deadlettered"
    assert "BOSS already tried that for this same cause" in step["last_error"]


def test_with_no_model_nothing_is_done_and_a_person_is_asked(client, engine, host, repairs,
                                                             monkeypatch):
    """No model connected: BOSS does NOT act on its coded triage (operator, 2026-10-18 — "it
    cannot work just on keywords without context or models"). The step asks a person, says no
    model decided, and names what BOSS's triage would suggest."""
    def no_model(*_a, **_k):
        raise RuntimeError("no model connected")

    monkeypatch.setattr(step_recovery, "routed_adapter", no_model)
    stack = host
    stack["manifest"] = {"*": SCRIPT_MANIFEST}
    stack["host"].install_fails_with = (1, PORT + "\n")
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    _deploy_and_approve(client, engine, stack)
    _tick(engine)
    decision = _recovery(engine, run_id)["decisions"][-1]
    assert decision["action"] == "ask_person"
    assert decision["by"]["engine"] == "none"
    assert "connected model" in decision["reasoning"]
    assert "cleaning it up" in decision["question"], "the triage's suggestion is named"
    assert not [c for c in stack["host"].writes() if " down" in c["command"]], "nothing done"
    assert repairs == []


def test_a_host_side_question_is_tried_again_after_a_boss_update(client, engine, host, repairs,
                                                                 monkeypatch):
    """A person was asked about a cause BOSS placed on the host (no model: BOSS's triage). When
    BOSS's build changes, the step is tried again by itself — the one Try again, by its agent."""
    stack = host
    stack["manifest"] = {"*": SCRIPT_MANIFEST}
    stack["host"].install_fails_with = (
        255, "ssh: connect to host 203.0.113.40 port 22: Connection refused\n")
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'))
    _tick(engine)
    _deploy_and_approve(client, engine, stack)
    _tick(engine)
    step = _step(engine, run_id)
    assert step["status"] == "failed" and "asks:" in step["last_error"]
    assert step["output"]["recovery_resume_on_update"]
    _tick(engine)
    assert _step(engine, run_id)["status"] == "failed", "nothing moves before BOSS changes"

    monkeypatch.setattr(step_recovery, "boss_build", lambda: "1.0.0+updated")
    stack["host"].install_fails_with = None
    _tick(engine)
    assert _events(engine, run_id, step_recovery.RESUMED_EVENT)
    _tick(engine)
    shipped = _deploy_and_approve(client, engine, stack)
    assert shipped["status"] == "healthy"
    _tick(engine)
    assert _step(engine, run_id)["status"] == "succeeded"
    assert _operator_retries(engine, run_id) == 0
