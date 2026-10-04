"""A reporting line lives in one place: the plan Organization › Lines edits (spec §4.6, §6.5).

The plan (`PUT /v1/console/v2/org`) wrote `org_assignments`; the Passport's declared lineage
read `roles.reports_to`, a second store nothing kept in step. So the chart and the Passport could
name two different bosses for one agent. Lineage `mode=declared` now reads the plan, a line set
on the agent itself is written to the plan, and the plan is copied onto roles.reports_to for
history.
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _declared(client, role):
    got = client.get(f"/v1/agents/{role}/lineage",
                     params={"project_id": PROJECT, "mode": "declared"})
    assert got.status_code == 200, got.text
    return [(e["from"], e["label"]) for e in got.json()["edges"] if e["label"] == "Reports to"]


def test_a_line_drawn_in_the_plan_is_the_line_the_passport_declares(client, engine):
    put = client.put("/v1/console/v2/org/qa",
                     json={"project_id": PROJECT, "actor": "ceo",
                           "reports_to": "engineering_captain"})
    assert put.status_code == 200, put.text
    assert _declared(client, "qa") == [("agent:engineering_captain", "Reports to")]

    moved = client.put("/v1/console/v2/org/qa",
                       json={"project_id": PROJECT, "actor": "ceo", "reports_to": "cto"})
    assert moved.status_code == 200, moved.text
    assert _declared(client, "qa") == [("agent:cto", "Reports to")]

    # The plan is copied onto roles.reports_to for history.
    from boss_engine.services.agent_acts import copy_declared_lines

    with engine.begin() as c:
        copy_declared_lines(c, PROJECT)
        assert c.execute(text("SELECT reports_to FROM roles WHERE project_id = :p "
                              "AND role_key = 'qa'"), {"p": PROJECT}).scalar() == "cto"
        assert copy_declared_lines(c, PROJECT) == 0  # idempotent


def test_a_line_set_on_the_agent_is_written_to_the_plan(client, engine):
    made = client.patch("/v1/agents/sre", json={"project_id": PROJECT, "actor": "ceo",
                                                 "reports_to": "coo"})
    assert made.status_code == 200, made.text
    with engine.begin() as c:
        planned = c.execute(text("SELECT reports_to FROM org_assignments WHERE project_id = :p "
                                 "AND agent_id = 'sre'"), {"p": PROJECT}).scalar()
    assert planned == "coo"
    assert _declared(client, "sre") == [("agent:coo", "Reports to")]
    chart = client.get("/v1/console/v2/org", params={"project_id": PROJECT}).json()
    assert (chart.get("declared") or {}).get("sre", {}).get("reports_to") == "coo"
