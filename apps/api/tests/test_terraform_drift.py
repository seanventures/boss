"""Terraform drift-sync tests (Track 1 inbound, BOSS-69).

Two layers, mirroring test_terraform_export.py's hermetic scaffolding:

1. Pure tests drive ``parse_tf_input`` / ``reverse_field_map`` directly —
   no DB, no network.
2. API tests drive POST /v1/projects/{id}/terraform-drift-reconcile through
   the real ChangeSet + BOSS-66 export flow on the boss_test DB.

THE INVARIANT under test: a customer-supplied .tf NEVER mutates the DB
directly.  Divergence yields a drift report + a PROPOSED corrective ChangeSet
through the normal dual-control approval gate; rejecting the proposal leaves
desired state unchanged.
"""

import copy
import json

import pytest
from sqlalchemy import text

from apps.api.app.services.terraform_drift import (
    DRIFT_PROPOSAL_TITLE,
    TerraformDriftError,
    parse_tf_input,
    reverse_field_map,
)
from apps.api.app.services.terraform_export import _FIELD_TRANSLATORS

PROJECT_ID = "boss-dogfood"


# --------------------------------------------------------------------------- #
# Pure parsing + reverse translation (no DB)                                  #
# --------------------------------------------------------------------------- #


def test_parse_tf_input_rejects_unknown_payloads_with_clear_errors():
    for bad in (None, [], "", 42, {}, {"nonsense": 1, "stuff": 2}):
        with pytest.raises(TerraformDriftError) as excinfo:
            parse_tf_input(bad)
        message = str(excinfo.value)
        assert ".tf.json" in message or "Terraform input" in message

    with pytest.raises(TerraformDriftError):
        parse_tf_input({"resource": ["not", "a", "map"]})


def test_parse_tf_json_dialect_extracts_bodies_and_boss_annotations():
    parsed = parse_tf_input(
        {
            "terraform": {"required_version": ">= 1.5.0"},
            "provider": {"aws": {}},
            "resource": {
                "aws_instance": {
                    "boss-web": {
                        "ami": "ami-123",
                        "instance_type": "t3.micro",
                        "//": {"boss:resource_ref": "boss-web"},
                    }
                }
            },
            "data": {"aws_ami": {"latest": {}}},
            "module": {"vpc": {"source": "./vpc"}},
        }
    )
    assert parsed.input_format == "tf_json"
    assert set(parsed.resources) == {"aws_instance.boss-web"}
    resource = parsed.resources["aws_instance.boss-web"]
    assert resource.tf_type == "aws_instance"
    assert resource.boss_resource_ref == "boss-web"
    assert "//" not in resource.body
    kinds = {warning["kind"] for warning in parsed.warnings}
    assert kinds == {"data_sources_ignored", "modules_ignored"}


def test_parse_plan_json_extracts_root_module_and_ignores_data_and_children():
    parsed = parse_tf_input(
        {
            "format_version": "1.2",
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": "aws_instance.boss-web",
                            "mode": "managed",
                            "type": "aws_instance",
                            "name": "boss-web",
                            "values": {"ami": "ami-123", "instance_type": "t3.micro"},
                        },
                        {
                            "address": "data.aws_ami.latest",
                            "mode": "data",
                            "type": "aws_ami",
                            "name": "latest",
                            "values": {},
                        },
                    ],
                    "child_modules": [{"address": "module.vpc", "resources": []}],
                }
            },
        }
    )
    assert parsed.input_format == "plan_json"
    assert set(parsed.resources) == {"aws_instance.boss-web"}
    kinds = {warning["kind"] for warning in parsed.warnings}
    assert kinds == {"data_sources_ignored", "modules_ignored"}


