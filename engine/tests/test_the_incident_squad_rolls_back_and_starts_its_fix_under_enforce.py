"""Alert -> incident squad -> staging rollback -> the fix run starts, with identity ENFORCED.

The always-on security squads had every launch refused (`forbidden`): they called the tool
registry with no identity, so it decided as the worker on the thread. The incident squad's fix
does not go through the registry — it starts Incident Response through the governed door
(event_dispatch.start_workflow_on_grant, door `incident_squad`), whose authority is re-derived
from the item (the class level, or a person's approval). This pins that the demo's A2 staging
path works under BOSS_AUTHZ_MODE=enforce with sys_worker on the thread, exactly as the ambient
tick runs it: the squad rolls back on its own and the fix run starts — no `forbidden`.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity.principal import (AuthContext, AuthMethod, Principal, PrincipalKind,
                                            PrincipalStatus)
from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services import workflow_catalogue as wc
from boss_engine.services import workflows
from boss_engine.services.ambient import collab, ledger
from boss_engine.tools import registry as registry_module

from ._sre_squad_support import eng, project  # noqa: F401

WORKER = AuthContext(Principal("sys_worker", PrincipalKind.SYSTEM, "org_amb", PrincipalStatus.ACTIVE,
                               "BOSS"), AuthMethod.SYSTEM)


class _RealFix(demo.FixtureEffects):
    """The demo's host stand-ins for the rollback and the recovery verdict; the fix is BOSS's
    real one: Incident Response through the squad's governed door."""

    def start_fix(self, connection, **kw):
        return squad.BossEffects(get_settings()).start_fix(connection, **kw)


def _publish_incident_response(c, pid: str) -> None:
    entry = next(e for e in wc.CATALOGUE if e["key"] == "incident_response")
    spec = wc.entry_spec(entry)
    definition = workflows.create_definition(c, project_id=pid, workflow_key="incident_response",
                                             name="incident_response", description=None,
                                             actor="test", namespace="product")
    version = workflows.publish_version(
        c, project_id=pid, definition_id=definition["id"], actor="test",
        spec={"entry_step": spec["entry_step"], "steps": spec["steps"]})
    c.execute(text("UPDATE workflow_versions SET spec = spec || CAST(:x AS JSONB) WHERE id = :v"),
              {"v": version["id"], "x": json.dumps({"input_schema": spec["input_schema"]})})
    c.execute(text("UPDATE workflow_definitions SET active_version_id = :v WHERE id = :d"),
              {"v": version["id"], "d": definition["id"]})


def test_staging_alert_rolls_back_and_starts_the_fix_with_identity_enforced(eng, project,  # noqa: F811
                                                                            monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "authz_mode", "enforce")
    monkeypatch.setattr(s, "authz_router_modes", "")
    # The project's readiness (a model, a deployment host) is the start's own preflight, with its
    # own suite; here the project is ready, so any refusal would be identity's or the door's.
    from boss_engine.services import workflow_readiness

    monkeypatch.setattr(workflow_readiness, "readiness_of_version",
                        lambda *a, **k: {"applies": False, "missing": []})
    token = registry_module.CURRENT_TOOL_AUTH.set(WORKER)
    try:
        with eng.begin() as c:
            _publish_incident_response(c, project)
            prepared = demo.prepare(c, project, environment="staging")
            out = squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                                       mind=demo.demo_mind("staging"), effects=_RealFix())
            item_id = prepared["item_id"]
            rollback = [d for d in ledger.list_decisions(c, project, work_item_id=item_id)
                        if d["action"] == "staging_rollback"]
            runs = c.execute(text(
                "SELECT r.id FROM workflow_runs r JOIN workflow_versions v ON v.id = "
                "r.workflow_version_id JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
                "WHERE r.project_id = :p AND d.workflow_key = 'incident_response'"),
                {"p": project}).scalars().all()
            links = c.execute(text(
                "SELECT ref FROM work_item_links WHERE work_item_id = :w AND kind = 'workflow_run'"),
                {"w": item_id}).scalars().all()
            said = json.dumps([m["body"] for m in collab.messages(c, item_id)])
    finally:
        registry_module.CURRENT_TOOL_AUTH.reset(token)
    assert len(rollback) == 1 and rollback[0]["decided_by"] == "agent:sre", rollback
    assert len(runs) == 1, (out, said[:600])
    assert runs[0] in links
    assert "forbidden" not in said and "identity_unavailable" not in said
