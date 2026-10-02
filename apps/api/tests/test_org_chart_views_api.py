from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.db import begin


def test_observed_view_does_not_clear_planned_assignments_and_changes_are_audited(
    client, engine
):
    project_id = get_settings().default_project_id
    placed = client.put(
        "/v1/console/v2/org/qa",
        json={"project_id": project_id, "reports_to": "cto", "actor": "ceo"},
    )
    assert placed.status_code == 200, placed.text

    chart = client.get("/v1/console/v2/org", params={"project_id": project_id})
    assert chart.status_code == 200, chart.text
    body = chart.json()
    assert body["declared"]["qa"]["reports_to"] == "cto"
    # REWRITTEN: this asserted a tree of just ["qa"] — qa saved as reporting to a cto the plan
    # did not contain, so the chart showed qa at the top with "Reports to: cto" beside it. The
    # parent is placed (at the top) along with the child now, so the plan and the chart agree.
    assert {node["id"] for node in body["tree"]} == {"cto", "qa"}
    assert body["declared"]["cto"]["reports_to"] is None
    assert "observed_tree" in body

    # Switching the UI to observed only reads this payload; it does not call DELETE.
    again = client.get("/v1/console/v2/org", params={"project_id": project_id})
    assert again.json()["declared"] == body["declared"]

    cleared = client.delete(
        "/v1/console/v2/org/qa",
        params={"project_id": project_id, "actor": "ceo"},
    )
    assert cleared.status_code == 200, cleared.text
    with begin(engine) as connection:
        events = connection.execute(
            text(
                "SELECT event_type, actor FROM events WHERE project_id=:p "
                "AND entity_id='qa' AND event_type LIKE 'org.%' ORDER BY created_at"
            ),
            {"p": project_id},
        ).mappings().all()
    assert [(row["event_type"], row["actor"]) for row in events] == [
        ("org.assignment_set", "ceo"), ("org.assignment_cleared", "ceo")
    ]