REPRESENTATIVE_DESIRED = {
    "aws_instance": {
        "image_id": "ami-123",
        "instance_type": "t3.micro",
        "name": "boss-web",
        "subnet_id": "subnet-1",
        "key_name": "boss-key",
        "security_group_ids": ["sg-1", "sg-2"],
        "tags": ["env:preview", "team:platform"],
    },
    "aws_s3_bucket": {
        "bucket_name": "boss-artifacts",
        "versioning": True,
        "encryption": "kms",
    },
    "aws_db_instance": {
        "db_instance_identifier": "boss-db",
        "engine": "postgres",
        "db_instance_class": "db.t3.micro",
        "allocated_storage_gb": 20,
        "master_username": "boss",
        "master_password_secret_ref": "env:BOSS_TEST_RDS_MASTER_PASSWORD",
        "db_name": "boss",
        "multi_az": False,
        "publicly_accessible": False,
        "db_subnet_group_name": "boss-subnets",
        "vpc_security_group_ids": ["sg-9"],
    },
    "aws_iam_role": {
        "role_name": "boss-role",
        "assume_role_policy_document": {
            "Statement": [{"Action": "sts:AssumeRole", "Effect": "Allow"}],
            "Version": "2012-10-17",
        },
        "description": "Boss role",
        "attach_policy_arns": ["arn:aws:iam::aws:policy/ReadOnlyAccess"],
    },
    "aws_route53_record": {
        "hosted_zone_id": "Z0123",
        "name": "app.example.com",
        "record_type": "a",
        "ttl": 300,
        "values": ["203.0.113.7"],
    },
    "digitalocean_droplet": {
        "name": "boss-worker",
        "region": "nyc3",
        "size": "s-1vcpu-1gb",
        "image": "ubuntu-22-04-x64",
        "tags": ["worker"],
    },
    "digitalocean_app": {"spec": {"name": "boss-app", "region": "nyc"}},
    "cloudflare_record": {
        "zone_id": "cfzone",
        "name": "app",
        "record_type": "cname",
        "content": "target.example.com",
        "ttl": 1,
        "proxied": True,
    },
}


def test_reverse_field_map_inverts_every_exported_type():
    """Forward-project each representative desired_state, reverse it, and
    forward-project the reversed form again: the tf bodies must agree, and no
    exporter-emitted field may land in unrecognized_fields."""
    for tf_type, desired in REPRESENTATIVE_DESIRED.items():
        operation = {"resource_ref": "round-trip-ref"}
        provider = tf_type.split("_", 1)[0]
        forward_body, _ = _FIELD_TRANSLATORS[tf_type](desired, operation)
        reversed_desired, unrecognized = reverse_field_map(
            provider, tf_type, forward_body
        )
        assert unrecognized == {}, (tf_type, unrecognized)
        # Re-projecting the reversed desired_state reproduces the same body
        # (modulo the sensitive password var, which is never round-tripped).
        again_body, _ = _FIELD_TRANSLATORS[tf_type](reversed_desired, operation)
        forward = {k: v for k, v in forward_body.items() if k != "password"}
        again = {k: v for k, v in again_body.items() if k != "password"}
        assert again == forward, tf_type


def test_reverse_field_map_carries_unknown_fields_never_drops_them():
    desired, unrecognized = reverse_field_map(
        "aws",
        "aws_instance",
        {
            "ami": "ami-123",
            "instance_type": "t3.micro",
            "monitoring": True,
            "ebs_optimized": False,
        },
    )
    assert desired["image_id"] == "ami-123"
    assert unrecognized == {"monitoring": True, "ebs_optimized": False}

    # Sensitive tf fields are skipped, never surfaced as literals.
    desired, unrecognized = reverse_field_map(
        "aws",
        "aws_db_instance",
        {"identifier": "boss-db", "password": "literal-secret"},
    )
    assert desired == {"db_instance_identifier": "boss-db"}
    assert "password" not in unrecognized


# --------------------------------------------------------------------------- #
# API integration (hermetic, boss_test DB)                                    #
# --------------------------------------------------------------------------- #


