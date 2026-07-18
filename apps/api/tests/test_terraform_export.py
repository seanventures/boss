"""Terraform/OpenTofu exporter tests (Track 1 export, BOSS-66).

Two layers, mirroring test_aws_changeset_flow.py's hermetic scaffolding:

1. Pure-projection tests drive ``changeset_to_tf`` directly — no DB, no network.
2. API tests drive POST/GET /v1/changesets/{id}/terraform-export through the
   real ChangeSet flow (with ``_aws_client`` faked, as in the BOSS-65 tests).

THE INVARIANT under test: the BOSS DB is canonical, the .tf.json is a
deterministic downstream projection, and no literal secret ever appears in it.
"""

from __future__ import annotations

import copy
import shutil
import subprocess

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.services.terraform_export import (
    RESOURCE_TYPE_MAP,
    canonical_tf_json,
    changeset_to_tf,
    content_hash_for,
    import_id_from_receipt,
)

CHANGESET = {"id": "chg-test", "project_id": "boss-dogfood", "status": "applied"}


def _op(index, skill_id, resource_type, resource_ref, desired, action="create"):
    return {
        "id": f"chgop-{index}",
        "operation_index": index,
        "skill_id": skill_id,
        "action": action,
        "resource_type": resource_type,
        "resource_ref": resource_ref,
        "desired_state": desired,
    }


MIXED_OPERATIONS = [
    _op(
        0,
        "aws.ec2.instance.create",
        "ec2_instance",
        "boss-web",
        {
            "image_id": "ami-123",
            "instance_type": "t3.micro",
            "name": "boss-web",
            "subnet_id": "subnet-1",
            "tags": ["env:preview", "team:platform"],
        },
    ),
    _op(
        1,
        "aws.s3.bucket.create",
        "s3_bucket",
        "boss-artifacts",
        {"bucket_name": "boss-artifacts", "versioning": True, "encryption": "kms"},
    ),
    _op(
        2,
        "digitalocean.droplet.create",
        "droplet",
        "boss-worker",
        {
            "name": "boss-worker",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "image": "ubuntu-22-04-x64",
            "tags": ["worker"],
        },
    ),
]


# --------------------------------------------------------------------------- #
# Pure projection (no DB)                                                     #
# --------------------------------------------------------------------------- #


def test_mixed_do_aws_changeset_projects_to_structured_tf_json():
    export = changeset_to_tf(CHANGESET, MIXED_OPERATIONS, mappings={})
    tf = export.tf_json

    # terraform block: only the providers actually present, pinned.
    required = tf["terraform"]["required_providers"]
    assert set(required) == {"aws", "digitalocean"}
    assert required["aws"] == {"source": "hashicorp/aws", "version": "~> 5.0"}
    assert required["digitalocean"] == {
        "source": "digitalocean/digitalocean",
        "version": "~> 2.0",
    }
    assert "cloudflare" not in required

    # Provider credentials are var references, annotated with the SecretRef.
    assert tf["provider"]["aws"]["secret_key"] == "${var.aws_secret_access_key}"
    assert tf["provider"]["digitalocean"]["token"] == "${var.digitalocean_token}"
    secret_var = tf["variable"]["aws_secret_access_key"]
    assert secret_var["sensitive"] is True
    assert secret_var["//"]["boss:credential_ref"] == "env:BOSS_AWS_SECRET_ACCESS_KEY"
    token_var = tf["variable"]["digitalocean_token"]
    assert token_var["sensitive"] is True
    assert token_var["//"]["boss:credential_ref"] == "env:BOSS_DIGITALOCEAN_TOKEN"

    # Field translation: image_id -> ami, tags list -> map (boss tags reserved).
    instance = tf["resource"]["aws_instance"]["boss-web"]
    assert instance["ami"] == "ami-123"
    assert instance["instance_type"] == "t3.micro"
    assert instance["subnet_id"] == "subnet-1"
    assert instance["tags"] == {
        "Name": "boss-web",
        "boss-managed": "true",
        "boss-providerops": "true",
        "env": "preview",
        "team": "platform",
    }

    bucket = tf["resource"]["aws_s3_bucket"]["boss-artifacts"]
    assert bucket["bucket"] == "boss-artifacts"
    assert bucket["versioning"] == {"enabled": True}
    assert (
        bucket["server_side_encryption_configuration"]["rule"][
            "apply_server_side_encryption_by_default"
        ]["sse_algorithm"]
        == "aws:kms"
    )

    droplet = tf["resource"]["digitalocean_droplet"]["boss-worker"]
    assert droplet["region"] == "nyc3"
    assert droplet["size"] == "s-1vcpu-1gb"
    assert droplet["image"] == "ubuntu-22-04-x64"
    assert droplet["tags"] == ["boss-managed", "boss-providerops", "worker"]

    assert export.warnings == []
    assert export.providers == ["aws", "digitalocean"]
    assert [entry["tf_address"] for entry in export.mapping] == [
        "aws_instance.boss-web",
        "aws_s3_bucket.boss-artifacts",
        "digitalocean_droplet.boss-worker",
    ]
    # No import blocks without succeeded receipts.
    assert "import" not in tf


