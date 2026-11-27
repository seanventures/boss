"""API tests for M5: Failure Pre-check Proposals (BOSSAPP-541) & One Rules Book (BOSSAPP-572)."""
from __future__ import annotations


def _project_id(client) -> str:
    res = client.get("/v1/projects")
    assert res.status_code == 200, res.text
    items = res.json().get("projects") or res.json().get("items") or []
    assert items, "expected seeded project"
    return str(items[0]["id"])


def test_rules_book_catalog_and_role_filtering(client):
    pid = _project_id(client)
    res = client.get(f"/v1/rules-book?project_id={pid}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["read_only"] is True
    assert body["total"] >= 20
    ids = {r["id"] for r in body["rules"]}
    assert "autonomy.dev_act" in ids
    assert "build.open_pr_when_checks_fail" in ids
    assert "security.never_print_credential" in ids

    # Search & category filter
    res_search = client.get(f"/v1/rules-book?project_id={pid}&category=build&q=prometheus")
    assert res_search.status_code == 200, res_search.text
    assert any(r["id"] == "build.observability_metrics" for r in res_search.json()["rules"])

    # Role-scoped endpoint respects role categories & token budget
    res_ux = client.get(f"/v1/rules-book/for-role/ux?project_id={pid}&max_tokens=1000")
    assert res_ux.status_code == 200, res_ux.text
    ux = res_ux.json()
    ux_ids = {r["id"] for r in ux["rules"]}
    assert "security.never_print_credential" in ux_ids
    assert "ux.tokens_and_copy_budget" in ux_ids
    assert "cost.call_classes" not in ux_ids
    assert ux["tokens_used"] <= 1000


def test_precheck_proposals_api_replay_dedupe_and_human_review(client):
    pid = _project_id(client)

    # Replay all three 2026-10-14 failures
    replays = [
        (
            "crun_api_socat",
            "Sandbox dependencies not available: socat not installed",
            "harness.missing_runtime",
            "sandbox.engine.binary.socat",
        ),
        (
            "crun_api_disconnected",
            "Not logged in (401 invalid bearer token) — source reported connected",
            "model.source_disconnected",
            "sandbox.model.live_source_state",
        ),
        (
            "crun_api_caps",
            "Budget cap exceeded: daily cap cannot fit metered billing",
            "budget.cap_vs_metered_billing",
            "budget.run_cost.metered_fit",
        ),
    ]
    proposal_ids = []
    for run_id, error, expected_class, expected_check in replays:
        r = client.post(
            "/v1/precheck-proposals/propose",
            json={"project_id": pid, "actor": "ceo", "run_id": run_id, "error": error},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["id"].startswith("pcp_")
        assert data["recommendation_id"].startswith("brec_")
        assert data["status"] == "proposed"
        assert data["proposal"]["cause_class"] == expected_class
        assert data["proposal"]["proposed_check_id"] == expected_check
        assert data["proposal"]["auto_apply"] is False
        proposal_ids.append(data["id"])

    # Dedupe second occurrence of socat failure
    dup = client.post(
        "/v1/precheck-proposals/propose",
        json={
            "project_id": pid,
            "actor": "ceo",
            "run_id": "crun_api_socat_2",
            "error": "Sandbox dependencies not available: socat not installed",
        },
    )
    assert dup.status_code == 201, dup.text
    assert dup.json()["id"] == proposal_ids[0]
    assert dup.json()["occurrences"] == 2

    # List endpoint returns all 3 proposals + cards
    listed = client.get(f"/v1/precheck-proposals?project_id={pid}")
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 3
    assert len(listed.json()["cards"]) == 3

    # Human review approves proposal 0
    rev = client.post(
        f"/v1/precheck-proposals/{proposal_ids[0]}/review",
        json={"project_id": pid, "actor": "ceo", "status": "approved", "note": "Ship pre-check PR"},
    )
    assert rev.status_code == 200, rev.text
    assert rev.json()["proposal"]["status"] == "approved"
    assert rev.json()["proposal"]["reviewed_by"] == "ceo"
