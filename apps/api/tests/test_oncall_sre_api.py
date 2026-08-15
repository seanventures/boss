"""On-call / SRE control plane — schedules, policies, paging, KG link, receipts."""
from __future__ import annotations

import json
import os

from apps.api.app.db import begin
from apps.api.app.services.knowledge_graph import KnowledgeGraphService
from apps.api.app.services.oncall_sre import OncallSreService, adapter_credential
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
ACTOR = "executive_console"
SVC = OncallSreService()
REL = ReliabilityService()
KG = KnowledgeGraphService()


def _errors(service: str, n: int = 12, tag: str = "err"):
    return [
        {"ts": i, "message": f"{tag} boom {i}", "service": service, "level": "error"}
        for i in range(n)
    ]


def _mk_schedule(client, name="primary", **extra):
    body = {
        "project_id": PID,
        "actor": ACTOR,
        "name": name,
        "timezone": "UTC",
        "layers": [
            {
                "name": "primary",
                "rotation_type": "weekly",
                "handoff_seconds": 3600,
                "start_at": "2020-01-01T00:00:00+00:00",
                "participants": [
                    {"user_id": "alice", "contact": "alice@example.com"},
                    {"user_id": "bob", "contact": "bob@example.com"},
                ],
            }
        ],
        **extra,
    }
    r = client.post("/v1/oncall-sre/schedules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _mk_policy(client, name="default", schedule_id=None):
    body = {
        "project_id": PID,
        "actor": ACTOR,
        "name": name,
        "schedule_id": schedule_id,
        "steps": [
            {
                "wait_minutes": 0,
                "channels": [{"adapter": "slack_webhook", "target": "default"}],
                "targets": ["alice"],
            },
            {
                "wait_minutes": 15,
                "channels": [{"adapter": "pagerduty", "target": "sev1"}],
                "targets": ["bob"],
            },
        ],
    }
    r = client.post("/v1/oncall-sre/policies", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── service unit ─────────────────────────────────────────────────────────────
def test_adapters_catalog_and_credential_dry_run():
    assert "pagerduty" in __import__(
        "apps.api.app.services.oncall_sre", fromlist=["ADAPTERS"]
    ).ADAPTERS
    # Without tokens, every adapter is dry-run
    for a in ("pagerduty", "opsgenie", "grafana_oncall", "slack_webhook", "mattermost"):
        live, env_name, token = adapter_credential(a)
        # May be live if env is set in the host; still a valid tuple
        assert isinstance(live, bool) and env_name and isinstance(token, str)


def test_service_schedule_policy_page_ack_resolve(engine):
    with begin(engine) as c:
        sched = SVC.create_schedule(
            c, PID, "svc-sched",
            layers=[{
                "name": "L1",
                "handoff_seconds": 86400,
                "start_at": "2020-01-01T00:00:00+00:00",
                "participants": [{"user_id": "carol"}],
            }],
        )
        assert sched["id"].startswith("ocs_")
        pol = SVC.create_policy(
            c, PID, "svc-pol", schedule_id=sched["id"],
            steps=[{"wait_minutes": 0, "adapter": "mattermost"}],
        )
        assert pol["id"].startswith("ocp_")
        result = SVC.page(
            c, PID,
            title="checkout latency",
            severity="critical",
            service="checkout",
            schedule_id=sched["id"],
            policy_id=pol["id"],
            adapter="slack_webhook",
            force_live=False,
            actor=ACTOR,
        )
        page = result["page"]
        assert page["status"] == "open"
        assert result["simulated"] is True
        assert result["notify"]["receipt_id"]
        assert page["assignee"] == "carol"

        # Dedupe reuses
        again = SVC.page(
            c, PID,
            title="checkout latency",
            severity="critical",
            service="checkout",
            dedupe_key=page["dedupe_key"],
            actor=ACTOR,
        )
        assert again["reused"] is True
        assert again["page"]["id"] == page["id"]

        acked = SVC.ack(c, PID, page["id"], actor=ACTOR)
        assert acked["page"]["status"] == "acked"
        resolved = SVC.resolve(c, PID, page["id"], actor=ACTOR)
        assert resolved["page"]["status"] == "resolved"

        # KG incident node exists
        node = KG.get_node(c, PID, "incident", page["id"])
        assert node is not None


def test_force_live_without_token_fails_closed(engine):
    # Ensure no slack webhook env for this assertion
    saved = {
        k: os.environ.pop(k, None)
        for k in ("ONCALL_SLACK_WEBHOOK", "RELIABILITY_ONCALL_WEBHOOK")
    }
    try:
        with begin(engine) as c:
            try:
                SVC.page(
                    c, PID,
                    title="force-live-test",
                    adapter="slack_webhook",
                    force_live=True,
                    dedupe_key=f"force-live-{os.getpid()}",
                    actor=ACTOR,
                )
                assert False, "expected OncallSreError"
            except Exception as exc:
                from apps.api.app.services.oncall_sre import OncallSreError
                assert isinstance(exc, OncallSreError)
                assert exc.status_code == 422
                assert exc.payload.get("simulated") is True
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ── HTTP ─────────────────────────────────────────────────────────────────────
def test_http_schedules_and_policies(client):
    sched = _mk_schedule(client, name="http-sched")
    assert sched["name"] == "http-sched"
    assert len(sched["layers"]) == 1

    lst = client.get("/v1/oncall-sre/schedules", params={"project_id": PID})
    assert lst.status_code == 200
    assert any(s["id"] == sched["id"] for s in lst.json()["items"])

    got = client.get(
        f"/v1/oncall-sre/schedules/{sched['id']}", params={"project_id": PID}
    )
    assert got.status_code == 200 and got.json()["id"] == sched["id"]

    pol = _mk_policy(client, name="http-pol", schedule_id=sched["id"])
    assert len(pol["steps"]) == 2
    plst = client.get("/v1/oncall-sre/policies", params={"project_id": PID})
    assert plst.status_code == 200
    assert any(p["id"] == pol["id"] for p in plst.json()["items"])


def test_http_page_ack_resolve_status(client):
    sched = _mk_schedule(client, name="page-sched")
    pol = _mk_policy(client, name="page-pol", schedule_id=sched["id"])

    opened = client.post(
        "/v1/oncall-sre/pages",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "title": "API 5xx spike",
            "severity": "critical",
            "service": "api-gateway",
            "schedule_id": sched["id"],
            "policy_id": pol["id"],
            "adapter": "slack_webhook",
        },
    )
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["simulated"] is True
    page_id = body["page"]["id"]
    assert body["notify"]["receipt_id"]
    assert body["page"]["assignee"] in ("alice", "bob")

    pages = client.get("/v1/oncall-sre/pages", params={"project_id": PID, "status": "open"})
    assert pages.status_code == 200
    assert any(p["id"] == page_id for p in pages.json()["items"])

    ack = client.post(
        f"/v1/oncall-sre/incidents/{page_id}/ack",
        json={"project_id": PID, "actor": ACTOR, "note": "looking"},
    )
    assert ack.status_code == 200 and ack.json()["page"]["status"] == "acked"

    status = client.get("/v1/oncall-sre/status", params={"project_id": PID})
    assert status.status_code == 200
    st = status.json()
    assert "current_oncall" in st and "adapters" in st
    assert st["acked_pages"] >= 1

    res = client.post(
        f"/v1/oncall-sre/incidents/{page_id}/resolve",
        json={"project_id": PID, "actor": ACTOR},
    )
    assert res.status_code == 200 and res.json()["page"]["status"] == "resolved"


def test_http_page_reliability_and_from_reliability(client, engine):
    # Seed a reliability incident via scan
    with begin(engine) as c:
        rep = REL.scan(
            c, PID,
            {"kind": "memory", "logs": _errors("billing-oncall", 14, tag="dbtimeout")},
            {"start": 0, "end": 10_000},
            persist=True,
        )
        assert rep["incidents"]
        inc_id = rep["incidents"][0]["id"]

    paged = client.post(
        f"/v1/oncall-sre/incidents/{inc_id}/page",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "adapter": "opsgenie",
        },
    )
    assert paged.status_code == 200, paged.text
    assert paged.json()["page"]["reliability_incident_id"] == inc_id
    assert paged.json()["simulated"] is True

    # from-reliability opens pages for open incidents (dedupe keeps one)
    fr = client.post(
        "/v1/oncall-sre/from-reliability",
        json={"project_id": PID, "actor": ACTOR, "limit": 10, "adapter": "pagerduty"},
    )
    assert fr.status_code == 200, fr.text
    data = fr.json()
    assert data["incidents_considered"] >= 1
    assert data["pages"]


