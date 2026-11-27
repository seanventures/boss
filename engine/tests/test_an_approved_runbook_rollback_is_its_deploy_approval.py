"""BOSSAPP-1293: a person approved an SRE runbook's rollback_deployment step for ONE deployment.
That approval is the rollback deploy's approval (no second "Deploy …?" card) — but only for the
deployment the step named, only once approved, and only from a runbook rollback step."""
from __future__ import annotations

import json

from sqlalchemy import text

from boss_engine.services import stack_deploy

from ._sre_squad_support import eng, project  # noqa: F401


def _card(c, pid, did, rec, status="approved"):
    c.execute(text("INSERT INTO decisions (id, project_id, question, status, approved_by, "
                   "recommendation) VALUES (:d, :p, 'Approve SRE runbook?', :s, 'ops-lead', "
                   "CAST(:r AS JSONB))"), {"d": did, "p": pid, "s": status, "r": json.dumps(rec)})


def test_a_runbook_rollback_approval_holds_for_its_own_deployment_only(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(stack_deploy, "_row", lambda c, i, **k: {"project_id": project})
    monkeypatch.setattr(stack_deploy, "get_stack_deployment",
                        lambda c, i: {"rollback_commit": "fa64985", "actions": ["rollback"]})
    rec = {"kind": "sre_runbook", "step_action": "rollback_deployment",
           "params": {"deployment_id": "sdep_bad"}}
    tag = project[-6:]
    with eng.begin() as c:
        _card(c, project, f"dec_rb_{tag}", rec)
        _card(c, project, f"dec_pe_{tag}", rec, status="pending")
        _card(c, project, f"dec_pg_{tag}", {**rec, "step_action": "page_oncall"})

        def ask(did, rolled="sdep_bad", source="sre_runbook"):
            return stack_deploy.rollback_approval(
                c, project, {"source": source, "approved_by": "ops-lead", "decision_id": did},
                rollback_of=rolled, commit="fa64985")

        ok = ask(f"dec_rb_{tag}")
        other_deployment = ask(f"dec_rb_{tag}", rolled="sdep_other")
        pending = ask(f"dec_pe_{tag}")
        not_a_rollback = ask(f"dec_pg_{tag}")
        not_from_a_runbook = ask(f"dec_rb_{tag}", source="incident_squad")
    assert ok == {"source": "sre_runbook", "decision_id": f"dec_rb_{tag}",
                  "approved_by": "ops-lead"}
    assert other_deployment is None and pending is None and not_a_rollback is None
    assert not_from_a_runbook is None