def test_projection_is_deterministic_and_byte_stable():
    first = changeset_to_tf(
        CHANGESET, copy.deepcopy(MIXED_OPERATIONS), mappings={}
    )
    second = changeset_to_tf(
        CHANGESET, copy.deepcopy(MIXED_OPERATIONS), mappings={}
    )
    first_bytes = canonical_tf_json(first.tf_json)
    second_bytes = canonical_tf_json(second.tf_json)
    assert first_bytes == second_bytes
    assert content_hash_for(first_bytes) == content_hash_for(second_bytes)


def test_stored_mappings_pin_addresses_and_collisions_suffix_deterministically():
    # A stored mapping wins even over the naturally generated name.
    pinned = changeset_to_tf(
        CHANGESET,
        MIXED_OPERATIONS,
        mappings={"boss-web": "aws_instance.legacy_web_name"},
    )
    assert "legacy_web_name" in pinned.tf_json["resource"]["aws_instance"]
    entry = pinned.mapping[0]
    assert entry["tf_address"] == "aws_instance.legacy_web_name"
    assert entry["reused"] is True

    # Distinct refs sanitizing to the same name are suffixed in index order.
    colliding = [
        _op(
            0,
            "aws.s3.bucket.create",
            "s3_bucket",
            "boss.data",
            {"bucket_name": "boss-data-a"},
        ),
        _op(
            1,
            "aws.s3.bucket.create",
            "s3_bucket",
            "boss_data",
            {"bucket_name": "boss-data-b"},
        ),
        _op(
            2,
            "aws.s3.bucket.create",
            "s3_bucket",
            "boss:data",
            {"bucket_name": "boss-data-c"},
        ),
    ]
    export = changeset_to_tf(CHANGESET, colliding, mappings={})
    assert [entry["tf_address"] for entry in export.mapping] == [
        "aws_s3_bucket.boss_data",
        "aws_s3_bucket.boss_data_2",
        "aws_s3_bucket.boss_data_3",
    ]


def test_rds_master_password_is_sensitive_var_reference_never_literal():
    secret_value = "sup3r-secret-rds-value"
    operations = [
        _op(
            0,
            "aws.rds.instance.create",
            "rds_instance",
            "boss-db",
            {
                "db_instance_identifier": "boss-db",
                "engine": "postgres",
                "db_instance_class": "db.t3.micro",
                "allocated_storage_gb": 20,
                "master_username": "boss",
                "master_password_secret_ref": "env:BOSS_TEST_RDS_MASTER_PASSWORD",
            },
        )
    ]
    export = changeset_to_tf(CHANGESET, operations, mappings={})
    db = export.tf_json["resource"]["aws_db_instance"]["boss-db"]
    assert db["password"] == "${var.boss_test_rds_master_password}"
    assert db["username"] == "boss"
    assert db["allocated_storage"] == 20
    variable = export.tf_json["variable"]["boss_test_rds_master_password"]
    assert variable["sensitive"] is True
    assert (
        variable["//"]["boss:credential_ref"] == "env:BOSS_TEST_RDS_MASTER_PASSWORD"
    )
    canonical = canonical_tf_json(export.tf_json)
    assert secret_value not in canonical
    assert "BOSS_TEST_RDS_MASTER_PASSWORD" in canonical  # pointer, not the value


