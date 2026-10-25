"""CONSENT ASKED WHERE NO SHEET CAN OPEN (operator, 2026-10-24, northpeak-bank).

Asked to start a build, the BOSS agent (Metatron, console chat) called create_code_task, which was
refused 409 consent_required; the registry turned the refusal into the exception's text and the
reply printed it raw ("Create one in the console (My account > Agents acting for me)"). The class:

  * every consent request carries a link to the console sheet (identity/consent.offer_link): a
    short-lived record keyed by an id, read only by the person it was raised for;
  * a tool refused for want of consent returns a STRUCTURED result (registry.consent_result);
  * the agent turn turns it into a "Give consent" offer of the exact call (engine_agent), whose
    offer carries the link (intent_router.consent_link) and whose press answers the 409 the
    console's consent sheet opens on (main.agent_action).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from boss_engine.identity import consent, lineage

from ._identity_agents_support import add_person, build_env, session_ctx


# ---- the structured result and the offer (no database) ----------------------------------------------

BODY = {"error": "consent_required", "task": "Loan ledger", "task_run_id": None,
        "agents": [{"id": "agt_se", "name": "Software Engineer", "role_key": "builder"}],
        "permissions": ["code_task:read", "github:sync"], "projects": ["prj_np"],
        "github": {"repo": "paramg/northpeak-fineract-b", "repos": ["paramg/northpeak-fineract-b"],
                   "permissions": {"contents": "write", "pull_requests": "write"}},
        "durations": [{"kind": "standing", "label": "For 30 days", "days": 30}], "max_days": 90,
        "consent_id": "pcn_abc", "grant_href": "/account/agents?consent=pcn_abc",
        "grant_url": "https://boss.example/account/agents?consent=pcn_abc"}


def _registry_raising():
    from boss_engine.tools.registry import ToolRegistry, ToolSpec

    def handler(ctx, *, project_id, actor, args):
        raise consent.ConsentRequired(dict(BODY))

    reg = ToolRegistry()
    reg.register(ToolSpec(name="create_code_task", description="start a build", kind="write",
                          handler=handler, parameters={"type": "object", "properties": {}},
                          inherits_route_permission=True, reversibility="reversible"))
    return reg


def test_a_tool_refused_for_consent_returns_the_request_not_its_text():
    from boss_engine.tools.registry import consent_result

    out = consent_result("create_code_task", {"title": "Loan ledger", "_thread_id": "athr_x"},
                         consent.ConsentRequired(dict(BODY)))
    assert out["ok"] is False and out["error"] == "consent_required"
    assert out["consent"]["agents"][0]["name"] == "Software Engineer"
    assert out["grant_url"].endswith("?consent=pcn_abc") and out["consent_id"] == "pcn_abc"
    assert out["args"] == {"title": "Loan ledger"}                 # never BOSS's own _keys
    assert "Software Engineer" in out["message"] and "pcn_abc" in out["message"]
    assert "ConsentRequired" not in str(out) and "409:" not in out["message"]
    assert consent_result("x", {}, ValueError("boom")) is None


def test_the_agent_turn_offers_the_exact_call_as_give_consent():
    from boss_engine.agent.engine_agent import _consent_offer
    from boss_engine.services.intent_router import consent_link
    from boss_engine.tools.registry import consent_result

    pending: list[dict] = []
    refused = consent_result("create_code_task", {"title": "Loan ledger"}, consent.ConsentRequired(dict(BODY)))
    offered = _consent_offer("create_code_task", {"title": "Loan ledger", "_writes_allowed": True}, refused, pending)
    assert pending == [{"tool": "create_code_task", "args": {"title": "Loan ledger"}}]
    assert offered["proposed"] is True and offered["label"].startswith("Give consent")
    # An offer, not a failure: the reply never lists it under "tried and did not work".
    assert offered["ok"] is True and "error" not in offered and offered["status"] == "needs_consent"
    assert "Loan ledger" in offered["label"] and "Never paste" in offered["note"]
    assert consent_link(offered) == {"consent": {"consent_id": "pcn_abc", "grant_href": "/account/agents?consent=pcn_abc",
                                                 "grant_url": "https://boss.example/account/agents?consent=pcn_abc"}}
    # Any other result passes through untouched, and makes no offer.
    other = {"ok": False, "error": "boom"}
    assert _consent_offer("create_code_task", {}, other, pending) is other and len(pending) == 1
    assert consent_link(other) == {}


def test_the_offer_survives_the_response_schema():
    from boss_engine.schemas import ProposedAction

    offer = ProposedAction(id="act_1", tool="create_code_task", args={}, label="Give consent · Build: x",
                           consent={"grant_href": "/account/agents?consent=pcn_abc"})
    assert offer.model_dump()["consent"]["grant_href"].endswith("pcn_abc")
    assert ProposedAction(id="a", tool="t").consent is None


def test_registry_invoke_never_lets_the_refusal_escape_as_text():
    reg = _registry_raising()
    out = reg.invoke("create_code_task", object(), project_id="prj_np", actor="sam", args={"title": "x"})
    assert out["error"] == "consent_required" and out["consent"]["task"] == "Loan ledger"


# ---- the pending record, and the route the console's link reads -------------------------------------

@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


@pytest.fixture()
def api(env, monkeypatch):
    from apps.api.app.routers import identity_agents

    monkeypatch.setattr(identity_agents, "_engine", lambda: env.engine)
    monkeypatch.setattr(identity_agents, "_ctx", lambda request: session_ctx(request.headers.get("x-test-as", env.alice)))
    app = FastAPI()
    app.include_router(identity_agents.build_identity_agents_router())
    identity_agents.install_consent_handler(app)

    @app.post("/test/start-build")
    def start_build():
        with env.db() as c, lineage.acting(session_ctx(env.alice)):
            consent.require_authority(org_id="org_boss", project_id=env.project, role_keys=["forge"],
                                      permissions=consent.CODE_TASK_PERMISSIONS, repos=["acme/web"],
                                      task="Interview Scorecard", run_id="run_ct:build1", db=c)
        return {}

    return TestClient(app)


def _seed_roles(env):
    with env.db() as c:
        c.execute(text("INSERT INTO roles (id, role_key, agent_id) VALUES ('r-forge','forge',:a) ON CONFLICT DO NOTHING"),
                  {"a": env.forge})


def test_every_consent_request_links_to_the_same_sheet_for_its_person_only(env, api):
    _seed_roles(env)
    r = api.post("/test/start-build")
    assert r.status_code == 409
    body = r.json()
    cid = body["consent_id"]
    assert cid.startswith("pcn_") and body["grant_href"] == f"/account/agents?consent={cid}"
    # The start was rolled back; the record was not (its own transaction).
    mine = api.get(f"/v1/identity/me/consent-requests/{cid}")
    assert mine.status_code == 200, mine.text
    got = mine.json()
    assert got["error"] == "consent_required" and got["task"] == "Interview Scorecard"
    assert got["agents"] == body["agents"] and got["permissions"] == body["permissions"]
    assert got["github"]["repos"] == ["acme/web"] and got["task_run_id"] == body["task_run_id"]
    assert got["grant_href"] == body["grant_href"]
    with env.db() as c:
        add_person(c, env.bob)
    assert api.get(f"/v1/identity/me/consent-requests/{cid}", headers={"x-test-as": env.bob}).status_code == 404
    assert api.get("/v1/identity/me/consent-requests/pcn_nope").status_code == 404
    with env.db() as c:
        c.execute(text("UPDATE identity_pending_consents SET expires_at = now() - interval '1 second' WHERE id = :i"), {"i": cid})
    assert api.get(f"/v1/identity/me/consent-requests/{cid}").status_code == 404


def test_the_link_is_absolute_when_boss_knows_its_public_address(env, monkeypatch):
    from boss_engine.identity import authn_support

    monkeypatch.setattr(authn_support, "public_origin", lambda db, org_id=None: "https://boss.example")
    with env.db() as c:
        out = consent.offer_link(c, session_ctx(env.alice), {"error": "consent_required", "task": "t"})
    assert out["grant_url"] == "https://boss.example" + out["grant_href"]
