"""Work started from a Jira ticket has the right defaults (BOSSAPP-1334, the FXQuote run, KAN-11).

1. Add project read the description's words but not the ticket they linked: it made up the
   must-haves and asked "share the spec". The understand route now reads the linked ticket (the
   same resolver as a start) and the brief's must-haves are the ticket's acceptance criteria.
2. Closing the consent sheet showed the raw "consent_required": a refusal's message is words.
4. New work and Add project offer the profile Change it offers (lean · balanced · strict): New
   work's `profile` rides in the run input and the build step runs on it.

The ticket system is the one thing replaced: it reads Jira over the network.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from apps.api.app.config import get_settings

KAN_11 = {
    "provider": "jira", "connection_id": "tkc_fx", "key": "KAN-11",
    "url": "https://fx.atlassian.net/browse/KAN-11", "title": "FXQuote: quote a currency pair",
    "type": "story", "status": "To Do", "body": "Traders need a firm FX quote.\n\nSize: Medium",
    "acceptance_criteria": ["GET /quote?pair=EURUSD returns bid and ask",
                            "A quote expires after 30 seconds",
                            "An unknown pair answers 404"],
    "comments": [], "parent": None, "screen": {"verdict": "clean"}, "untrusted": False,
}


def _project() -> str:
    return get_settings().default_project_id


# ── 1. Add project reads the linked ticket ──────────────────────────────────────────────────

def test_add_project_writes_the_brief_from_the_linked_ticket(client, monkeypatch):
    from apps.api.app import main

    asked: list[dict] = []

    def read(project_id, run_input):
        asked.append(dict(run_input))
        return dict(KAN_11), None

    monkeypatch.setattr(main, "_ticket_for_start", read)
    words = "FXQuote service for the desk. Spec: https://fx.atlassian.net/browse/KAN-11"
    r = client.post(f"/v1/projects/{_project()}/add-project/understand", json={"words": words})
    assert r.status_code == 200, r.text
    out = r.json()
    assert asked and asked[0]["directive"] == words          # read as the start would
    assert out["ticket"]["key"] == "KAN-11" and out["ticket"]["criteria"] == 3
    assert out["brief"]["must_haves"] == KAN_11["acceptance_criteria"]
    assert not any("spec" in str(q).lower() for q in out["brief"]["open_questions"])


def test_add_project_without_a_ticket_is_unchanged(client, monkeypatch):
    from apps.api.app import main

    monkeypatch.setattr(main, "_ticket_for_start", lambda project_id, run_input: (None, None))
    r = client.post(f"/v1/projects/{_project()}/add-project/understand",
                    json={"words": "A shift swap board for nurses"})
    assert r.status_code == 200, r.text
    assert "ticket" not in r.json()


def test_a_named_ticket_that_cannot_be_read_is_a_note_not_a_failure(client, monkeypatch):
    from apps.api.app import main

    def refuse(project_id, run_input):
        raise HTTPException(status_code=403, detail={"error": "auth_failed",
                                                     "message": "Jira refused the token."})

    monkeypatch.setattr(main, "_ticket_for_start", refuse)
    r = client.post(f"/v1/projects/{_project()}/add-project/understand",
                    json={"words": "Build KAN-11"})
    assert r.status_code == 200, r.text
    assert r.json()["ticket_note"]["message"] == "Jira refused the token."


# ── 2. a refusal is words, never a bare code ───────────────────────────────────────────────

def test_a_consent_refusal_is_said_in_words():
    from apps.api.app.routers.add_project import _words_of

    said = _words_of(HTTPException(status_code=409, detail={"error": "consent_required",
                                                            "task": "Plan it", "agents": []}))
    assert said["message"] != "consent_required" and " " in said["message"]
    assert "consent" in said["message"].lower()
    assert said["error"] == "consent_required"               # the sheet still opens from it
    # A refusal with its own sentence keeps it.
    assert _words_of(HTTPException(status_code=409, detail={
        "error": "x", "message": "The host is full."}))["message"] == "The host is full."


# ── 4. the profile picked rides to the build ────────────────────────────────────────────────

def _start_work(client, body):
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": _project(), "actor": "ceo"})
    path = f"/v1/projects/{_project()}/work"
    r = client.post(path, json=body)
    if r.status_code == 409 and isinstance(r.json().get("detail"), dict):
        detail = r.json()["detail"]
        ready = detail.get("readiness") if isinstance(detail.get("readiness"), dict) else detail
        missing = [m["key"] for m in (ready.get("missing") or [])]
        if missing:
            r = client.post(path, json={**body, "start_anyway": missing})
    return r


def test_new_work_carries_the_profile_picked(client, engine):
    started = _start_work(client, {"kind": "feature", "profile": "strict",
                                   "text": "Add a /healthz route that reports the pool."})
    assert started.status_code == 200, started.text
    with engine.begin() as connection:
        run_input = connection.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                                       {"r": started.json()["run_id"]}).scalar()
    assert run_input["profile"] == "strict"


def test_the_build_step_runs_on_the_run_inputs_profile():
    """Whatever started the run (New work, Add project), its build step is given that profile
    (create_code_task's `profile`), unless the step bound one itself."""
    from boss_engine.services.workflow_capability_runner import with_run_profile

    assert with_run_profile({"title": "x"}, {"profile": "strict"})["profile"] == "strict"
    assert with_run_profile({"profile": "lean"}, {"profile": "strict"})["profile"] == "lean"
    assert "profile" not in with_run_profile({}, {"profile": "turbo"})
    assert "profile" not in with_run_profile({}, {})


def test_a_profile_that_is_not_one_is_refused(client):
    r = client.post(f"/v1/projects/{_project()}/work",
                    json={"kind": "feature", "profile": "turbo", "text": "Add a route."})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "bad_profile"


@pytest.mark.parametrize("profile,expected", [("lean", "lean"), (None, None)])
def test_add_project_carries_the_profile_in_its_run_input(profile, expected):
    from apps.api.app.routers.add_project import CreateAndStartBody, _run_input

    body = CreateAndStartBody(context_project_id="p", name="FXQuote", words="Quote FX",
                              profile=profile)
    run_input = _run_input(body, {"title": "FXQuote"}, None)
    assert run_input.get("profile") == expected
