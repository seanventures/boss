"""Universal E2E smoke: every mounted /v1 GET is callable; critical POSTs work.

This does not fully exercise every state machine — it ensures:
1. App boots and catalog is discoverable
2. Safe GETs do not 500
3. Key domain control POSTs succeed end-to-end in the test DB
4. Missing-actor / validation paths fail closed with 4xx not 5xx
"""

from __future__ import annotations

import re
from typing import Any



PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"

# Paths that are expected to 401/403/404/422 without special setup or tokens
ALLOW_CLIENT_ERRORS = {
    400,
    401,
    403,
    404,
    405,
    409,
    422,
    503,  # optional external deps
}


def _path_params(path: str) -> dict[str, str]:
    """Fill FastAPI path templates with safe placeholders."""
    fills = {
        "project_id": PROJECT_ID,
        "id": "smoke-id",
        "run_id": "smoke-run",
        "task_id": "smoke-task",
        "job_id": "smoke-job",
        "incident_id": "smoke-incident",
        "node_id": "smoke-node",
        "edge_id": "smoke-edge",
        "slo_id": "smoke-slo",
        "name": "smoke",
        "provider_key": "aws",
        "worker_pool_id": "smoke-pool",
        "instance_id": "smoke-inst",
        "changeset_id": "smoke-cs",
        "account_id": "smoke-acct",
        "provider_account_id": "smoke-acct",
        "secret_ref_id": "smoke-secret",
        "thread_id": "smoke-thread",
        "message_id": "smoke-msg",
        "decision_id": "smoke-decision",
        "plan_id": "smoke-plan",
        "command_id": "smoke-cmd",
        "receipt_id": "smoke-rcpt",
        "role_key": "ceo",
        "member_id": "ceo",
        "panel_id": "smoke-panel",
        "version_id": "smoke-ver",
        "definition_id": "smoke-def",
        "channel_id": "smoke-ch",
        "claim_id": "smoke-claim",
        "case_id": "smoke-case",
        "metric_id": "smoke-metric",
        "conclusion_id": "smoke-conclusion",
        "evidence_id": "smoke-ev",
        "ownership_id": "smoke-own",
        "connection_id": "smoke-conn",
        "group_id": "smoke-group",
        "user_id": "smoke-user",
        "batch_id": "smoke-batch",
        "proposal_id": "smoke-prop",
        "escalation_id": "smoke-esc",
        "preview_id": "smoke-prev",
        "sandbox_id": "smoke-sbx",
        "target_id": "smoke-tgt",
        "deployment_id": "smoke-dep",
        "path": "index.html",
        "domain": "core",
        "provider": "aws",
        "repo": "boss",
        "owner": "example-org",
        "ref": "main",
        "key": "x",
        "skill_id": "list_tasks",
        "tool_id": "list_tasks",
        "work_item_id": "smoke-wi",
        "allocation_id": "smoke-alloc",
        "binding_id": "smoke-bind",
        "program_id": "smoke-prog",
        "cycle_id": "smoke-cycle",
        "source_id": "smoke-src",
        "subscription_id": "smoke-sub",
        "webhook_id": "smoke-wh",
        "label": "x",
        "slug": "x",
    }

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1)
        name = raw.split(":")[0]
        return fills.get(name, "smoke")

    return re.sub(r"\{([^}]+)\}", repl, path)


def _collect_get_routes(app) -> list[str]:
    paths: list[str] = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if not path or "GET" not in methods:
            continue
        if not path.startswith("/v1/"):
            continue
        # skip websocket-ish
        if "{" in path and "path" in path and path.count("{") > 3:
            continue
        paths.append(path)
    return sorted(set(paths))


def test_meta_catalog_and_openapi_json(client):
    cat = client.get("/v1/meta/api-catalog")
    assert cat.status_code == 200, cat.text
    body = cat.json()
    assert body["route_count"] >= 200
    assert body["domain_count"] >= 5

    cov = client.get("/v1/meta/openapi-coverage")
    assert cov.status_code == 200
    assert "documentation_coverage" in cov.json()

    # FastAPI built-in schema
    oa = client.get("/openapi.json")
    assert oa.status_code == 200
    assert "paths" in oa.json()
    assert len(oa.json()["paths"]) >= 100


