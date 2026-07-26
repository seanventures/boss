"""Terraform drift-sync reconciler (Track 1 inbound, BOSS-69).

THE INVARIANT (docs/planning/oss-readiness/05-infrastructure-as-code.md s3.1):
a customer-supplied ``.tf`` NEVER mutates the BOSS database directly.  This
module performs a three-way comparison between

  (a) BOSS desired state — the latest ChangeSet operations per resource_ref,
      joined to Terraform addresses through the BOSS-66 ``terraform_exports``
      mapping table (applied ChangeSets always win over unapplied ones),
  (b) the supplied ``.tf.json`` / ``terraform show`` / plan JSON, and
  (c) live provider inventory — the ``external_resources`` rows the existing
      reconciliation loop already persisted (NO live provider calls here),

and its only outputs are a drift report plus, when the ``.tf`` diverges, a
PROPOSED corrective ChangeSet routed through the normal dual-control approval
gate (``create_changeset`` with ``approval_required=True`` -> the standard
``pending_approval`` status and blocking decision).  Nothing is auto-approved
or auto-applied; rejecting the proposal leaves desired state unchanged.

Inherited BOSS-66 semantic (preserved here): a resource_ref whose
resource_type changes gets a fresh tf_address and the old mapping row is
overwritten.  A supplied ``.tf`` that still carries the stale address is
re-matched through the ``boss:resource_ref`` annotation and the supersession
is documented in the drift report instead of being silently treated as an
unknown resource.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event
from .terraform_export import (
    _DESTROY_ACTIONS,
    _FIELD_TRANSLATORS,
    _RESERVED_TAG_KEYS,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .resource_plane import ResourcePlaneService


class TerraformDriftError(RuntimeError):
    pass


# Per-resource classification taxonomy (spec 05 s3.1 drift-sync bullet).
CLASS_IN_SYNC = "in_sync"
CLASS_TF_DIVERGED = "tf_diverged"  # supplied .tf disagrees with BOSS desired
CLASS_LIVE_DRIFTED = "live_drifted"  # live inventory disagrees with BOSS desired
CLASS_BOTH_DIVERGED = "both_diverged"
CLASS_TF_ONLY = "tf_only"  # in .tf, unknown to BOSS (adoption candidate)
CLASS_BOSS_ONLY = "boss_only"  # BOSS-managed, absent from the .tf

ALL_CLASSES = (
    CLASS_IN_SYNC,
    CLASS_TF_DIVERGED,
    CLASS_LIVE_DRIFTED,
    CLASS_BOTH_DIVERGED,
    CLASS_TF_ONLY,
    CLASS_BOSS_ONLY,
)

DRIFT_PROPOSAL_TITLE = "Drift-sync proposal from Terraform input"


# --------------------------------------------------------------------------- #
# Input parsing                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class ParsedTfResource:
    tf_address: str
    tf_type: str
    tf_name: str
    body: dict[str, Any]
    boss_resource_ref: Optional[str] = None


@dataclass
class ParsedTfInput:
    input_format: str  # "tf_json" | "plan_json"
    resources: dict[str, ParsedTfResource] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)


_TF_JSON_TOP_KEYS = {
    "resource",
    "terraform",
    "provider",
    "variable",
    "output",
    "locals",
    "data",
    "module",
    "import",
}


def parse_tf_input(payload: Any) -> ParsedTfInput:
    """Parse either the exporter's own .tf.json dialect or terraform
    show/plan JSON into ``tf_address -> desired body``.

    Data sources and non-root modules are ignored with warning entries; any
    other shape is rejected with a clear error.  The parse NEVER touches the
    database — the supplied document is untrusted input, only ever compared.
    """
    if not isinstance(payload, dict) or not payload:
        raise TerraformDriftError(
            "Terraform input must be a non-empty JSON object: either a "
            ".tf.json document (with a top-level 'resource' map) or "
            "`terraform show -json` / plan JSON (with 'values' or "
            "'planned_values')."
        )
    if ("values" in payload or "planned_values" in payload) and (
        "resource" not in payload
    ):
        return _parse_plan_json(payload)
    if _TF_JSON_TOP_KEYS.intersection(payload):
        return _parse_tf_json(payload)
    raise TerraformDriftError(
        "Unrecognized Terraform input. Supply a .tf.json document "
        "(top-level 'resource'/'terraform' keys) or `terraform show -json` "
        "/ plan JSON ('values.root_module.resources'). Got top-level keys: "
        + ", ".join(sorted(str(key) for key in payload)[:10])
    )


def _parse_tf_json(payload: dict[str, Any]) -> ParsedTfInput:
    parsed = ParsedTfInput(input_format="tf_json")
    if payload.get("data"):
        parsed.warnings.append(
            {
                "kind": "data_sources_ignored",
                "detail": "Top-level 'data' blocks are read-only lookups; "
                "they never participate in drift reconciliation.",
            }
        )
    if payload.get("module"):
        parsed.warnings.append(
            {
                "kind": "modules_ignored",
                "detail": "Module blocks beyond the root module are not "
                "reconciled in this slice.",
            }
        )
    resource_map = payload.get("resource") or {}
    if not isinstance(resource_map, dict):
        raise TerraformDriftError("'resource' must be a map of type -> name -> body.")
    for tf_type, instances in resource_map.items():
        if not isinstance(instances, dict):
            parsed.warnings.append(
                {
                    "kind": "malformed_resource_block",
                    "tf_type": tf_type,
                    "detail": "Resource block is not a name -> body map; skipped.",
                }
            )
            continue
        for tf_name, raw_body in instances.items():
            if not isinstance(raw_body, dict):
                parsed.warnings.append(
                    {
                        "kind": "malformed_resource_block",
                        "tf_type": tf_type,
                        "tf_name": tf_name,
                        "detail": "Resource body is not an object; skipped.",
                    }
                )
                continue
            body = dict(raw_body)
            comment = body.pop("//", None)
            boss_ref = None
            if isinstance(comment, dict) and comment.get("boss:resource_ref"):
                boss_ref = str(comment["boss:resource_ref"])
            address = f"{tf_type}.{tf_name}"
            parsed.resources[address] = ParsedTfResource(
                tf_address=address,
                tf_type=str(tf_type),
                tf_name=str(tf_name),
                body=body,
                boss_resource_ref=boss_ref,
            )
    return parsed


def _parse_plan_json(payload: dict[str, Any]) -> ParsedTfInput:
    parsed = ParsedTfInput(input_format="plan_json")
    document = payload.get("values") or payload.get("planned_values") or {}
    if not isinstance(document, dict):
        raise TerraformDriftError("'values'/'planned_values' must be an object.")
    root = document.get("root_module") or {}
    if not isinstance(root, dict):
        raise TerraformDriftError("'root_module' must be an object.")
    if root.get("child_modules"):
        parsed.warnings.append(
            {
                "kind": "modules_ignored",
                "detail": "child_modules beyond the root module are not "
                "reconciled in this slice.",
            }
        )
    for entry in root.get("resources") or []:
        if not isinstance(entry, dict):
            continue
        tf_type = str(entry.get("type") or "")
        tf_name = str(entry.get("name") or "")
        address = str(entry.get("address") or f"{tf_type}.{tf_name}")
        if str(entry.get("mode") or "managed") == "data":
            parsed.warnings.append(
                {
                    "kind": "data_sources_ignored",
                    "tf_address": address,
                    "detail": "Data sources never participate in drift "
                    "reconciliation.",
                }
            )
            continue
        values = entry.get("values")
        body = dict(values) if isinstance(values, dict) else {}
        body.pop("//", None)
        parsed.resources[address] = ParsedTfResource(
            tf_address=address,
            tf_type=tf_type,
            tf_name=tf_name,
            body=body,
        )
    return parsed


# --------------------------------------------------------------------------- #
# Reverse field translation (inverse of terraform_export._FIELD_TRANSLATORS)  #
# --------------------------------------------------------------------------- #


def _reverse_tags_map(tags: Any, desired: dict[str, Any]) -> None:
    """Inverse of terraform_export._tags_map: Name -> name, the rest back to
    the ChangeSet "key:value" string-list form (reserved boss tags dropped)."""
    if not isinstance(tags, dict):
        return
    if tags.get("Name"):
        desired["name"] = str(tags["Name"])
    user_tags = sorted(
        f"{key}:{value}"
        for key, value in tags.items()
        if key not in _RESERVED_TAG_KEYS
    )
    if user_tags:
        desired["tags"] = user_tags


def _take(body: dict[str, Any], key: str) -> Any:
    """Consume a key from the body copy (None values count as absent)."""
    value = body.pop(key, None)
    return value


def _reverse_aws_instance(body: dict[str, Any]) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    ami = _take(body, "ami")
    if ami is not None:
        desired["image_id"] = str(ami)
    for tf_key, boss_key in (
        ("instance_type", "instance_type"),
        ("subnet_id", "subnet_id"),
        ("key_name", "key_name"),
    ):
        value = _take(body, tf_key)
        if value is not None:
            desired[boss_key] = str(value)
    groups = _take(body, "vpc_security_group_ids")
    if isinstance(groups, list) and groups:
        desired["security_group_ids"] = [str(item) for item in groups]
    _reverse_tags_map(_take(body, "tags"), desired)
    return desired


def _reverse_aws_s3_bucket(body: dict[str, Any]) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    bucket = _take(body, "bucket")
    if bucket is not None:
        desired["bucket_name"] = str(bucket)
    versioning = _take(body, "versioning")
    if isinstance(versioning, dict):
        desired["versioning"] = bool(versioning.get("enabled"))
    elif versioning is not None:
        desired["versioning"] = bool(versioning)
    sse = _take(body, "server_side_encryption_configuration")
    if isinstance(sse, dict):
        algorithm = str(
            (((sse.get("rule") or {}).get(
                "apply_server_side_encryption_by_default"
            ) or {}).get("sse_algorithm"))
            or ""
        )
        desired["encryption"] = "kms" if algorithm == "aws:kms" else "AES256"
    return desired


def _reverse_aws_db_instance(body: dict[str, Any]) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    for tf_key, boss_key in (
        ("identifier", "db_instance_identifier"),
        ("engine", "engine"),
        ("instance_class", "db_instance_class"),
        ("username", "master_username"),
        ("db_name", "db_name"),
        ("db_subnet_group_name", "db_subnet_group_name"),
    ):
        value = _take(body, tf_key)
        if value is not None:
            desired[boss_key] = str(value)
    storage = _take(body, "allocated_storage")
    if storage is not None:
        desired["allocated_storage_gb"] = int(storage)
    for tf_key, boss_key in (
        ("multi_az", "multi_az"),
        ("publicly_accessible", "publicly_accessible"),
    ):
        value = _take(body, tf_key)
        if value is not None:
            desired[boss_key] = bool(value)
    groups = _take(body, "vpc_security_group_ids")
    if isinstance(groups, list) and groups:
        desired["vpc_security_group_ids"] = [str(item) for item in groups]
    return desired


def _reverse_aws_iam_role(body: dict[str, Any]) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    name = _take(body, "name")
    if name is not None:
        desired["role_name"] = str(name)
    assume = _take(body, "assume_role_policy")
    if assume is not None:
        if isinstance(assume, str):
            try:
                desired["assume_role_policy_document"] = json.loads(assume)
            except (TypeError, ValueError):
                desired["assume_role_policy_document"] = assume
        else:
            desired["assume_role_policy_document"] = assume
    description = _take(body, "description")
    if description is not None:
        desired["description"] = str(description)
    policy_arns = _take(body, "managed_policy_arns")
    if isinstance(policy_arns, list) and policy_arns:
        desired["attach_policy_arns"] = [str(item) for item in policy_arns]
    return desired


def _reverse_aws_route53_record(body: dict[str, Any]) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    zone_id = _take(body, "zone_id")
    if zone_id is not None:
        desired["hosted_zone_id"] = str(zone_id)
    name = _take(body, "name")
    if name is not None:
        desired["name"] = str(name)
    record_type = _take(body, "type")
    if record_type is not None:
        desired["record_type"] = str(record_type).upper()
    ttl = _take(body, "ttl")
    if ttl is not None:
        desired["ttl"] = int(ttl)
    records = _take(body, "records")
    if isinstance(records, list):
        desired["values"] = [str(item) for item in records]
    return desired


def _reverse_digitalocean_droplet(body: dict[str, Any]) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    for tf_key in ("name", "region", "size", "image"):
        value = _take(body, tf_key)
        if value is not None:
            desired[tf_key] = str(value)
    tags = _take(body, "tags")
    if isinstance(tags, list):
        user_tags = sorted(
            str(tag)
            for tag in tags
            if str(tag) not in {"boss-managed", "boss-providerops"}
        )
        if user_tags:
            desired["tags"] = user_tags
    return desired


def _reverse_digitalocean_app(body: dict[str, Any]) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    spec = _take(body, "spec")
    if isinstance(spec, dict) and spec:
        desired["spec"] = spec
    return desired


def _reverse_cloudflare_record(body: dict[str, Any]) -> dict[str, Any]:
    desired: dict[str, Any] = {}
    zone_id = _take(body, "zone_id")
    if zone_id is not None:
        desired["zone_id"] = str(zone_id)
    name = _take(body, "name")
    if name is not None:
        desired["name"] = str(name)
    record_type = _take(body, "type")
    if record_type is not None:
        desired["record_type"] = str(record_type).upper()
    value = _take(body, "value")
    if value is not None:
        desired["content"] = str(value)
    ttl = _take(body, "ttl")
    if ttl is not None:
        desired["ttl"] = int(ttl)
    proxied = _take(body, "proxied")
    if proxied is not None:
        desired["proxied"] = bool(proxied)
    priority = _take(body, "priority")
    if priority is not None:
        desired["priority"] = int(priority)
    return desired


_REVERSE_TRANSLATORS = {
    "aws_instance": _reverse_aws_instance,
    "aws_s3_bucket": _reverse_aws_s3_bucket,
    "aws_db_instance": _reverse_aws_db_instance,
    "aws_iam_role": _reverse_aws_iam_role,
    "aws_route53_record": _reverse_aws_route53_record,
    "digitalocean_droplet": _reverse_digitalocean_droplet,
    "digitalocean_app": _reverse_digitalocean_app,
    "cloudflare_record": _reverse_cloudflare_record,
}

# Fields whose values are secret-shaped in tf-space: never compared, only
# noted, so a var-reference vs literal mismatch can't leak or false-positive.
_SENSITIVE_TF_FIELDS: dict[str, set[str]] = {
    "aws_db_instance": {"password"},
}

# tf-space field -> BOSS desired_state field, for readable drift reports.
_FIELD_NAME_MAP: dict[str, dict[str, str]] = {
    "aws_instance": {
        "ami": "image_id",
        "vpc_security_group_ids": "security_group_ids",
        "tags": "name/tags",
    },
    "aws_s3_bucket": {
        "bucket": "bucket_name",
        "server_side_encryption_configuration": "encryption",
    },
    "aws_db_instance": {
        "identifier": "db_instance_identifier",
        "instance_class": "db_instance_class",
        "allocated_storage": "allocated_storage_gb",
        "username": "master_username",
    },
    "aws_iam_role": {
        "name": "role_name",
        "assume_role_policy": "assume_role_policy_document",
        "managed_policy_arns": "attach_policy_arns",
    },
    "aws_route53_record": {
        "zone_id": "hosted_zone_id",
        "type": "record_type",
        "records": "values",
    },
    "cloudflare_record": {"type": "record_type", "value": "content"},
}


def reverse_field_map(
    provider: str,
    tf_type: str,
    tf_body: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inverse of the exporter's field translation: tf body -> BOSS
    desired_state.

    Returns ``(boss_desired_state, unrecognized_fields)``.  Unknown fields are
    carried into ``unrecognized_fields`` (surfaced as a note on the
    comparison) — never silently dropped.  Sensitive tf fields (e.g.
    aws_db_instance.password) are excluded from both sides.
    """
    body = dict(tf_body or {})
    body.pop("//", None)
    for sensitive in _SENSITIVE_TF_FIELDS.get(tf_type, ()):  # noted by caller
        body.pop(sensitive, None)
    translator = _REVERSE_TRANSLATORS.get(tf_type)
    if translator is None:
        return {}, body
    desired = translator(body)
    unrecognized = {key: value for key, value in body.items() if value is not None}
    return desired, unrecognized


