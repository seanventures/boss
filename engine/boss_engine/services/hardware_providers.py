"""Community provider places for AWS, DigitalOcean, and a generic VM or container.

A provider place is an account the operator connects so BOSS can propose capacity
somewhere other than the community installer machine. The provider is never the
default hardware. Registration stores a SecretRef, never the secret.
propose_capacity returns a reviewable ChangeSet in the Resource Plane shape and
does not create a VM. A create proposal names an install of
scripts/install/node_agent.sh on that machine — the same hardware agent as the
local community-installer machine — and does not run the script. apply_capacity
is refused without an approval, an active BudgetReservation, and the ChangeSet's
ResourceReceipt dedupe_key. The transport is an offline planner: this module
does not import a provider SDK and does not open a network connection.

ResourcePlaneService is intentionally not imported here. That module constructs
the DigitalOcean adapter and an HTTP client. The gates below follow
packages/specpack/policies/resource_plane_policy.yaml and the ChangeSet fields
from create_changeset / apply_changeset.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml
from sqlalchemy import text

from .core import json_hash, make_id
from .multi_cloud_catalog import provider_entry
from .secret_refs import SecretRefService


class ProviderPlaceError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


ALLOWED_PROVIDER_KEYS = frozenset({"aws", "digitalocean", "generic"})
# One hardware agent for the local installer machine and every proposed VM or container.
HARDWARE_AGENT_INSTALLER = "scripts/install/node_agent.sh"
DEFAULT_HARDWARE = "community_installer"
_CREATE_ACTIONS = frozenset({"create"})
_DESTROY_ACTIONS = frozenset({"destroy", "teardown"})
_KINDS = frozenset({"vm", "container"})
_SENSITIVE_SPEC_KEYS = frozenset(
    {
        "access_key",
        "api_key",
        "authorization",
        "client_secret",
        "password",
        "secret",
        "secret_value",
        "token",
    }
)
_SECRET_MARKERS = ("AKIA", "dop_v1_", "sk-", "sk_live", "xoxb-", "BEGIN PRIVATE KEY")
_SKILLS = {
    ("aws", "vm", "create"): "aws.ec2.instance.create",
    ("aws", "vm", "destroy"): "aws.ec2.instance.destroy",
    ("aws", "container", "create"): "aws.ecs.service.create",
    ("aws", "container", "destroy"): "aws.ecs.service.destroy",
    ("digitalocean", "vm", "create"): "digitalocean.droplet.create",
    ("digitalocean", "vm", "destroy"): "digitalocean.droplet.destroy",
    ("digitalocean", "container", "create"): "digitalocean.app.deploy",
    ("digitalocean", "container", "destroy"): "digitalocean.app.destroy",
    ("generic", "vm", "create"): "generic.vm.create",
    ("generic", "vm", "destroy"): "generic.vm.destroy",
    ("generic", "container", "create"): "generic.container.create",
    ("generic", "container", "destroy"): "generic.container.destroy",
}

# Used only when hardware_manager.register_provider_place is absent.
_CREATE_HARDWARE_PLACES = """
CREATE TABLE IF NOT EXISTS hardware_places (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    status TEXT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    secret_ref TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""
_INSERT_HARDWARE_PLACE = """
INSERT INTO hardware_places (
    id, project_id, kind, provider_key, status, is_default,
    secret_ref, display_name, created_by, created_at
) VALUES (
    :id, :project_id, :kind, :provider_key, :status, :is_default,
    :secret_ref, :display_name, :created_by, :created_at
)
"""
_SELECT_HARDWARE_PLACE = """
SELECT id, project_id, kind, provider_key, status, is_default,
       secret_ref, display_name, created_by, created_at
FROM hardware_places
WHERE id = :id
"""

_POLICY_CACHE: Optional[dict[str, Any]] = None


class PlannedProviderTransport:
    """Offline planner. Returns a planned receipt and performs no I/O."""

    def plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise ProviderPlaceError(
                "transport request must be a mapping",
                code="invalid_transport_request",
            )
        return {
            "status": "planned",
            "dedupe_key": request["dedupe_key"],
            "provider": request["provider_key"],
            "action": request["action"],
            "resource_type": request["resource_type"],
            "resource_ref": request.get("resource_ref"),
            "provider_ref": None,
            "result": {"executed": False, "network": False, "created_vm": False},
            "error": None,
        }


def register_provider_place(
    connection: Any,
    *,
    project_id: str,
    provider_key: str,
    secret_ref: str,
    display_name: Optional[str] = None,
    actor: str = "operator",
    is_default: bool = False,
    secret_value: Optional[str] = None,
) -> dict[str, Any]:
    """Store a provider place. The secret value is rejected, not persisted."""

    key = _provider_key(provider_key)
    ref = _require_secret_ref(secret_ref)
    if secret_value is not None:
        raise ProviderPlaceError(
            "Register stores a secret reference, never the secret value.",
            code="secret_value_rejected",
        )
    if is_default:
        raise ProviderPlaceError(
            "A provider place cannot be the default install place.",
            code="provider_place_cannot_be_default",
        )
    label = _display_name(key, display_name)
    who = _require_text(actor, "actor")
    project = _require_text(project_id, "project_id")
    delegate = _hardware_manager_register()
    if delegate is not None:
        # The kernel records the account on the shared hardware_places table.
        # It does not accept a default flag: a provider place is never default.
        delegated = delegate(
            connection,
            project_id=project,
            name=label,
            provider_key=key,
            actor=who,
            secret_ref=ref,
        )
        place = _public_place(delegated)
        if place["kind"] != "provider" or place["is_default"]:
            raise ProviderPlaceError(
                "A provider place cannot be the default install place.",
                code="provider_place_cannot_be_default",
            )
        return place

    created_at = _now()
    params = {
        "id": make_id("hpl"),
        "project_id": project,
        "kind": "provider",
        "provider_key": key,
        "status": "pending",
        "is_default": False,
        "secret_ref": ref,
        "display_name": label,
        "created_by": who,
        "created_at": created_at,
    }
    connection.execute(text(_CREATE_HARDWARE_PLACES))
    connection.execute(text(_INSERT_HARDWARE_PLACE), params)
    return _public_place(params)


def get_provider_place(connection: Any, place_id: str) -> dict[str, Any]:
    row = (
        connection.execute(
            text(_SELECT_HARDWARE_PLACE),
            {"id": _require_text(place_id, "place_id")},
        )
        .mappings()
        .first()
    )
    if not row:
        raise ProviderPlaceError(
            f"provider place {place_id} was not found",
            code="not_found",
        )
    return _public_place(row)


def propose_capacity(place: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return a reviewable ChangeSet. Does not create a VM or call a provider."""

    if not isinstance(place, Mapping):
        raise ProviderPlaceError("place must be a provider place", code="invalid_place")
    if not isinstance(spec, Mapping):
        raise ProviderPlaceError("spec must be a mapping", code="invalid_spec")
    _reject_sensitive_spec(spec)
    if place.get("kind") not in (None, "provider"):
        raise ProviderPlaceError(
            "propose_capacity requires a provider place",
            code="invalid_place",
        )
    if place.get("is_default"):
        raise ProviderPlaceError(
            "A provider place cannot be the default install place.",
            code="provider_place_cannot_be_default",
        )
    provider_key = _provider_key(str(place.get("provider_key") or ""))
    kind = _kind(spec.get("kind") or "vm")
    action = _action(spec.get("action") or "create")
    name = _require_text(str(spec.get("name") or ""), "spec.name")
    cost = _cost(spec.get("expected_cost_usd_micros", 0))
    rollback_note = str(spec.get("rollback_note") or "").strip()
    destructive = action == "destroy"
    skill_id = _SKILLS[(provider_key, kind, action)]
    resource_ref = f"{provider_key}:{kind}:{name}"
    desired_state = {
        "name": name,
        "kind": kind,
        "provider_key": provider_key,
    }
    for field in ("region", "size", "image"):
        value = spec.get(field)
        if value is not None and str(value).strip():
            desired_state[field] = str(value).strip()
    operation = {
        "skill_id": skill_id,
        "action": action,
        "resource_type": kind,
        "resource_ref": resource_ref,
        "desired_state": desired_state,
        "risk_tier": 3 if destructive else 2,
        "destructive": destructive,
        "production_affecting": False,
    }
    operations = [operation]
    steps = [
        {
            "step": "propose_capacity",
            "index": 0,
            "provider_key": provider_key,
            "kind": kind,
            "name": name,
            "creates_vm": False,
        }
    ]
    # The provider is extra capacity. The machine, once it exists, gets the same
    # agent script as the local community-installer machine. Naming the step does
    # not run it and does not contact the provider.
    if not destructive:
        operations.append(
            {
                "skill_id": "hardware.agent.install",
                "action": "install",
                "resource_type": "hardware_agent",
                "resource_ref": resource_ref,
                "desired_state": {
                    "installer": HARDWARE_AGENT_INSTALLER,
                    "target": resource_ref,
                    "same_agent_as_local": True,
                },
                "risk_tier": 2,
                "destructive": False,
                "production_affecting": False,
            }
        )
        steps.append(
            {
                "step": "install_hardware_agent",
                "index": 1,
                "installer": HARDWARE_AGENT_INSTALLER,
                "target": resource_ref,
                "same_agent_as_local": True,
            }
        )
    changeset_id = make_id("chg")
    dedupe_key = (
        "resource_plane:"
        f"{changeset_id}:0:{skill_id}:{action}:{resource_ref}"
    )
    now = _now()
    policy = _policy_result()
    title_verb = "Tear down" if destructive else "Propose"
    summary = (
        "Reviewable provider-place capacity proposal. "
        "No VM or container is created by this proposal. "
        "The provider is not the default hardware."
    )
    if not destructive:
        summary += (
            " When the machine exists, install the same hardware agent used on "
            f"the local community-installer machine: {HARDWARE_AGENT_INSTALLER}."
        )
    return {
        "id": changeset_id,
        "project_id": place.get("project_id"),
        "provider_account_id": place.get("id"),
        "provider_key": provider_key,
        "title": f"{title_verb} {kind} {name}",
        "summary": summary,
        "status": "pending_approval",
        "risk_tier": operation["risk_tier"],
        "operations": operations,
        "steps": steps,
        "default_hardware": DEFAULT_HARDWARE,
        "provider_is_default": False,
        "hardware_agent": {
            "installer": HARDWARE_AGENT_INSTALLER,
            "install_on_proposed_machine": not destructive,
            "same_agent_as_local": True,
        },
        "expected_cost_usd_micros": cost,
        "cost_quote": {
            "amount_usd_micros": cost,
            "confidence": "estimated_from_price_catalog",
            "exact": False,
        },
        "reservation_id": None,
        "approval_decision_id": None,
        "rollback_plan": [rollback_note] if rollback_note else [],
        "rollback_note": rollback_note or None,
        "expected_receipts": [
            {
                "dedupe_key": dedupe_key,
                "skill_id": skill_id,
                "action": action,
                "resource_type": kind,
            }
        ],
        "policy_result": policy,
        "created_by": "operator",
        "approved_by": None,
        "approved_at": None,
        "applied_by": None,
        "applied_at": None,
        "failure_reason": None,
        "created_at": now,
        "updated_at": now,
    }


def apply_capacity(
    changeset: Mapping[str, Any],
    *,
    approval: Optional[Mapping[str, Any]],
    budget_reservation: Optional[Mapping[str, Any]],
    dedupe_key: Optional[str],
    transport: Optional[Any] = None,
) -> dict[str, Any]:
    """Gate a proposal. A passing create is planned only. Destroy is never executed."""

    if not isinstance(changeset, Mapping):
        raise ProviderPlaceError("changeset must be a mapping", code="invalid_changeset")
    if not _approval_ok(approval):
        raise ProviderPlaceError(
            "Apply requires an approval before any provider action.",
            code="approval_required",
        )
    if not _reservation_ok(budget_reservation):
        raise ProviderPlaceError(
            "Apply requires an active BudgetReservation.",
            code="budget_reservation_required",
        )
    expected = _expected_dedupe_keys(changeset)
    if not isinstance(dedupe_key, str) or dedupe_key not in expected:
        raise ProviderPlaceError(
            "Apply requires a ResourceReceipt dedupe_key that matches the ChangeSet.",
            code="dedupe_key_required",
        )
    operation = _primary_operation(changeset)
    destructive = bool(operation.get("destructive")) or operation.get("action") in _DESTROY_ACTIONS
    if destructive and not _rollback_text(changeset):
        raise ProviderPlaceError(
            "Destructive teardown requires a rollback note on the changeset.",
            code="rollback_note_required",
        )
    reviewed = dict(changeset)
    reviewed["applied_at"] = None
    reviewed["applied_by"] = None
    if destructive:
        reviewed["status"] = "unapplied"
        reviewed["updated_at"] = _now()
        return {
            "status": "unapplied",
            "created_vm": False,
            "changeset": reviewed,
            "resource_receipts": [],
            "reason": "destructive_teardown_is_not_executed",
        }

    planner = transport if transport is not None else PlannedProviderTransport()
    request = {
        "provider_key": changeset.get("provider_key") or _provider_from_operation(operation),
        "action": operation["action"],
        "skill_id": operation["skill_id"],
        "resource_type": operation["resource_type"],
        "resource_ref": operation.get("resource_ref"),
        "desired_state": dict(operation.get("desired_state") or {}),
        "dedupe_key": dedupe_key,
    }
    planned = dict(planner.plan(request) or {})
    result_body = dict(planned.get("result") or {})
    result_body["executed"] = False
    result_body["created_vm"] = False
    result_body.setdefault("network", False)
    now = _now()
    receipt = {
        "id": make_id("rr"),
        "project_id": changeset.get("project_id"),
        "changeset_id": changeset.get("id"),
        "operation_id": None,
        "provider_account_id": changeset.get("provider_account_id"),
        "receipt_id": None,
        "dedupe_key": dedupe_key,
        "provider": request["provider_key"],
        "action": request["action"],
        "resource_type": request["resource_type"],
        "resource_ref": request.get("resource_ref"),
        "status": "planned",
        "provider_ref": None,
        "request_hash": json_hash(
            {key: request[key] for key in request if key != "desired_state"}
            | {"desired_state": request["desired_state"]}
        ),
        "result": result_body,
        "error": None,
        "created_by_job_id": None,
        "created_at": now,
        "updated_at": now,
    }
    reviewed["status"] = "planned"
    reviewed["reservation_id"] = budget_reservation["id"] if budget_reservation else None
    reviewed["approved_by"] = str(approval.get("actor") or "operator") if approval else None
    reviewed["updated_at"] = now
    return {
        "status": "planned",
        "created_vm": False,
        "changeset": reviewed,
        "resource_receipts": [receipt],
    }


def _hardware_manager_register():
    try:
        module = importlib.import_module("boss_engine.services.hardware_manager")
    except ImportError:
        return None
    register = getattr(module, "register_provider_place", None)
    return register if callable(register) else None


def _provider_key(value: str) -> str:
    key = str(value or "").strip().lower()
    if key not in ALLOWED_PROVIDER_KEYS:
        raise ProviderPlaceError(
            f"unknown provider_key: {value}",
            code="unknown_provider",
        )
    return key


def _require_secret_ref(secret_ref: str) -> str:
    if not isinstance(secret_ref, str):
        raise ProviderPlaceError(
            "Register stores a secret reference, never a raw secret.",
            code="secret_reference_required",
        )
    try:
        kind, ref_key = SecretRefService.parse_credential_ref(secret_ref)
    except ValueError as error:
        raise ProviderPlaceError(
            "Register stores a secret reference, never a raw secret.",
            code="secret_reference_required",
        ) from error
    normalized = f"{kind}:{ref_key}"
    _reject_secret_markers(normalized, field="secret_ref")
    return normalized


def _reject_secret_markers(value: str, *, field: str) -> None:
    if any(marker in value for marker in _SECRET_MARKERS):
        raise ProviderPlaceError(
            f"{field} must be a secret reference, not a secret value.",
            code="secret_value_rejected",
        )


def _display_name(provider_key: str, display_name: Optional[str]) -> str:
    if display_name is not None and str(display_name).strip():
        label = str(display_name).strip()
        _reject_secret_markers(label, field="display_name")
        return label
    if provider_key == "generic":
        return "Generic VM or container"
    return str(provider_entry(provider_key)["display_name"])


def _public_place(row: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(row.get("kind") or "provider")
    display_name = row.get("display_name")
    if display_name is None:
        display_name = row.get("name")
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "kind": kind,
        "provider_key": row.get("provider_key"),
        "status": row.get("status") or "pending",
        "is_default": False if kind == "provider" else bool(row.get("is_default")),
        "secret_ref": row.get("secret_ref"),
        "display_name": display_name,
        "created_by": row.get("created_by") or row.get("actor"),
        "created_at": row.get("created_at"),
    }


def _reject_sensitive_spec(spec: Mapping[str, Any]) -> None:
    for key, value in spec.items():
        if str(key).strip().lower() in _SENSITIVE_SPEC_KEYS:
            raise ProviderPlaceError(
                "spec must not carry a secret value; pass a secret reference on the place.",
                code="secret_value_rejected",
            )
        if isinstance(value, str):
            _reject_secret_markers(value, field=f"spec.{key}")


def _kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind not in _KINDS:
        raise ProviderPlaceError(
            "spec.kind must be vm or container",
            code="invalid_kind",
        )
    return kind


def _action(value: Any) -> str:
    action = str(value or "").strip().lower()
    if action in _CREATE_ACTIONS:
        return "create"
    if action in _DESTROY_ACTIONS:
        return "destroy"
    raise ProviderPlaceError(
        "spec.action must be create, destroy, or teardown",
        code="invalid_action",
    )


def _cost(value: Any) -> int:
    try:
        amount = int(value)
    except (TypeError, ValueError) as error:
        raise ProviderPlaceError(
            "expected_cost_usd_micros must be an integer",
            code="invalid_cost",
        ) from error
    if amount < 0:
        raise ProviderPlaceError(
            "expected_cost_usd_micros must be non-negative",
            code="invalid_cost",
        )
    return amount


def _policy_result() -> dict[str, Any]:
    global _POLICY_CACHE
    if _POLICY_CACHE is None:
        _POLICY_CACHE = _load_policy()
    return dict(_POLICY_CACHE)


def _load_policy() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "specpack"
        / "policies"
        / "resource_plane_policy.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = document["rules"]
    actions = rules["provider_actions"]
    secrets = rules["secrets"]
    return {
        "allowed": True,
        "applied": False,
        "creates_vm": False,
        "requires_approval": True,
        "requires_budget_reservation": bool(actions["require_budget_reservation_for_write"]),
        "requires_resource_receipt": bool(actions["require_resource_receipt"]),
        "requires_dedupe_key": bool(actions["require_dedupe_key"]),
        "require_approval_for_destructive": bool(actions["require_approval_for_destructive"]),
        "use_secret_ref_only": bool(secrets["use_secret_ref_only"]),
        "provider_adapters_are_plugins": bool(rules["architecture"]["provider_adapters_are_plugins"]),
        "resource_plane_kernel_is_core": bool(
            rules["architecture"]["resource_plane_kernel_is_core"]
        ),
        "policy_id": document["id"],
    }


def _approval_ok(approval: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(approval, Mapping):
        return False
    if approval.get("approved") is True:
        return True
    return str(approval.get("status") or "").strip().lower() == "approved"


def _reservation_ok(reservation: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(reservation, Mapping):
        return False
    if not str(reservation.get("id") or "").strip():
        return False
    return str(reservation.get("status") or "").strip().lower() == "active"


def _expected_dedupe_keys(changeset: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for receipt in changeset.get("expected_receipts") or []:
        if isinstance(receipt, Mapping) and receipt.get("dedupe_key"):
            keys.add(str(receipt["dedupe_key"]))
    return keys


def _primary_operation(changeset: Mapping[str, Any]) -> dict[str, Any]:
    operations = changeset.get("operations") or []
    if not operations or not isinstance(operations[0], Mapping):
        raise ProviderPlaceError(
            "changeset has no operation to apply",
            code="invalid_changeset",
        )
    return dict(operations[0])


def _rollback_text(changeset: Mapping[str, Any]) -> str:
    note = str(changeset.get("rollback_note") or "").strip()
    if note:
        return note
    parts = []
    for item in changeset.get("rollback_plan") or []:
        text_item = str(item or "").strip()
        if text_item:
            parts.append(text_item)
    return " ".join(parts)


def _provider_from_operation(operation: Mapping[str, Any]) -> str:
    desired = operation.get("desired_state") or {}
    if isinstance(desired, Mapping) and desired.get("provider_key"):
        return _provider_key(str(desired["provider_key"]))
    skill = str(operation.get("skill_id") or "")
    prefix = skill.split(".", 1)[0]
    return _provider_key(prefix)


def _require_text(value: str, field: str) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        raise ProviderPlaceError(f"{field} is required", code="required")
    return text_value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