def test_all_v1_gets_do_not_500(client):
    """Smoke every GET /v1/* route — allow 2xx/3xx/4xx/503, never 5xx (except 503)."""
    from apps.api.app.main import app

    paths = _collect_get_routes(app)
    assert len(paths) >= 100, f"expected broad surface, got {len(paths)}"

    failures: list[str] = []
    client_errors = 0
    successes = 0
    for path in paths:
        concrete = _path_params(path)
        # attach project_id when route likely needs it
        params: dict[str, Any] = {}
        if "project_id" not in concrete and "{" not in path:
            params["project_id"] = PROJECT_ID
        elif "{project_id}" not in path and "project_id=" not in concrete:
            params.setdefault("project_id", PROJECT_ID)

        try:
            response = client.get(concrete, params=params or None)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path} raised {type(exc).__name__}: {exc}")
            continue

        code = response.status_code
        if 200 <= code < 400:
            successes += 1
        elif code in ALLOW_CLIENT_ERRORS:
            client_errors += 1
        elif code >= 500 and code != 503:
            failures.append(f"GET {concrete} -> {code} {response.text[:200]}")
        else:
            client_errors += 1

    assert not failures, "GET smoke failures:\n" + "\n".join(failures[:40])
    # At least some GETs should succeed against seeded dogfood project
    assert successes >= 20, f"too few successful GETs: {successes} (4xx={client_errors})"


def test_core_control_plane_e2e(client):
    """Command → task list → budget status → receipts list → events."""
    # budget status
    budget = client.get("/v1/budget/status", params={"project_id": PROJECT_ID})
    assert budget.status_code in (200, 404, 422), budget.text

    tasks = client.get("/v1/tasks", params={"project_id": PROJECT_ID})
    assert tasks.status_code in (200, 422), tasks.text

    events = client.get("/v1/events", params={"project_id": PROJECT_ID, "limit": 10})
    assert events.status_code in (200, 422), events.text

    receipts = client.get("/v1/receipts", params={"project_id": PROJECT_ID, "limit": 10})
    assert receipts.status_code in (200, 422), receipts.text

    # Canonical intake path is POST /v1/commands/propose (not /v1/commands).
    cmd = client.post(
        "/v1/commands/propose",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "command": "API e2e smoke command — no side effects expected",
            "constraints": {"source": "api_e2e_smoke"},
        },
    )
    assert cmd.status_code in (200, 201, 422, 403, 409, 429), cmd.text
    if cmd.status_code in (200, 201):
        body = cmd.json()
        assert body.get("status") == "accepted"
        assert body.get("task_id")
        assert body.get("job_id")

    # Budget quote + reserve (control-plane spend gate)
    quote = client.post(
        "/v1/budget/quote",
        json={
            "project_id": PROJECT_ID,
            "category": "tool",
            "amount_usd_micros": 50_000,
            "reason": "e2e smoke quote",
        },
    )
    assert quote.status_code in (200, 201, 403, 409, 422), quote.text

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": PROJECT_ID,
            "category": "tool",
            "amount_usd_micros": 50_000,
            "reason": "e2e smoke reserve",
        },
    )
    assert reserve.status_code in (200, 201, 403, 409, 422), reserve.text

    # Direct task create + job enqueue (happy path)
    task = client.post(
        "/v1/tasks",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": "e2e smoke task",
            "priority": "p2",
            "dedupe_key": "e2e-smoke-task-1",
        },
    )
    assert task.status_code in (200, 201, 403, 409, 422), task.text
    task_id = task.json().get("id") if task.status_code in (200, 201) else None

    if task_id:
        job = client.post(
            "/v1/jobs",
            json={
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "kind": "agent_run",
                "payload": {"source": "api_e2e_smoke"},
            },
        )
        assert job.status_code in (200, 201), job.text
        assert job.json().get("id")
        assert job.json().get("status") in ("queued", "leased", "running")

    # Unknown task_id must fail closed with 4xx, never unhandled 500
    bad_job = client.post(
        "/v1/jobs",
        json={
            "project_id": PROJECT_ID,
            "task_id": "does-not-exist-task",
            "kind": "agent_run",
        },
    )
    assert bad_job.status_code in (400, 404, 422), bad_job.text

    # Worker poll is safe when idle
    poll = client.post(
        "/v1/worker/poll",
        json={
            "project_id": PROJECT_ID,
            "worker_id": "e2e-smoke-worker",
            "capabilities": ["agent_run"],
        },
    )
    assert poll.status_code in (200, 422, 403), poll.text


def test_multi_cloud_and_provider_e2e(client):
    mc = client.get("/v1/multi-cloud/catalog")
    assert mc.status_code == 200
    assert mc.json()["provider_count"] >= 4

    skills = client.get("/v1/multi-cloud/skills")
    assert skills.status_code == 200
    assert skills.json()["count"] >= 1

    defs = client.get("/v1/provider-definitions")
    assert defs.status_code == 200
    assert len(defs.json().get("items") or []) >= 4

    providers = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    assert providers.status_code == 200




