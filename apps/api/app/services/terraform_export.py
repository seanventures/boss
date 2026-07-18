"""Terraform/OpenTofu exporter for Resource Plane ChangeSets (Track 1 export, BOSS-66).

THE INVARIANT (docs/planning/oss-readiness/05-infrastructure-as-code.md s2.2/s3.4):
the BOSS database is the canonical source of desired state.  The `.tf.json`
emitted here is a *deterministic downstream projection* of a ChangeSet — never an
authority, never read back as truth, and it never contains a literal secret.
Credentials render as sensitive Terraform variables annotated with the BOSS
SecretRef an operator resolves out-of-band.

Determinism contract: identical input (changeset + operations + mappings) must
produce byte-identical canonical output.  All dict keys are emitted through
``json.dumps(sort_keys=True)`` with fixed separators/indent, addresses are pinned
by the ``terraform_exports`` mapping table, and collisions are suffixed in
operation_index order.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, make_id

TERRAFORM_EXPORT_ARTIFACT_TYPE = "terraform_export"

# Providers the exporter can project, with pinned required_providers entries and
# the credential variables (SecretRef pointers, never literals) each one needs.
PROVIDER_REGISTRY: dict[str, dict[str, Any]] = {
    "aws": {
        "source": "hashicorp/aws",
        "version": "~> 5.0",
        "variables": {
            "aws_access_key_id": "env:BOSS_AWS_ACCESS_KEY_ID",
            "aws_secret_access_key": "env:BOSS_AWS_SECRET_ACCESS_KEY",
        },
    },
    "digitalocean": {
        "source": "digitalocean/digitalocean",
        "version": "~> 2.0",
        "variables": {
            "digitalocean_token": "env:BOSS_DIGITALOCEAN_TOKEN",
        },
    },
    "cloudflare": {
        "source": "cloudflare/cloudflare",
        "version": "~> 4.0",
        "variables": {
            "cloudflare_api_token": "env:BOSS_CLOUDFLARE_API_TOKEN",
        },
    },
}

# (provider, resource_type) and (provider, skill_id) -> Terraform resource type.
# Unmapped combinations are skipped with a structured warning — never invented.
RESOURCE_TYPE_MAP: dict[tuple[str, str], str] = {
    ("aws", "ec2_instance"): "aws_instance",
    ("aws", "aws.ec2.instance.create"): "aws_instance",
    ("aws", "s3_bucket"): "aws_s3_bucket",
    ("aws", "aws.s3.bucket.create"): "aws_s3_bucket",
    ("aws", "rds_instance"): "aws_db_instance",
    ("aws", "aws.rds.instance.create"): "aws_db_instance",
    ("aws", "iam_role"): "aws_iam_role",
    ("aws", "aws.iam.role.upsert"): "aws_iam_role",
    ("aws", "route53_record"): "aws_route53_record",
    ("aws", "aws.route53.record.upsert"): "aws_route53_record",
    ("digitalocean", "droplet"): "digitalocean_droplet",
    ("digitalocean", "digitalocean.droplet.create"): "digitalocean_droplet",
    ("digitalocean", "app"): "digitalocean_app",
    ("digitalocean", "app_deployment"): "digitalocean_app",
    ("digitalocean", "digitalocean.app.deploy"): "digitalocean_app",
    ("cloudflare", "dns_record"): "cloudflare_record",
    ("cloudflare", "cloudflare.dns.record.upsert"): "cloudflare_record",
}

# desired_state keys whose values are secrets and must NEVER be copied into the
# projection.  Keys ending in "_ref" are SecretRef *pointers* and are allowed.
_SECRET_KEY_PATTERN = re.compile(
    r"password|secret|token|credential|api_key|private_key", re.IGNORECASE
)

_DESTROY_ACTIONS = {"destroy", "delete", "teardown"}

_RESERVED_TAG_KEYS = ("Name", "boss-managed", "boss-providerops")


class TerraformExportError(RuntimeError):
    pass


@dataclass
class TerraformExport:
    tf_json: dict[str, Any]
    warnings: list[dict[str, Any]] = field(default_factory=list)
    mapping: list[dict[str, Any]] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)


def canonical_tf_json(tf_json: dict[str, Any]) -> str:
    """The one true serializer: byte-identical output for identical input."""
    return json.dumps(tf_json, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def content_hash_for(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sanitize_tf_name(raw: Any) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw or "").strip())
    if not name:
        name = "resource"
    if not re.match(r"[A-Za-z_]", name):
        name = f"r_{name}"
    return name


def _variable_name_for_secret_ref(secret_ref: str) -> str:
    """Deterministic sensitive-variable name for a SecretRef like env:BOSS_X."""
    _, _, key = str(secret_ref).partition(":")
    slug = re.sub(r"[^a-z0-9_]", "_", (key or secret_ref).lower()).strip("_")
    return slug or "secret_value"


def _sensitive_variable_block(secret_ref: str) -> dict[str, Any]:
    return {
        # "//" is Terraform's JSON-dialect comment key; the SecretRef pointer
        # lives here so operators know which secret to resolve out-of-band.
        "//": {"boss:credential_ref": secret_ref},
        "description": (
            f"Resolved by the operator from BOSS SecretRef {secret_ref}. "
            "BOSS never exports literal secrets."
        ),
        "sensitive": True,
        "type": "string",
    }


def _clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _tags_map(desired: dict[str, Any], name: Optional[str]) -> dict[str, str]:
    """Mirror of the AWS adapters' tag semantics: reserved boss tags win, then
    user tags from the ChangeSet's "key:value" string list."""
    tags: dict[str, str] = {"boss-managed": "true", "boss-providerops": "true"}
    if name:
        tags["Name"] = name
    for tag in desired.get("tags") or []:
        if not isinstance(tag, str) or not tag.strip():
            continue
        key, _, value = tag.strip().partition(":")
        if key and key not in _RESERVED_TAG_KEYS:
            tags[key] = value
    return tags