def enable_terraform_export(client, engine, app_name: str = "boss-app") -> str:
    """Create a deployment target and flip its opt-in terraform_export switch."""
    response = client.post(
        "/v1/deployments",
        json={
            "project_id": PROJECT_ID,
            "app_name": app_name,
            "repo": "example-user/boss-app",
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


def disable_terraform_export(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE deployment_targets SET terraform_export = false "
                "WHERE project_id = :project_id"
            ),
            {"project_id": PROJECT_ID},
        )


def create_changeset_via_api(client, operations, title="Drift-sync seed changeset"):
    response = client.post(
        "/v1/changesets",
        json={
            "project_id": PROJECT_ID,
            "provider_account_id": None,
            "title": title,
            "summary": "ChangeSet used by terraform drift-sync tests.",
            "expected_cost_usd_micros": 0,
            "rollback_plan": ["Remove the resources via a cleanup ChangeSet."],
            "operations": operations,
            "actor": "ceo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def export_changeset(client, changeset_id):
    response = client.post(
        f"/v1/changesets/{changeset_id}/terraform-export",
        json={"actor": "ceo"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def reconcile(
    client,
    *,
    tf_json=None,
    plan_json=None,
    dry_run=False,
    force=False,
    actor="ceo",
    provider_account_id=None,
):
    return client.post(
        f"/v1/projects/{PROJECT_ID}/terraform-drift-reconcile",
        json={
            "actor": actor,
            "provider_account_id": provider_account_id,
            "tf_json": tf_json,
            "plan_json": plan_json,
            "dry_run": dry_run,
            "force": force,
        },
    )


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


def seed_do_aws_export(client, engine):
    """Round-trip scaffold: seeded DO+AWS ChangeSet exported through BOSS-66."""
    enable_terraform_export(client, engine)
    changeset = create_changeset_via_api(client, API_MIXED_OPERATIONS)
    export = export_changeset(client, changeset["id"])
    return changeset, export


def seed_inventory_row(
    engine,
    *,
    name,
    metadata,
    provider="aws",
    resource_type="ec2_instance",
    external_id=None,
):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO external_resources (
                    id, project_id, provider, resource_type, external_id,
                    name, state, ownership, environment, health_status,
                    tags, metadata
                )
                VALUES (
                    :id, :project_id, :provider, :resource_type, :external_id,
                    :name, 'active', 'managed_by_boss', 'preview', 'running',
                    CAST(:tags AS JSONB), CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": f"res-{name}",
                "project_id": PROJECT_ID,
                "provider": provider,
                "resource_type": resource_type,
                "external_id": external_id or f"ext-{name}",
                "name": name,
                "tags": json.dumps(["boss-managed"]),
                "metadata": json.dumps(metadata),
            },
        )


def table_count(engine, table, where="project_id = :p"):
    with engine.begin() as connection:
        row = connection.execute(
            text(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}"),
            {"p": PROJECT_ID},
        ).mappings().one()
    return int(row["n"])


def load_changeset_row(engine, changeset_id):
    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT id, title, summary, status, rollback_plan, "
                "approval_decision_id, approved_by, approved_at, applied_at "
                "FROM changesets WHERE id = :id"
            ),
            {"id": changeset_id},
        ).mappings().one()
    return dict(row)


def load_operations(engine, changeset_id):
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT resource_ref, skill_id, action, resource_type, "
                "desired_state, destructive FROM changeset_operations "
                "WHERE changeset_id = :id ORDER BY operation_index"
            ),
            {"id": changeset_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def by_ref(payload, resource_ref):
    return next(
        entry
        for entry in payload["resources"]
        if entry.get("resource_ref") == resource_ref
    )


def test_roundtrip_export_reconciles_all_in_sync_with_no_proposal(client, engine):
    changeset, export = seed_do_aws_export(client, engine)

    response = reconcile(client, tf_json=export["tf_json"])
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["counts"] == {
        "in_sync": 2,
        "tf_diverged": 0,
        "live_drifted": 0,
        "both_diverged": 0,
        "tf_only": 0,
        "boss_only": 0,
    }
    assert payload["changeset_id"] is None
    assert payload["resource_refs"] == []
    assert payload["drift_report_id"]

    # The report was persisted with the benign vocabulary; no new ChangeSet.
    with engine.begin() as connection:
        report = connection.execute(
            text("SELECT classification, detail FROM drift_reports WHERE id = :id"),
            {"id": payload["drift_report_id"]},
        ).mappings().one()
    assert report["classification"] == "benign"
    assert report["detail"]["source"] == "terraform_drift_sync"
    assert table_count(engine, "changesets") == 1  # only the seeded one


def test_tf_edit_yields_tf_diverged_and_pending_proposal(client, engine):
    changeset, export = seed_do_aws_export(client, engine)
    edited = copy.deepcopy(export["tf_json"])
    edited["resource"]["aws_instance"]["boss-web"]["instance_type"] = "m5.large"

    response = reconcile(client, tf_json=edited)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["counts"]["tf_diverged"] == 1
    assert payload["counts"]["in_sync"] == 1
    assert payload["resource_refs"] == ["boss-web"]

    entry = by_ref(payload, "boss-web")
    assert entry["classification"] == "tf_diverged"
    assert entry["live_comparison"]["status"] == "unobserved"
    differences = entry["tf_comparison"]["differences"]
    assert differences == [
        {
            "field": "instance_type",
            "boss_field": "instance_type",
            "boss": "t3.micro",
            "tf": "m5.large",
        }
    ]
    # The .tf's implied desired state is surfaced for approvers, but it is
    # NEVER written into desired state.
    assert (
        entry["tf_comparison"]["tf_implied_desired_state"]["instance_type"]
        == "m5.large"
    )

    # Drift report references the affected resource_refs.
    with engine.begin() as connection:
        report = connection.execute(
            text(
                "SELECT classification, detail, recommendations "
                "FROM drift_reports WHERE id = :id"
            ),
            {"id": payload["drift_report_id"]},
        ).mappings().one()
    assert report["classification"] == "cost-impacting"
    assert report["detail"]["resource_refs"] == ["boss-web"]
    assert report["detail"]["proposed_changeset_id"] == payload["changeset_id"]
    assert any(
        rec["action"] == "review_drift_sync_proposal"
        and rec["changeset_id"] == payload["changeset_id"]
        and rec["requires_approval"] is True
        for rec in report["recommendations"]
    )

    # The proposal sits in the NORMAL dual-control gate: pending_approval
    # (create_changeset's status for approval_required proposals), never
    # approved, never applied.
    proposal = load_changeset_row(engine, payload["changeset_id"])
    assert proposal["title"] == DRIFT_PROPOSAL_TITLE
    assert payload["drift_report_id"] in proposal["summary"]
    assert proposal["status"] == "pending_approval"
    assert proposal["approved_by"] is None
    assert proposal["approved_at"] is None
    assert proposal["applied_at"] is None
    assert proposal["approval_decision_id"]
    assert proposal["rollback_plan"]  # populated

    # One corrective operation carrying BOSS's canonical desired_state.
    operations = load_operations(engine, payload["changeset_id"])
    assert len(operations) == 1
    assert operations[0]["resource_ref"] == "boss-web"
    assert operations[0]["skill_id"] == "aws.ec2.instance.create"
    assert operations[0]["desired_state"]["instance_type"] == "t3.micro"
    assert operations[0]["destructive"] is False

    # THE INVARIANT: BOSS desired_state rows are untouched (DB canonical).
    seeded_ops = load_operations(engine, changeset["id"])
    assert seeded_ops[0]["desired_state"]["instance_type"] == "t3.micro"


def test_rejecting_the_proposal_leaves_everything_unchanged(client, engine):
    changeset, export = seed_do_aws_export(client, engine)
    edited = copy.deepcopy(export["tf_json"])
    edited["resource"]["aws_instance"]["boss-web"]["instance_type"] = "m5.large"

    first = reconcile(client, tf_json=edited).json()
    proposal = load_changeset_row(engine, first["changeset_id"])
    rejected = client.post(
        f"/v1/decisions/{proposal['approval_decision_id']}/reject",
        json={"actor": "ceo", "note": "Not adopting the .tf edit."},
    )
    assert rejected.status_code == 200, rejected.text

    # Desired state unchanged; proposal never approved/applied.
    seeded_ops = load_operations(engine, changeset["id"])
    assert seeded_ops[0]["desired_state"]["instance_type"] == "t3.micro"
    proposal_after = load_changeset_row(engine, first["changeset_id"])
    assert proposal_after["approved_at"] is None
    assert proposal_after["applied_at"] is None

    # Re-running the reconcile finds the SAME divergence again (nothing was
    # absorbed from the .tf) and proposes afresh.
    second = reconcile(client, tf_json=edited).json()
    assert second["counts"]["tf_diverged"] == 1
    assert second["resource_refs"] == ["boss-web"]
    assert second["changeset_id"] and second["changeset_id"] != first["changeset_id"]
    assert (
        by_ref(second, "boss-web")["tf_comparison"]["differences"]
        == by_ref(first, "boss-web")["tf_comparison"]["differences"]
    )


def test_live_drift_and_tf_divergence_are_distinguished(client, engine):
    changeset, export = seed_do_aws_export(client, engine)
    # Live inventory (synced rows in the DB — no provider calls) says the
    # instance actually runs m5.large while BOSS desired_state says t3.micro.
    seed_inventory_row(
        engine,
        name="boss-web",
        metadata={"instance_type": "m5.large", "image_id": "ami-123"},
    )

    # .tf matches BOSS -> live_drifted ONLY, and no corrective proposal (the
    # .tf did not diverge; live drift stays in the report).
    clean = reconcile(client, tf_json=export["tf_json"]).json()
    assert clean["counts"]["live_drifted"] == 1
    assert clean["counts"]["tf_diverged"] == 0
    assert clean["counts"]["both_diverged"] == 0
    assert clean["changeset_id"] is None
    entry = by_ref(clean, "boss-web")
    assert entry["classification"] == "live_drifted"
    assert entry["tf_comparison"]["status"] == "in_sync"
    assert entry["live_comparison"]["status"] == "drifted"
    assert entry["live_comparison"]["differences"] == [
        {"field": "instance_type", "boss": "t3.micro", "live": "m5.large"}
    ]

    # .tf ALSO diverges -> both_diverged, and the tf divergence makes it
    # actionable: a corrective proposal is created.
    edited = copy.deepcopy(export["tf_json"])
    edited["resource"]["aws_instance"]["boss-web"]["instance_type"] = "c5.xlarge"
    both = reconcile(client, tf_json=edited).json()
    assert both["counts"]["both_diverged"] == 1
    assert both["counts"]["tf_diverged"] == 0
    assert both["counts"]["live_drifted"] == 0
    assert both["changeset_id"]
    entry = by_ref(both, "boss-web")
    assert entry["classification"] == "both_diverged"
    # tf vs live divergence carry their own separate comparisons.
    assert entry["tf_comparison"]["differences"][0]["tf"] == "c5.xlarge"
    assert entry["live_comparison"]["differences"][0]["live"] == "m5.large"


def test_tf_only_is_adoption_candidate_and_boss_only_is_note_only(client, engine):
    changeset, export = seed_do_aws_export(client, engine)
    edited = copy.deepcopy(export["tf_json"])
    # A resource BOSS does not know about...
    edited["resource"]["aws_instance"]["rogue"] = {
        "ami": "ami-999",
        "instance_type": "t2.nano",
        "tags": {"Name": "rogue"},
    }
    # ...and the BOSS-managed droplet deleted from the .tf.
    del edited["resource"]["digitalocean_droplet"]

    payload = reconcile(client, tf_json=edited).json()
    assert payload["counts"]["tf_only"] == 1
    assert payload["counts"]["boss_only"] == 1
    assert payload["counts"]["in_sync"] == 1
    # Neither class is actionable: no corrective ChangeSet at all.
    assert payload["changeset_id"] is None

    tf_only_entry = next(
        entry
        for entry in payload["resources"]
        if entry["classification"] == "tf_only"
    )
    assert tf_only_entry["tf_address"] == "aws_instance.rogue"
    assert "Adoption candidate" in tf_only_entry["note"]
    assert any(
        warning["kind"] == "adoption_candidate"
        and warning["tf_address"] == "aws_instance.rogue"
        for warning in payload["warnings"]
    )

    boss_only_entry = by_ref(payload, "boss-worker")
    assert boss_only_entry["classification"] == "boss_only"
    assert "re-export" in boss_only_entry["note"].lower()

    # No operation for either: the report is the only output.
    assert table_count(engine, "changesets") == 1
    with engine.begin() as connection:
        report = connection.execute(
            text("SELECT unmanaged_count, orphaned_count FROM drift_reports WHERE id = :id"),
            {"id": payload["drift_report_id"]},
        ).mappings().one()
    assert report["unmanaged_count"] == 1  # tf_only, mirroring the vocabulary
    assert report["orphaned_count"] == 1  # boss_only


def test_plan_json_input_is_equivalent_to_tf_json_input(client, engine):
    changeset, export = seed_do_aws_export(client, engine)
    edited = copy.deepcopy(export["tf_json"])
    edited["resource"]["aws_instance"]["boss-web"]["instance_type"] = "m5.large"

    # Build the same document as `terraform show -json` would render it.
    plan_resources = []
    for tf_type, instances in edited["resource"].items():
        for tf_name, body in instances.items():
            values = {k: v for k, v in body.items() if k != "//"}
            plan_resources.append(
                {
                    "address": f"{tf_type}.{tf_name}",
                    "mode": "managed",
                    "type": tf_type,
                    "name": tf_name,
                    "values": values,
                }
            )
    plan_json = {
        "format_version": "1.2",
        "values": {"root_module": {"resources": plan_resources}},
    }

    from_tf = reconcile(client, tf_json=edited, dry_run=True).json()
    from_plan = reconcile(client, plan_json=plan_json, dry_run=True).json()
    assert from_plan["input_format"] == "plan_json"
    assert from_tf["counts"] == from_plan["counts"]
    assert from_tf["resource_refs"] == from_plan["resource_refs"] == ["boss-web"]
    assert (
        by_ref(from_plan, "boss-web")["tf_comparison"]["differences"]
        == by_ref(from_tf, "boss-web")["tf_comparison"]["differences"]
    )


def test_dry_run_computes_comparison_but_creates_no_rows(client, engine):
    changeset, export = seed_do_aws_export(client, engine)
    edited = copy.deepcopy(export["tf_json"])
    edited["resource"]["aws_instance"]["boss-web"]["instance_type"] = "m5.large"

    response = reconcile(client, tf_json=edited, dry_run=True)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["counts"]["tf_diverged"] == 1
    assert by_ref(payload, "boss-web")["tf_comparison"]["differences"]
    assert payload["drift_report_id"] is None
    assert payload["changeset_id"] is None

    assert table_count(engine, "drift_reports") == 0
    assert table_count(engine, "changesets") == 1  # only the seeded one
    with engine.begin() as connection:
        events = connection.execute(
            text(
                "SELECT COUNT(*) AS n FROM events "
                "WHERE event_type LIKE 'terraform_drift%'"
            ),
        ).mappings().one()
    assert events["n"] == 0


def test_gating_mirrors_export_opt_in_and_force_is_audited(client, engine):
    changeset, export = seed_do_aws_export(client, engine)
    disable_terraform_export(engine)

    refused = reconcile(client, tf_json=export["tf_json"])
    assert refused.status_code == 409
    assert "opt-in" in refused.json()["detail"]
    assert table_count(engine, "drift_reports") == 0

    forced = reconcile(client, tf_json=export["tf_json"], force=True)
    assert forced.status_code == 200, forced.text
    assert forced.json()["forced"] is True
    assert forced.json()["opt_in_targets"] == 0
    with engine.begin() as connection:
        audit = connection.execute(
            text(
                "SELECT COUNT(*) AS n FROM events "
                "WHERE event_type = 'terraform_drift.forced'"
            ),
        ).mappings().one()
    assert audit["n"] == 1


def test_exactly_one_input_document_is_required(client, engine):
    seed_do_aws_export(client, engine)
    assert reconcile(client).status_code == 422
    assert (
        reconcile(
            client, tf_json={"resource": {}}, plan_json={"values": {}}
        ).status_code
        == 422
    )


def test_resource_type_change_keeps_fresh_address_and_is_documented(
    client, engine
):
    """BOSS-66 semantic preserved: a resource_ref whose resource_type changes
    gets a fresh tf_address (old mapping row overwritten). A stale .tf using
    the old address is matched via its boss:resource_ref annotation and the
    supersession is documented in the drift report."""
    enable_terraform_export(client, engine)
    first_changeset = create_changeset_via_api(
        client, [API_MIXED_OPERATIONS[0]], title="EC2 incarnation"
    )
    stale_export = export_changeset(client, first_changeset["id"])

    # Same resource_ref re-created as a different resource_type.
    create_changeset_via_api(
        client,
        [
            {
                "skill_id": "aws.s3.bucket.create",
                "action": "create",
                "resource_type": "s3_bucket",
                "resource_ref": "boss-web",
                "desired_state": {"bucket_name": "boss-web", "versioning": True},
                "risk_tier": 2,
            }
        ],
        title="S3 incarnation of the same ref",
    )
    fresh_export = export_changeset(
        client,
        create_changeset_via_api(
            client,
            [
                {
                    "skill_id": "aws.s3.bucket.create",
                    "action": "create",
                    "resource_type": "s3_bucket",
                    "resource_ref": "boss-web",
                    "desired_state": {"bucket_name": "boss-web", "versioning": True},
                    "risk_tier": 2,
                }
            ],
            title="S3 incarnation (exported)",
        )["id"],
    )
    assert fresh_export["mapping"][0]["tf_address"].startswith("aws_s3_bucket.")

    payload = reconcile(client, tf_json=stale_export["tf_json"]).json()
    entry = by_ref(payload, "boss-web")
    assert entry["classification"] == "tf_diverged"
    assert entry["tf_address"].startswith("aws_s3_bucket.")
    assert entry["supplied_tf_address"].startswith("aws_instance.")
    assert "resource_type_changed" in entry["tf_comparison"].get("reasons", [])
    assert any(
        warning["kind"] == "tf_address_superseded"
        and warning["resource_ref"] == "boss-web"
        for warning in payload["warnings"]
    )
    # The corrective proposal re-asserts the CURRENT desired state (s3).
    operations = load_operations(engine, payload["changeset_id"])
    assert operations[0]["resource_type"] == "s3_bucket"
    assert operations[0]["desired_state"]["bucket_name"] == "boss-web"
