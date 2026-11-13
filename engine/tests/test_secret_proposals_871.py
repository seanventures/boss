"""BOSSAPP-871: an agent proposes a secret; only a PERSON types its value.

propose_secret (the registry tool, no value argument) -> a store_proposed_secret card -> the
person's typed value approves it through the one approval door and is written through put_secret
(and bound to the named target) -> a plain approval (no typed value) is refused -> a No is kept for
a day. The value is never in the decision, its events or the proposal row.
"""
from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import run_secret_redaction, secret_access_log, secret_backends, secrets_service
from boss_engine.services import secret_proposals as sp
from boss_engine.services.decisions import DecisionService, InvalidDecisionTransitionError

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")

VALUE = "whsec-typed-by-a-person-871"


class _Mem:
    store: dict = {}

    def get(self, key):
        return self.store[key]

    def put(self, key, value):
        self.store[key] = value


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def project(eng, monkeypatch):
    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings: _Mem())
    monkeypatch.setattr(secrets_service, "backend_for", lambda kind, settings: _Mem())
    monkeypatch.setattr(secret_backends, "durable_secret_backend", lambda settings: "database")
    secret_access_log.reset_for_tests()
    run_secret_redaction.reset_for_tests()
    pid = f"p_871_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_871', 'o') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_871', 'org_871', 'w') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_871', :p, :p)"),
                  {"p": pid})
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) VALUES "
                       "(:i, :p, 'deployer', 'deployer')"), {"i": f"role_{uuid.uuid4().hex[:8]}", "p": pid})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', CAST(:r AS JSONB), "
            "CAST(:perm AS JSONB)) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{pid}_ceo", "p": pid, "r": json.dumps(["ceo"]),
             "perm": json.dumps(["decision_resolve"])})
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
            " target_ref, status, created_by) VALUES (:t, :p, 'shop', 'acme/shop', 'prod', 'ssh', "
            " 'deploy@10.0.0.9:22', 'active', 'test')"), {"t": f"dt_{pid}", "p": pid})
    yield pid
    run_secret_redaction.reset_for_tests()


def _tool(eng, project, args, actor="agent:deployer"):
    from boss_engine.tools.builtin import build_default_registry

    spec = next(s for s in build_default_registry().specs(kind="write") if s.name == "propose_secret")
    return spec.handler(SimpleNamespace(begin=eng.begin, settings=None), project_id=project,
                        actor=actor, args=args)


def _approve(eng, decision_id, value=None):
    from contextlib import nullcontext

    from boss_engine.config import get_settings

    held = sp.typed_value(decision_id, value) if value is not None else nullcontext()
    with held, eng.begin() as c:
        DecisionService(get_settings()).transition(c, decision_id, action="approve", actor="ceo",
                                                   note=None)


def test_the_tool_takes_no_value():
    from boss_engine.tools.builtin import build_default_registry

    spec = next(s for s in build_default_registry().specs(kind="write") if s.name == "propose_secret")
    props = json.dumps(spec.input_schema if hasattr(spec, "input_schema") else spec.parameters)
    assert '"value"' not in props


def test_a_proposal_opens_a_card_and_stores_nothing(eng, project):
    out = _tool(eng, project, {"name": "stripe_webhook_secret", "reason": "Checkout must verify webhooks.",
                               "target": "deployment_target", "target_id": "shop", "value": VALUE})
    assert out["ok"] and out["status"] == "asked"
    with eng.begin() as c:
        card = c.execute(text("SELECT status, recommendation FROM decisions WHERE id = :d"),
                         {"d": out["decision_id"]}).mappings().one()
        refs = c.execute(text("SELECT count(*) FROM secret_refs WHERE project_id = :p"),
                         {"p": project}).scalar()
    assert card["status"] == "pending"
    assert card["recommendation"]["action"] == "store_proposed_secret"
    assert card["recommendation"]["target_id"] == f"dt_{project}"
    assert VALUE not in json.dumps(card["recommendation"])      # a value the agent sent is ignored
    assert refs == 0
    again = _tool(eng, project, {"name": "stripe_webhook_secret", "reason": "Checkout must verify webhooks."})
    assert again["status"] == "already_asked"