# --------------------------------------------------------------------------- #
# Comparison                                                                  #
# --------------------------------------------------------------------------- #


def _normalized(value: Any) -> Any:
    """Order-insensitive scalar-list / string-vs-number tolerant normal form."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value):
            return sorted(str(item) for item in value)
        return [_normalized(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalized(item) for key, item in sorted(value.items())}
    return str(value)


def _compare_tf_body(
    tf_type: str,
    expected_body: dict[str, Any],
    supplied_body: dict[str, Any],
) -> dict[str, Any]:
    """Field-level diff of the forward-projected BOSS desired state (a) vs the
    supplied tf body (b), in tf space.  Unknown supplied fields become an
    ``unrecognized_fields`` note, never a silent drop and never a divergence
    on their own guesswork."""
    supplied = dict(supplied_body or {})
    supplied.pop("//", None)
    sensitive_skipped = sorted(
        key for key in _SENSITIVE_TF_FIELDS.get(tf_type, ()) if key in supplied
    )
    expected = {
        key: value
        for key, value in (expected_body or {}).items()
        if key != "//" and key not in _SENSITIVE_TF_FIELDS.get(tf_type, ())
    }
    for key in sensitive_skipped:
        supplied.pop(key, None)
    _, unrecognized = reverse_field_map("", tf_type, supplied)
    field_names = _FIELD_NAME_MAP.get(tf_type, {})
    differences: list[dict[str, Any]] = []
    comparable_supplied_keys = {
        key for key in supplied if key not in unrecognized
    }
    for key in sorted(set(expected) | comparable_supplied_keys):
        boss_value = expected.get(key)
        tf_value = supplied.get(key)
        if _normalized(boss_value) != _normalized(tf_value):
            differences.append(
                {
                    "field": key,
                    "boss_field": field_names.get(key, key),
                    "boss": boss_value,
                    "tf": tf_value,
                }
            )
    return {
        "status": "diverged" if differences else "in_sync",
        "differences": differences,
        "unrecognized_fields": unrecognized,
        "sensitive_fields_skipped": sensitive_skipped,
    }


# Live-inventory comparison: BOSS desired_state key aliases -> the metadata
# key `_sync_inventory` persisted on external_resources.  Only fields present
# on BOTH sides are compared — the inventory is a partial observation.
_LIVE_FIELD_PAIRS: dict[str, list[tuple[tuple[str, ...], str]]] = {
    "ec2_instance": [
        (("instance_type",), "instance_type"),
        (("image_id", "ami"), "image_id"),
        (("subnet_id",), "subnet_id"),
    ],
    "droplet": [
        (("region",), "region"),
        (("size",), "size"),
        (("image",), "image"),
    ],
    "rds_instance": [
        (("engine",), "engine"),
        (("db_instance_class", "instance_class"), "instance_class"),
        (("allocated_storage_gb", "allocated_storage"), "allocated_storage_gb"),
        (("multi_az",), "multi_az"),
    ],
}


def _live_comparison(
    live_rows: list[dict[str, Any]],
    *,
    provider: str,
    resource_type: str,
    resource_ref: str,
    desired: dict[str, Any],
    boss_absent: bool,
) -> dict[str, Any]:
    candidates = {
        str(value)
        for value in (
            resource_ref,
            desired.get("name"),
            desired.get("bucket_name"),
            desired.get("db_instance_identifier"),
        )
        if value
    }
    row = next(
        (
            item
            for item in live_rows
            if str(item.get("provider")) == provider
            and str(item.get("resource_type")) == resource_type
            and str(item.get("name")) in candidates
        ),
        None,
    )
    if row is None:
        return {"status": "unobserved", "differences": []}
    if boss_absent:
        return {
            "status": "drifted",
            "differences": [
                {
                    "field": "presence",
                    "boss": "absent (destroyed)",
                    "live": f"present ({row.get('state')})",
                }
            ],
            "external_id": row.get("external_id"),
        }
    metadata = row.get("metadata") or {}
    differences: list[dict[str, Any]] = []
    for desired_aliases, metadata_key in _LIVE_FIELD_PAIRS.get(resource_type, []):
        desired_value = next(
            (
                desired[alias]
                for alias in desired_aliases
                if desired.get(alias) is not None
            ),
            None,
        )
        live_value = metadata.get(metadata_key)
        if desired_value is None or live_value is None:
            continue
        if _normalized(desired_value) != _normalized(live_value):
            differences.append(
                {
                    "field": desired_aliases[0],
                    "boss": desired_value,
                    "live": live_value,
                }
            )
    return {
        "status": "drifted" if differences else "in_sync",
        "differences": differences,
        "external_id": row.get("external_id"),
    }


# --------------------------------------------------------------------------- #
# Desired-state + inventory loading                                           #
# --------------------------------------------------------------------------- #


def _load_mapping_rows(
    connection: Connection, project_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT resource_ref, tf_address, provider, changeset_id
            FROM terraform_exports
            WHERE project_id = :project_id
            ORDER BY resource_ref
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _load_latest_operations(
    connection: Connection, project_id: str
) -> dict[str, dict[str, Any]]:
    """Latest ChangeSet operation per resource_ref.

    Applied ChangeSets are authoritative and always win over unapplied ones;
    within the same bucket the newest ChangeSet (then highest operation_index)
    wins.  Corrective drift-sync proposals carry BOSS's own canonical
    desired_state, so re-running a reconcile after proposing never changes
    the comparison baseline.
    """
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (co.resource_ref)
                   co.resource_ref, co.skill_id, co.action, co.resource_type,
                   co.desired_state, co.risk_tier, co.destructive,
                   co.production_affecting,
                   c.id AS changeset_id, c.status AS changeset_status
            FROM changeset_operations co
            JOIN changesets c ON c.id = co.changeset_id
            WHERE co.project_id = :project_id AND co.resource_ref IS NOT NULL
            ORDER BY co.resource_ref,
                     (c.status = 'applied') DESC,
                     c.created_at DESC,
                     co.operation_index DESC
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return {str(row["resource_ref"]): dict(row) for row in rows}


def _load_live_inventory(
    connection: Connection,
    project_id: str,
    provider_account_id: Optional[str],
) -> list[dict[str, Any]]:
    clauses = ["project_id = :project_id"]
    params: dict[str, Any] = {"project_id": project_id}
    if provider_account_id:
        clauses.append("provider_account_id = :provider_account_id")
        params["provider_account_id"] = provider_account_id
    rows = connection.execute(
        text(
            f"""
            SELECT provider, resource_type, external_id, name, state,
                   tags, metadata
            FROM external_resources
            WHERE {' AND '.join(clauses)}
            """
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Reconciliation orchestration                                                #
# --------------------------------------------------------------------------- #


def terraform_drift_reconcile(
    connection: Connection,
    settings: Settings,
    project_id: str,
    *,
    tf_payload: Any,
    provider_account_id: Optional[str] = None,
    actor: str,
    live_inventory: Optional[list[dict[str, Any]]] = None,
    dry_run: bool = False,
    plane: Optional["ResourcePlaneService"] = None,
) -> dict[str, Any]:
    """Three-way drift reconciliation between BOSS desired state, a supplied
    .tf/plan document, and previously synced live inventory.

    Persists (unless ``dry_run``): one drift report, plus — only when the .tf
    diverges from BOSS desired state — ONE proposed corrective ChangeSet in
    the normal ``pending_approval`` dual-control state.  The supplied .tf
    NEVER mutates desired state directly; rejecting the proposal leaves
    everything unchanged.
    """
    if plane is None:
        from .resource_plane import ResourcePlaneService

        plane = ResourcePlaneService(settings)

    parsed = parse_tf_input(tf_payload)
    warnings: list[dict[str, Any]] = list(parsed.warnings)

    mapping_rows = _load_mapping_rows(connection, project_id)
    latest_ops = _load_latest_operations(connection, project_id)
    if live_inventory is None:
        live_inventory = _load_live_inventory(
            connection, project_id, provider_account_id
        )

    comparisons: list[dict[str, Any]] = []
    corrective_operations: list[dict[str, Any]] = []
    claimed_addresses: set[str] = set()
    resources_by_ref = {
        resource.boss_resource_ref: resource
        for resource in parsed.resources.values()
        if resource.boss_resource_ref
    }

    for mapping in mapping_rows:
        resource_ref = str(mapping["resource_ref"])
        tf_address = str(mapping["tf_address"])
        tf_type = tf_address.split(".", 1)[0]
        operation = latest_ops.get(resource_ref)
        if operation is None:
            warnings.append(
                {
                    "kind": "mapping_without_operation",
                    "resource_ref": resource_ref,
                    "tf_address": tf_address,
                    "detail": "terraform_exports row has no ChangeSet "
                    "operation; skipped.",
                }
            )
            continue
        provider = str(mapping.get("provider") or "")
        desired = operation.get("desired_state") or {}
        if not isinstance(desired, dict):
            desired = {}
        boss_absent = str(operation.get("action") or "").lower() in _DESTROY_ACTIONS

        supplied = parsed.resources.get(tf_address)
        supersession_note = None
        if supplied is None:
            # BOSS-66 semantic: a resource_type change re-addresses the ref
            # and overwrites the old mapping row.  A stale .tf still carries
            # the old address but keeps the boss:resource_ref annotation.
            stale = resources_by_ref.get(resource_ref)
            if stale is not None and stale.tf_address not in claimed_addresses:
                supplied = stale
                supersession_note = {
                    "kind": "tf_address_superseded",
                    "resource_ref": resource_ref,
                    "supplied_address": stale.tf_address,
                    "current_address": tf_address,
                    "detail": (
                        "resource_ref's resource_type changed after this .tf "
                        "was produced; the exporter assigned a fresh "
                        "tf_address and overwrote the old mapping row "
                        "(BOSS-66 semantic). Re-export to refresh the .tf."
                    ),
                }
                warnings.append(supersession_note)
        if supplied is not None:
            claimed_addresses.add(supplied.tf_address)

        translator = _FIELD_TRANSLATORS.get(tf_type)
        entry: dict[str, Any] = {
            "resource_ref": resource_ref,
            "tf_address": tf_address,
            "supplied_tf_address": supplied.tf_address if supplied else None,
            "tf_type": tf_type,
            "provider": provider,
            "changeset_id": operation.get("changeset_id"),
            "boss_desires_absence": boss_absent,
        }
        if supersession_note:
            entry["notes"] = [supersession_note]

        # --- (a) vs (b): BOSS desired state vs supplied .tf ---------------- #
        actionable_tf_divergence = False
        if supplied is None:
            if boss_absent:
                tf_comparison = {
                    "status": "in_sync",
                    "differences": [],
                    "detail": "BOSS desires absence and the .tf omits the "
                    "resource (destroy projects as absence).",
                }
                tf_diverged = False
            else:
                tf_comparison = {"status": "missing", "differences": []}
                tf_diverged = True  # boss_only classification below
        elif boss_absent:
            tf_comparison = {
                "status": "diverged",
                "differences": [
                    {
                        "field": "presence",
                        "boss": "absent (destroyed)",
                        "tf": "present",
                    }
                ],
                "detail": "BOSS desired state is absence (latest operation "
                "is a destroy); the supplied .tf still declares the "
                "resource. Realignment means removing it from the .tf, not "
                "a provider operation.",
            }
            tf_diverged = True
            warnings.append(
                {
                    "kind": "tf_declares_destroyed_resource",
                    "resource_ref": resource_ref,
                    "tf_address": supplied.tf_address,
                    "detail": "No corrective operation proposed for a "
                    "resource BOSS already destroyed; fix the .tf instead.",
                }
            )
        elif translator is None:
            warnings.append(
                {
                    "kind": "unmapped_resource_type",
                    "resource_ref": resource_ref,
                    "tf_address": tf_address,
                    "detail": f"No field translator for '{tf_type}'; "
                    "comparison skipped.",
                }
            )
            tf_comparison = {"status": "not_comparable", "differences": []}
            tf_diverged = False
        else:
            expected_body, _ = translator(desired, operation)
            tf_comparison = _compare_tf_body(
                tf_type, expected_body, supplied.body
            )
            if supplied.tf_type != tf_type:
                tf_comparison["status"] = "diverged"
                tf_comparison.setdefault("reasons", []).append(
                    "resource_type_changed"
                )
            tf_diverged = tf_comparison["status"] == "diverged"
            actionable_tf_divergence = tf_diverged
            implied_desired, _ = reverse_field_map(
                provider, supplied.tf_type, supplied.body
            )
            tf_comparison["tf_implied_desired_state"] = implied_desired

        # --- (a) vs (c): BOSS desired state vs live inventory -------------- #
        live_comparison = _live_comparison(
            live_inventory,
            provider=provider,
            resource_type=str(operation.get("resource_type") or ""),
            resource_ref=resource_ref,
            desired=desired,
            boss_absent=boss_absent,
        )
        live_drifted = live_comparison["status"] == "drifted"

        if supplied is None and not boss_absent:
            classification = CLASS_BOSS_ONLY
            entry["note"] = (
                "BOSS-managed resource absent from the supplied .tf; the "
                ".tf author likely deleted it. Realignment means "
                "re-exporting the projection, not a provider operation."
            )
        elif tf_diverged and live_drifted:
            classification = CLASS_BOTH_DIVERGED
        elif tf_diverged:
            classification = CLASS_TF_DIVERGED
        elif live_drifted:
            classification = CLASS_LIVE_DRIFTED
        else:
            classification = CLASS_IN_SYNC

        entry["classification"] = classification
        entry["tf_comparison"] = tf_comparison
        entry["live_comparison"] = live_comparison
        comparisons.append(entry)

        if actionable_tf_divergence and not boss_absent:
            corrective_operations.append(
                {
                    "skill_id": operation["skill_id"],
                    "action": operation["action"],
                    "resource_type": operation["resource_type"],
                    "resource_ref": resource_ref,
                    # The DB is canonical: the corrective operation re-asserts
                    # BOSS's desired_state, never the .tf's version of it.
                    "desired_state": desired,
                    "risk_tier": max(int(operation.get("risk_tier") or 2), 2),
                    "destructive": False,
                    "production_affecting": bool(
                        operation.get("production_affecting")
                    ),
                }
            )

    # --- resources in the .tf that BOSS does not manage -------------------- #
    for address, resource in parsed.resources.items():
        if address in claimed_addresses:
            continue
        implied_desired, unrecognized = reverse_field_map(
            "", resource.tf_type, resource.body
        )
        comparisons.append(
            {
                "resource_ref": resource.boss_resource_ref,
                "tf_address": address,
                "tf_type": resource.tf_type,
                "classification": CLASS_TF_ONLY,
                "tf_implied_desired_state": implied_desired,
                "unrecognized_fields": unrecognized,
                "note": "Adoption candidate: present in the .tf but unknown "
                "to BOSS. BOSS does not adopt unknown resources from .tf in "
                "this slice (report-only).",
            }
        )
        warnings.append(
            {
                "kind": "adoption_candidate",
                "tf_address": address,
                "detail": "Resource exists only in the supplied .tf; no "
                "ChangeSet operation proposed (BOSS never adopts desired "
                "state from an untrusted .tf).",
            }
        )

    counts = {name: 0 for name in ALL_CLASSES}
    for entry in comparisons:
        counts[entry["classification"]] += 1
    affected_refs = sorted(
        {
            str(entry["resource_ref"])
            for entry in comparisons
            if entry["classification"] != CLASS_IN_SYNC and entry.get("resource_ref")
        }
    )

    result: dict[str, Any] = {
        "project_id": project_id,
        "input_format": parsed.input_format,
        "dry_run": dry_run,
        "drift_report_id": None,
        "changeset_id": None,
        "counts": counts,
        "resources": comparisons,
        "resource_refs": affected_refs,
        "warnings": warnings,
    }
    if dry_run:
        # Comparison only: no report row, no proposal, no events.
        return result

    divergent = sum(
        counts[name] for name in ALL_CLASSES if name != CLASS_IN_SYNC
    )
    # Mirror the reconciliation loop's classification vocabulary
    # (_create_drift_report): divergence is flagged, clean runs are benign.
    classification = "cost-impacting" if divergent else "benign"
    summary = (
        "Terraform drift-sync: "
        + ", ".join(f"{counts[name]} {name}" for name in ALL_CLASSES)
        + f" across {len(comparisons)} resources."
    )
    recommendations: list[dict[str, Any]] = []
    if corrective_operations:
        recommendations.append(
            {
                "action": "review_drift_sync_proposal",
                "count": len(corrective_operations),
                "requires_approval": True,
            }
        )
    if counts[CLASS_TF_ONLY]:
        recommendations.append(
            {
                "action": "classify_or_import_resources",
                "count": counts[CLASS_TF_ONLY],
            }
        )
    if counts[CLASS_BOSS_ONLY]:
        recommendations.append(
            {
                "action": "reexport_terraform_projection",
                "count": counts[CLASS_BOSS_ONLY],
            }
        )
    detail = {
        "source": "terraform_drift_sync",
        "input_format": parsed.input_format,
        "provider_account_id": provider_account_id,
        "counts": counts,
        "resource_refs": affected_refs,
        "resources": comparisons,
        "warnings": warnings,
        "proposed_changeset_id": None,
    }
    report = plane._create_drift_report(
        connection,
        project_id,
        provider_account_id,
        classification=classification,
        summary=summary,
        unmanaged_count=counts[CLASS_TF_ONLY],
        orphaned_count=counts[CLASS_BOSS_ONLY],
        recommendations=recommendations,
        detail=detail,
    )
    result["drift_report_id"] = report["id"]

    changeset = None
    if corrective_operations:
        changeset = plane.create_changeset(
            connection,
            project_id=project_id,
            provider_account_id=provider_account_id,
            title=DRIFT_PROPOSAL_TITLE,
            summary=(
                f"Corrective proposal from drift report {report['id']}: "
                "realign the provider to BOSS desired state where the "
                "supplied .tf diverged. The BOSS database stays canonical; "
                "rejecting this proposal leaves desired state unchanged."
            ),
            operations=corrective_operations,
            expected_cost_usd_micros=0,
            reservation_id=None,
            rollback_plan=[
                "No provider mutation occurs unless this proposal passes "
                "dual-control approval and is explicitly applied.",
                "Rejecting the proposal leaves BOSS desired state and the "
                "provider untouched.",
                f"If applied, re-run terraform drift reconciliation for "
                f"project {project_id} (drift report {report['id']}) to "
                "verify convergence, and roll back via a follow-up "
                "ChangeSet re-asserting the prior desired state.",
            ],
            risk_tier=2,
            approval_required=True,  # normal dual-control gate, NEVER auto
            actor=actor,
        )
        result["changeset_id"] = changeset["id"]
        detail["proposed_changeset_id"] = changeset["id"]
        for recommendation in recommendations:
            if recommendation["action"] == "review_drift_sync_proposal":
                recommendation["changeset_id"] = changeset["id"]
        connection.execute(
            text(
                """
                UPDATE drift_reports
                SET recommendations = CAST(:recommendations AS JSONB),
                    detail = CAST(:detail AS JSONB)
                WHERE id = :id
                """
            ),
            {
                "id": report["id"],
                "recommendations": json.dumps(recommendations),
                "detail": json.dumps(detail),
            },
        )

    emit_event(
        connection,
        project_id,
        "terraform_drift.reconciled",
        actor,
        "drift_report",
        report["id"],
        {
            "input_format": parsed.input_format,
            "provider_account_id": provider_account_id,
            "counts": counts,
            "resource_refs": affected_refs,
            "proposed_changeset_id": result["changeset_id"],
            "proposal_status": changeset["status"] if changeset else None,
        },
    )
    return result