def test_import_blocks_generated_only_for_succeeded_receipts():
    receipts = {
        0: {
            "status": "succeeded",
            "provider_ref": "aws:ec2_instance:i-0abc1234567890def",
            "result": {"resource_external_id": "i-0abc1234567890def"},
        },
        # op 1 has no receipt; op 2 succeeded via a DO api_url provider_ref.
        2: {
            "status": "succeeded",
            "provider_ref": "https://api.digitalocean.com/v2/droplets/12345",
            "result": {"resource_external_id": "12345"},
        },
    }
    export = changeset_to_tf(
        CHANGESET,
        MIXED_OPERATIONS,
        mappings={},
        receipts_by_operation_index=receipts,
    )
    assert export.tf_json["import"] == [
        {"id": "i-0abc1234567890def", "to": "aws_instance.boss-web"},
        {"id": "12345", "to": "digitalocean_droplet.boss-worker"},
    ]


def test_route53_import_id_uses_terraform_zone_name_type_format():
    receipt = {
        "status": "succeeded",
        "provider_ref": "aws:route53_record:Z0123456789:A:app.example.com",
        "result": {},
    }
    assert (
        import_id_from_receipt("aws_route53_record", receipt)
        == "Z0123456789_app.example.com_A"
    )


def test_unmapped_resource_type_warns_but_export_succeeds():
    operations = [
        _op(
            0,
            "supabase.migration.apply",
            "migration",
            "supabase-mig-1",
            {"project_ref": "abc", "sql": "SELECT 1"},
        ),
        MIXED_OPERATIONS[1],
    ]
    export = changeset_to_tf(CHANGESET, operations, mappings={})
    assert "aws_s3_bucket" in export.tf_json["resource"]
    assert len(export.warnings) == 1
    warning = export.warnings[0]
    assert warning["kind"] == "unmapped_resource_type"
    assert warning["resource_type"] == "migration"
    assert warning["skill_id"] == "supabase.migration.apply"
    # The unmapped op never invents a resource or a provider block.
    assert set(export.tf_json["terraform"]["required_providers"]) == {"aws"}


def test_destroy_operations_project_as_absence():
    operations = [
        _op(
            0,
            "aws.ec2.instance.destroy",
            "ec2_instance",
            "i-dead",
            {"instance_id": "i-dead"},
            action="destroy",
        ),
        MIXED_OPERATIONS[1],
    ]
    export = changeset_to_tf(CHANGESET, operations, mappings={})
    assert "aws_instance" not in export.tf_json.get("resource", {})
    assert any(w["kind"] == "destroy_not_projected" for w in export.warnings)


def test_resource_type_map_covers_ticket_surface():
    assert RESOURCE_TYPE_MAP[("aws", "ec2_instance")] == "aws_instance"
    assert RESOURCE_TYPE_MAP[("aws", "s3_bucket")] == "aws_s3_bucket"
    assert RESOURCE_TYPE_MAP[("aws", "rds_instance")] == "aws_db_instance"
    assert RESOURCE_TYPE_MAP[("aws", "iam_role")] == "aws_iam_role"
    assert RESOURCE_TYPE_MAP[("aws", "route53_record")] == "aws_route53_record"
    assert RESOURCE_TYPE_MAP[("digitalocean", "droplet")] == "digitalocean_droplet"
    assert RESOURCE_TYPE_MAP[("digitalocean", "app")] == "digitalocean_app"
    assert RESOURCE_TYPE_MAP[("cloudflare", "dns_record")] == "cloudflare_record"


# --------------------------------------------------------------------------- #
# API integration (hermetic: fake AWS client seam, boss_test DB)              #
# --------------------------------------------------------------------------- #

PROJECT_ID = "boss-dogfood"


class FakeAwsClient:
    """Records calls and replays canned responses (as in test_aws_changeset_flow)."""

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


def install_fake_aws(monkeypatch, clients: dict[str, FakeAwsClient]) -> None:
    monkeypatch.setenv("BOSS_AWS_SECRET_ACCESS_KEY", "test-aws-secret-key")

    def fake_aws_client(service_name: str, _credential: str):
        if service_name not in clients:
            raise AssertionError(f"Unexpected AWS client requested: {service_name}")
        return clients[service_name]

    monkeypatch.setattr(api_main.resource_plane, "_aws_client", fake_aws_client)


