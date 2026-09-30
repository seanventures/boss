"""HTTP E2E for the stacked AI SRE surfaces.

Seams (public HTTP / MCP list only — isolated Postgres via TestClient):

  1. GET  /v1/ai-sre/capabilities
  2. POST /v1/ai-sre/overwatch
  3. POST/GET /v1/reliability/incidents/{id}/postmortem
  4. POST /v1/ai-sre/runbooks + /{id}/runs
  5. GET  /v1/ai-sre/noise  + POST /v1/ai-sre/ask
  6. GET  /v1/ai-sre/bench
  7. MCP TOOLS list (stdio catalog; no live vendors)

Not public GA. FEATURES stays partial. Never invent RCA, PromQL, or shipped.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from boss_engine.services.sre_bench import MISSING_CASE_ID, SURGE_CASE_ID

PROJECT_ID = "boss-dogfood"
ACTOR = "executive_console"
ROOT = Path(__file__).resolve().parents[3]


def _errors(service: str, n: int, tag: str = "err"):
    return [
        {"ts": i, "message": f"{tag} boom {i}", "service": service, "level": "error"}
        for i in range(n)
    ]


def _mcp():
    path = ROOT / "scripts" / "reliability_mcp_server.py"
    spec = importlib.util.spec_from_file_location("reliability_mcp_server_e2e", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_http_capabilities_are_honest_and_not_ga(client):
    response = client.get("/v1/ai-sre/capabilities")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["public_ga"] is False
    by_id = {item["id"]: item for item in body["capabilities"]}
    assert "runbooks" in by_id
    assert "sre_bench" in by_id
    assert by_id["runbooks"]["status"] != "shipped"
    assert by_id["sre_bench"]["status"] != "shipped"
    assert all(item["status"] in {"shipped", "partial", "missing"} for item in body["capabilities"])
    assert body["counts"]["shipped"] == 0


def test_http_overwatch_empty_sources_never_remediates(client):
    response = client.post(
        "/v1/ai-sre/overwatch",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "empty"
    assert body["reason"] == "no_registered_sources"
    assert body["observe_only"] is True
    assert body["sources"] == []
    assert body["scanned"] == []
    assert body["remediated"] == []
    assert body.get("auto_applied_decisions") == []
    assert body.get("merged_prs") == []


def test_http_postmortem_missing_404_and_no_invented_rca(client):
    missing = client.post(
        "/v1/reliability/incidents/inc_does_not_exist/postmortem",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert missing.status_code == 404, missing.text
    missing_get = client.get(
        "/v1/reliability/incidents/inc_does_not_exist/postmortem",
        params={"project_id": PROJECT_ID},
    )
    assert missing_get.status_code == 404, missing_get.text

    scan = client.post(
        "/v1/reliability/scan",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "persist": True,
            "source": {"kind": "memory", "logs": _errors("e2e-pm-svc", 12)},
            "window": {"start": 0, "end": 10_000},
        },
    )
    assert scan.status_code == 200, scan.text
    incidents = scan.json().get("incidents") or []
    assert incidents, scan.text
    inc_id = incidents[0]["id"]

    drafted = client.post(
        f"/v1/reliability/incidents/{inc_id}/postmortem",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert drafted.status_code == 200, drafted.text
    payload = drafted.json()
    assert payload["status"] == "draft"
    assert payload.get("winner_hypothesis") is None
    assert payload.get("root_cause") is None
    assert "unknown" not in str(payload.get("root_cause") or "").lower()

    stored = client.get(
        f"/v1/reliability/incidents/{inc_id}/postmortem",
        params={"project_id": PROJECT_ID},
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["status"] == "draft"
    assert stored.json().get("root_cause") is None


def test_http_runbook_page_oncall_held_and_unknown_fail_closed(client):
    listed = client.get("/v1/ai-sre/runbooks", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200, listed.text
    items = listed.json().get("items") or []
    page_book = next(item for item in items if item["name"] == "page_oncall")

    held = client.post(
        f"/v1/ai-sre/runbooks/{page_book['id']}/runs",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "incident_id": "inc_e2e_page",
        },
    )
    assert held.status_code == 200, held.text
    body = held.json()
    assert body["status"] == "held"
    assert body.get("decision_id")
    assert body.get("receipt_id")
    assert body.get("simulated") is True
    assert body.get("public_ga") is False

    created = client.post(
        "/v1/ai-sre/runbooks",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "name": "e2e_rogue_drop",
            "steps": [{"id": "drop", "action": "drop_production"}],
        },
    )
    assert created.status_code == 201, created.text
    failed = client.post(
        f"/v1/ai-sre/runbooks/{created.json()['id']}/runs",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "incident_id": "inc_e2e_drop",
        },
    )
    assert failed.status_code == 200, failed.text
    rogue = failed.json()
    assert rogue["status"] == "failed"
    assert rogue.get("reason") == "unknown_step"
    assert rogue.get("decision_id") in (None, "")
    if rogue.get("receipt_id"):
        receipt = client.get(f"/v1/receipts/{rogue['receipt_id']}")
        if receipt.status_code == 200:
            assert receipt.json().get("status") != "succeeded"


def test_http_noise_empty_zeros_and_ask_unknown(client):
    noise = client.get("/v1/ai-sre/noise", params={"project_id": PROJECT_ID})
    assert noise.status_code == 200, noise.text
    body = noise.json()
    assert body["raw_signal_count"] == 0
    assert body["clustered_count"] == 0
    assert body["suppressed_duplicates"] == 0
    assert body["public_ga"] is False
    assert body["reduction"]["ratio"] in (None, 0, 0.0)
    assert "95" not in str(body.get("reduction"))

    ask = client.post(
        "/v1/ai-sre/ask",
        json={"project_id": PROJECT_ID, "question": "what should I have for lunch"},
    )
    assert ask.status_code == 200, ask.text
    plan = ask.json()
    assert plan["status"] == "unknown"
    assert plan["executed"] is False
    assert plan.get("suggested_query") in (None, "")
    blob = str(plan).lower()
    assert "sum(rate(" not in blob
    assert "promql" not in blob


def test_http_bench_fixtures_only_unknown_low_confidence(client):
    response = client.get("/v1/ai-sre/bench")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["public_ga"] is False
    assert report["deterministic"] is True
    assert report.get("vendor_calls") == 0
    ids = {case["id"] for case in report["cases"]}
    assert ids == {SURGE_CASE_ID, MISSING_CASE_ID}
    missing = next(case for case in report["cases"] if case["id"] == MISSING_CASE_ID)
    winner = missing["winner"]
    assert winner["root_cause"] == "unknown"
    assert float(winner["confidence"]) < 0.3
    assert float(winner["confidence"]) != 0.99


def test_mcp_tools_include_capabilities_verify_and_page():
    names = {tool["name"] for tool in _mcp().TOOLS}
    assert names >= {
        "list_capabilities",
        "verify_incident",
        "page_incident",
    }


def test_features_ai_sre_stays_partial_not_ga():
    text = (ROOT / "docs" / "community" / "FEATURES.md").read_text()
    row = next(line for line in text.splitlines() if "AI SRE kernel" in line)
    assert "◐" in row
    assert "✅" not in row
    assert "Preview" in row