def _fields_aws_instance(desired: dict, operation: dict) -> tuple[dict, dict]:
    name = str(
        desired.get("name") or operation.get("resource_ref") or ""
    ).strip() or None
    body: dict[str, Any] = {
        "ami": str(desired.get("image_id") or desired.get("ami") or ""),
        "instance_type": str(desired.get("instance_type") or ""),
        "tags": _tags_map(desired, name),
    }
    subnet_id = desired.get("subnet_id")
    if isinstance(subnet_id, str) and subnet_id.strip():
        body["subnet_id"] = subnet_id.strip()
    key_name = desired.get("key_name")
    if isinstance(key_name, str) and key_name.strip():
        body["key_name"] = key_name.strip()
    groups = _clean_str_list(desired.get("security_group_ids"))
    if groups:
        body["vpc_security_group_ids"] = groups
    return body, {}


def _fields_aws_s3_bucket(desired: dict, operation: dict) -> tuple[dict, dict]:
    bucket = str(
        desired.get("bucket_name")
        or desired.get("name")
        or operation.get("resource_ref")
        or ""
    )
    body: dict[str, Any] = {"bucket": bucket}
    if desired.get("versioning"):
        body["versioning"] = {"enabled": True}
    encryption = desired.get("encryption")
    if encryption:
        sse_algorithm = (
            "aws:kms" if str(encryption).lower() in {"kms", "aws:kms"} else "AES256"
        )
        body["server_side_encryption_configuration"] = {
            "rule": {
                "apply_server_side_encryption_by_default": {
                    "sse_algorithm": sse_algorithm
                }
            }
        }
    return body, {}


def _fields_aws_db_instance(desired: dict, operation: dict) -> tuple[dict, dict]:
    body: dict[str, Any] = {
        "identifier": str(
            desired.get("db_instance_identifier")
            or desired.get("name")
            or operation.get("resource_ref")
            or ""
        ),
        "engine": str(desired.get("engine") or ""),
        "instance_class": str(
            desired.get("db_instance_class") or desired.get("instance_class") or ""
        ),
        "allocated_storage": int(
            desired.get("allocated_storage_gb")
            or desired.get("allocated_storage")
            or 0
        ),
        "username": str(desired.get("master_username") or ""),
    }
    extra_variables: dict[str, str] = {}
    secret_ref = desired.get("master_password_secret_ref")
    if isinstance(secret_ref, str) and secret_ref.strip():
        # The password is ALWAYS a var reference to a sensitive variable carrying
        # the SecretRef pointer — the literal value never enters the projection.
        variable_name = _variable_name_for_secret_ref(secret_ref.strip())
        body["password"] = f"${{var.{variable_name}}}"
        extra_variables[variable_name] = secret_ref.strip()
    db_name = desired.get("db_name")
    if isinstance(db_name, str) and db_name.strip():
        body["db_name"] = db_name.strip()
    if desired.get("multi_az") is not None:
        body["multi_az"] = bool(desired["multi_az"])
    if desired.get("publicly_accessible") is not None:
        body["publicly_accessible"] = bool(desired["publicly_accessible"])
    subnet_group = desired.get("db_subnet_group_name")
    if isinstance(subnet_group, str) and subnet_group.strip():
        body["db_subnet_group_name"] = subnet_group.strip()
    groups = _clean_str_list(desired.get("vpc_security_group_ids"))
    if groups:
        body["vpc_security_group_ids"] = groups
    return body, extra_variables


