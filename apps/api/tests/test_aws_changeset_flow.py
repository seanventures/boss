"""End-to-end ChangeSet-flow acceptance tests for the AWS provider adapter (BOSS-65).

These mirror the DigitalOcean/Cloudflare governed-flow tests in test_api.py:
every AWS write goes through ProviderAccount -> BudgetReservation -> ChangeSet ->
approval(s) -> apply -> ResourceReceipt -> external_resources/cost ledger, with
`ResourcePlaneService._aws_client` monkeypatched so no network or real AWS account
is ever touched.
"""

from datetime import datetime, timezone

from botocore.exceptions import ClientError

from apps.api.app import main as api_main


class FakeAwsClient:
    """Records calls and replays canned responses for boto3-style clients."""

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, method_name: str):
        def _call(**kwargs):
            self.calls.append((method_name, kwargs))
            value = self.responses.get(method_name)
            if isinstance(value, Exception):
                raise value
            if callable(value):
                return value(**kwargs)
            return value or {}

        return _call

    def calls_for(self, method_name: str) -> list[dict]:
        return [kwargs for name, kwargs in self.calls if name == method_name]


def install_fake_aws(monkeypatch, clients: dict[str, FakeAwsClient]) -> None:
    """Route every AWS call made by the live service instance to fake clients.

    All AWS traffic funnels through `ResourcePlaneService._aws_client`, so this is
    the single seam that keeps the whole governed flow real while faking boto3.
    """
    monkeypatch.setenv("BOSS_AWS_SECRET_ACCESS_KEY", "test-aws-secret-key")

    def fake_aws_client(service_name: str, _credential: str):
        if service_name not in clients:
            raise AssertionError(f"Unexpected AWS client requested: {service_name}")
        return clients[service_name]

    monkeypatch.setattr(api_main.resource_plane, "_aws_client", fake_aws_client)


def create_aws_provider_account(client, label: str, **overrides) -> dict:
    payload = {
        "project_id": "boss-dogfood",
        "provider": "aws",
        "account_label": label,
        "scopes": ["ec2:write", "s3:write", "iam:write"],
        "read_only": False,
        "destructive_actions_disabled": False,
        "actor": "ceo",
    }
    payload.update(overrides)
    response = client.post("/v1/providers", json=payload)
    assert response.status_code == 201
    account = response.json()
    assert account["provider"] == "aws"
    assert account["credential_ref"] == "env:BOSS_AWS_SECRET_ACCESS_KEY"
    return account


# Stays within the default burst cap (BOSS_BURST_CAP_USD_MICROS_PER_MIN=100_000)
# so the suite passes without a repo .env raising budget limits.
INFRA_RESERVATION_USD_MICROS = 100_000


def reserve_for_infra_provision(client, reason: str = "Reserve AWS infra budget.") -> str:
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": "boss-dogfood",
            "category": "infra",
            "amount_usd_micros": INFRA_RESERVATION_USD_MICROS,
            "reason": reason,
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]
    assert reservation_id, f"budget reservation denied: {reserve.json()}"
    return reservation_id


EC2_RUN_INSTANCES_RESPONSE = {
    "Instances": [
        {
            "InstanceId": "i-0abc1234567890def",
            "State": {"Name": "running"},
            "Placement": {"AvailabilityZone": "us-east-1a"},
        }
    ]
}


