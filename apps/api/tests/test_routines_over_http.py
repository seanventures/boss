"""Routines over HTTP (BOSSAPP-877): a schedule made with a cron and its rules, edited to new
rules, a bad cron refused with the field named, and its firings listed with their reasons."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from boss_engine.services import workflow_schedules as sched
from boss_engine.services import workflows

HOME = "boss-dogfood"


def test_a_cron_routine_is_made_edited_and_its_firings_listed(client, engine):
    key = f"routine_{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        made = workflows.create_definition(
            c, project_id=HOME, workflow_key=key, name=key, description=None, actor="test",
            namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=False)
        workflows.publish_version(c, project_id=HOME, definition_id=made["id"], actor="test", spec={
            "entry_step": "hold", "steps": [{"id": "hold", "kind": "timer", "wait_seconds": 60,
                                             "binding": {}, "next": []}]})
    base = {"project_id": HOME, "actor": "ceo", "workflow_key": key, "name": f"{key} weekdays",
            "cadence": "weekly", "timezone": "Europe/London", "input": {}}
    bad = client.post("/v1/delivery-workflows/schedules", json={**base, "cron_expr": "0 25 * * *"})
    assert bad.status_code == 400 and "hour field" in bad.json()["detail"]
    made = client.post("/v1/delivery-workflows/schedules", json={
        **base, "cron_expr": "0 9 * * 1-5", "concurrency_policy": "skip_if_active",
        "catch_up": "all", "catch_up_max": 2, "activity_gate": {"kinds": ["work_item."]}})
    assert made.status_code == 201, made.text
    row = made.json()
    assert row["cadence"] == "cron" and row["describes"] == "on cron '0 9 * * 1-5' (Europe/London)"
    assert (row["concurrency_policy"], row["catch_up"], row["catch_up_max"]) == ("skip_if_active", "all", 2)
    edited = client.post("/v1/delivery-workflows/schedules", json={
        **base, "schedule_id": row["id"], "concurrency_policy": "coalesce", "activity_gate": None})
    assert edited.status_code == 200, edited.text
    assert edited.json()["concurrency_policy"] == "coalesce" and edited.json()["activity_gate"] is None
    with engine.begin() as c:
        c.execute(text("UPDATE workflow_schedules SET next_run_at = now() - interval '1 minute' "
                       "WHERE id = :i"), {"i": row["id"]})
        sched.run_due(c, project_id=HOME)
    firings = client.get(f"/v1/delivery-workflows/schedules/{row['id']}/firings",
                         params={"project_id": HOME})
    assert firings.status_code == 200, firings.text
    assert firings.json()["firings"][0]["outcome"] in ("started", "refused", "failed")
    with engine.begin() as c:
        c.execute(text("DELETE FROM workflow_schedules WHERE id = :i"), {"i": row["id"]})