def _fields_aws_iam_role(desired: dict, operation: dict) -> tuple[dict, dict]:
    assume_document = (
        desired.get("assume_role_policy_document") or desired.get("assume_role_policy")
    )
    if isinstance(assume_document, dict):
        assume_text = json.dumps(assume_document, sort_keys=True)
    else:
        assume_text = str(assume_document or "")
    body: dict[str, Any] = {
        "name": str(
            desired.get("role_name")
            or desired.get("name")
            or operation.get("resource_ref")
            or ""
        ),
        "assume_role_policy": assume_text,
    }
    description = desired.get("description")
    if isinstance(description, str) and description.strip():
        body["description"] = description.strip()
    policy_arns = _clean_str_list(
        desired.get("attach_policy_arns") or desired.get("policy_arns")
    )
    if policy_arns:
        body["managed_policy_arns"] = policy_arns
    return body, {}


def _fields_aws_route53_record(desired: dict, operation: dict) -> tuple[dict, dict]:
    raw_values = desired.get("values")
    if not isinstance(raw_values, list):
        raw_values = [desired.get("content") or desired.get("value")]
    values = [str(v).strip() for v in raw_values if v and str(v).strip()]
    body: dict[str, Any] = {
        "zone_id": str(desired.get("hosted_zone_id") or desired.get("zone_id") or ""),
        "name": str(desired.get("name") or ""),
        "type": str(desired.get("record_type") or desired.get("type") or "").upper(),
        "ttl": int(desired.get("ttl") or 300),
        "records": values,
    }
    return body, {}


def _fields_digitalocean_droplet(desired: dict, operation: dict) -> tuple[dict, dict]:
    body: dict[str, Any] = {
        "name": str(desired.get("name") or operation.get("resource_ref") or ""),
        "region": str(desired.get("region") or ""),
        "size": str(desired.get("size") or ""),
        "tags": sorted(
            {"boss-managed", "boss-providerops", *_clean_str_list(desired.get("tags"))}
        ),
    }
    image = desired.get("image")
    if isinstance(image, str) and image.strip():
        body["image"] = image.strip()
    return body, {}


def _fields_digitalocean_app(desired: dict, operation: dict) -> tuple[dict, dict]:
    body: dict[str, Any] = {}
    spec = desired.get("spec")
    if isinstance(spec, dict) and spec:
        body["spec"] = spec
    return body, {}


def _fields_cloudflare_record(desired: dict, operation: dict) -> tuple[dict, dict]:
    body: dict[str, Any] = {
        "zone_id": str(desired.get("zone_id") or ""),
        "name": str(desired.get("name") or ""),
        "type": str(desired.get("record_type") or desired.get("type") or "").upper(),
        "value": str(desired.get("content") or desired.get("value") or ""),
        "ttl": int(desired.get("ttl") or 1),
    }
    if "proxied" in desired:
        body["proxied"] = bool(desired["proxied"])
    if desired.get("priority") is not None:
        body["priority"] = int(desired["priority"])
    return body, {}


_FIELD_TRANSLATORS = {
    "aws_instance": _fields_aws_instance,
    "aws_s3_bucket": _fields_aws_s3_bucket,
    "aws_db_instance": _fields_aws_db_instance,
    "aws_iam_role": _fields_aws_iam_role,
    "aws_route53_record": _fields_aws_route53_record,
    "digitalocean_droplet": _fields_digitalocean_droplet,
    "digitalocean_app": _fields_digitalocean_app,
    "cloudflare_record": _fields_cloudflare_record,
}


