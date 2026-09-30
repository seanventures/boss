"""BOSS-244 — GET /v1/finops/summary multi-dimension chart payload.

Proves spend_by_provider / spend_by_day / anomalies_open_count /
commitment_utilization / top_resources aggregate correctly from
cost_events, cost_anomalies, commitments, and external_resources.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.core import make_id
from boss_engine.services.finops_reconciler import FinopsReconcilerService

PID = "boss-dogfood"


def _settings():
    from apps.api.app.config import get_settings

    return get_settings()


def _insert_cost_event(
    connection,
    *,
    provider: str,
    amount: int,
    created_at: datetime | None = None,
    external_resource_id: str | None = None,
    category: str = "compute",
):
    ce_id = make_id("ce")
    connection.execute(
        text(
            """
            INSERT INTO cost_events
              (id, project_id, provider, category, amount_usd_micros, confidence,
               external_resource_id, created_at)
            VALUES
              (:id, :p, :prov, :cat, :amt, 'high', :er, COALESCE(:created, now()))
            """
        ),
        {
            "id": ce_id,
            "p": PID,
            "prov": provider,
            "cat": category,
            "amt": amount,
            "er": external_resource_id,
            "created": created_at,
        },
    )
    return ce_id


def _insert_external_resource(connection, *, provider: str, external_id: str, name: str | None = None):
    rid = make_id("eres")
    connection.execute(
        text(
            """
            INSERT INTO external_resources
              (id, project_id, provider, resource_type, external_id, name, state, ownership)
            VALUES
              (:id, :p, :prov, 'instance', :ext, :name, 'discovered_unmanaged', 'discovered_unmanaged')
            """
        ),
        {
            "id": rid,
            "p": PID,
            "prov": provider,
            "ext": external_id,
            "name": name or external_id,
        },
    )
    return rid


def test_build_summary_empty_project(engine):
    svc = FinopsReconcilerService(_settings())
    out = svc.build_summary(engine, PID, days=30)
    assert out["project_id"] == PID
    assert out["days"] == 30
    assert out["spend_by_provider"] == []
    assert out["spend_by_day"] == []
    assert out["anomalies_open_count"] == 0
    assert out["commitment_utilization"] == []
    assert out["top_resources"] == []
    assert out["period_start"]
    assert out["period_end"]


def test_build_summary_aggregates_providers_days_resources_anomalies(engine):
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        res_a = _insert_external_resource(c, provider="aws", external_id="i-hot")
        res_b = _insert_external_resource(c, provider="aws", external_id="i-cool")
        _insert_cost_event(
            c, provider="aws", amount=500_000, external_resource_id=res_a, created_at=now
        )
        _insert_cost_event(
            c, provider="aws", amount=200_000, external_resource_id=res_a,
            created_at=now - timedelta(days=1),
        )
        _insert_cost_event(
            c, provider="aws", amount=50_000, external_resource_id=res_b, created_at=now
        )
        _insert_cost_event(
            c, provider="gcp", amount=100_000, created_at=now - timedelta(days=2)
        )
        # Outside window — must not count
        _insert_cost_event(
            c, provider="aws", amount=9_999_999, created_at=now - timedelta(days=60)
        )
        c.execute(
            text(
                """
                INSERT INTO cost_anomalies
                  (id, project_id, provider, anomaly_type, severity, status,
                   amount_usd_micros, threshold_usd_micros, summary)
                VALUES
                  (:id1, :p, 'aws', 'spike', 'high', 'open', 500000, 100000, 'spike open'),
                  (:id2, :p, 'aws', 'spike', 'low', 'resolved', 10000, 5000, 'old')
                """
            ),
            {"id1": make_id("can"), "id2": make_id("can"), "p": PID},
        )
        c.execute(
            text(
                """
                INSERT INTO commitments
                  (id, project_id, provider, commitment_type, amount_usd_micros,
                   period_start, period_end, status)
                VALUES
                  ('cmt_sum_aws', :p, 'aws', 'savings_plan', 1000000,
                   :start, :end, 'active'),
                  ('cmt_sum_gcp', :p, 'gcp', 'cud', 1000000,
                   :start, :end, 'active')
                """
            ),
            {
                "p": PID,
                "start": now - timedelta(days=10),
                "end": now + timedelta(days=10),
            },
        )

    svc = FinopsReconcilerService(_settings())
    out = svc.build_summary(engine, PID, days=30)

    by_prov = {row["provider"]: row for row in out["spend_by_provider"]}
    assert by_prov["aws"]["cost_usd_micros"] == 750_000
    assert by_prov["aws"]["event_count"] == 3
    assert by_prov["gcp"]["cost_usd_micros"] == 100_000
    assert by_prov["gcp"]["event_count"] == 1
    # highest spend first
    assert out["spend_by_provider"][0]["provider"] == "aws"

    days = {row["day"]: row["cost_usd_micros"] for row in out["spend_by_day"]}
    assert len(days) >= 2
    assert sum(days.values()) == 850_000

    assert out["anomalies_open_count"] == 1

    util = {row["commitment_id"]: row for row in out["commitment_utilization"]}
    # aws actual in commitment period = 750_000 / 1_000_000 = 0.75 → on_track
    assert util["cmt_sum_aws"]["provider"] == "aws"
    assert abs(util["cmt_sum_aws"]["utilization"] - 0.75) < 1e-6
    assert util["cmt_sum_aws"]["verdict"] == "on_track"
    # gcp actual = 100_000 / 1_000_000 = 0.1 → under_utilized
    assert util["cmt_sum_gcp"]["verdict"] == "under_utilized"

    top = {row["resource_ref"]: row for row in out["top_resources"]}
    assert top["i-hot"]["cost_usd_micros"] == 700_000
    assert top["i-hot"]["provider"] == "aws"
    assert top["i-cool"]["cost_usd_micros"] == 50_000
    # ranked by cost desc
    assert out["top_resources"][0]["resource_ref"] == "i-hot"


def test_finops_summary_http(client, engine):
    now = datetime.now(timezone.utc)
    with begin(engine) as c:
        _insert_cost_event(c, provider="azure", amount=42_000, created_at=now)
        c.execute(
            text(
                """
                INSERT INTO cost_anomalies
                  (id, project_id, provider, anomaly_type, severity, status,
                   amount_usd_micros, threshold_usd_micros, summary)
                VALUES (:id, :p, 'azure', 'spike', 'medium', 'open', 42000, 10000, 'open')
                """
            ),
            {"id": make_id("can"), "p": PID},
        )

    r = client.get("/v1/finops/summary", params={"project_id": PID, "days": 14})
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == PID
    assert body["days"] == 14
    assert body["anomalies_open_count"] >= 1
    assert any(row["provider"] == "azure" for row in body["spend_by_provider"])
    # contract keys always present
    for key in (
        "spend_by_provider",
        "spend_by_day",
        "anomalies_open_count",
        "commitment_utilization",
        "top_resources",
        "period_start",
        "period_end",
    ):
        assert key in body


def test_finops_summary_rejects_bad_days(client):
    bad = client.get("/v1/finops/summary", params={"project_id": PID, "days": 0})
    assert bad.status_code == 400
    bad2 = client.get("/v1/finops/summary", params={"project_id": PID, "days": 999})
    assert bad2.status_code == 400


def test_finops_summary_default_days(client):
    r = client.get("/v1/finops/summary", params={"project_id": PID})
    assert r.status_code == 200
    assert r.json()["days"] == 30
