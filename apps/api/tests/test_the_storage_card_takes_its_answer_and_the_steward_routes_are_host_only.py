"""PR #111 verification (BOSSAPP-1157): the storage card has a form, so the approve body's typed
`answer` is accepted — and `make_rule: false` creates NO rule; the steward's routes refuse the
owner's own token (only the Host tools principal may post readings or take jobs).
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _incident_with_a_card(engine) -> tuple[str, str]:
    """A storage incident whose plan waits on a tier-3 backups card, as storage_cleanup._ask
    writes it (recommendation + form_schema)."""
    from boss_engine.services import storage_cleanup
    from boss_engine.services.typed_forms import normalize_form

    inc_id = f"si_test_{uuid4().hex[:8]}"
    dec_id = f"dec_storage_{uuid4().hex[:8]}"
    host = f"host_{uuid4().hex[:6]}"
    step = {"tool": "storage.remove_backups", "tier": 3, "where": "host", "label": "Remove old backups",
            "state": "ask", "items": 4, "gb": 1.0, "freed_gb": 0.0, "plan_hash": "ph_test",
            "decision_id": dec_id, "receipt_id": None, "action_id": None, "reason": None,
            "args": {"keep_newest": 10, "keep_days": 7, "cap_gb": 20, "install_id": "inst_t"},
            "reversibility": "irreversible", "rule_kind": "backups", "asked_at": None,
            "dry_run_at": None}
    rec = {"action": "storage_delete", "incident_id": inc_id, "host_id": host, "tool": step["tool"],
           "args": step["args"], "plan_hash": "ph_test", "items": 4, "gb": 1.0, "to_delete": ["a", "b", "c", "d"],
           "rule_proposal": {"kind": "backups", "rule": {"keep_newest": 10, "keep_days": 7, "cap_gb": 20},
                             "preticked": True}}
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO storage_incidents (id, host_id, status, severity, cause_key, target_pct, plan, project_id)
            VALUES (:i, :h, 'asking', 'critical', 'backups.no_retention', 25, CAST(:p AS JSONB), :pr)"""),
                  {"i": inc_id, "h": host, "p": json.dumps([step]), "pr": PROJECT})
        c.execute(text("""
            INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, default_action, status,
                                   form_schema)
            VALUES (:d, :pr, 'Delete 4 old backups?', CAST(:r AS JSONB), 3, 'hold', 'pending', CAST(:f AS JSONB))"""),
                  {"d": dec_id, "pr": PROJECT, "r": json.dumps(rec),
                   "f": json.dumps(normalize_form(storage_cleanup.ASK_FORM))})
        c.execute(text("DELETE FROM retention_rules WHERE kind = 'backups' AND host_id = :h"), {"h": host})
    return inc_id, dec_id


def test_approving_with_make_rule_false_is_accepted_and_creates_no_rule(client, engine):
    inc_id, dec_id = _incident_with_a_card(engine)
    approve = client.post(f"/v1/decisions/{dec_id}/approve",
                          json={"actor": "ceo", "answer": {"make_rule": False}})
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "approved"
    with engine.begin() as c:
        host = c.execute(text("SELECT host_id FROM storage_incidents WHERE id = :i"), {"i": inc_id}).scalar()
        rules = c.execute(text("SELECT count(*) FROM retention_rules WHERE kind = 'backups' AND host_id = :h"),
                          {"h": host}).scalar()
        answer = c.execute(text("SELECT answer FROM decisions WHERE id = :d"), {"d": dec_id}).scalar()
        plan = c.execute(text("SELECT plan FROM storage_incidents WHERE id = :i"), {"i": inc_id}).scalar()
        c.execute(text("DELETE FROM decisions WHERE id = :d"), {"d": dec_id})
        c.execute(text("DELETE FROM storage_incidents WHERE id = :i"), {"i": inc_id})
    assert rules == 0, "the pre-tick was overridden: no rule"
    assert answer["make_rule"] is False
    # a dry run that never happened is stale: the step is planned again, still tier 3
    step = plan[0]
    assert step["state"] == "planned" and step["tier"] == 3 and step["decision_id"] is None


def test_approving_with_other_arguments_replans_with_them(client, engine):
    inc_id, dec_id = _incident_with_a_card(engine)
    approve = client.post(f"/v1/decisions/{dec_id}/approve",
                          json={"actor": "ceo", "answer": {"make_rule": False, "args": {"keep_newest": 20}}})
    assert approve.status_code == 200, approve.text
    with engine.begin() as c:
        plan = c.execute(text("SELECT plan FROM storage_incidents WHERE id = :i"), {"i": inc_id}).scalar()
        c.execute(text("DELETE FROM decisions WHERE id = :d"), {"d": dec_id})
        c.execute(text("DELETE FROM storage_incidents WHERE id = :i"), {"i": inc_id})
    assert plan[0]["args"]["keep_newest"] == 20 and plan[0]["state"] == "planned"


def test_an_unknown_answer_key_is_refused_with_the_forms_words(client, engine):
    inc_id, dec_id = _incident_with_a_card(engine)
    refused = client.post(f"/v1/decisions/{dec_id}/approve", json={"actor": "ceo", "answer": {"nuke": True}})
    assert refused.status_code in (400, 409, 422), refused.text
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": dec_id}).scalar()
        c.execute(text("DELETE FROM decisions WHERE id = :d"), {"d": dec_id})
        c.execute(text("DELETE FROM storage_incidents WHERE id = :i"), {"i": inc_id})
    assert status == "pending"


def test_the_owner_token_is_refused_on_the_steward_routes(client):
    """The test client is the Owner's own key: an org admin/owner, not the Host tools account."""
    sample = client.post("/v1/infra/storage/samples",
                         json={"host_id": "h_forged", "total_gb": 500, "free_gb": 10})
    assert sample.status_code == 403, sample.text
    assert sample.json()["detail"]["permission"] == "system:operate"
    lease = client.get("/v1/host-jobs/lease", params={"host_id": "h_forged"})
    assert lease.status_code == 403, lease.text
    result = client.post("/v1/host-jobs/hjob_x/result", json={"ok": True, "items": [], "bytes": 0})
    assert result.status_code == 403, result.text
    key = client.get("/v1/host-jobs/public-key")
    assert key.status_code == 403, key.text
    # the console's own reads stay open to the owner
    current = client.get("/v1/infra/storage/incidents/current")
    assert current.status_code == 200, current.text
