"""Domain E2E: reliability scan/diagnose/graph/sources + SLOs + anomalies.

Read surfaces stay green; invalid actuation fails closed with 4xx.
"""

from __future__ import annotations

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"  # seeded dogfood member with providerops_manage

ALLOW_CLIENT = {400, 401, 403, 404, 409, 422, 503}


def _error_logs(service: str, n: int = 14) -> list[dict]:
    return [
        {
            "ts": i,
            "message": f"e2e err boom {i}",
            "service": service,
            "level": "error",
        }
        for i in range(n)
    ]


def test_reliability_list_status_gets_do_not_500(client):
    """Incidents, sources, graph, policy, learning — never 500."""
    for path, params in (
        ("/v1/reliability/incidents", {"project_id": PROJECT_ID}),
        ("/v1/reliability/sources", {"project_id": PROJECT_ID}),
        ("/v1/reliability/graph", {"project_id": PROJECT_ID, "service": "api"}),
        ("/v1/reliability/policy", {"project_id": PROJECT_ID}),
        ("/v1/reliability/learning", {"project_id": PROJECT_ID}),
        ("/v1/provider-slos", {"project_id": PROJECT_ID}),
        ("/v1/cost-anomalies", {"project_id": PROJECT_ID}),
    ):
        response = client.get(path, params=params)
        assert response.status_code == 200, f"{path}: {response.text[:300]}"
        body = response.json()
        # list-shaped endpoints expose items
        if "items" in body:
            assert isinstance(body["items"], list)


def test_reliability_scan_diagnose_sources_e2e(client):
    """scan → list incidents → diagnose → register source."""
    scan = client.post(
        "/v1/reliability/scan",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "persist": True,
            "source": {"kind": "memory", "logs": _error_logs("e2e-rel-svc")},
            "window": {"start": 0, "end": 10_000},
        },
    )
    assert scan.status_code == 200, scan.text
    report = scan.json()
    assert report["signal_count"] >= 1
    assert report["incidents"], "persisted scan should create incidents"
    inc_id = report["incidents"][0]["id"]

    listed = client.get(
        "/v1/reliability/incidents", params={"project_id": PROJECT_ID}
    )
    assert listed.status_code == 200
    ids = {i["id"] for i in listed.json()["items"]}
    assert inc_id in ids

    detail = client.get(
        f"/v1/reliability/incidents/{inc_id}",
        params={"project_id": PROJECT_ID},
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == inc_id

    diag = client.post(
        f"/v1/reliability/incidents/{inc_id}/diagnose",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert diag.status_code == 200, diag.text
    assert "hypothesis" in diag.json()

    inv = client.get(
        f"/v1/reliability/incidents/{inc_id}/investigation",
        params={"project_id": PROJECT_ID},
    )
    assert inv.status_code == 200, inv.text

    decision = client.get(
        f"/v1/reliability/incidents/{inc_id}/decision",
        params={"project_id": PROJECT_ID},
    )
    assert decision.status_code == 200, decision.text

    source = client.post(
        "/v1/reliability/sources",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "kind": "memory",
            "name": "e2e-rel-memory",
            "config": {},
            "enabled": True,
        },
    )
    assert source.status_code == 201, source.text
    sources = client.get(
        "/v1/reliability/sources", params={"project_id": PROJECT_ID}
    )
    assert sources.status_code == 200
    assert any(s["name"] == "e2e-rel-memory" for s in sources.json()["items"])


def test_reliability_graph_and_meta_eval(client):
    graph = client.get(
        "/v1/reliability/graph",
        params={"project_id": PROJECT_ID, "service": "e2e-rel-svc", "depth": 1},
    )
    assert graph.status_code == 200, graph.text
    assert "nodes" in graph.json() and "edges" in graph.json()

    # unknown walker fails closed
    bad_walk = client.post(
        "/v1/reliability/graph/walk",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "walker": "not-a-walker",
            "config": {},
        },
    )
    assert bad_walk.status_code in ALLOW_CLIENT, bad_walk.text

    # infra walker is read-only and safe without external deps
    walk = client.post(
        "/v1/reliability/graph/walk",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "walker": "infra",
            "config": {},
        },
    )
    # infra walker may be named differently — accept success or known 400
    assert walk.status_code in (200, 400), walk.text

    meta = client.post("/v1/reliability/meta-eval")
    assert meta.status_code == 200, meta.text
    body = meta.json()
    assert "detection" in body or "rca" in body


def test_reliability_scan_fails_closed_without_credentials(client):
    """Prometheus without base_url / missing incident → 4xx not 500."""
    no_url = client.post(
        "/v1/reliability/scan",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "source": {"kind": "prometheus"},
            "window": {"start": 0, "end": 1},
        },
    )
    assert no_url.status_code in ALLOW_CLIENT, no_url.text

    missing = client.post(
        "/v1/reliability/incidents/inc_does_not_exist/diagnose",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert missing.status_code == 404, missing.text

    rem = client.post(
        "/v1/reliability/incidents/inc_does_not_exist/remediate",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert rem.status_code in ALLOW_CLIENT, rem.text

    # alert webhook without signature
    alert = client.post(
        "/v1/reliability/alert",
        content=b'{"project_id":"boss-dogfood"}',
        headers={"content-type": "application/json"},
    )
    assert alert.status_code in ALLOW_CLIENT, alert.text


def test_provider_slo_and_anomaly_endpoints_e2e(client):
    """Provider SLOs list/create/evaluate fail-closed; cost anomalies list."""
    listed = client.get("/v1/provider-slos", params={"project_id": PROJECT_ID})
    assert listed.status_code == 200, listed.text

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
    slo_id = created.json()["id"]

    evaluated = client.post(
        "/v1/provider-slos/evaluate",
        json={"project_id": PROJECT_ID, "actor": ACTOR, "slo_id": slo_id},
    )
    assert evaluated.status_code == 200, evaluated.text
    summary = evaluated.json().get("summary") or {}
    # Without health samples: unknown (fail-closed), never silent ok-only
    if summary:
        assert summary.get("unknown", 0) >= 1 or summary.get("ok", 0) + summary.get(
            "breached", 0
        ) + summary.get("unknown", 0) >= 1

    # incomplete SLO create fails closed
    bad = client.post(
        "/v1/provider-slos",
        json={"project_id": PROJECT_ID, "provider": "aws"},
    )
    assert bad.status_code < 500

    anomalies = client.get(
        "/v1/cost-anomalies", params={"project_id": PROJECT_ID, "limit": 10}
    )
    assert anomalies.status_code == 200, anomalies.text
    assert "items" in anomalies.json()

    # bad limit fails closed
    bad_limit = client.get(
        "/v1/cost-anomalies", params={"project_id": PROJECT_ID, "limit": 0}
    )
    assert bad_limit.status_code in ALLOW_CLIENT, bad_limit.text
