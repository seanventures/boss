"""BOSS-241 depth — provider SLO definitions + evaluation.

Covers:
* upsert + list
* evaluation from mocked provider_health samples
* evaluation from cost_events
* fail-closed: no data ⇒ status=unknown (never silent ok)
* HTTP surface
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.core import make_id
from boss_engine.services.provider_slo import ProviderSloService

PID = "boss-dogfood"
PROVIDER = "digitalocean"


def _insert_health(
    connection,
    *,
    provider: str = PROVIDER,
    status: str = "healthy",
    api_available: bool = True,
    checked_at: datetime | None = None,
    account_id: str | None = None,
) -> str:
    health_id = make_id("ph")
    connection.execute(
        text(
            """
            INSERT INTO provider_health (
                id, project_id, provider_account_id, provider, status,
                api_available, last_checked_at, detail
            )
            VALUES (
                :id, :project_id, :account_id, :provider, :status,
                :api_available, :checked_at, '{}'::jsonb
            )
            """
        ),
        {
            "id": health_id,
            "project_id": PID,
            "account_id": account_id,
            "provider": provider,
            "status": status,
            "api_available": api_available,
            "checked_at": checked_at or datetime.now(timezone.utc),
        },
    )
    return health_id


def _insert_cost(
    connection,
    *,
    amount: int,
    provider: str = PROVIDER,
    created_at: datetime | None = None,
) -> str:
    event_id = make_id("ce")
    connection.execute(
        text(
            """
            INSERT INTO cost_events (
                id, project_id, provider, category, amount_usd_micros,
                confidence, created_at
            )
            VALUES (
                :id, :project_id, :provider, 'compute', :amount,
                'observed', :created_at
            )
            """
        ),
        {
            "id": event_id,
            "project_id": PID,
            "provider": provider,
            "amount": amount,
            "created_at": created_at or datetime.now(timezone.utc),
        },
    )
    return event_id


def test_upsert_and_list_slos(engine):
    svc = ProviderSloService()
    with begin(engine) as c:
        created = svc.upsert_slo(
            c,
            project_id=PID,
            provider=PROVIDER,
            metric_key="availability",
            target=0.99,
            window_seconds=3600,
            comparator="gte",
            actor="test",
        )
        assert created["id"].startswith("pslo_")
        assert created["status"] == "unknown"  # never evaluated yet
        assert created["last_value"] is None

        # Idempotent upsert by natural key
        again = svc.upsert_slo(
            c,
            project_id=PID,
            provider=PROVIDER,
            metric_key="availability",
            target=0.95,
            window_seconds=3600,
            comparator="gte",
            actor="test",
        )
        assert again["id"] == created["id"]
        assert again["target"] == 0.95

        items = svc.list_slos(c, PID)
        assert any(i["id"] == created["id"] for i in items)


def test_evaluate_availability_ok_and_breached(engine):
    svc = ProviderSloService()
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        # 4 healthy + 0 failed → availability 1.0
        for _ in range(4):
            _insert_health(c, api_available=True, status="healthy", checked_at=now)

        slo = svc.upsert_slo(
            c,
            project_id=PID,
            provider=PROVIDER,
            metric_key="availability",
            target=0.99,
            window_seconds=3600,
            comparator="gte",
        )
        ok_eval = svc.evaluate_slo(c, slo["id"], project_id=PID, now=now)
        assert ok_eval["status"] == "ok"
        assert ok_eval["last_value"] == 1.0
        assert ok_eval["metadata"]["fail_closed_no_data"] is False

        # Add 4 unavailable samples → availability 0.5 < 0.99
        for _ in range(4):
            _insert_health(c, api_available=False, status="unavailable", checked_at=now)
        breached = svc.evaluate_slo(c, slo["id"], project_id=PID, now=now)
        assert breached["status"] == "breached"
        assert breached["last_value"] == 0.5

        # Cached status on definition
        listed = svc.list_slos(c, PID)
        match = next(i for i in listed if i["id"] == slo["id"])
        assert match["status"] == "breached"
        assert match["last_value"] == 0.5


def test_evaluate_fail_closed_unknown_when_no_health_data(engine):
    """No silent success: empty window → status=unknown, not ok."""
    svc = ProviderSloService()
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        # Health outside the window must not count
        _insert_health(
            c,
            api_available=True,
            checked_at=now - timedelta(hours=48),
        )
        slo = svc.upsert_slo(
            c,
            project_id=PID,
            provider=PROVIDER,
            metric_key="availability",
            target=0.0,  # even a trivial target must not pass with no data
            window_seconds=3600,
            comparator="gte",
        )
        evaluation = svc.evaluate_slo(c, slo["id"], project_id=PID, now=now)
        assert evaluation["status"] == "unknown"
        assert evaluation["last_value"] is None
        assert evaluation["metadata"]["fail_closed_no_data"] is True
        assert evaluation["metadata"]["sample_meta"]["reason"] == "no_health_samples_in_window"


def test_evaluate_api_available_latest(engine):
    svc = ProviderSloService()
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        _insert_health(
            c, api_available=True, checked_at=now - timedelta(minutes=10)
        )
        _insert_health(c, api_available=False, status="degraded", checked_at=now)

        slo = svc.upsert_slo(
            c,
            project_id=PID,
            provider=PROVIDER,
            metric_key="api_available",
            target=1.0,
            window_seconds=3600,
            comparator="eq",
        )
        evaluation = svc.evaluate_slo(c, slo["id"], project_id=PID, now=now)
        assert evaluation["status"] == "breached"
        assert evaluation["last_value"] == 0.0


def test_evaluate_cost_usd_micros(engine):
    svc = ProviderSloService()
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        _insert_cost(c, amount=1_000_000, created_at=now)
        _insert_cost(c, amount=500_000, created_at=now)

        slo = svc.upsert_slo(
            c,
            project_id=PID,
            provider=PROVIDER,
            metric_key="cost_usd_micros",
            target=2_000_000,
            window_seconds=3600,
            comparator="lte",
        )
        evaluation = svc.evaluate_slo(c, slo["id"], project_id=PID, now=now)
        assert evaluation["status"] == "ok"
        assert evaluation["last_value"] == 1_500_000.0

        # No cost events → unknown (fail-closed; zero is not silent ok)
        empty = svc.upsert_slo(
            c,
            project_id=PID,
            provider="openai",
            metric_key="cost_usd_micros",
            target=0,
            window_seconds=3600,
            comparator="lte",
        )
        no_data = svc.evaluate_slo(c, empty["id"], project_id=PID, now=now)
        assert no_data["status"] == "unknown"
        assert no_data["last_value"] is None


def test_evaluate_all_for_project(engine):
    svc = ProviderSloService()
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        _insert_health(c, api_available=True, checked_at=now)
        svc.upsert_slo(
            c,
            project_id=PID,
            provider=PROVIDER,
            metric_key="availability",
            target=0.5,
            window_seconds=3600,
            comparator="gte",
        )
        svc.upsert_slo(
            c,
            project_id=PID,
            provider=PROVIDER,
            metric_key="sample_count",
            target=1,
            window_seconds=3600,
            comparator="gte",
        )
        # No data metric → unknown
        svc.upsert_slo(
            c,
            project_id=PID,
            provider="cloudflare",
            metric_key="availability",
            target=0.99,
            window_seconds=3600,
            comparator="gte",
        )
        result = svc.evaluate_all_for_project(c, PID, now=now)
        assert result["count"] >= 3
        assert result["summary"]["ok"] >= 2
        assert result["summary"]["unknown"] >= 1


def test_provider_slos_http(client, engine):
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        _insert_health(c, api_available=True, status="healthy", checked_at=now)

    created = client.post(
        "/v1/provider-slos",
        json={
            "project_id": PID,
            "provider": PROVIDER,
            "metric_key": "availability",
            "target": 0.99,
            "window_seconds": 3600,
            "comparator": "gte",
            "actor": "test",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"].startswith("pslo_")
    assert body["status"] == "unknown"

    listed = client.get("/v1/provider-slos", params={"project_id": PID})
    assert listed.status_code == 200
    assert any(i["id"] == body["id"] for i in listed.json()["items"])

    evaluated = client.post(
        "/v1/provider-slos/evaluate",
        json={"project_id": PID, "slo_id": body["id"], "actor": "test"},
    )
    assert evaluated.status_code == 200, evaluated.text
    payload = evaluated.json()
    assert payload["count"] == 1
    assert payload["evaluations"][0]["status"] == "ok"
    assert payload["summary"]["ok"] == 1

    # Evaluate all
    all_eval = client.post(
        "/v1/provider-slos/evaluate",
        json={"project_id": PID, "actor": "test"},
    )
    assert all_eval.status_code == 200
    assert all_eval.json()["count"] >= 1

    # Fail-closed via HTTP: brand-new provider with no samples
    empty = client.post(
        "/v1/provider-slos",
        json={
            "project_id": PID,
            "provider": "supabase",
            "metric_key": "availability",
            "target": 0.0,
            "window_seconds": 3600,
            "comparator": "gte",
        },
    )
    assert empty.status_code == 201
    empty_eval = client.post(
        "/v1/provider-slos/evaluate",
        json={"project_id": PID, "slo_id": empty.json()["id"]},
    )
    assert empty_eval.status_code == 200
    assert empty_eval.json()["evaluations"][0]["status"] == "unknown"
    assert empty_eval.json()["evaluations"][0]["last_value"] is None


def test_upsert_rejects_invalid_metric(client):
    r = client.post(
        "/v1/provider-slos",
        json={
            "project_id": PID,
            "provider": PROVIDER,
            "metric_key": "not_a_real_metric",
            "target": 1.0,
        },
    )
    assert r.status_code == 422
