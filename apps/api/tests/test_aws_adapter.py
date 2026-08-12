from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apps.api.app.config import Settings
from apps.api.app.services.resource_plane import (
    SUPPORTED_PROVIDERS,
    ProviderAdapterError,
    ProviderCredentialError,
    ResourcePlaneService,
)


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


def _service() -> ResourcePlaneService:
    settings = Settings(
        BOSS_AWS_ACCESS_KEY_ID="AKIATESTKEY",
        BOSS_AWS_REGION="us-east-1",
    )
    return ResourcePlaneService(settings)


def _install_fake_clients(
    monkeypatch,
    service: ResourcePlaneService,
    clients: dict[str, FakeAwsClient],
) -> None:
    def fake_aws_client(service_name: str, _credential: str):
        if service_name not in clients:
            raise AssertionError(f"Unexpected AWS client requested: {service_name}")
        return clients[service_name]

    monkeypatch.setattr(service, "_aws_client", fake_aws_client)


def test_aws_is_a_supported_provider_with_expected_capabilities():
    assert "aws" in SUPPORTED_PROVIDERS
    entry = SUPPORTED_PROVIDERS["aws"]
    assert entry["credential_ref"] == "env:BOSS_AWS_SECRET_ACCESS_KEY"
    caps = entry["capabilities"]
    for required in [
        "provider_connection_test",
        "resource_inventory_sync",
        "aws.ec2.instance.create",
        "aws.ec2.instance.destroy",
        "aws.s3.bucket.create",
        "aws.rds.instance.create",
        "aws.iam.role.upsert",
        "aws.route53.record.upsert",
        "aws.costs.sync",
        "aws.bedrock.converse",
        "aws.bedrock.list_models",
    ]:
        assert required in caps


def test_aws_supports_billing_sync():
    service = _service()
    assert service._provider_supports_billing("aws") is True


def test_aws_connection_test_returns_account_identity(monkeypatch):
    service = _service()
    sts = FakeAwsClient(
        {
            "get_caller_identity": {
                "UserId": "AIDATESTUSER",
                "Account": "123456789012",
                "Arn": "arn:aws:iam::123456789012:user/boss",
                "ResponseMetadata": {"HTTPStatusCode": 200},
            }
        }
    )
    _install_fake_clients(monkeypatch, service, {"sts": sts})

    result = service._test_connection("aws", "test-secret-key")

    assert result["provider"] == "aws"
    assert result["status_code"] == 200
    assert result["summary"] == {
        "account_id": "123456789012",
        "arn": "arn:aws:iam::123456789012:user/boss",
    }
    assert sts.calls_for("get_caller_identity") == [{}]


def test_aws_client_requires_access_key_id(monkeypatch):
    monkeypatch.delenv("BOSS_AWS_ACCESS_KEY_ID", raising=False)
    service = ResourcePlaneService(Settings(BOSS_AWS_ACCESS_KEY_ID=None))
    with pytest.raises(ProviderCredentialError):
        service._aws_client("sts", "test-secret-key")


def test_aws_inventory_maps_ec2_s3_and_rds_resources(monkeypatch):
    service = _service()
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
                                "VpcId": "vpc-1",
                                "SubnetId": "subnet-1",
                                "PrivateIpAddress": "10.0.0.5",
                                "PublicIpAddress": "54.1.2.3",
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
                        "Name": "boss-artifacts",
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
                        "EngineVersion": "16.3",
                        "DBInstanceClass": "db.t3.micro",
                        "AllocatedStorage": 20,
                        "MultiAZ": False,
                        "Endpoint": {"Address": "boss-db.abc.rds.amazonaws.com"},
                        "TagList": [{"Key": "env", "Value": "staging"}],
                    }
                ]
            }
        }
    )
    _install_fake_clients(monkeypatch, service, {"ec2": ec2, "s3": s3, "rds": rds})

    resources = service._sync_inventory("aws", "test-secret-key")

    assert [r.resource_type for r in resources] == [
        "ec2_instance",
        "s3_bucket",
        "rds_instance",
    ]

    instance = resources[0]
    assert instance.provider == "aws"
    assert instance.external_id == "i-0abc1234567890def"
    assert instance.name == "web-1"
    assert instance.state == "active"
    assert instance.health_status == "running"
    assert instance.ownership == "managed_by_boss"
    assert instance.environment == "production"
    assert "env:production" in instance.tags
    assert "boss-managed" in instance.tags
    assert instance.metadata["region"] == "us-east-1"
    assert instance.metadata["instance_type"] == "t3.micro"
    assert instance.metadata["availability_zone"] == "us-east-1a"

    bucket = resources[1]
    assert bucket.external_id == "boss-artifacts"
    assert bucket.name == "boss-artifacts"
    assert bucket.state == "active"
    assert bucket.ownership == "discovered_unmanaged"
    assert bucket.metadata["region"] == "us-east-1"
    assert bucket.metadata["creation_date"] == "2026-01-02T03:04:05+00:00"

    db_instance = resources[2]
    assert db_instance.external_id == "boss-db"
    assert db_instance.state == "active"
    assert db_instance.health_status == "available"
    assert db_instance.environment == "staging"
    assert db_instance.metadata["engine"] == "postgres"
    assert db_instance.metadata["allocated_storage_gb"] == 20
    assert db_instance.metadata["endpoint"] == "boss-db.abc.rds.amazonaws.com"