def _provider_block(provider: str, aws_region: str) -> dict[str, Any]:
    if provider == "aws":
        return {
            "access_key": "${var.aws_access_key_id}",
            "region": aws_region,
            "secret_key": "${var.aws_secret_access_key}",
        }
    if provider == "digitalocean":
        return {"token": "${var.digitalocean_token}"}
    if provider == "cloudflare":
        return {"api_token": "${var.cloudflare_api_token}"}
    raise TerraformExportError(f"No provider block template for '{provider}'.")


_PROVIDER_REF_PATTERN = re.compile(r"^([a-z0-9_]+):([a-z0-9_]+):(.+)$")


def import_id_from_receipt(tf_type: str, receipt: dict[str, Any]) -> Optional[str]:
    """External id for a Terraform >=1.5 import block.

    BOSS-65 AWS handlers emit provider_ref "provider:type:id"
    (e.g. "aws:ec2_instance:i-0abc..."); DigitalOcean emits an API URL, for
    which the receipt result's resource_external_id is used instead.
    """
    provider_ref = str(receipt.get("provider_ref") or "")
    match = _PROVIDER_REF_PATTERN.match(provider_ref)
    if match:
        rest = match.group(3)
        if tf_type == "aws_route53_record":
            # provider_ref carries "zone:TYPE:name"; Terraform imports want
            # "ZONEID_name_TYPE".
            parts = rest.split(":", 2)
            if len(parts) == 3:
                zone, record_type, name = parts
                return f"{zone}_{name}_{record_type}"
        return rest
    result = receipt.get("result")
    if isinstance(result, dict) and result.get("resource_external_id"):
        return str(result["resource_external_id"])
    return None


def _provider_for_operation(operation: dict[str, Any]) -> str:
    skill_id = str(operation.get("skill_id") or "")
    return skill_id.split(".", 1)[0] if skill_id else ""


def _collect_secret_literals(operations: list[dict[str, Any]]) -> set[str]:
    """Values of secret-looking desired_state keys (excluding *_ref pointers)."""
    denied: set[str] = set()
    for operation in operations:
        desired = operation.get("desired_state") or {}
        if not isinstance(desired, dict):
            continue
        for key, value in desired.items():
            if not _SECRET_KEY_PATTERN.search(str(key)):
                continue
            if str(key).lower().endswith("_ref"):
                continue
            if isinstance(value, str) and value.strip():
                denied.add(value)
    return denied