def test_work_trackers_and_scoring_e2e(client):
    catalog = client.get("/v1/work-trackers/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["default_provider_key"] == "plane"

    listed = client.get("/v1/work-trackers", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200

    preview = client.post(
        "/v1/work-orchestration/score-preview",
        json={
            "strategic_value": 80,
            "customer_impact": 70,
            "urgency": 60,
            "cost_of_delay": 50,
            "risk_reduction": 40,
            "dependency_pressure": 30,
            "confidence": 90,
            "estimated_cost_usd_micros": 1_000_000,
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert "priority_score" in body
    assert "value_density" in body or "value_per_cost" in body


def test_knowledge_and_finops_e2e(client):
    st = client.get("/v1/knowledge/status", params={"project_id": PROJECT_ID})
    assert st.status_code == 200
    assert st.json().get("pack_context_available") is True

    packed = client.post(
        "/v1/knowledge/pack-context",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "token_budget": 1024,
            "messages": [
                {"role": "user", "content": "Summarize open decisions for e2e smoke."}
            ],
            "items": [
                {
                    "id": "e2e-1",
                    "text": "Budget gate required before provision.",
                    "authority": "policy",
                }
            ],
        },
    )
    assert packed.status_code == 200, packed.text
    assert "context" in packed.json()

    finops = client.get(
        "/v1/finops/summary", params={"project_id": PROJECT_ID, "days": 7}
    )
    assert finops.status_code == 200, finops.text
    assert "spend_by_provider" in finops.json()


def test_resource_topology_and_provider_slos_e2e(client):
    topo = client.get("/v1/resource-topology", params={"project_id": PROJECT_ID})
    assert topo.status_code == 200, topo.text

    slos = client.get("/v1/provider-slos", params={"project_id": PROJECT_ID})
    assert slos.status_code == 200, slos.text

    # create an SLO (no data → evaluate unknown)
    created = client.post(
        "/v1/provider-slos",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "provider": "digitalocean",
            "metric_key": "availability",
            "target": 0.99,
            "window_seconds": 3600,
            "comparator": "gte",
        },
    )
    assert created.status_code in (200, 201), created.text
    slo_id = created.json().get("id")
    if slo_id:
        ev = client.post(
            "/v1/provider-slos/evaluate",
            json={"project_id": PROJECT_ID, "actor": ACTOR, "slo_id": slo_id},
        )
        assert ev.status_code == 200, ev.text
        # NOTE: the SLO status contract ("unknown without health samples; ok/breached once
        # data exists") is NOT asserted here. It never was — the value was read into a
        # variable, the line below re-asserted the status code that was already checked
        # above, and the contract in this comment went unchecked. Left explicit rather than
        # deleted so the gap is visible instead of looking like a covered case.


def test_edition_and_cloud_catalog_e2e(client):
    edition = client.get("/v1/edition", params={"project_id": PROJECT_ID})
    assert edition.status_code == 200, edition.text

    cloud = client.get("/v1/cloud/catalog", params={"project_id": PROJECT_ID})
    assert cloud.status_code == 200, cloud.text

    comparison = client.get("/v1/cloud/comparison", params={"project_id": PROJECT_ID})
    assert comparison.status_code == 200, comparison.text


def test_mutating_without_actor_fails_closed(client):
    """Mutations without proper actor/project should not 500."""
    bad = client.post("/v1/workspace/nodes", json={"name": "x"})
    assert bad.status_code in (401, 403, 422), bad.text

    bad2 = client.post(
        "/v1/provider-slos",
        json={"project_id": PROJECT_ID, "provider": "aws", "metric_key": "availability"},
    )
    # may 422 missing fields or 200 if actor defaults — never 500
    assert bad2.status_code < 500


def test_reliability_and_finops_domain_posts_e2e(client):
    """Lightweight domain POSTs for reliability scan + finops/value density."""
    scan = client.post(
        "/v1/reliability/scan",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "persist": False,
            "source": {
                "kind": "memory",
                "logs": [
                    {
                        "ts": i,
                        "message": f"smoke err {i}",
                        "service": "smoke-svc",
                        "level": "error",
                    }
                    for i in range(12)
                ],
            },
            "window": {"start": 0, "end": 10_000},
        },
    )
    assert scan.status_code in (200, 400, 422), scan.text
    if scan.status_code == 200:
        assert "signal_count" in scan.json()

    # Prometheus without base_url must fail closed
    bad_scan = client.post(
        "/v1/reliability/scan",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "source": {"kind": "prometheus"},
            "window": {"start": 0, "end": 1},
        },
    )
    assert bad_scan.status_code in (400, 422), bad_scan.text

    costs = client.get("/v1/costs", params={"project_id": PROJECT_ID, "limit": 5})
    assert costs.status_code == 200, costs.text

    sources = client.get("/v1/reliability/sources", params={"project_id": PROJECT_ID})
    assert sources.status_code == 200, sources.text