def test_a_plain_approval_is_refused(eng, project):
    out = _tool(eng, project, {"name": "db_password", "reason": "The app cannot reach its database."})
    with pytest.raises(InvalidDecisionTransitionError, match="Type the value"):
        _approve(eng, out["decision_id"])
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                         {"d": out["decision_id"]}).scalar() == "pending"


def test_the_persons_value_is_stored_and_bound(eng, project):
    out = _tool(eng, project, {"name": "stripe_webhook_secret", "reason": "Checkout must verify webhooks.",
                               "target": "deployment_target", "target_id": "shop",
                               "binding_name": "STRIPE_WEBHOOK_SECRET"})
    _approve(eng, out["decision_id"], VALUE)
    with eng.begin() as c:
        prop = c.execute(text("SELECT * FROM secret_proposals WHERE decision_id = :d"),
                         {"d": out["decision_id"]}).mappings().one()
        dec = c.execute(text("SELECT status, recommendation, resolution_note, answer FROM decisions "
                             "WHERE id = :d"), {"d": out["decision_id"]}).mappings().one()
        bindings = c.execute(text("SELECT secret_refs FROM deployment_targets WHERE id = :t"),
                             {"t": f"dt_{project}"}).scalar()
        events = c.execute(text("SELECT data FROM events WHERE project_id = :p"),
                           {"p": project}).scalars().all()
        ref = c.execute(text("SELECT scope_type, scope_id FROM secret_refs WHERE id = :i"),
                        {"i": prop["secret_ref_id"]}).mappings().one()
    assert prop["status"] == "stored" and prop["resolved_by"] == "ceo"
    assert dec["status"] == "approved"
    assert {"name": "STRIPE_WEBHOOK_SECRET", "secret_ref_id": prop["secret_ref_id"]} in bindings
    assert (ref["scope_type"], ref["scope_id"]) == ("deployment_target", f"dt_{project}")
    assert VALUE in _Mem.store.values()
    for blob in (dict(prop), dict(dec), events):
        assert VALUE not in json.dumps(blob, default=str)
    # and the value is scrubbed from any run output from here on (BOSSAPP-866)
    assert VALUE not in run_secret_redaction.scrub(f"echo {VALUE}")


def test_an_agent_scoped_proposal_is_scoped_to_the_proposer(eng, project):
    out = _tool(eng, project, {"name": "my_token", "reason": "I call the vendor API as myself.",
                               "target": "agent"})
    _approve(eng, out["decision_id"], VALUE)
    with eng.begin() as c:
        ref = c.execute(text("SELECT r.scope_type, r.scope_id FROM secret_refs r JOIN secret_proposals s "
                             "ON s.secret_ref_id = r.id WHERE s.decision_id = :d"),
                        {"d": out["decision_id"]}).mappings().one()
    assert (ref["scope_type"], ref["scope_id"]) == ("agent", "deployer")


def test_a_no_is_kept_for_a_day(eng, project):
    from boss_engine.config import get_settings

    out = _tool(eng, project, {"name": "aws_key", "reason": "To upload the build artifacts."})
    with eng.begin() as c:
        DecisionService(get_settings()).transition(c, out["decision_id"], action="reject",
                                                   actor="ceo", note="not now")
    again = _tool(eng, project, {"name": "aws_key", "reason": "To upload the build artifacts."})
    assert again["status"] == "refused_recently" and not again["ok"]


def test_only_an_agent_proposes(eng, project):
    out = _tool(eng, project, {"name": "x_key", "reason": "Because I want it."}, actor="ceo")
    assert not out["ok"] and "Only an agent" in out["error"]


def test_a_value_for_another_card_does_not_approve_this_one(eng, project):
    a = _tool(eng, project, {"name": "one_key", "reason": "The first integration needs it."})
    b = _tool(eng, project, {"name": "two_key", "reason": "The second integration needs it."})
    from boss_engine.config import get_settings

    with sp.typed_value(a["decision_id"], VALUE), eng.begin() as c, \
            pytest.raises(InvalidDecisionTransitionError):
        DecisionService(get_settings()).transition(c, b["decision_id"], action="approve",
                                                   actor="ceo", note=None)