def enable_terraform_export(client, engine, app_name: str = "boss-app") -> str:
    """Create a deployment target and flip its opt-in terraform_export switch."""
    response = client.post(
        "/v1/deployments",
        json={
            "project_id": PROJECT_ID,
            "app_name": app_name,
            "repo": "paramg/boss-app",
            "environment": "staging",
            "target_kind": "ssh_host",
            "target_ref": "root@staging-host",
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    target_id = response.json()["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE deployment_targets SET terraform_export = true "
                "WHERE id = :id"
            ),
            {"id": target_id},
        )
    return target_id


def create_changeset_via_api(client, operations, title="Terraform export changeset"):
    response = client.post(
        "/v1/changesets",
        json={
            "project_id": PROJECT_ID,
            "provider_account_id": None,
            "title": title,
            "summary": "ChangeSet used by terraform export tests.",
            "expected_cost_usd_micros": 0,
            "rollback_plan": ["Remove the exported resources via a cleanup ChangeSet."],
            "operations": operations,
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


API_MIXED_OPERATIONS = [
    {
        "skill_id": "aws.ec2.instance.create",
        "action": "create",
        "resource_type": "ec2_instance",
        "resource_ref": "boss-web",
        "desired_state": {
            "image_id": "ami-123",
            "instance_type": "t3.micro",
            "name": "boss-web",
            "tags": ["env:preview"],
        },
        "risk_tier": 2,
    },
    {
        "skill_id": "digitalocean.droplet.create",
        "action": "create",
        "resource_type": "droplet",
        "resource_ref": "boss-worker",
        "desired_state": {
            "name": "boss-worker",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "image": "ubuntu-22-04-x64",
            "tags": ["worker"],
        },
        "risk_tier": 2,
    },
]


def test_export_endpoint_is_opt_in_and_force_is_audited(client, engine):
    changeset = create_changeset_via_api(client, API_MIXED_OPERATIONS)

    # Default posture: no target opted in -> export refused, nothing stored.
    refused = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert refused.status_code == 409
    assert "opt-in" in refused.json()["detail"]
    assert (
        client.get(f"/v1/changesets/{changeset['id']}/terraform-export").status_code
        == 404
    )

    # force=true is the audited escape hatch: marked in response + event log.
    forced = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo", "force": True},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["forced"] is True
    assert forced.json()["opt_in_targets"] == 0
    with engine.begin() as connection:
        audit = connection.execute(
            text(
                "SELECT COUNT(*) AS n FROM events "
                "WHERE event_type = 'terraform_export.forced' "
                "AND entity_id = :changeset_id"
            ),
            {"changeset_id": changeset["id"]},
        ).mappings().one()
    assert audit["n"] == 1

    # After a target opts in, plain export succeeds and is not marked forced.
    enable_terraform_export(client, engine)
    allowed = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["forced"] is False
    assert allowed.json()["opt_in_targets"] == 1


def test_export_endpoint_projects_mixed_changeset_and_stores_artifact(
    client, engine
):
    enable_terraform_export(client, engine)
    changeset = create_changeset_via_api(client, API_MIXED_OPERATIONS)

    exported = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert exported.status_code == 200, exported.text
    payload = exported.json()
    tf = payload["tf_json"]
    assert set(tf["terraform"]["required_providers"]) == {"aws", "digitalocean"}
    assert tf["resource"]["aws_instance"]["boss-web"]["ami"] == "ami-123"
    assert tf["resource"]["digitalocean_droplet"]["boss-worker"]["region"] == "nyc3"
    assert payload["warnings"] == []
    assert payload["artifact_id"].startswith("art")

    # GET returns the stored artifact with the byte-exact canonical projection.
    fetched = client.get(f"/v1/changesets/{changeset['id']}/terraform-export")
    assert fetched.status_code == 200
    artifact = fetched.json()
    assert artifact["artifact_id"] == payload["artifact_id"]
    assert artifact["content_hash"] == payload["content_hash"]
    assert artifact["export"]["tf_json_canonical"] == payload["canonical"]
    assert artifact["export"]["source_of_truth"] == "boss_database"

    # Mapping rows were pinned for every resource_ref.
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT resource_ref, tf_address, provider, artifact_id "
                "FROM terraform_exports WHERE project_id = :project_id "
                "ORDER BY resource_ref"
            ),
            {"project_id": PROJECT_ID},
        ).mappings().all()
    assert [(row["resource_ref"], row["tf_address"]) for row in rows] == [
        ("boss-web", "aws_instance.boss-web"),
        ("boss-worker", "digitalocean_droplet.boss-worker"),
    ]
    assert all(row["artifact_id"] == payload["artifact_id"] for row in rows)


def test_reexport_is_byte_stable_and_reuses_pinned_addresses(client, engine):
    enable_terraform_export(client, engine)
    changeset = create_changeset_via_api(client, API_MIXED_OPERATIONS)

    first = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert second.status_code == 200, second.text

    # Byte-identical projection: same hash AND same serialized bytes.
    assert first.json()["content_hash"] == second.json()["content_hash"]
    assert first.json()["canonical"] == second.json()["canonical"]
    assert first.json()["tf_json"] == second.json()["tf_json"]

    # Second export reused every pinned address and mutated nothing.
    assert all(entry["reused"] for entry in second.json()["mapping"])
    assert [e["tf_address"] for e in first.json()["mapping"]] == [
        e["tf_address"] for e in second.json()["mapping"]
    ]
    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT COUNT(*) AS n FROM terraform_exports "
                "WHERE project_id = :project_id"
            ),
            {"project_id": PROJECT_ID},
        ).mappings().one()
    assert count["n"] == 2


