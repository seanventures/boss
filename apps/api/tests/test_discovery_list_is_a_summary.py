"""The discovery LIST returns list rows; the dossier is fetched when one is opened.

GET /v1/discoveries returned every run's full research state: 447 KB for 22 runs on boss-1,
425 KB of it `state`, serialised into every console refresh that rendered Discovery, for a list
that reads four numbers out of it. Each row is now held to a budget, and the detail endpoint
still carries the whole dossier.
"""

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.services.discovery_service import LIST_ITEM_BUDGET_BYTES

PROJECT = "boss-dogfood"


def _dossier(i: int) -> dict:
    """Shaped like _run_discovery's dossier, ~20 KB, the weight boss-1's runs carry."""
    filler = "evidence from a cited market source " * 12
    return {
        "job_id": None,
        "deep": i % 2 == 0,
        "research_brief": {"hypothesis": filler, "questions": [filler] * 4},
        "signal_findings": [
            {"angle": "demand", "finding": filler, "evidence": filler * 2,
             "citation_url": f"https://example.com/{i}/{k}"} for k in range(7)
        ],
        "market_analysis": {"tam": filler, "dynamics": {"growth": filler}},
        "critique": {"overall_confidence_0_1": 0.5, "biggest_risk": filler,
                     "claims": [{"claim": filler, "verdict": "supported", "reason": filler}] * 3,
                     "disconfirming_evidence": [filler] * 2},
        "candidate_outcomes": [{"outcome": filler, "wedge": filler, "target_pilot": filler,
                                "confidence_0_1": 0.6}] * 3,
        "gate": {"verdict": "promising", "confidence": 0.62, "checks": {"has_outcome": True}},
        "_diagnostics": {s: {"reply_head": "x" * 1200} for s in ("sweep", "vet", "shape")},
    }


def test_the_list_is_bounded_rows_and_the_detail_keeps_the_dossier(client, engine):
    project = f"discovery_list_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:id"), {"id": PROJECT},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :w, 'Discovery list', :id)"),
            {"id": project, "w": workspace_id},
        )
        for i in range(22):
            connection.execute(
                text("INSERT INTO discovery_runs (id, project_id, status, phase, title, seed, "
                     "context, state, usage) VALUES (:id, :p, 'completed', 'done', :t, :seed, "
                     ":ctx, CAST(:s AS JSONB), CAST(:u AS JSONB))"),
                {"id": f"drun_list_{i}_{uuid4().hex[:6]}", "p": project, "t": f"Run {i}",
                 # A seed is the operator's own text and can be a pasted page.
                 "seed": "an idea worth researching " * (200 if i == 0 else 2),
                 "ctx": "operator context " * 300,
                 "s": json.dumps(_dossier(i)),
                 "u": json.dumps({"cost_usd_micros": 950000, "model": "claude"})},
            )

    response = client.get("/v1/discoveries", params={"project_id": project, "limit": 50})
    assert response.status_code == 200
    rows = response.json()["discoveries"]
    assert len(rows) == 22
    for row in rows:
        assert "state" not in row, "a list row must not carry the dossier"
        size = len(json.dumps(row))
        assert size <= LIST_ITEM_BUDGET_BYTES, f"{row['id']} is {size} bytes"
        # What the Discovery list shows, all of it, from the summary.
        assert row["summary"]["verdict"] == "promising"
        assert row["summary"]["confidence"] == 0.62
        assert row["summary"]["findings"] == 7
        assert row["summary"]["outcomes"] == 3
        assert row["usage"]["cost_usd_micros"] == 950000
    # The whole page of 22 is what one dossier used to weigh, not what all of them did.
    assert len(response.content) <= 22 * LIST_ITEM_BUDGET_BYTES

    detail = client.get(f"/v1/discoveries/{rows[0]['id']}")
    assert detail.status_code == 200
    state = detail.json()["state"]
    assert len(state["signal_findings"]) == 7
    assert state["critique"]["biggest_risk"]


def test_a_state_with_unexpected_shapes_still_lists(client, engine):
    """`state` is free-form JSON a model wrote; a summary of it must never 500 the list."""
    project = f"discovery_odd_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        workspace_id = connection.execute(
            text("SELECT workspace_id FROM projects WHERE id=:id"), {"id": PROJECT},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:id, :w, 'Odd discovery', :id)"),
            {"id": project, "w": workspace_id},
        )
        connection.execute(
            text("INSERT INTO discovery_runs (id, project_id, status, phase, title, state) "
                 "VALUES (:id, :p, 'completed', 'done', 'Odd', CAST(:s AS JSONB))"),
            {"id": f"drun_odd_{uuid4().hex[:6]}", "p": project,
             "s": json.dumps({"signal_findings": "not a list", "candidate_outcomes": None,
                              "gate": {"verdict": "pursue", "confidence": "high"}})},
        )
    response = client.get("/v1/discoveries", params={"project_id": project})
    assert response.status_code == 200
    (row,) = response.json()["discoveries"]
    assert row["summary"] == {"verdict": "pursue", "confidence": None, "findings": 0,
                              "outcomes": 0, "deep": False}