def test_aws_cost_records_map_cost_explorer_groups_to_usd_micros(monkeypatch):
    service = _service()
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
                            },
                            {
                                "Keys": ["AWS Lambda"],
                                "Metrics": {
                                    "UnblendedCost": {"Amount": "0", "Unit": "USD"}
                                },
                            },
                        ],
                    }
                ]
            }
        }
    )
    _install_fake_clients(monkeypatch, service, {"ce": cost_explorer})

    records = service._sync_billing("aws", "test-secret-key")

    assert len(records) == 1
    record = records[0]
    assert record.provider == "aws"
    assert record.category == "Amazon Elastic Compute Cloud - Compute"
    assert record.amount_usd_micros == 12_345_679  # 12.3456789 USD, half-up
    assert record.cost_layer == "observed"
    assert record.confidence == "provider_reported"
    assert record.source_ref == (
        "aws.cost_explorer:2026-07-01:Amazon Elastic Compute Cloud - Compute"
    )
    assert record.period_start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert record.period_end == datetime(2026, 7, 2, tzinfo=timezone.utc)
    assert record.billing_account_external_id == "aws:cost_explorer"
    assert record.metadata["provider_source"] == "cost_explorer"
    assert record.metadata["unit"] == "USD"

    request = cost_explorer.calls_for("get_cost_and_usage")[0]
    assert request["Granularity"] == "DAILY"
    assert request["Metrics"] == ["UnblendedCost"]
    assert request["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]


def test_aws_ec2_instance_create_returns_standard_result(monkeypatch):
    service = _service()
    ec2 = FakeAwsClient(
        {
            "run_instances": {
                "Instances": [
                    {
                        "InstanceId": "i-0abc1234567890def",
                        "State": {"Name": "pending"},
                        "Placement": {"AvailabilityZone": "us-east-1a"},
                    }
                ]
            }
        }
    )
    _install_fake_clients(monkeypatch, service, {"ec2": ec2})

    operation = {
        "id": "op_test1234",
        "resource_ref": None,
        "desired_state": {
            "image_id": "ami-123",
            "instance_type": "t3.micro",
            "name": "web-1",
            "subnet_id": "subnet-1",
            "key_name": "boss-key",
            "tags": ["env:staging"],
        },
    }
    result = service._aws_ec2_instance_create("test-secret-key", operation)

    assert result["provider_ref"] == "aws:ec2_instance:i-0abc1234567890def"
    assert result["resource_external_id"] == "i-0abc1234567890def"
    assert result["resource_name"] == "web-1"
    assert result["resource_type"] == "ec2_instance"
    assert result["state"] == "degraded"  # pending is not yet active
    assert result["health_status"] == "pending"
    assert result["operation"] == "created"
    assert result["region"] == "us-east-1"

    request = ec2.calls_for("run_instances")[0]
    assert request["ImageId"] == "ami-123"
    assert request["InstanceType"] == "t3.micro"
    assert request["SubnetId"] == "subnet-1"
    assert request["KeyName"] == "boss-key"
    assert request["MinCount"] == 1 and request["MaxCount"] == 1
    tags = request["TagSpecifications"][0]["Tags"]
    assert {"Key": "Name", "Value": "web-1"} in tags
    assert {"Key": "boss-managed", "Value": "true"} in tags
    assert {"Key": "env", "Value": "staging"} in tags


def test_aws_ec2_instance_create_requires_image_id_and_instance_type(monkeypatch):
    service = _service()
    _install_fake_clients(monkeypatch, service, {"ec2": FakeAwsClient()})
    operation = {
        "id": "op_test1234",
        "resource_ref": None,
        "desired_state": {"instance_type": "t3.micro"},
    }
    with pytest.raises(ValueError, match="image_id"):
        service._aws_ec2_instance_create("test-secret-key", operation)

    operation["desired_state"] = {"image_id": "ami-123"}
    with pytest.raises(ValueError, match="instance_type"):
        service._aws_ec2_instance_create("test-secret-key", operation)


def test_aws_ec2_instance_destroy_terminates_instance(monkeypatch):
    service = _service()
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
    _install_fake_clients(monkeypatch, service, {"ec2": ec2})

    operation = {
        "id": "op_test1234",
        "resource_ref": "i-0abc1234567890def",
        "desired_state": {},
    }
    result = service._aws_ec2_instance_destroy("test-secret-key", operation)

    assert result["provider_ref"] == "aws:ec2_instance:i-0abc1234567890def"
    assert result["state"] == "deleted"
    assert result["operation"] == "destroyed"
    assert result["health_status"] == "shutting-down"
    assert ec2.calls_for("terminate_instances") == [
        {"InstanceIds": ["i-0abc1234567890def"]}
    ]


def test_aws_s3_bucket_create_is_region_aware(monkeypatch):
    service = ResourcePlaneService(
        Settings(BOSS_AWS_ACCESS_KEY_ID="AKIATESTKEY", BOSS_AWS_REGION="eu-west-1")
    )
    s3 = FakeAwsClient({"create_bucket": {}})
    _install_fake_clients(monkeypatch, service, {"s3": s3})

    operation = {
        "id": "op_test1234",
        "resource_ref": None,
        "desired_state": {"bucket_name": "boss-artifacts", "versioning": True},
    }
    result = service._aws_s3_bucket_create("test-secret-key", operation)

    assert result["provider_ref"] == "aws:s3_bucket:boss-artifacts"
    assert result["resource_type"] == "s3_bucket"
    assert result["operation"] == "created"
    assert result["region"] == "eu-west-1"
    assert result["versioning_enabled"] is True

    create_request = s3.calls_for("create_bucket")[0]
    assert create_request["Bucket"] == "boss-artifacts"
    assert create_request["CreateBucketConfiguration"] == {
        "LocationConstraint": "eu-west-1"
    }
    versioning_request = s3.calls_for("put_bucket_versioning")[0]
    assert versioning_request["VersioningConfiguration"] == {"Status": "Enabled"}


def test_aws_rds_instance_create_rejects_literal_master_password(monkeypatch):
    service = _service()
    _install_fake_clients(monkeypatch, service, {"rds": FakeAwsClient()})
    operation = {
        "id": "op_test1234",
        "resource_ref": None,
        "desired_state": {
            "db_instance_identifier": "boss-db",
            "engine": "postgres",
            "db_instance_class": "db.t3.micro",
            "allocated_storage_gb": 20,
            "master_username": "boss",
            "master_password": "super-secret-literal",
        },
    }
    with pytest.raises(ProviderAdapterError, match="literal master"):
        service._aws_rds_instance_create("test-secret-key", operation)


def test_aws_rds_instance_create_resolves_password_secret_ref(monkeypatch):
    service = _service()
    rds = FakeAwsClient(
        {
            "create_db_instance": {
                "DBInstance": {
                    "DBInstanceIdentifier": "boss-db",
                    "DBInstanceStatus": "creating",
                }
            }
        }
    )
    _install_fake_clients(monkeypatch, service, {"rds": rds})
    monkeypatch.setenv("BOSS_TEST_RDS_MASTER_PASSWORD", "resolved-secret")

    operation = {
        "id": "op_test1234",
        "resource_ref": None,
        "desired_state": {
            "db_instance_identifier": "boss-db",
            "engine": "postgres",
            "db_instance_class": "db.t3.micro",
            "allocated_storage_gb": 20,
            "master_username": "boss",
            "master_password_secret_ref": "env:BOSS_TEST_RDS_MASTER_PASSWORD",
        },
    }
    result = service._aws_rds_instance_create("test-secret-key", operation)

    assert result["provider_ref"] == "aws:rds_instance:boss-db"
    assert result["resource_type"] == "rds_instance"
    assert result["operation"] == "created"
    assert result["health_status"] == "creating"

    request = rds.calls_for("create_db_instance")[0]
    assert request["MasterUserPassword"] == "resolved-secret"
    assert request["Engine"] == "postgres"
    assert request["AllocatedStorage"] == 20


def test_aws_iam_role_upsert_creates_and_attaches_policies(monkeypatch):
    service = _service()

    class NoSuchEntity(Exception):
        def __init__(self):
            super().__init__("NoSuchEntity")
            self.response = {"Error": {"Code": "NoSuchEntity"}}

    iam = FakeAwsClient(
        {
            "get_role": NoSuchEntity(),
            "create_role": {
                "Role": {
                    "RoleId": "AROATESTROLE",
                    "Arn": "arn:aws:iam::123456789012:role/boss-role",
                }
            },
        }
    )
    _install_fake_clients(monkeypatch, service, {"iam": iam})

    operation = {
        "id": "op_test1234",
        "resource_ref": None,
        "desired_state": {
            "role_name": "boss-role",
            "assume_role_policy_document": {
                "Version": "2012-10-17",
                "Statement": [],
            },
            "attach_policy_arns": [
                "arn:aws:iam::aws:policy/ReadOnlyAccess",
            ],
        },
    }
    result = service._aws_iam_role_upsert("test-secret-key", operation)

    assert result["provider_ref"] == "aws:iam_role:boss-role"
    assert result["resource_external_id"] == "AROATESTROLE"
    assert result["operation"] == "created"
    assert result["role_arn"] == "arn:aws:iam::123456789012:role/boss-role"
    assert result["attached_policy_arns"] == [
        "arn:aws:iam::aws:policy/ReadOnlyAccess"
    ]
    attach_request = iam.calls_for("attach_role_policy")[0]
    assert attach_request["RoleName"] == "boss-role"


def test_aws_route53_record_upsert_returns_standard_result(monkeypatch):
    service = _service()
    route53 = FakeAwsClient(
        {
            "change_resource_record_sets": {
                "ChangeInfo": {"Id": "/change/C123", "Status": "PENDING"}
            }
        }
    )
    _install_fake_clients(monkeypatch, service, {"route53": route53})

    operation = {
        "id": "op_test1234",
        "resource_ref": None,
        "desired_state": {
            "hosted_zone_id": "Z123",
            "record_type": "a",
            "name": "app.example.com",
            "content": "203.0.113.10",
            "ttl": 60,
        },
    }
    result = service._aws_route53_record_upsert("test-secret-key", operation)

    assert result["provider_ref"] == "aws:route53_record:Z123:A:app.example.com"
    assert result["resource_type"] == "route53_record"
    assert result["operation"] == "updated"
    assert result["change_id"] == "/change/C123"

    request = route53.calls_for("change_resource_record_sets")[0]
    change = request["ChangeBatch"]["Changes"][0]
    assert change["Action"] == "UPSERT"
    assert change["ResourceRecordSet"]["Type"] == "A"
    assert change["ResourceRecordSet"]["TTL"] == 60
    assert change["ResourceRecordSet"]["ResourceRecords"] == [
        {"Value": "203.0.113.10"}
    ]


def test_dispatch_rejects_unknown_aws_skill(monkeypatch):
    service = _service()
    monkeypatch.setenv("BOSS_AWS_SECRET_ACCESS_KEY", "test-secret-key")
    account = {
        "provider": "aws",
        "read_only": False,
        "destructive_actions_disabled": False,
        "credential_ref": "env:BOSS_AWS_SECRET_ACCESS_KEY",
    }
    operation = {
        "id": "op_test1234",
        "skill_id": "aws.unknown.thing",
        "action": "apply",
        "destructive": False,
        "desired_state": {},
        "resource_ref": None,
    }
    with pytest.raises(ProviderAdapterError, match="not enabled"):
        service._execute_provider_operation(None, account, operation)