def test_export_never_contains_secret_literals(client, engine, monkeypatch):
    monkeypatch.setenv("BOSS_AWS_SECRET_ACCESS_KEY", "test-aws-secret-key-value")
    monkeypatch.setenv("BOSS_AWS_ACCESS_KEY_ID", "AKIAFAKEACCESSKEYID")
    monkeypatch.setenv("BOSS_TEST_RDS_MASTER_PASSWORD", "sup3r-secret-rds-value")
    enable_terraform_export(client, engine)
    changeset = create_changeset_via_api(
        client,
        [
            {
                "skill_id": "aws.rds.instance.create",
                "action": "create",
                "resource_type": "rds_instance",
                "resource_ref": "boss-db",
                "desired_state": {
                    "db_instance_identifier": "boss-db",
                    "engine": "postgres",
                    "db_instance_class": "db.t3.micro",
                    "allocated_storage_gb": 20,
                    "master_username": "boss",
                    "master_password_secret_ref": (
                        "env:BOSS_TEST_RDS_MASTER_PASSWORD"
                    ),
                },
                "risk_tier": 2,
            }
        ],
        title="RDS changeset with secret refs",
    )

    exported = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert exported.status_code == 200, exported.text
    payload = exported.json()

    # The FULL serialized export (canonical bytes and the whole response body)
    # contains no resolved secret value and no provider credential value.
    serialized = exported.text
    for secret in (
        "sup3r-secret-rds-value",
        "test-aws-secret-key-value",
        "AKIAFAKEACCESSKEYID",
    ):
        assert secret not in serialized
        assert secret not in payload["canonical"]

    db = payload["tf_json"]["resource"]["aws_db_instance"]["boss-db"]
    assert db["password"] == "${var.boss_test_rds_master_password}"
    variable = payload["tf_json"]["variable"]["boss_test_rds_master_password"]
    assert variable["sensitive"] is True
    assert (
        variable["//"]["boss:credential_ref"]
        == "env:BOSS_TEST_RDS_MASTER_PASSWORD"
    )


