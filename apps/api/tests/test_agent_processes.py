"""Agent process HTTP — PRD handoff chain completes in simulated mode."""

RICH_PRD = """
# ApproveIt PRD
Users (requesters and managers) submit expenses.
Acceptance: must submit, must verify approval, shall reject negatives.
Data model and API for requests. Security: no self-approval.
Risks: terminal immutability. Success metric: one approved request.
Deploy with npm run verify.
"""


def test_start_simulated_prd_process_reaches_captain_accept(client):
    started = client.post(
        "/v1/agent-processes/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "title": "ApproveIt process",
            "prd": RICH_PRD,
            "simulated": True,
            "auto_build": False,
        },
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["status"] == "completed"
    keys = [step["step_key"] for step in body.get("steps") or []]
    assert "intake_prd" in keys
    assert "review_architect" in keys
    assert "review_cto" in keys
    assert "review_vp" in keys
    assert "improve_prd" in keys
    assert "captain_build" in keys
    assert "qa_check" in keys
    assert "ux_review" in keys
    assert "captain_verify" in keys
    assert "panel_certify" in keys
    assert "captain_accept" in keys
    accept = next(step for step in body["steps"] if step["step_key"] == "captain_accept")
    assert accept["status"] == "succeeded"
    panel = next(step for step in body["steps"] if step["step_key"] == "panel_certify")
    assert (panel.get("output") or {}).get("verdict") in {"certified", "certified_planned"}


def test_empty_prd_is_rejected(client):
    missing = client.post(
        "/v1/agent-processes/runs",
        json={"project_id": "boss-dogfood", "actor": "ceo", "prd": "   "},
    )
    assert missing.status_code == 422


def test_templates_include_default_prd_process(client):
    listed = client.get("/v1/agent-processes/templates", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200, listed.text
    keys = [item["process_key"] for item in listed.json()["templates"]]
    assert "prd_to_accepted_work" in keys