def test_aws_ec2_create_requires_approved_changeset_and_writes_receipts(
    client, monkeypatch
):
    ec2 = FakeAwsClient({"run_instances": EC2_RUN_INSTANCES_RESPONSE})
    install_fake_aws(monkeypatch, {"ec2": ec2})
    provider = create_aws_provider_account(client, "write-aws")
    reservation_id = reserve_for_infra_provision(
        client, "Reserve AWS EC2 ChangeSet budget."
    )

    created = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Create preview EC2 instance",
            "summary": "Provision a preview EC2 instance through ProviderOps.",
            "expected_cost_usd_micros": INFRA_RESERVATION_USD_MICROS,
            "reservation_id": reservation_id,
            "rollback_plan": [
                "Terminate the instance through an approved cleanup ChangeSet."
            ],
            "operations": [
                {
                    "skill_id": "aws.ec2.instance.create",
                    "action": "create",
                    "resource_type": "ec2_instance",
                    "resource_ref": "boss-preview-web",
                    "desired_state": {
                        "image_id": "ami-123",
                        "instance_type": "t3.micro",
                        "name": "boss-preview-web",
                        "tags": ["env:preview"],
                        "environment": "preview",
                    },
                    "risk_tier": 3,
                }
            ],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    changeset = created.json()
    assert changeset["status"] == "pending_approval"
    assert changeset["approval_decision_id"]
    assert changeset["expected_receipts"][0]["dedupe_key"].startswith(
        f"resource_plane:{changeset['id']}:0"
    )

    # Apply is rejected before approval; nothing has hit the (fake) provider.
    blocked_apply = client.post(
        f"/v1/changesets/{changeset['id']}/apply",
        json={"actor": "ceo"},
    )
    assert blocked_apply.status_code == 422
    assert ec2.calls == []

    # Dual-control: aws.ec2.instance.create requires [ceo, cto] like DO droplet.create.
    partial = client.post(
        f"/v1/changesets/{changeset['id']}/approve",
        json={"actor": "ceo"},
    )
    assert partial.status_code == 200
    assert partial.json()["status"] == "pending_approval"
    half_apply = client.post(
        f"/v1/changesets/{changeset['id']}/apply",
        json={"actor": "ceo"},
    )
    assert half_apply.status_code == 422
    approved = client.post(
        f"/v1/changesets/{changeset['id']}/approve",
        json={"actor": "cto"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    applied = client.post(
        f"/v1/changesets/{changeset['id']}/apply",
        json={"actor": "ceo"},
    )
    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    assert payload["changeset"]["status"] == "applied"

    run_request = ec2.calls_for("run_instances")[0]
    assert run_request["ImageId"] == "ami-123"
    assert run_request["InstanceType"] == "t3.micro"
    tags = run_request["TagSpecifications"][0]["Tags"]
    assert {"Key": "Name", "Value": "boss-preview-web"} in tags
    assert {"Key": "boss-managed", "Value": "true"} in tags
    assert {"Key": "env", "Value": "preview"} in tags

    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider"] == "aws"
    assert receipt["provider_ref"] == "aws:ec2_instance:i-0abc1234567890def"
    assert receipt["receipt_id"]

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "aws"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "i-0abc1234567890def"
    assert resource["name"] == "boss-preview-web"
    assert resource["state"] == "active"
    assert resource["ownership"] == "managed_by_boss"
    assert resource["receipt_id"] == receipt["receipt_id"]

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_item = costs.json()["items"][0]
    assert cost_item["provider"] == "aws"
    assert cost_item["receipt_id"] == receipt["receipt_id"]


def test_aws_destructive_op_blocked_when_account_disables_destructive_actions(
    client, monkeypatch
):
    ec2 = FakeAwsClient()
    install_fake_aws(monkeypatch, {"ec2": ec2})
    provider = create_aws_provider_account(
        client,
        "locked-aws",
        destructive_actions_disabled=True,
    )

    blocked = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Destroy EC2 instance on locked account",
            "summary": "Destructive AWS ops must be blocked on this account.",
            "expected_cost_usd_micros": 0,
            "rollback_plan": ["Recreate the instance from the recorded launch spec."],
            "operations": [
                {
                    "skill_id": "aws.ec2.instance.destroy",
                    "action": "destroy",
                    "resource_type": "ec2_instance",
                    "resource_ref": "i-0abc1234567890def",
                    "desired_state": {"instance_id": "i-0abc1234567890def"},
                    "risk_tier": 3,
                    "destructive": True,
                }
            ],
            "actor": "ceo",
        },
    )
    assert blocked.status_code == 422
    assert "destructive" in blocked.json()["detail"].lower()
    assert ec2.calls == []


def test_aws_ec2_destroy_via_approved_changeset_records_rollback_plan(
    client, monkeypatch
):
    ec2 = FakeAwsClient(
        {
            "terminate_instances": {
                "TerminatingInstances": [
                    {
                        "InstanceId": "i-0abc1234567890def",
                        "CurrentState": {"Name": "shutting-down"},
                    }
                ]
            }
        }
    )
    install_fake_aws(monkeypatch, {"ec2": ec2})
    provider = create_aws_provider_account(client, "cleanup-aws")
    rollback_plan = ["Recreate the instance from the recorded launch spec."]

    created = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Destroy preview EC2 instance",
            "summary": "Terminate an approved preview instance through ProviderOps.",
            "expected_cost_usd_micros": 0,
            "rollback_plan": rollback_plan,
            "operations": [
                {
                    "skill_id": "aws.ec2.instance.destroy",
                    "action": "destroy",
                    "resource_type": "ec2_instance",
                    "resource_ref": "i-0abc1234567890def",
                    "desired_state": {
                        "instance_id": "i-0abc1234567890def",
                        "name": "boss-preview-web",
                    },
                    "risk_tier": 3,
                    "destructive": True,
                }
            ],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    changeset = created.json()
    assert changeset["status"] == "pending_approval"
    assert changeset["rollback_plan"] == rollback_plan

    approved = client.post(
        f"/v1/changesets/{changeset['id']}/approve",
        json={"actor": "ceo"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    applied = client.post(
        f"/v1/changesets/{changeset['id']}/apply",
        json={"actor": "ceo"},
    )
    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    # Rollback plan stays on the applied ChangeSet payload as actuation evidence.
    assert payload["changeset"]["rollback_plan"] == rollback_plan
    assert ec2.calls_for("terminate_instances") == [
        {"InstanceIds": ["i-0abc1234567890def"]}
    ]

    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "succeeded"
    assert receipt["provider_ref"] == "aws:ec2_instance:i-0abc1234567890def"

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "aws"},
    )
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["external_id"] == "i-0abc1234567890def"
    assert resource["state"] == "deleted"
    assert resource["health_status"] == "shutting-down"
    assert resource["receipt_id"] == receipt["receipt_id"]