def test_http_reliability_page_simulated_receipt_and_kg_writeback(client, engine):
    """Cross-plane hole: reliability incident → page must persist a simulated
    notify receipt and write the incident+service nodes onto the knowledge graph.

    Does **not** claim live PagerDuty/Opsgenie delivery.
    """
    saved = {
        k: os.environ.pop(k, None)
        for k in (
            "PAGERDUTY_TOKEN",
            "OPSGENIE_API_KEY",
            "GRAFANA_ONCALL_WEBHOOK",
            "ONCALL_SLACK_WEBHOOK",
            "ONCALL_MATTERMOST_WEBHOOK",
            "RELIABILITY_ONCALL_WEBHOOK",
        )
    }
    service = "billing-kg-e2e"
    try:
        with begin(engine) as c:
            rep = REL.scan(
                c, PID,
                {"kind": "memory", "logs": _errors(service, 14, tag="poolwait")},
                {"start": 0, "end": 10_000},
                persist=True,
            )
            assert rep["incidents"]
            inc_id = rep["incidents"][0]["id"]

        paged = client.post(
            f"/v1/oncall-sre/incidents/{inc_id}/page",
            json={
                "project_id": PID,
                "actor": ACTOR,
                "adapter": "pagerduty",
                "service": service,
            },
        )
        assert paged.status_code == 200, paged.text
        body = paged.json()
        assert body["simulated"] is True
        notify = body["notify"]
        assert notify["posted"] is False
        assert notify["simulated"] is True
        assert notify["adapter"] == "pagerduty"
        receipt_id = notify["receipt_id"]
        assert receipt_id
        page_id = body["page"]["id"]
        assert body["page"]["reliability_incident_id"] == inc_id
        assert body["page"]["service"] == service

        receipt = client.get(f"/v1/receipts/{receipt_id}")
        assert receipt.status_code == 200, receipt.text
        rec = receipt.json()
        assert rec["id"] == receipt_id
        assert rec["effect_kind"] == "oncall_sre.notify"
        assert rec["status"] == "succeeded"
        assert str(rec.get("provider_ref") or "").startswith("simulated:")
        result = rec.get("result") or {}
        if isinstance(result, str):
            result = json.loads(result)
        assert result.get("posted") is False
        assert result.get("simulated") is True

        inc_nodes = client.get(
            "/v1/knowledge/graph/nodes",
            params={"project_id": PID, "kind": "incident", "node_key": page_id},
        )
        assert inc_nodes.status_code == 200, inc_nodes.text
        assert inc_nodes.json()["items"]
        assert inc_nodes.json()["items"][0]["node_key"] == page_id

        svc_nodes = client.get(
            "/v1/knowledge/graph/nodes",
            params={"project_id": PID, "kind": "service", "node_key": service},
        )
        assert svc_nodes.status_code == 200, svc_nodes.text
        assert svc_nodes.json()["items"]
        assert svc_nodes.json()["items"][0]["node_key"] == service

        neighbors = client.get(
            "/v1/knowledge/graph/neighbors",
            params={"project_id": PID, "kind": "incident", "node_key": page_id},
        )
        assert neighbors.status_code == 200, neighbors.text
        assert any(
            n["node_key"] == service and n.get("edge_kind") == "correlates_with"
            for n in neighbors.json()["items"]
        )

        sub = client.get(
            "/v1/knowledge/graph/subgraph",
            params={
                "project_id": PID,
                "kind": "incident",
                "node_key": page_id,
                "depth": 1,
            },
        )
        assert sub.status_code == 200, sub.text
        sub_body = sub.json()
        assert any(n["node_key"] == service for n in sub_body["nodes"])
        assert any(e["kind"] == "correlates_with" for e in sub_body["edges"])
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_http_reliability_pagerduty_force_live_422(client, engine):
    """PagerDuty page from a reliability incident with no token + force_live → 422.

    Separate incident from the simulated-200 path so dedupe cannot skip notify.
    """
    saved = {
        k: os.environ.pop(k, None)
        for k in (
            "PAGERDUTY_TOKEN",
            "OPSGENIE_API_KEY",
            "GRAFANA_ONCALL_WEBHOOK",
            "ONCALL_SLACK_WEBHOOK",
            "ONCALL_MATTERMOST_WEBHOOK",
            "RELIABILITY_ONCALL_WEBHOOK",
        )
    }
    try:
        with begin(engine) as c:
            rep = REL.scan(
                c, PID,
                {"kind": "memory", "logs": _errors("billing-force-live", 14, tag="pd422")},
                {"start": 0, "end": 10_000},
                persist=True,
            )
            assert rep["incidents"]
            inc_id = rep["incidents"][0]["id"]

        r = client.post(
            f"/v1/oncall-sre/incidents/{inc_id}/page",
            json={
                "project_id": PID,
                "actor": ACTOR,
                "adapter": "pagerduty",
                "service": "billing-force-live",
                "force_live": True,
            },
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        if isinstance(detail, dict):
            assert detail.get("posted") is False
            assert "credentials" in str(detail).lower() or detail.get("reason") == "credentials_missing"
            assert detail.get("adapter") == "pagerduty"
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_http_graph_link(client, engine):
    opened = client.post(
        "/v1/oncall-sre/pages",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "title": "KG link page",
            "service": "payments",
            "severity": "warning",
        },
    )
    assert opened.status_code == 200, opened.text
    page_id = opened.json()["page"]["id"]

    linked = client.post(
        "/v1/oncall-sre/graph/link",
        json={
            "project_id": PID,
            "actor": ACTOR,
            "page_id": page_id,
            "node_kind": "service",
            "node_key": "payments",
            "edge_kind": "correlates_with",
        },
    )
    assert linked.status_code == 200, linked.text
    body = linked.json()
    assert body["page_id"] == page_id
    assert body["edge_id"]
    assert body["kg_node_key"] == "service:payments"
    assert body["edge_kind"] == "correlates_with"

    sub = client.get(
        "/v1/knowledge/graph/subgraph",
        params={
            "project_id": PID,
            "kind": "incident",
            "node_key": page_id,
            "depth": 1,
        },
    )
    assert sub.status_code == 200, sub.text
    keys = {n["node_key"] for n in sub.json()["nodes"]}
    assert "payments" in keys
    assert any(e["kind"] == "correlates_with" for e in sub.json()["edges"])

    with begin(engine) as c:
        node = KG.get_node(c, PID, "service", "payments")
        assert node is not None
        page_node = KG.get_node(c, PID, "incident", page_id)
        assert page_node is not None


