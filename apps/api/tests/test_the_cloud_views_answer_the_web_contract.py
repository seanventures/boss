"""Infra › Cloud answers the web's row contract (spec §4.8; the keys app/infra/cloud-rows.ts
reads, fixed in apps/web/tests/_helpers/infra-cloud-fixtures.ts). One server composition per
sub-view replaces the console's 17-call fan-out; every number is a count or sum of stored rows
(recounted here), money is micros AND a dollar string, never micros alone."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main

HOME = "boss-dogfood"

ACCOUNT_KEYS = {"id", "provider", "provider_name", "label", "state", "credential", "last_test",
                "authority", "destructive_blocked", "resources", "spend_30d_usd_micros",
                "spend_30d_usd", "owner", "scopes", "flags", "impact", "last_error",
                "rotation_slots", "events", "receipts", "can"}
RESOURCE_KEYS = {"id", "kind", "name", "provider", "account", "region", "service", "env", "owner",
                 "health", "cost_month_usd_micros", "cost_month_usd", "last_seen_at", "ttl",
                 "expires_at", "external_id", "links", "droplet"}
POOL_KEYS = {"id", "pool_key", "kind", "desired", "actual", "min", "max", "queue_per_worker",
             "cost_hour_usd_micros", "cost_hour_usd", "plan", "history", "plans"}
SPEND_KEYS = {"id", "provider", "spend_30d_usd_micros", "spend_30d_usd", "share", "events",
              "anomalies", "estimated", "included", "categories", "cost_events",
              "reconciliation", "usage"}
CHANGE_KEYS = {"id", "title", "summary", "account", "risk", "cost_usd_micros", "cost_usd",
               "state", "created_at", "operations", "operations_json", "rollback_plan",
               "receipts", "drift", "can"}
COUNT_KEYS = {"accounts", "resources", "pools", "providers", "changes", "anomalies"}


@pytest.fixture()
def plane(engine):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO provider_accounts (id, project_id, provider, account_label, status, "
            "credential_refs, read_only, deploy_capable, last_test_status, last_tested_at, "
            "created_by) VALUES "
            "('acct_do', :p, 'digitalocean', 'DO prod', 'active', '{\"api_token\": \"sref_x\"}', "
            " false, true, 'passed', now(), 'ceo'), "
            "('acct_aws', :p, 'aws', 'AWS sandbox', 'disabled', '{}', true, false, NULL, NULL, "
            " 'ceo')"), {"p": HOME})
        c.execute(text(
            "INSERT INTO external_resources (id, project_id, provider_account_id, provider, "
            "resource_type, external_id, name, environment, health_status, "
            "actual_monthly_usd_micros, metadata) VALUES "
            "('res_db', :p, 'acct_do', 'digitalocean', 'databases', 'db-1', 'invoice-db', 'prod', "
            " 'healthy', 15000000, '{\"region\": \"nyc3\"}'), "
            "('res_s3', :p, 'acct_aws', 'aws', 's3', 'arn:s3', 'assets', 'sandbox', 'unknown', "
            " 3000, '{}')"), {"p": HOME})
        c.execute(text(
            "INSERT INTO infra_resources (id, project_id, provider, resource_type, resource_name, "
            "external_resource_id, region, size, image, ttl_minutes, expires_at) VALUES "
            "('drop_1', :p, 'digitalocean', 'droplet', 'runner-1', '4123987', 'nyc3', "
            " 's-1vcpu-1gb', 'ubuntu', 240, now() + interval '4 hours')"), {"p": HOME})
        c.execute(text(
            "INSERT INTO cost_events (id, project_id, provider_account_id, provider, category, "
            "amount_usd_micros, confidence, receipt_id) SELECT 'ce_' || g, :p, "
            "CASE WHEN g <= 3 THEN 'acct_do' ELSE 'acct_aws' END, "
            "CASE WHEN g <= 3 THEN 'digitalocean' ELSE 'aws' END, "
            "CASE WHEN g = 1 THEN 'databases' ELSE 'compute' END, g * 1000000, "
            "CASE WHEN g = 4 THEN 'estimated_from_price_catalog' ELSE 'billed' END, NULL "
            "FROM generate_series(1, 4) g"), {"p": HOME})
        c.execute(text(
            "INSERT INTO cost_anomalies (id, project_id, provider, anomaly_type, severity, "
            "amount_usd_micros, threshold_usd_micros, summary, cost_event_id) VALUES "
            "('an_1', :p, 'digitalocean', 'spike', 'high', 3000000, 1000000, "
            " 'Compute spend 3x the 7-day average', 'ce_3')"), {"p": HOME})
        c.execute(text(
            "INSERT INTO worker_pools (id, project_id, pool_key, kind, current_workers, "
            "desired_workers, min_workers, max_workers, cost_per_worker_hour_usd_micros, "
            "created_by) VALUES ('pool_1', :p, 'local-runners', 'local', 2, 3, 1, 4, 18000, "
            "'ceo')"), {"p": HOME})
        c.execute(text(
            "INSERT INTO scale_plans (id, project_id, worker_pool_id, status, trigger_reason, "
            "desired_workers_before, desired_workers_after, budget_state, created_by) VALUES "
            "('plan_1', :p, 'pool_1', 'proposed', 'Queue 12 over 2 workers', 2, 3, 'green', "
            "'ceo')"), {"p": HOME})
        c.execute(text(
            "INSERT INTO changesets (id, project_id, provider_account_id, title, summary, status, "
            "operations, expected_cost_usd_micros, rollback_plan, created_by) VALUES "
            "('cs_1', :p, 'acct_do', 'Resize droplet', 'Month end', 'pending_approval', "
            " '[{\"action\": \"resize\", \"resource_type\": \"droplet\", \"resource_ref\": "
            "\"drop_1\", \"destructive\": false}]', 12000000, '[\"Resize back\"]', 'ceo')"),
            {"p": HOME})
        c.execute(text(
            "INSERT INTO drift_reports (id, project_id, provider_account_id, classification, "
            "summary, unmanaged_count) VALUES ('drift_1', :p, 'acct_do', 'unmanaged', "
            "'1 droplet BOSS did not create', 1)"), {"p": HOME})
    yield
    with engine.begin() as c:
        for t in ("drift_reports", "changesets", "scale_plans", "worker_pools", "cost_anomalies",
                  "cost_events", "infra_resources", "external_resources", "provider_accounts"):
            c.execute(text(f"DELETE FROM {t} WHERE project_id = :p"), {"p": HOME})


def _view(client, view, **extra):
    r = client.get("/v1/infra/cloud", params={"project_id": HOME, "view": view, **extra})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["partial"] == [], body["partial"]
    assert set(body["counts"]) == COUNT_KEYS
    assert "micros" not in json.dumps(body["notices"])
    for row in body["rows"]:
        assert len(json.dumps(row).encode()) <= 2048, row["id"]
    return body


def test_every_view_answers_the_row_contract(client, plane, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    acc = _view(client, "accounts")
    assert acc["counts"] == {"accounts": 2, "resources": 3, "pools": 1, "providers": 2,
                             "changes": 1, "anomalies": 1}
    rows = {r["id"]: r for r in acc["rows"]}
    assert all(set(r) == ACCOUNT_KEYS for r in acc["rows"])
    do = rows["acct_do"]
    assert do["state"] == {"word": "Active", "tone": "green"}
    assert do["last_test"]["word"] == "Fresh" and do["authority"] == "Deploy capable"
    assert do["spend_30d_usd_micros"] == 6_000_000 and do["spend_30d_usd"] == "$6.00"
    assert do["credential"]["slots"] == ["api_token"] and do["impact"]["open_changes"] == 1
    assert rows["acct_aws"]["last_test"]["word"] == "Untested"
    assert rows["acct_aws"]["can"]["enable"] and not rows["acct_aws"]["can"]["disable"]
    cat = {c["provider"]: c for c in acc["header"]["catalog"]}
    assert {"aws", "digitalocean"} <= set(cat)
    assert cat["aws"]["auth_methods"] and cat["aws"]["skill_count"] >= len(cat["aws"]["skills"])
    assert acc["notices"] == [{"key": "byok", "tip": "Community connects your own provider keys"}]

    res = _view(client, "resources")
    assert all(set(r) == RESOURCE_KEYS for r in res["rows"])
    by = {r["id"]: r for r in res["rows"]}
    assert by["drop_1"]["kind"] == "Droplet" and by["drop_1"]["ttl"] == "4h"
    assert by["drop_1"]["droplet"]["size"] == "s-1vcpu-1gb"
    assert by["res_db"]["health"]["word"] == "Up" and by["res_db"]["cost_month_usd"] == "$15.00"
    assert by["res_s3"]["cost_month_usd"] == "<$0.01"
    assert by["res_db"]["account"] == {"id": "acct_do", "name": "DO prod"}
    only = _view(client, "resources", provider="aws")
    assert [r["id"] for r in only["rows"]] == ["res_s3"]

    cap = _view(client, "capacity")
    assert all(set(r) == POOL_KEYS for r in cap["rows"])
    pool = cap["rows"][0]
    assert pool["cost_hour_usd"] == "$0.02" and pool["plan"]["word"] == "Scale up"
    assert set(cap["header"]) >= {"queue", "api_health", "fabric", "accounts"}

    spend = _view(client, "spend")
    assert all(set(r) == SPEND_KEYS for r in spend["rows"])
    sp = {r["id"]: r for r in spend["rows"]}
    assert sp["digitalocean"]["spend_30d_usd_micros"] == 6_000_000
    assert sp["aws"]["spend_30d_usd_micros"] == 4_000_000 and sp["aws"]["estimated"] is True
    assert sp["digitalocean"]["estimated"] is False
    assert sp["digitalocean"]["share"] == 0.6 and sp["digitalocean"]["anomalies"] == 1
    assert spend["header"]["anomalies"][0]["severity"] == {"word": "High", "tone": "amber"}

    ch = _view(client, "changes")
    assert all(set(r) == CHANGE_KEYS for r in ch["rows"])
    change = ch["rows"][0]
    assert change["state"]["word"] == "Needs you" and change["can"] == {"approve": True,
                                                                       "apply": False}
    assert change["cost_usd"] == "$12.00" and change["drift"][0]["unmanaged"] == 1


def test_a_reader_without_providerops_manage_gets_read_only(client, plane, engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO project_members (id, project_id, member_id, display_name, "
                       "permissions) VALUES ('pm_ro', :p, 'reader-ro', 'Reader', '[]') "
                       "ON CONFLICT DO NOTHING"), {"p": HOME})
    body = _view(client, "changes", viewer="reader-ro")
    assert {"key": "read_only", "tip": "You lack providerops_manage"} in body["notices"]
    assert body["rows"][0]["can"] == {"approve": False, "apply": False}
    with engine.begin() as c:
        c.execute(text("DELETE FROM project_members WHERE id = 'pm_ro'"))