def changeset_to_tf(
    changeset: dict[str, Any],
    operations: list[dict[str, Any]],
    *,
    mappings: dict[str, str],
    receipts_by_operation_index: Optional[dict[int, dict[str, Any]]] = None,
    aws_region: str = "us-east-1",
) -> TerraformExport:
    """Pure projection: (changeset, operations, mappings) -> .tf.json document.

    Deterministic and side-effect free.  ``mappings`` is the persisted
    resource_ref -> tf_address table; matching addresses are reused verbatim so
    re-exports stay byte-stable, and new addresses collide deterministically
    (_2, _3 in operation_index order).
    """
    receipts_by_operation_index = receipts_by_operation_index or {}
    warnings: list[dict[str, Any]] = []
    mapping_entries: list[dict[str, Any]] = []
    resources: dict[str, dict[str, dict[str, Any]]] = {}
    variables: dict[str, dict[str, Any]] = {}
    imports: list[dict[str, str]] = []
    providers_used: set[str] = set()
    used_addresses: set[str] = set()

    ordered = sorted(
        operations, key=lambda op: int(op.get("operation_index") or 0)
    )

    for operation in ordered:
        operation_index = int(operation.get("operation_index") or 0)
        skill_id = str(operation.get("skill_id") or "")
        resource_type = str(operation.get("resource_type") or "")
        resource_ref = operation.get("resource_ref")
        action = str(operation.get("action") or "").lower()
        provider = _provider_for_operation(operation)

        base_warning = {
            "operation_index": operation_index,
            "skill_id": skill_id,
            "resource_type": resource_type,
            "resource_ref": resource_ref,
        }
        if provider not in PROVIDER_REGISTRY:
            warnings.append(
                {
                    **base_warning,
                    "kind": "unmapped_resource_type",
                    "detail": (
                        f"Provider '{provider or 'unknown'}' has no Terraform "
                        "projection; operation skipped (export never invents "
                        "resources)."
                    ),
                }
            )
            continue
        tf_type = RESOURCE_TYPE_MAP.get((provider, resource_type)) or (
            RESOURCE_TYPE_MAP.get((provider, skill_id))
        )
        if not tf_type:
            warnings.append(
                {
                    **base_warning,
                    "kind": "unmapped_resource_type",
                    "detail": (
                        f"No Terraform resource type mapped for provider "
                        f"'{provider}' resource_type '{resource_type}'; "
                        "operation skipped."
                    ),
                }
            )
            continue
        if action in _DESTROY_ACTIONS:
            # Destroy means desired absence: the projection simply omits the
            # block (the DB, not .tf, is the authority on what should exist).
            warnings.append(
                {
                    **base_warning,
                    "kind": "destroy_not_projected",
                    "detail": (
                        "Destroy operations project as absence; no resource "
                        "block emitted."
                    ),
                }
            )
            continue

        ref_key = str(resource_ref) if resource_ref else None
        stored_address = mappings.get(ref_key) if ref_key else None
        reused = bool(
            stored_address
            and stored_address.startswith(f"{tf_type}.")
            and stored_address not in used_addresses
        )
        if reused:
            address = stored_address
        else:
            fallback_name = ref_key or f"{resource_type}_{operation_index}"
            base_address = f"{tf_type}.{sanitize_tf_name(fallback_name)}"
            address = base_address
            suffix = 2
            while address in used_addresses:
                address = f"{base_address}_{suffix}"
                suffix += 1
        used_addresses.add(address)
        resource_name = address.split(".", 1)[1]

        desired = operation.get("desired_state") or {}
        if not isinstance(desired, dict):
            desired = {}
        translator = _FIELD_TRANSLATORS[tf_type]
        body, extra_variables = translator(desired, operation)
        body["//"] = {
            "boss:operation_index": operation_index,
            "boss:resource_ref": ref_key,
            "boss:skill_id": skill_id,
        }
        resources.setdefault(tf_type, {})[resource_name] = body
        providers_used.add(provider)
        for variable_name, secret_ref in extra_variables.items():
            variables[variable_name] = _sensitive_variable_block(secret_ref)

        receipt = receipts_by_operation_index.get(operation_index)
        if receipt and str(receipt.get("status")) == "succeeded":
            import_id = import_id_from_receipt(tf_type, receipt)
            if import_id:
                imports.append({"id": import_id, "to": address})
            else:
                warnings.append(
                    {
                        **base_warning,
                        "kind": "import_id_unresolved",
                        "detail": (
                            "Succeeded receipt has no parseable external id; "
                            f"import block omitted (provider_ref="
                            f"{receipt.get('provider_ref')!r})."
                        ),
                    }
                )

        mapping_entries.append(
            {
                "operation_index": operation_index,
                "provider": provider,
                "resource_ref": ref_key,
                "reused": reused,
                "tf_address": address,
                "tf_type": tf_type,
            }
        )

    for provider in sorted(providers_used):
        for variable_name, secret_ref in PROVIDER_REGISTRY[provider][
            "variables"
        ].items():
            variables[variable_name] = _sensitive_variable_block(secret_ref)

    tf_json: dict[str, Any] = {
        "//": {
            "boss:changeset_id": changeset.get("id"),
            "boss:generator": "boss.terraform_export",
            "boss:invariant": (
                "BOSS database is canonical; this file is a deterministic "
                "downstream projection, never an authority."
            ),
            "boss:project_id": changeset.get("project_id"),
        },
        "terraform": {
            "required_providers": {
                provider: {
                    "source": PROVIDER_REGISTRY[provider]["source"],
                    "version": PROVIDER_REGISTRY[provider]["version"],
                }
                for provider in sorted(providers_used)
            },
            "required_version": ">= 1.5.0",
        },
    }
    if providers_used:
        tf_json["provider"] = {
            provider: _provider_block(provider, aws_region)
            for provider in sorted(providers_used)
        }
    if variables:
        tf_json["variable"] = variables
    if resources:
        tf_json["resource"] = resources
    if imports:
        tf_json["import"] = imports

    # Belt-and-braces invariant check: no secret-valued desired_state literal
    # may survive into the serialized projection.
    canonical = canonical_tf_json(tf_json)
    for literal in _collect_secret_literals(ordered):
        if literal in canonical:
            raise TerraformExportError(
                "Refusing to export: a secret-valued desired_state literal "
                "leaked into the Terraform projection."
            )

    return TerraformExport(
        tf_json=tf_json,
        warnings=warnings,
        mapping=mapping_entries,
        providers=sorted(providers_used),
    )