def test_http_force_live_422(client):
    saved = {
        k: os.environ.pop(k, None)
        for k in (
            "ONCALL_SLACK_WEBHOOK",
            "RELIABILITY_ONCALL_WEBHOOK",
            "PAGERDUTY_TOKEN",
            "OPSGENIE_API_KEY",
            "GRAFANA_ONCALL_WEBHOOK",
            "ONCALL_MATTERMOST_WEBHOOK",
        )
    }
    try:
        r = client.post(
            "/v1/oncall-sre/pages",
            json={
                "project_id": PID,
                "actor": ACTOR,
                "title": "must fail closed",
                "adapter": "slack_webhook",
                "force_live": True,
                "dedupe_key": f"http-force-live-{os.getpid()}",
            },
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        if isinstance(detail, dict):
            assert detail.get("simulated") is True or "credentials" in str(detail).lower()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_http_adapters_and_missing_schedule_404(client):
    adapters = client.get("/v1/oncall-sre/adapters")
    assert adapters.status_code == 200
    names = {a["adapter"] for a in adapters.json()["items"]}
    assert names >= {"pagerduty", "opsgenie", "grafana_oncall", "slack_webhook", "mattermost"}

    missing = client.get(
        "/v1/oncall-sre/schedules/ocs_does_not_exist",
        params={"project_id": PID},
    )
    assert missing.status_code == 404

    bad_page = client.post(
        "/v1/oncall-sre/incidents/ocpg_missing/ack",
        json={"project_id": PID, "actor": ACTOR},
    )
    assert bad_page.status_code == 404