def test_aws_s3_and_iam_operations_share_one_approved_changeset(client, monkeypatch):
    s3 = FakeAwsClient({"create_bucket": {}})
    iam = FakeAwsClient(
        {
            "get_role": ClientError(
                {"Error": {"Code": "NoSuchEntity", "Message": "Role not found"}},
                "GetRole",
            ),
            "create_role": {
                "Role": {
                    "RoleId": "AROATESTROLE",
                    "Arn": "arn:aws:iam::123456789012:role/boss-app-role",
                }
            },
        }
    )
    install_fake_aws(monkeypatch, {"s3": s3, "iam": iam})
    provider = create_aws_provider_account(client, "storage-aws")
    reservation_id = reserve_for_infra_provision(
        client, "Reserve AWS S3+IAM ChangeSet budget."
    )

    created = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Create artifact bucket and app role",
            "summary": "S3 bucket plus IAM role in one governed ChangeSet.",
            "expected_cost_usd_micros": INFRA_RESERVATION_USD_MICROS,
            "reservation_id": reservation_id,
            "rollback_plan": [
                "Delete the bucket and detach/delete the role via a cleanup ChangeSet."
            ],
            "operations": [
                {
                    "skill_id": "aws.s3.bucket.create",
                    "action": "create",
                    "resource_type": "s3_bucket",
                    "resource_ref": "boss-artifacts",
                    "desired_state": {"bucket_name": "boss-artifacts", "versioning": True},
                    "risk_tier": 2,
                },
                {
                    "skill_id": "aws.iam.role.upsert",
                    "action": "upsert",
                    "resource_type": "iam_role",
                    "resource_ref": "boss-app-role",
                    "desired_state": {
                        "role_name": "boss-app-role",
                        "assume_role_policy_document": {
                            "Version": "2012-10-17",
                            "Statement": [],
                        },
                        "attach_policy_arns": [
                            "arn:aws:iam::aws:policy/ReadOnlyAccess"
                        ],
                    },
                    "risk_tier": 3,
                },
            ],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    changeset = created.json()
    assert changeset["status"] == "pending_approval"
    assert len(changeset["expected_receipts"]) == 2

    # iam.role.upsert carries [ceo, cto] dual-control for the whole ChangeSet.
    client.post(f"/v1/changesets/{changeset['id']}/approve", json={"actor": "ceo"})
    approved = client.post(
        f"/v1/changesets/{changeset['id']}/approve",
        json={"actor": "cto"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    applied = client.post(
        f"/v1/changesets/{changeset['id']}/apply",
        json={"actor": "ceo"},
    )
    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "applied"
    receipts = payload["resource_receipts"]
    assert [item["status"] for item in receipts] == ["succeeded", "succeeded"]
    assert receipts[0]["provider_ref"] == "aws:s3_bucket:boss-artifacts"
    assert receipts[1]["provider_ref"] == "aws:iam_role:boss-app-role"

    assert s3.calls_for("create_bucket")[0]["Bucket"] == "boss-artifacts"
    assert s3.calls_for("put_bucket_versioning")[0]["VersioningConfiguration"] == {
        "Status": "Enabled"
    }
    assert iam.calls_for("create_role")[0]["RoleName"] == "boss-app-role"
    assert iam.calls_for("attach_role_policy") == [
        {
            "RoleName": "boss-app-role",
            "PolicyArn": "arn:aws:iam::aws:policy/ReadOnlyAccess",
        }
    ]

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "aws"},
    )
    assert resources.status_code == 200
    by_type = {item["resource_type"]: item for item in resources.json()["items"]}
    assert by_type["s3_bucket"]["external_id"] == "boss-artifacts"
    assert by_type["s3_bucket"]["ownership"] == "managed_by_boss"
    assert by_type["iam_role"]["external_id"] == "AROATESTROLE"
    assert by_type["iam_role"]["name"] == "boss-app-role"
    assert by_type["iam_role"]["ownership"] == "managed_by_boss"


