"""The incident squad starts its fix through the one governed start door (design §12.5).

Incident Response starts per alert rule, never per app (BOSSAPP-598): every unattended door but
a rule's own Respond automatically is refused. The squad is not an app-wide switch: the Chief of
Staff routed ONE incident to it under the project's autonomy matrix. So the squad's door
(`incident_squad`) is admitted — but only on authority re-derived from the database, never from
the door's name:

* the door_id is a live ambient incident item of THIS project;
* and either the class's level here lets an agent start a run without a person (the gate), or a
  person approved the fix on that item's card (consent, a person's ledger row);
* the stop wins; every other door is still refused as per_rule_only.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import event_dispatch as ed
from boss_engine.services.ambient import ledger, policy

from ._ambient_support import eng, open_item, project  # noqa: F401


def _start(c, pid, item_id, door=ed.SQUAD_DOOR):
    return ed.start_workflow_on_grant(
        c, project_id=pid, workflow_key="incident_response", link=None, actor="agent:sre",
        input_data={"incident_id": "inc_x"}, dedupe_key=f"t:{uuid4().hex}", door=door,
        door_id=item_id, door_name="Incident squad fix")


def _item(c, pid, task_class):
    return open_item(c, pid, fingerprint=f"fp:{uuid4().hex[:8]}", task_class=task_class)["item"]


@pytest.mark.parametrize("level,allowed", [("A0", False), ("A1", False), ("A2", True),
                                           ("A3", True)])
def test_a_staging_incident_starts_its_fix_at_a2_and_above(eng, project, level, allowed):
    with eng.begin() as c:
        item = _item(c, project, "incident_staging_mitigation")
        policy.set_level(c, project, "incident_staging_mitigation", actor="person:owner",
                         level=level)
        got = ed.squad_authority(c, project_id=project, item_id=item["id"])
        assert got["ok"] is allowed, got
        assert got["level"] == level
        start = _start(c, project, item["id"])
    if allowed:
        assert got["basis"] == "level"
        # Admitted: what stops it now is only that this bare project publishes no workflow.
        assert start["word"] not in (ed.SQUAD_WORD, ed.PER_RULE_WORD)
    else:
        assert start["entry"]["reason"] == "squad_not_authorised"
        assert start["word"] == ed.SQUAD_WORD


@pytest.mark.parametrize("level", ["A1", "A2", "A3"])
def test_a_production_incident_needs_a_persons_approval_at_every_level(eng, project, level):
    with eng.begin() as c:
        item = _item(c, project, "incident_response")
        policy.set_level(c, project, "incident_response", actor="person:owner", level=level)
        assert not ed.squad_authority(c, project_id=project, item_id=item["id"])["ok"]
        assert _start(c, project, item["id"])["entry"]["reason"] == "squad_not_authorised"
        ledger.record_decision(
            c, project_id=project, decided_by="person:ceo", question="Roll back production?",
            chosen="Fix only", rationale="No rollback; fix forward.", reversibility="reversible",
            undo={"action": "cancel_run"}, work_item_id=item["id"])
        got = ed.squad_authority(c, project_id=project, item_id=item["id"])
    assert got["ok"] and got["basis"] == "person" and got["by"] == "person:ceo"


def test_a_persons_hold_is_not_consent(eng, project):
    with eng.begin() as c:
        item = _item(c, project, "incident_response")
        ledger.record_decision(
            c, project_id=project, decided_by="person:ceo", question="Roll back production?",
            chosen="Hold", rationale="I will take it.", reversibility="reversible",
            undo={"action": "cancel_run"}, work_item_id=item["id"])
        assert not ed.squad_authority(c, project_id=project, item_id=item["id"])["ok"]


def test_a_non_incident_item_or_another_projects_item_is_refused(eng, project):
    with eng.begin() as c:
        other = _item(c, project, "flaky_test")
        policy.set_level(c, project, "flaky_test", actor="person:owner", level="A3")
        assert "not an incident" in ed.squad_authority(c, project_id=project,
                                                       item_id=other["id"])["why"]
        staged = _item(c, project, "incident_staging_mitigation")
        assert not ed.squad_authority(c, project_id="p_someone_else", item_id=staged["id"])["ok"]
        assert not ed.squad_authority(c, project_id=project, item_id="wi_nope")["ok"]


def test_the_stop_wins(eng, project):
    with eng.begin() as c:
        item = _item(c, project, "incident_staging_mitigation")
        policy.stop(c, project_id=project, actor="person:owner", reason="bad day")
        got = ed.squad_authority(c, project_id=project, item_id=item["id"])
    assert not got["ok"] and "stopped" in got["why"]


@pytest.mark.parametrize("door", ["ambient", "subscription", "schedule", ""])
def test_every_other_door_is_still_per_rule_only(eng, project, door):
    with eng.begin() as c:
        item = _item(c, project, "incident_staging_mitigation")
        got = _start(c, project, item["id"], door=door)
    assert got["entry"]["reason"] == "per_rule_only"


def test_the_squad_uses_its_own_door_and_never_records_a_refused_start():
    from pathlib import Path

    body = (Path(__file__).resolve().parents[1] / "boss_engine/services/incident_squad.py"
            ).read_text()
    assert "door=SQUAD_DOOR" in body and 'door="ambient"' not in body
    assert 'if not done.get("ok", True):' in body.split("def start_fix(self, *")[1]
