"""Architecture approval through the HTTP API the console and the worker use.

A workflow with a panel, an Architecture step and a run report is published, started and ticked
through `/v1/delivery-workflows/*`; the document is read through `/v1/architecture-reviews`, and
answered through the decision endpoints every approval uses. Only the architect's model and the
panel's tool are scripted, as the PRD path's own tests script theirs.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.tests.workflow_management_support import PROJECT, published, run_view, started, tick
from boss_engine.services import architecture_docs as arch
from boss_engine.services import workflow_capability_runner as runner

PRD = "# PRD: Helpdesk\n\nREQUIREMENT-HD-7: tickets are tenant-isolated."


class _Architect:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def create_text_response(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        n = len(self.prompts)
        return "ref", "\n\n".join(f"## {h}\n{k} — draft {n}." for k, h, _w in arch.SECTIONS), \
            {"model": "scripted"}


class _Tool:
    def __init__(self, name, returns):
        self.name, self.kind, self.description, self.parameters = name, "write", name, {}
        self.returns = returns

    def handler(self, ctx, *, project_id, actor, args):
        return dict(self.returns)


class _Registry:
    def __init__(self, tools):
        self._tools = tools

    def specs(self):
        return list(self._tools)


@pytest.fixture()
def scripted(monkeypatch):
    model = _Architect()
    from boss_engine.services import architecture_spend

    def scripted_generate(connection, settings, project_id, **kwargs):
        kwargs["check_work"]()
        return kwargs["adapter"].create_text_response(
            kwargs["prompt"], system_prompt=kwargs["system_prompt"])

    monkeypatch.setattr(architecture_spend, "generate", scripted_generate)
    meeting_run = f"mrun_{uuid4().hex[:8]}"
    monkeypatch.setattr(arch, "architect_adapter", lambda *a, **k: model)
    monkeypatch.setattr(runner, "_tool_registry", lambda: _Registry(
        [_Tool("request_meeting", {"meeting_run_id": meeting_run})]))
    return model, meeting_run


SPEC = {
    "entry_step": "panel",
    "steps": [
        {"id": "panel", "kind": "capability", "next": ["arch"],
         "binding": {"capability": "meeting", "inputs": {"directive": {"from_input": "directive"}}}},
        {"id": "arch", "kind": "capability", "next": ["report"],
         "binding": {"capability": "architecture", "inputs": {}}},
        {"id": "report", "kind": "capability", "next": [],
         "binding": {"capability": "report", "inputs": {}}},
    ],
}


def _panel_done(engine, meeting_run: str) -> None:
    task_id = f"tsk_{uuid4().hex[:8]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO tasks (id, project_id, title, status, dedupe_key) "
                       "VALUES (:t, :p, 'helpdesk', 'open', :t)"), {"t": task_id, "p": PROJECT})
        c.execute(text("INSERT INTO meeting_runs (id, project_id, task_id, status, phase, "
                       "directive, state) VALUES (:i, :p, :t, 'completed', 'done', 'helpdesk', "
                       "CAST(:s AS JSONB))"),
                  {"i": meeting_run, "p": PROJECT, "t": task_id,
                   "s": json.dumps({"prd_markdown": PRD})})
        c.execute(text("INSERT INTO decisions (id, project_id, task_id, question, status, "
                       "approved_by, recommendation) VALUES (:i, :p, :t, 'Publish the PRD?', 'approved', 'ceo', CAST(:rec AS JSONB))"),
                  {"i": f"dec_{uuid4().hex[:8]}", "p": PROJECT, "t": task_id, "rec": json.dumps({"action": "execute_agent_tool",
                      "tool": "publish_document", "args": {"markdown": PRD}})})


def _reviews(client, run_id):
    listed = client.get("/v1/architecture-reviews",
                        params={"project_id": PROJECT, "workflow_run_id": run_id})
    assert listed.status_code == 200, listed.text
    return listed.json()["items"]


def _step(client, run_id, step_id):
    return next(s for s in run_view(client, run_id)["steps"] if s["step_id"] == step_id)


def test_a_run_designs_waits_for_a_person_revises_and_continues(client, engine, scripted):
    model, meeting_run = scripted
    saved = published(client, SPEC)
    run_id = started(client, saved["definition"]["workflow_key"], {"directive": "A helpdesk",
                                                    "title": "Helpdesk"})
    tick(client)
    _panel_done(engine, meeting_run)
    for _ in range(3):
        tick(client)

    [review] = _reviews(client, run_id)
    assert review["status"] == "awaiting_approval" and review["current_version"] == 1
    assert "waiting for a person" in review["status_words"]
    assert [a["action"] for a in review["decision"]["actions"]] == [
        "approve", "revise", "reject", "delegate"]
    detail = client.get(f"/v1/architecture-reviews/{review['id']}",
                        params={"project_id": PROJECT}).json()
    for _key, heading, _what in arch.SECTIONS:
        assert f"## {heading}" in detail["markdown"]
    assert "REQUIREMENT-HD-7" in model.prompts[0]
    assert _step(client, run_id, "arch")["status"] == "waiting"

    # It is in the Approvals inbox as an architecture review, with the document on the card.
    decision_id = review["decision"]["id"]
    card = client.get(f"/v1/decisions/{decision_id}").json()
    assert card["kind"] == "architecture_review"
    assert "draft 1" in card["recommendation"]["document_markdown"]

    # An agent cannot answer it; the architect least of all.
    refused = client.post(f"/v1/decisions/{decision_id}/approve",
                          json={"actor": "chief_architect", "note": "looks fine"})
    assert refused.status_code in (403, 409), refused.text
    assert client.get(f"/v1/decisions/{decision_id}").json()["status"] == "pending"

    # A person asks for changes; the architect writes version 2 and asks again.
    sent_back = client.post(f"/v1/decisions/{decision_id}/revise",
                            json={"actor": "ceo", "note": "Add row-level security."})
    assert sent_back.status_code == 200, sent_back.text
    tick(client)
    [review] = _reviews(client, run_id)
    assert review["status"] == "awaiting_approval" and review["current_version"] == 2
    assert "Add row-level security." in model.prompts[1]
    detail = client.get(f"/v1/architecture-reviews/{review['id']}",
                        params={"project_id": PROJECT}).json()
    assert [v["version"] for v in detail["versions"]] == [1, 2]

    approved = client.post(f"/v1/decisions/{review['decision']['id']}/approve",
                           json={"actor": "ceo", "note": "Approved."})
    assert approved.status_code == 200, approved.text
    for _ in range(3):
        tick(client)
    [review] = _reviews(client, run_id)
    assert review["status"] == "approved" and review["approved_by"] == "ceo"
    assert _step(client, run_id, "arch")["status"] == "succeeded"
    assert _step(client, run_id, "report")["status"] == "succeeded"


def test_reviews_are_read_only_inside_their_own_project(client, engine, scripted):
    _model, meeting_run = scripted
    saved = published(client, SPEC)
    run_id = started(client, saved["definition"]["workflow_key"], {"directive": "A helpdesk"})
    tick(client)
    _panel_done(engine, meeting_run)
    for _ in range(3):
        tick(client)
    [review] = _reviews(client, run_id)
    elsewhere = client.get(f"/v1/architecture-reviews/{review['id']}",
                           params={"project_id": "some-other-project"})
    assert elsewhere.status_code == 404
    missing = client.get("/v1/architecture-reviews/arch_nope", params={"project_id": PROJECT})
    assert missing.status_code == 404


def test_without_an_approved_prd_the_step_says_why(client, engine, scripted):
    spec = {"entry_step": "arch", "steps": [
        {"id": "arch", "kind": "capability", "next": [],
         "binding": {"capability": "architecture", "inputs": {}}}]}
    saved = published(client, spec)
    run_id = started(client, saved["definition"]["workflow_key"], {"directive": "A helpdesk"})
    tick(client)
    step = _step(client, run_id, "arch")
    assert step["status"] in {"failed", "deadlettered"}
    assert "no PRD to design from" in json.dumps(step)
    assert _reviews(client, run_id) == []