def test_aws_failed_actuation_produces_failed_receipt_and_deadletter(
    client, monkeypatch
):
    ec2 = FakeAwsClient(
        {
            "run_instances": ClientError(
                {
                    "Error": {
                        "Code": "UnauthorizedOperation",
                        "Message": "You are not authorized to perform this operation.",
                    }
                },
                "RunInstances",
            )
        }
    )
    install_fake_aws(monkeypatch, {"ec2": ec2})
    provider = create_aws_provider_account(client, "failing-aws")
    reservation_id = reserve_for_infra_provision(
        client, "Reserve AWS failing ChangeSet budget."
    )

    created = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Create EC2 instance that fails actuation",
            "summary": "Provider-side failure must produce a failed receipt.",
            "expected_cost_usd_micros": INFRA_RESERVATION_USD_MICROS,
            "reservation_id": reservation_id,
            "rollback_plan": ["Nothing was created; retry with corrected IAM policy."],
            "operations": [
                {
                    "skill_id": "aws.ec2.instance.create",
                    "action": "create",
                    "resource_type": "ec2_instance",
                    "resource_ref": "boss-denied-web",
                    "desired_state": {
                        "image_id": "ami-123",
                        "instance_type": "t3.micro",
                        "name": "boss-denied-web",
                    },
                    "risk_tier": 3,
                }
            ],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201
    changeset = created.json()
    client.post(f"/v1/changesets/{changeset['id']}/approve", json={"actor": "ceo"})
    client.post(f"/v1/changesets/{changeset['id']}/approve", json={"actor": "cto"})

    applied = client.post(
        f"/v1/changesets/{changeset['id']}/apply",
        json={"actor": "ceo"},
    )
    assert applied.status_code == 200
    payload = applied.json()
    assert payload["status"] == "deadlettered"
    assert payload["changeset"]["status"] == "deadlettered"
    assert payload["changeset"]["failure_reason"]
    receipt = payload["resource_receipts"][0]
    assert receipt["status"] == "failed"
    assert receipt["receipt_id"]
    assert "UnauthorizedOperation" in receipt["error"]["message"]

    receipts = client.get(
        "/v1/resource-receipts",
        params={"project_id": "boss-dogfood"},
    )
    assert receipts.status_code == 200
    assert len(receipts.json()["items"]) == 1
    assert receipts.json()["items"][0]["status"] == "failed"

    # Reservation is released on failed actuation.
    budget_status = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": "boss-dogfood"},
    )
    assert budget_status.status_code == 200
    assert budget_status.json()["reserved_usd_micros"] == 0

    deadletters = client.get("/v1/deadletters", params={"project_id": "boss-dogfood"})
    assert any(
        item["source_kind"] == "resource_receipt"
        and item["payload"]["changeset_id"] == changeset["id"]
        for item in deadletters.json()["items"]
    )

    # No resource was registered for the failed create.
    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "aws"},
    )
    assert resources.status_code == 200
    assert resources.json()["items"] == []