# --------------------------------------------------------------------------- #
# DB orchestration                                                            #
# --------------------------------------------------------------------------- #


def count_terraform_export_targets(connection: Connection, project_id: str) -> int:
    """Opt-in gate: number of deployment targets that enabled terraform_export."""
    row = connection.execute(
        text(
            """
            SELECT COUNT(*) AS enabled
            FROM deployment_targets
            WHERE project_id = :project_id AND terraform_export = true
            """
        ),
        {"project_id": project_id},
    ).mappings().one()
    return int(row["enabled"])


def _load_changeset(connection: Connection, changeset_id: str) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT id, project_id, title, summary, status, operations,
                   created_at, updated_at
            FROM changesets
            WHERE id = :changeset_id
            """
        ),
        {"changeset_id": changeset_id},
    ).mappings().first()
    if not row:
        raise ValueError("ChangeSet not found.")
    return dict(row)


def _load_operations(
    connection: Connection, changeset: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT id, operation_index, skill_id, action, resource_type,
                   resource_ref, desired_state
            FROM changeset_operations
            WHERE changeset_id = :changeset_id
            ORDER BY operation_index ASC
            """
        ),
        {"changeset_id": changeset["id"]},
    ).mappings().all()
    if rows:
        return [dict(row) for row in rows]
    # Legacy fallback: older ChangeSets carry operations only as JSONB.
    operations = changeset.get("operations") or []
    return [
        {**dict(operation), "id": None, "operation_index": index}
        for index, operation in enumerate(operations)
        if isinstance(operation, dict)
    ]


