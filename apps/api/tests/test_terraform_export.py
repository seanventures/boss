"""Terraform/OpenTofu exporter tests (Track 1 export, BOSS-66).

Two layers, mirroring test_aws_changeset_flow.py's hermetic scaffolding:

1. Pure-projection tests drive ``changeset_to_tf`` directly — no DB, no network.
2. API tests drive POST/GET /v1/changesets/{id}/terraform-export through the
   real ChangeSet flow (with ``_aws_client`` faked, as in the BOSS-65 tests).

THE INVARIANT under test: the BOSS DB is canonical, the .tf.json is a
deterministic downstream projection, and no literal secret ever appears in it.
"""

import copy

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