def test_aws_inventory_sync_populates_resources(client, monkeypatch):
    ec2 = FakeAwsClient(
        {
            "describe_instances": {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-0abc1234567890def",
                                "InstanceType": "t3.micro",
                                "ImageId": "ami-123",
                                "State": {"Name": "running"},
                                "Placement": {"AvailabilityZone": "us-east-1a"},
                                "Tags": [
                                    {"Key": "Name", "Value": "web-1"},
                                    {"Key": "env", "Value": "production"},
                                    {"Key": "boss-managed", "Value": "true"},
                                ],
                            }
                        ]
                    }
                ]
            }
        }
    )
    s3 = FakeAwsClient(
        {
            "list_buckets": {
                "Buckets": [
                    {
                        "Name": "manually-created-bucket",
                        "CreationDate": datetime(
                            2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
                        ),
                    }
                ]
            }
        }
    )
    rds = FakeAwsClient(
        {
            "describe_db_instances": {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "boss-db",
                        "DBInstanceStatus": "available",
                        "Engine": "postgres",
                        "DBInstanceClass": "db.t3.micro",
                        "AllocatedStorage": 20,
                        "TagList": [{"Key": "env", "Value": "staging"}],
                    }
                ]
            }
        }
    )
    install_fake_aws(monkeypatch, {"ec2": ec2, "s3": s3, "rds": rds})
    provider = create_aws_provider_account(
        client,
        "inventory-aws",
        scopes=["read"],
        read_only=True,
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
    sync_payload = synced.json()
    assert sync_payload["resources_seen"] == 3
    assert sync_payload["resources_changed"] == 3
    assert sync_payload["runs"][0]["status"] == "succeeded"

    resources = client.get(
        "/v1/resources",
        params={"project_id": "boss-dogfood", "provider": "aws"},
    )
    assert resources.status_code == 200
    items = {item["resource_type"]: item for item in resources.json()["items"]}
    assert set(items) == {"ec2_instance", "s3_bucket", "rds_instance"}

    instance = items["ec2_instance"]
    assert instance["external_id"] == "i-0abc1234567890def"
    assert instance["name"] == "web-1"
    assert instance["state"] == "active"
    assert instance["ownership"] == "managed_by_boss"
    assert instance["environment"] == "production"

    bucket = items["s3_bucket"]
    assert bucket["external_id"] == "manually-created-bucket"
    assert bucket["ownership"] == "discovered_unmanaged"

    db = items["rds_instance"]
    assert db["external_id"] == "boss-db"
    assert db["state"] == "active"
    assert db["environment"] == "staging"


def test_aws_billing_sync_populates_cost_ledger(client, monkeypatch):
    empty_inventory_clients = {
        "ec2": FakeAwsClient({"describe_instances": {"Reservations": []}}),
        "s3": FakeAwsClient({"list_buckets": {"Buckets": []}}),
        "rds": FakeAwsClient({"describe_db_instances": {"DBInstances": []}}),
    }
    cost_explorer = FakeAwsClient(
        {
            "get_cost_and_usage": {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-07-01", "End": "2026-07-02"},
                        "Estimated": True,
                        "Groups": [
                            {
                                "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                                "Metrics": {
                                    "UnblendedCost": {
                                        "Amount": "12.3456789",
                                        "Unit": "USD",
                                    }
                                },
                            }
                        ],
                    }
                ]
            }
        }
    )
    install_fake_aws(
        monkeypatch, {**empty_inventory_clients, "ce": cost_explorer}
    )
    provider = create_aws_provider_account(
        client,
        "billing-aws",
        scopes=["billing:read"],
        read_only=True,
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
    sync_payload = synced.json()
    run = sync_payload["runs"][0]
    assert run["status"] == "succeeded"
    assert run["billing_status"] == "synced"
    assert sync_payload["costs_seen"] == 1
    assert cost_explorer.calls_for("get_cost_and_usage")

    costs = client.get("/v1/costs", params={"project_id": "boss-dogfood"})
    assert costs.status_code == 200
    cost_items = [
        item for item in costs.json()["items"] if item["provider"] == "aws"
    ]
    assert len(cost_items) == 1
    cost_event = cost_items[0]
    assert cost_event["amount_usd_micros"] == 12_345_679  # 12.3456789 USD, half-up
    assert cost_event["cost_layer"] == "observed"
    assert cost_event["confidence"] == "provider_reported"
    assert cost_event["source_ref"] == (
        "aws.cost_explorer:2026-07-01:Amazon Elastic Compute Cloud - Compute"
    )
    assert cost_event["billing_account_id"]

    # Second sync is idempotent for the same Cost Explorer bucket.
    second = client.post(
        "/v1/resources/sync",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "actor": "ceo",
        },
    )
    assert second.status_code == 200
    assert second.json()["costs_seen"] == 0


def test_aws_write_skill_rejected_on_read_only_account(client, monkeypatch):
    ec2 = FakeAwsClient()
    install_fake_aws(monkeypatch, {"ec2": ec2})
    provider = create_aws_provider_account(
        client,
        "readonly-aws",
        scopes=["read"],
        read_only=True,
    )

    blocked = client.post(
        "/v1/changesets",
        json={
            "project_id": "boss-dogfood",
            "provider_account_id": provider["id"],
            "title": "Create EC2 instance on read-only account",
            "summary": "Writes must be rejected on read-only provider accounts.",
            "expected_cost_usd_micros": 0,
            "rollback_plan": [],
            "operations": [
                {
                    "skill_id": "aws.ec2.instance.create",
                    "action": "create",
                    "resource_type": "ec2_instance",
                    "resource_ref": "boss-blocked-web",
                    "desired_state": {
                        "image_id": "ami-123",
                        "instance_type": "t3.micro",
                    },
                    "risk_tier": 3,
                }
            ],
            "actor": "ceo",
        },
    )
    assert blocked.status_code == 422
    assert "read-only" in blocked.json()["detail"].lower()
    assert ec2.calls == []