def test_import_blocks_appear_exactly_for_succeeded_receipts(
    client, engine, monkeypatch
):
    ec2 = FakeAwsClient(
        {
            "run_instances": {
                "Instances": [
                    {
                        "InstanceId": "i-0abc1234567890def",
                        "State": {"Name": "running"},
                        "Placement": {"AvailabilityZone": "us-east-1a"},
                    }
                ]
            }
        }
    )
    install_fake_aws(monkeypatch, {"ec2": ec2})
    enable_terraform_export(client, engine)

    provider = client.post(
        "/v1/providers",
        json={
            "project_id": PROJECT_ID,
            "provider": "aws",
            "account_label": "export-aws",
            "scopes": ["ec2:write"],
            "read_only": False,
            "destructive_actions_disabled": False,
            "actor": "ceo",
        },
    )
    assert provider.status_code == 201, provider.text
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": PROJECT_ID,
            "category": "infra",
            "amount_usd_micros": 100_000,
            "reason": "Reserve AWS EC2 ChangeSet budget for export test.",
        },
    )
    assert reserve.status_code == 201, reserve.text
    reservation_id = reserve.json()["reservation_id"]
    assert reservation_id

    applied_changeset = client.post(
        "/v1/changesets",
        json={
            "project_id": PROJECT_ID,
            "provider_account_id": provider.json()["id"],
            "title": "Create EC2 instance for export",
            "summary": "Applied ChangeSet whose receipt feeds import blocks.",
            "expected_cost_usd_micros": 100_000,
            "reservation_id": reservation_id,
            "rollback_plan": ["Terminate via cleanup ChangeSet."],
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
                    },
                    "risk_tier": 3,
                }
            ],
            "actor": "ceo",
        },
    ).json()
    client.post(
        f"/v1/changesets/{applied_changeset['id']}/approve", json={"actor": "ceo"}
    )
    client.post(
        f"/v1/changesets/{applied_changeset['id']}/approve", json={"actor": "cto"}
    )
    applied = client.post(
        f"/v1/changesets/{applied_changeset['id']}/apply", json={"actor": "ceo"}
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"

    exported = client.post(
        f"/v1/changesets/{applied_changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["tf_json"]["import"] == [
        {
            "id": "i-0abc1234567890def",
            "to": "aws_instance.boss-preview-web",
        }
    ]

    # A ChangeSet that was never applied has no receipts -> no import blocks.
    unapplied = create_changeset_via_api(client, API_MIXED_OPERATIONS)
    exported_unapplied = client.post(
        f"/v1/changesets/{unapplied['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert exported_unapplied.status_code == 200, exported_unapplied.text
    assert "import" not in exported_unapplied.json()["tf_json"]


def test_unmapped_operation_warns_via_api_but_export_succeeds(client, engine):
    enable_terraform_export(client, engine)
    changeset = create_changeset_via_api(
        client,
        [
            {
                "skill_id": "supabase.migration.apply",
                "action": "apply",
                "resource_type": "migration",
                "resource_ref": "supabase-mig-1",
                "desired_state": {
                    "project_ref": "abc",
                    "sql": "SELECT 1",
                    "environment": "preview",
                },
                "risk_tier": 2,
            },
            API_MIXED_OPERATIONS[0],
        ],
        title="Mixed supabase + aws changeset",
    )
    exported = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert exported.status_code == 200, exported.text
    payload = exported.json()
    assert "aws_instance" in payload["tf_json"]["resource"]
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0]["kind"] == "unmapped_resource_type"
    assert payload["warnings"][0]["resource_type"] == "migration"


TF_BINARY = shutil.which("tofu") or shutil.which("terraform")


@pytest.mark.skipif(TF_BINARY is None, reason="terraform/tofu not on PATH")
def test_exported_tf_json_passes_terraform_validate(client, engine, tmp_path):
    """Bonus: real `terraform init -backend=false` + `validate` on the export.

    Provider downloads need the network; an init failure (offline sandbox)
    skips rather than fails."""
    enable_terraform_export(client, engine)
    changeset = create_changeset_via_api(client, API_MIXED_OPERATIONS)
    exported = client.post(
        f"/v1/changesets/{changeset['id']}/terraform-export",
        json={"actor": "ceo"},
    )
    assert exported.status_code == 200, exported.text
    (tmp_path / "main.tf.json").write_text(
        exported.json()["canonical"], encoding="utf-8"
    )

    init = subprocess.run(
        [TF_BINARY, "init", "-backend=false", "-input=false", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if init.returncode != 0:
        pytest.skip(
            "terraform init could not fetch providers (offline?): "
            + (init.stderr or init.stdout)[:300]
        )
    validate = subprocess.run(
        [TF_BINARY, "validate", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert validate.returncode == 0, validate.stdout + validate.stderr