def _load_succeeded_receipts_by_index(
    connection: Connection,
    changeset_id: str,
    operations: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    index_by_operation_id = {
        operation["id"]: int(operation["operation_index"])
        for operation in operations
        if operation.get("id")
    }
    rows = connection.execute(
        text(
            """
            SELECT operation_id, status, provider_ref, result
            FROM resource_receipts
            WHERE changeset_id = :changeset_id AND status = 'succeeded'
            """
        ),
        {"changeset_id": changeset_id},
    ).mappings().all()
    receipts: dict[int, dict[str, Any]] = {}
    for row in rows:
        operation_index = index_by_operation_id.get(row["operation_id"])
        if operation_index is not None:
            receipts[operation_index] = dict(row)
    return receipts


def _load_mappings(connection: Connection, project_id: str) -> dict[str, str]:
    rows = connection.execute(
        text(
            """
            SELECT resource_ref, tf_address
            FROM terraform_exports
            WHERE project_id = :project_id
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return {row["resource_ref"]: row["tf_address"] for row in rows}


def _upsert_mappings(
    connection: Connection,
    *,
    project_id: str,
    changeset_id: str,
    artifact_id: str,
    content_hash: str,
    mapping_entries: list[dict[str, Any]],
) -> None:
    seen_refs: set[str] = set()
    for entry in mapping_entries:
        resource_ref = entry.get("resource_ref")
        if not resource_ref or resource_ref in seen_refs:
            continue
        seen_refs.add(resource_ref)
        connection.execute(
            text(
                """
                INSERT INTO terraform_exports (
                    id, project_id, changeset_id, resource_ref, tf_address,
                    provider, artifact_id, content_hash
                )
                VALUES (
                    :id, :project_id, :changeset_id, :resource_ref, :tf_address,
                    :provider, :artifact_id, :content_hash
                )
                ON CONFLICT (project_id, resource_ref) DO UPDATE
                SET tf_address = EXCLUDED.tf_address,
                    provider = EXCLUDED.provider,
                    changeset_id = EXCLUDED.changeset_id,
                    artifact_id = EXCLUDED.artifact_id,
                    content_hash = EXCLUDED.content_hash,
                    updated_at = now()
                """
            ),
            {
                "id": make_id("tfexp"),
                "project_id": project_id,
                "changeset_id": changeset_id,
                "resource_ref": resource_ref,
                "tf_address": entry["tf_address"],
                "provider": entry["provider"],
                "artifact_id": artifact_id,
                "content_hash": content_hash,
            },
        )


def export_changeset(
    connection: Connection,
    settings: Settings,
    changeset_id: str,
    *,
    actor: str,
) -> dict[str, Any]:
    """Project a ChangeSet to .tf.json, store it as a durable artifact, and pin
    the resource_ref -> tf_address mappings for byte-stable re-exports."""
    changeset = _load_changeset(connection, changeset_id)
    project_id = changeset["project_id"]
    operations = _load_operations(connection, changeset)
    receipts = _load_succeeded_receipts_by_index(connection, changeset_id, operations)
    mappings = _load_mappings(connection, project_id)

    export = changeset_to_tf(
        changeset,
        operations,
        mappings=mappings,
        receipts_by_operation_index=receipts,
        aws_region=settings.aws_region,
    )
    canonical = canonical_tf_json(export.tf_json)
    content_hash = content_hash_for(canonical)

    content = {
        "kind": TERRAFORM_EXPORT_ARTIFACT_TYPE,
        "changeset_id": changeset_id,
        "project_id": project_id,
        "changeset_status": changeset.get("status"),
        "content_hash": content_hash,
        "tf_json": export.tf_json,
        # JSONB reorders keys, so the byte-exact projection is preserved as a
        # string alongside the structured document.
        "tf_json_canonical": canonical,
        "warnings": export.warnings,
        "mapping": export.mapping,
        "providers": export.providers,
        "source_of_truth": "boss_database",
    }
    artifact_id = create_artifact(
        connection,
        settings,
        project_id,
        None,
        TERRAFORM_EXPORT_ARTIFACT_TYPE,
        f"Terraform export for ChangeSet {changeset_id}",
        content,
    )
    _upsert_mappings(
        connection,
        project_id=project_id,
        changeset_id=changeset_id,
        artifact_id=artifact_id,
        content_hash=content_hash,
        mapping_entries=export.mapping,
    )
    emit_event(
        connection,
        project_id,
        "terraform_export.created",
        actor,
        "changeset",
        changeset_id,
        {
            "artifact_id": artifact_id,
            "content_hash": content_hash,
            "providers": export.providers,
            "warning_count": len(export.warnings),
        },
    )
    return {
        "artifact_id": artifact_id,
        "changeset_id": changeset_id,
        "project_id": project_id,
        "content_hash": content_hash,
        "tf_json": export.tf_json,
        "canonical": canonical,
        "warnings": export.warnings,
        "mapping": export.mapping,
        "providers": export.providers,
    }


def latest_export_artifact(
    connection: Connection, project_id: str, changeset_id: str
) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text(
            """
            SELECT id, project_id, content, content_hash, created_at
            FROM artifacts
            WHERE project_id = :project_id
              AND artifact_type = :artifact_type
              AND content->>'changeset_id' = :changeset_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {
            "project_id": project_id,
            "artifact_type": TERRAFORM_EXPORT_ARTIFACT_TYPE,
            "changeset_id": changeset_id,
        },
    ).mappings().first()
    return dict(row) if row else None


def commit_export_to_repo(
    settings: Settings,
    changeset_id: str,
    canonical: str,
    *,
    branch: str,
    path: Optional[str] = None,
    message: Optional[str] = None,
) -> dict[str, Any]:
    """Explicitly requested repo mirror of the projection.  Never wired into an
    automatic path — the repo copy is a convenience mirror, not an authority."""
    from .github_adapter import GitHubAdapter

    adapter = GitHubAdapter(settings)
    target_path = path or f"infra/boss/{changeset_id}.tf.json"
    adapter.ensure_branch(branch)
    commit_sha = adapter.upsert_file(
        branch,
        target_path,
        canonical,
        message
        or (
            f"chore(iac): export ChangeSet {changeset_id} as Terraform projection "
            "(BOSS DB remains canonical)"
        ),
    )
    return {"branch": branch, "path": target_path, "commit_sha": commit_sha}
