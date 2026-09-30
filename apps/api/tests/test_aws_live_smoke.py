"""LIVE read-only smoke tests for the AWS provider adapter (BOSS-65).

Skipped unless BOSS_AWS_LIVE_TEST=1. These hit a REAL AWS account with the
credentials in BOSS_AWS_ACCESS_KEY_ID / BOSS_AWS_SECRET_ACCESS_KEY (and
optionally BOSS_AWS_REGION / BOSS_AWS_ROLE_ARN) — read-only operations only:

- STS get_caller_identity via the provider connection-test path
- resource inventory sync (EC2/S3/RDS describe/list calls)
- Cost Explorer billing sync (note: AWS bills ~$0.01 per Cost Explorer request)

No create/destroy operations belong in this file; the governed write flow is
covered with mocks in test_aws_changeset_flow.py.

Run:
    BOSS_AWS_LIVE_TEST=1 BOSS_AWS_ACCESS_KEY_ID=... BOSS_AWS_SECRET_ACCESS_KEY=... \
        pytest apps/api/tests/test_aws_live_smoke.py -q
"""

import os

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("BOSS_AWS_LIVE_TEST") != "1",
        reason="Set BOSS_AWS_LIVE_TEST=1 to run live AWS smoke tests.",
    ),
    pytest.mark.skipif(
        os.environ.get("BOSS_AWS_LIVE_TEST") == "1"
        and not (
            os.environ.get("BOSS_AWS_ACCESS_KEY_ID")
            and os.environ.get("BOSS_AWS_SECRET_ACCESS_KEY")
        ),
        reason=(
            "BOSS_AWS_LIVE_TEST=1 requires BOSS_AWS_ACCESS_KEY_ID and "
            "BOSS_AWS_SECRET_ACCESS_KEY in the environment."
        ),
    ),
]


def _create_live_aws_account(client, label: str, **overrides) -> dict:
    payload = {
        "project_id": "boss-dogfood",
        "provider": "aws",
        "account_label": label,
        "scopes": ["read"],
        "read_only": True,
        "destructive_actions_disabled": True,
        "actor": "ceo",
    }
    payload.update(overrides)
    response = client.post("/v1/providers", json=payload)
    assert response.status_code == 201
    return response.json()


def test_live_sts_connection_test_reports_caller_identity(client):
    provider = _create_live_aws_account(client, "live-smoke-aws")

    tested = client.post(
        f"/v1/providers/{provider['id']}/test",
        params={"project_id": "boss-dogfood", "actor": "ceo"},
    )
    assert tested.status_code == 200
    payload = tested.json()
    assert payload["status"] == "passed", payload.get("detail")
    summary = payload["detail"]["summary"]
    account_id = str(summary.get("account_id") or "")
    assert account_id.isdigit() and len(account_id) == 12
    assert str(summary.get("arn") or "").startswith("arn:aws:")


def test_live_inventory_sync_read_only(client):
    provider = _create_live_aws_account(client, "live-smoke-aws-inventory")

    synced = client.post(
        "/v1/resources/sync",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "actor": "ceo",
        },
    )
    assert synced.status_code == 200
    payload = synced.json()
    run = payload["runs"][0]
    assert run["status"] == "succeeded", run
    assert payload["resources_seen"] >= 0

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "aws"},
    )
    assert resources.status_code == 200
    assert len(resources.json()["items"]) == payload["resources_seen"]


def test_live_cost_sync_populates_ledger(client):
    provider = _create_live_aws_account(
        client,
        "live-smoke-aws-billing",
        scopes=["read", "billing:read"],
        billing_readable=True,
    )

    synced = client.post(
        "/v1/resources/sync",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "actor": "ceo",
        },
    )
    assert synced.status_code == 200
    payload = synced.json()
    run = payload["runs"][0]
    assert run["status"] == "succeeded", run
    assert run["billing_status"] == "synced"
    assert payload["costs_seen"] >= 0

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    aws_costs = [
        item for item in costs.json()["items"] if item["provider"] == "aws"
    ]
    assert len(aws_costs) == payload["costs_seen"]
    for item in aws_costs:
        assert item["amount_usd_micros"] > 0
        assert item["source_ref"].startswith("aws.cost_explorer:")
