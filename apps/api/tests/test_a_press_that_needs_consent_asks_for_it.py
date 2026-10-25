"""A PRESS THAT NEEDS YOUR CONSENT ASKS FOR IT (operator, 2026-10-24, northpeak-bank).

An agent turn that met consent_required now offers the exact call as "Give consent · …"
(engine_agent._consent_offer). Pressing it — the console's button, Metatron's /pick — must answer
the flat 409 consent_required the console's consent sheet opens on (and Metatron prints the link
from), not a 200 whose result buries the refusal. Nothing ran, so nothing is recorded in the
thread: the offer stays standing to be pressed again after the grant.
"""
from __future__ import annotations

from boss_engine.tools.registry import ToolRegistry, consent_result
from boss_engine.identity.consent import ConsentRequired

BODY = {"error": "consent_required", "task": "Loan ledger", "task_run_id": None,
        "agents": [{"id": "agt_se", "name": "Software Engineer", "role_key": "builder"}],
        "permissions": ["code_task:read"], "projects": ["boss-dogfood"], "github": None,
        "durations": [{"kind": "standing", "label": "For 30 days", "days": 30}], "max_days": 90,
        "consent_id": "pcn_abc", "grant_href": "/account/agents?consent=pcn_abc"}


def test_pressing_a_give_consent_offer_answers_the_409_the_sheet_opens_on(client, monkeypatch):
    real = ToolRegistry.invoke

    def invoke(self, name, ctx, **kw):
        if name == "create_code_task":
            return consent_result(name, kw.get("args") or {}, ConsentRequired(dict(BODY)))
        return real(self, name, ctx, **kw)

    monkeypatch.setattr(ToolRegistry, "invoke", invoke)
    thread = client.post("/v1/agent/threads/ensure", json={"project_id": "boss-dogfood", "actor": "ceo"})
    thread_id = thread.json().get("thread_id") if thread.status_code == 200 else None
    before = client.get(f"/v1/agent/threads/{thread_id}/messages",
                        params={"project_id": "boss-dogfood", "actor": "ceo"}).json() if thread_id else {}
    pressed = client.post("/v1/engine/agent/action", json={
        "project_id": "boss-dogfood", "actor": "ceo", "tool": "create_code_task",
        "args": {"title": "Loan ledger"}, "thread_id": thread_id, "label": "Give consent · Build: Loan ledger"})
    assert pressed.status_code == 409, pressed.text
    body = pressed.json()
    assert body["error"] == "consent_required" and body["task"] == "Loan ledger"
    assert body["grant_href"] == "/account/agents?consent=pcn_abc"     # flat: the sheet reads it as is
    if thread_id:
        after = client.get(f"/v1/agent/threads/{thread_id}/messages",
                           params={"project_id": "boss-dogfood", "actor": "ceo"}).json()
        assert len(after.get("messages") or []) == len(before.get("messages") or [])
