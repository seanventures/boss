from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import (
    create_artifact,
    create_blocking_decision,
    emit_event,
    json_hash,
    make_id,
    now_utc,
)
from .digitalocean_adapter import DigitalOceanAdapter
from .ssh_host_adapter import SSHHostAdapter
from .receipts import ReceiptService
from .secret_refs import SecretRefNotFoundError, SecretRefService


class ProviderAccountNotFoundError(RuntimeError):
    pass


class ChangeSetNotFoundError(RuntimeError):
    pass


class ProviderCredentialError(RuntimeError):
    pass


class ProviderAdapterError(RuntimeError):
    pass


SUPPORTED_PROVIDERS = {
    "aws": {
        "credential_ref": "env:BOSS_AWS_SECRET_ACCESS_KEY",
        "capabilities": [
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
        ],
    },
    "digitalocean": {
        "credential_ref": "env:BOSS_DIGITALOCEAN_TOKEN",
        "capabilities": [
            "provider_connection_test",
            "resource_inventory_sync",
            "digitalocean.droplet.create",
            "digitalocean.droplet.destroy",
            "digitalocean.app.deploy",
            "digitalocean.firewall.ensure",
            "digitalocean.floating_ip.create",
            "digitalocean.inference.chat",
            "digitalocean.billing.sync",
        ],
    },
    "azure": {
        "credential_ref": "env:BOSS_AZURE_CLIENT_SECRET",
        "capabilities": [
            "provider_connection_test",
            "resource_inventory_sync",
            "azure.resource_group.ensure",
            "azure.vm.create",
            "azure.vm.destroy",
            "azure.openai.chat",
        ],
    },
    "gcp": {
        "credential_ref": "env:BOSS_GCP_SERVICE_ACCOUNT",
        "capabilities": [],
    },
    "ovhcloud": {
        "credential_ref": "env:BOSS_OVH_APPLICATION_SECRET",
        "capabilities": [
            "provider_connection_test",
            "resource_inventory_sync",
            "ovh.dedicated.server.list",
            "ovh.cloud.instance.create",
            "ovh.cloud.instance.destroy",
        ],
    },
    "cloudflare": {
        "credential_ref": "env:BOSS_CLOUDFLARE_API_TOKEN",
        "capabilities": [
            "provider_connection_test",
            "resource_inventory_sync",
            "cloudflare.dns.record.upsert",
            "cloudflare.worker.deploy",
            "cloudflare.pages.deploy",
            "cloudflare.r2.bucket.create",
        ],
    },
    "supabase": {
        "credential_ref": "env:BOSS_SUPABASE_ACCESS_TOKEN",
        "capabilities": [
            "provider_connection_test",
            "resource_inventory_sync",
            "supabase.project.create",
            "supabase.project.pause",
            "supabase.migration.apply",
        ],
    },
    "openai": {
        "credential_ref": "env:OPENAI_API_KEY",
        "capabilities": [
            "provider_connection_test",
            "openai.costs.sync",
        ],
    },
    "openai_compatible": {
        "credential_ref": "env:OPENAI_API_KEY",
        "capabilities": [
            "provider_connection_test",
            "openai.costs.sync",
        ],
    },
    "anthropic": {
        "credential_ref": "env:ANTHROPIC_API_KEY",
        "capabilities": [
            "provider_connection_test",
            "anthropic.usage_cost.sync",
        ],
    },
    "ssh_host": {
        "credential_ref": "env:BOSS_SSH_HOST_PRIVATE_KEY",
        "capabilities": [
            "provider_connection_test",
            "resource_inventory_sync",
            "ssh_host.workload.deploy",
            "ssh_host.workload.health_check",
            "ssh_host.workload.teardown",
            "ssh_host.job.run",
        ],
    },
}

PROVIDER_CONNECTION_PROFILES: dict[str, dict[str, Any]] = {
    "aws": {
        "display_name": "Amazon Web Services",
        "category": "infrastructure",
        "adapter_status": "available",
        "default_auth_method": "access_key",
        "auth_methods": {
            "access_key": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "access_key_id",
                        "secret_kind": "access_key",
                        "required": True,
                        "default_ref": "env:BOSS_AWS_ACCESS_KEY_ID",
                    },
                    {
                        "key": "secret_access_key",
                        "secret_kind": "client_secret",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_AWS_SECRET_ACCESS_KEY",
                    },
                    {
                        "key": "session_token",
                        "secret_kind": "session",
                        "required": False,
                        "default_ref": None,
                    },
                ],
            },
            "assume_role": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "access_key_id",
                        "secret_kind": "access_key",
                        "required": True,
                        "default_ref": "env:BOSS_AWS_ACCESS_KEY_ID",
                    },
                    {
                        "key": "secret_access_key",
                        "secret_kind": "client_secret",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_AWS_SECRET_ACCESS_KEY",
                    },
                ],
            },
            "workload_identity": {
                "runtime_status": "planned",
                "credential_slots": [],
            },
        },
    },
    "azure": {
        "display_name": "Microsoft Azure",
        "category": "infrastructure",
        "adapter_status": "available",
        "default_auth_method": "service_principal",
        "auth_methods": {
            "service_principal": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "tenant_id",
                        "secret_kind": "access_key",
                        "required": True,
                        "default_ref": "env:BOSS_AZURE_TENANT_ID",
                    },
                    {
                        "key": "client_id",
                        "secret_kind": "access_key",
                        "required": True,
                        "default_ref": "env:BOSS_AZURE_CLIENT_ID",
                    },
                    {
                        "key": "client_secret",
                        "secret_kind": "client_secret",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_AZURE_CLIENT_SECRET",
                    },
                    {
                        "key": "subscription_id",
                        "secret_kind": "access_key",
                        "required": True,
                        "default_ref": "env:BOSS_AZURE_SUBSCRIPTION_ID",
                    },
                ],
            },
            "workload_identity": {
                "runtime_status": "planned",
                "credential_slots": [],
            },
        },
    },
    "gcp": {
        "display_name": "Google Cloud",
        "category": "infrastructure",
        "adapter_status": "planned",
        "default_auth_method": "service_account",
        "auth_methods": {
            "service_account": {
                "runtime_status": "planned",
                "credential_slots": [
                    {
                        "key": "service_account",
                        "secret_kind": "service_account",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_GCP_SERVICE_ACCOUNT",
                    }
                ],
            },
            "workload_identity": {
                "runtime_status": "planned",
                "credential_slots": [],
            },
        },
    },
    "digitalocean": {
        "display_name": "DigitalOcean",
        "category": "infrastructure",
        "adapter_status": "available",
        "default_auth_method": "api_token",
        "auth_methods": {
            "api_token": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "token",
                        "secret_kind": "api_token",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_DIGITALOCEAN_TOKEN",
                    }
                ],
            }
        },
    },
    "ovhcloud": {
        "display_name": "OVHcloud",
        "category": "infrastructure",
        "adapter_status": "available",
        "default_auth_method": "application_consumer_key",
        "auth_methods": {
            "application_consumer_key": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "application_key",
                        "secret_kind": "access_key",
                        "required": True,
                        "default_ref": "env:BOSS_OVH_APPLICATION_KEY",
                    },
                    {
                        "key": "application_secret",
                        "secret_kind": "client_secret",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_OVH_APPLICATION_SECRET",
                    },
                    {
                        "key": "consumer_key",
                        "secret_kind": "consumer_key",
                        "required": True,
                        "default_ref": "env:BOSS_OVH_CONSUMER_KEY",
                    },
                    {
                        "key": "endpoint",
                        "secret_kind": "access_key",
                        "required": False,
                        "default_ref": "env:BOSS_OVH_ENDPOINT",
                    },
                    {
                        "key": "cloud_project_id",
                        "secret_kind": "access_key",
                        "required": False,
                        "default_ref": "env:BOSS_OVH_CLOUD_PROJECT_ID",
                    },
                ],
            }
        },
    },
    "openai": {
        "display_name": "OpenAI",
        "category": "models",
        "adapter_status": "available",
        "default_auth_method": "api_key",
        "auth_methods": {
            "api_key": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "api_key",
                        "secret_kind": "api_token",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:OPENAI_API_KEY",
                    }
                ],
            }
        },
    },
    "openai_compatible": {
        "display_name": "OpenAI-compatible (Ollama / vLLM)",
        "category": "models",
        "adapter_status": "available",
        "default_auth_method": "api_key",
        "auth_methods": {
            "api_key": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "api_key",
                        "secret_kind": "api_token",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:OPENAI_API_KEY",
                    }
                ],
            }
        },
    },
    "anthropic": {
        "display_name": "Anthropic",
        "category": "models",
        "adapter_status": "available",
        "default_auth_method": "api_key",
        "auth_methods": {
            "api_key": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "api_key",
                        "secret_kind": "api_token",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:ANTHROPIC_API_KEY",
                    }
                ],
            }
        },
    },
    "cloudflare": {
        "display_name": "Cloudflare",
        "category": "infrastructure",
        "adapter_status": "available",
        "default_auth_method": "api_token",
        "auth_methods": {
            "api_token": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "token",
                        "secret_kind": "api_token",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_CLOUDFLARE_API_TOKEN",
                    }
                ],
            }
        },
    },
    "supabase": {
        "display_name": "Supabase",
        "category": "infrastructure",
        "adapter_status": "available",
        "default_auth_method": "api_token",
        "auth_methods": {
            "api_token": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "token",
                        "secret_kind": "api_token",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_SUPABASE_ACCESS_TOKEN",
                    }
                ],
            }
        },
    },
    "ssh_host": {
        "display_name": "SSH host",
        "category": "infrastructure",
        "adapter_status": "available",
        "default_auth_method": "private_key",
        "auth_methods": {
            "private_key": {
                "runtime_status": "available",
                "credential_slots": [
                    {
                        "key": "private_key",
                        "secret_kind": "private_key",
                        "required": True,
                        "primary": True,
                        "default_ref": "env:BOSS_SSH_HOST_PRIVATE_KEY",
                    }
                ],
            }
        },
    },
}

DEPLOYMENT_ROLLOUT_MODES = {
    "disabled",
    "metadata_only",
    "preview_allowed",
    "staging_allowed",
    "production_allowed",
}
DEPLOYMENT_SANDBOX_ENVIRONMENTS = {
    "local",
    "dev",
    "development",
    "test",
    "sandbox",
    "preview",
}
DEPLOYMENT_SKILL_IDS = {
    "cloudflare.worker.deploy",
    "cloudflare.pages.deploy",
    "digitalocean.app.deploy",
    "supabase.migration.apply",
    "ssh_host.workload.deploy",
}


@dataclass
class InventoryResource:
    provider: str
    resource_type: str
    external_id: str
    name: str
    state: str
    ownership: str
    environment: str
    health_status: str
    tags: list[str]
    metadata: dict[str, Any]
    cost_confidence: str = "unknown"
    estimated_monthly_usd_micros: Optional[int] = None
    actual_monthly_usd_micros: Optional[int] = None


@dataclass
class ProviderCostRecord:
    provider: str
    category: str
    amount_usd_micros: int
    cost_layer: str
    confidence: str
    source_ref: str
    period_start: Optional[datetime]
    period_end: Optional[datetime]
    metadata: dict[str, Any]
    billing_account_external_id: Optional[str] = None
    billing_account_label: Optional[str] = None
    external_resource_external_id: Optional[str] = None


#: How long one deployment may hold a surface before the claim lapses.
#:
#: Longer than any real deploy (the connector's own build+ship path is minutes) and short
#: enough that a crashed deployer does not block the next attempt for a working day.
#:
#: Twenty minutes, not an hour. The first version of this was 3600, which is defensible as a
#: safety margin and awful to live with: after a failed deploy the operator is locked out of
#: their own target for the rest of the hour, staring at "owned by another deployer" with
#: nothing to do about it. The margin over a real deploy is what matters, not the absolute
#: number, and 20 minutes is several times the longest build-and-ship observed.
_SURFACE_LEASE_SECONDS = 1200


class ResourcePlaneService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()
        self.digitalocean = DigitalOceanAdapter(settings)
        self.ssh_host = SSHHostAdapter(settings)
        self.secret_refs = SecretRefService()

    def list_provider_definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for provider, profile in sorted(PROVIDER_CONNECTION_PROFILES.items()):
            auth_methods = []
            for key, method in profile["auth_methods"].items():
                auth_methods.append(
                    {
                        "key": key,
                        "runtime_status": method["runtime_status"],
                        "credential_slots": [
                            {
                                "key": slot["key"],
                                "secret_kind": slot["secret_kind"],
                                "required": bool(slot["required"]),
                                "default_ref": slot.get("default_ref"),
                            }
                            for slot in method["credential_slots"]
                        ],
                    }
                )
            definitions.append(
                {
                    "provider": provider,
                    "display_name": profile["display_name"],
                    "category": profile["category"],
                    "adapter_status": profile["adapter_status"],
                    "default_auth_method": profile["default_auth_method"],
                    "auth_methods": auth_methods,
                    "capabilities": list(
                        SUPPORTED_PROVIDERS.get(provider, {}).get("capabilities") or []
                    ),
                }
            )
        return definitions

    def list_provider_accounts(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, provider, account_label, auth_method,
                       credential_ref, credential_refs, connection_metadata, scopes,
                       status, owner_actor, credential_version, credential_rotated_at,
                       disabled_at, disabled_by, revoked_at, revoked_by,
                       read_only, deploy_capable, billing_readable,
                       destructive_actions_disabled, capabilities, missing_permissions,
                       last_test_status, last_tested_at, last_error, created_by,
                       created_at, updated_at
                FROM provider_accounts
                WHERE project_id = :project_id
                ORDER BY provider ASC, account_label ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize_provider_account(row) for row in rows]

    def create_provider_account(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        account_label: str,
        auth_method: Optional[str] = None,
        credential_ref: Optional[str] = None,
        credential_refs: Optional[dict[str, str]] = None,
        connection_metadata: Optional[dict[str, Any]] = None,
        scopes: list[str],
        read_only: bool,
        deploy_capable: bool,
        billing_readable: bool,
        destructive_actions_disabled: bool,
        actor: str,
    ) -> dict[str, Any]:
        normalized_provider = self._normalize_provider(provider)
        normalized_label = self._require_text(account_label, "account_label")
        normalized_auth_method, refs, ref, slot_kinds = (
            self._normalize_provider_credentials(
                normalized_provider,
                auth_method=auth_method,
                credential_ref=credential_ref,
                credential_refs=credential_refs or {},
            )
        )
        safe_connection_metadata = self.secret_refs._validate_metadata(
            connection_metadata or {}
        )
        existing = connection.execute(
            text(
                """
                SELECT id, status, auth_method, credential_ref, credential_refs
                FROM provider_accounts
                WHERE project_id = :project_id
                  AND provider = :provider
                  AND account_label = :account_label
                """
            ),
            {
                "project_id": project_id,
                "provider": normalized_provider,
                "account_label": normalized_label,
            },
        ).mappings().first()
        if existing and existing["status"] in {"disabled", "revoked"}:
            raise ValueError(
                f"Provider account is {existing['status']}; use the explicit lifecycle endpoint."
            )
        if existing:
            existing_refs = existing["credential_refs"] or (
                {"primary": existing["credential_ref"]}
                if existing["credential_ref"]
                else {}
            )
            if dict(existing_refs) != refs:
                raise ValueError(
                    "Provider credentials changed; use the explicit rotation endpoint."
                )
        account_status = (
            "active"
            if self._provider_adapter_status(normalized_provider) == "available"
            else "pending_adapter"
        )
        capabilities = SUPPORTED_PROVIDERS[normalized_provider]["capabilities"]
        provider_account_id = make_id("prov")
        for slot, credential_pointer in refs.items():
            self._ensure_secret_ref(
                connection,
                project_id=project_id,
                provider=normalized_provider,
                credential_ref=credential_pointer,
                secret_kind=slot_kinds[slot],
                actor=actor,
            )
        connection.execute(
            text(
                """
                INSERT INTO provider_accounts (
                    id, project_id, provider, account_label, auth_method,
                    credential_ref, credential_refs, connection_metadata, scopes,
                    status, owner_actor, read_only, deploy_capable, billing_readable,
                    destructive_actions_disabled, capabilities, created_by
                )
                VALUES (
                    :id, :project_id, :provider, :account_label, :auth_method,
                    :credential_ref, CAST(:credential_refs AS JSONB),
                    CAST(:connection_metadata AS JSONB), CAST(:scopes AS JSONB),
                    :status, :actor, :read_only, :deploy_capable,
                    :billing_readable, :destructive_actions_disabled,
                    CAST(:capabilities AS JSONB), :actor
                )
                ON CONFLICT (project_id, provider, account_label) DO UPDATE
                SET auth_method = EXCLUDED.auth_method,
                    credential_ref = EXCLUDED.credential_ref,
                    credential_refs = EXCLUDED.credential_refs,
                    connection_metadata = EXCLUDED.connection_metadata,
                    scopes = EXCLUDED.scopes,
                    status = provider_accounts.status,
                    read_only = EXCLUDED.read_only,
                    deploy_capable = EXCLUDED.deploy_capable,
                    billing_readable = EXCLUDED.billing_readable,
                    destructive_actions_disabled = EXCLUDED.destructive_actions_disabled,
                    capabilities = EXCLUDED.capabilities,
                    updated_at = now()
                RETURNING id
                """
            ),
            {
                "id": provider_account_id,
                "project_id": project_id,
                "provider": normalized_provider,
                "account_label": normalized_label,
                "auth_method": normalized_auth_method,
                "credential_ref": ref,
                "credential_refs": json.dumps(refs),
                "connection_metadata": json.dumps(safe_connection_metadata),
                "scopes": json.dumps(self._normalize_string_list(scopes)),
                "status": account_status,
                "read_only": read_only,
                "deploy_capable": deploy_capable,
                "billing_readable": billing_readable,
                "destructive_actions_disabled": destructive_actions_disabled,
                "capabilities": json.dumps(capabilities),
                "actor": actor,
            },
        )
        row = self._get_provider_account_by_label(
            connection,
            project_id,
            normalized_provider,
            normalized_label,
        )
        emit_event(
            connection,
            project_id,
            "provider.account_upserted",
            actor,
            "provider_account",
            row["id"],
            {
                "provider": normalized_provider,
                "auth_method": normalized_auth_method,
                "credential_slots": sorted(refs),
                "read_only": read_only,
                "deploy_capable": deploy_capable,
                "billing_readable": billing_readable,
                "destructive_actions_disabled": destructive_actions_disabled,
                "credential_version": int(row.get("credential_version") or 1),
                "status": row["status"],
            },
        )
        self._upsert_capabilities(
            connection,
            project_id,
            row["id"],
            capabilities,
        )
        return self._serialize_provider_account(row)

    MIN_EASY_CONNECT_SECRET_LENGTH = 8
    COMMUNITY_EASY_CONNECT_BACKENDS = frozenset({"local_vault"})

    def easy_connect(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        provider: str,
        account_label: str,
        secret_value: str,
        slot: Optional[str] = None,
        auth_method: Optional[str] = None,
        base_url: Optional[str] = None,
        backend: str = "local_vault",
        read_only: bool = True,
        test_after: bool = False,
    ) -> dict[str, Any]:
        """Write-through paste of a provider key into a SecretRef-backed account.

        The raw value is handed to the secret backend and dropped. Postgres keeps
        only a pointer plus last4/fingerprint metadata. Agents never receive the
        material. Re-pasting the same provider+label replaces or rotates.
        """
        from .secrets_service import put_secret

        cleaned_secret = str(secret_value or "").strip()
        if not cleaned_secret:
            raise ValueError("secret_value is required")
        if len(cleaned_secret) < self.MIN_EASY_CONNECT_SECRET_LENGTH:
            raise ValueError(
                "secret_value is too short "
                f"(minimum {self.MIN_EASY_CONNECT_SECRET_LENGTH} characters)"
            )
        backend_key = (backend or "local_vault").strip().lower() or "local_vault"
        if backend_key not in self.COMMUNITY_EASY_CONNECT_BACKENDS:
            raise ValueError(
                "Community paste-and-connect uses local_vault only; "
                "hosted KMS is not available on this edition."
            )
        normalized_provider = self._normalize_provider(provider)
        normalized_label = self._require_text(account_label, "account_label")
        profile = PROVIDER_CONNECTION_PROFILES.get(normalized_provider)
        method_key = (
            auth_method or (profile or {}).get("default_auth_method") or "api_token"
        ).strip().lower()
        requested_slot = (slot or "").strip().lower()
        if profile:
            method = profile["auth_methods"].get(method_key)
            if not method:
                raise ValueError(
                    f"{normalized_provider} auth_method must be one of: "
                    + ", ".join(sorted(profile["auth_methods"]))
                    + "."
                )
            slots = {item["key"]: item for item in method["credential_slots"]}
            primary = next(
                (
                    item["key"]
                    for item in method["credential_slots"]
                    if item.get("primary")
                ),
                next(iter(slots), "primary"),
            )
            slot_key = requested_slot or primary
            if slot_key not in slots:
                if slot_key in {"token", "api_key", "primary"}:
                    slot_key = primary
                else:
                    raise ValueError(
                        f"Unknown credential slot {slot_key!r} for "
                        f"{normalized_provider}/{method_key}."
                    )
            secret_kind = str(slots[slot_key]["secret_kind"])
        else:
            if requested_slot and requested_slot not in {"token", "api_key", "primary"}:
                raise ValueError(
                    f"{normalized_provider} credential_refs supports only the "
                    "'primary' slot."
                )
            slot_key = "primary"
            secret_kind = "api_token"

        stored = put_secret(
            connection,
            self.settings,
            project_id=project_id,
            name=f"{normalized_provider}_{normalized_label}_{slot_key}",
            value=cleaned_secret,
            secret_kind=secret_kind,
            backend=backend_key,
            actor=actor,
        )
        connection.execute(
            text("UPDATE secret_refs SET provider = :provider WHERE id = :id"),
            {"provider": normalized_provider, "id": stored["secret_ref_id"]},
        )
        pointer = f"{backend_key}:{stored['ref_key']}"
        metadata: dict[str, Any] = {}
        cleaned_base = str(base_url or "").strip()
        if cleaned_base:
            metadata["base_url"] = cleaned_base.rstrip("/")

        existing_row = connection.execute(
            text(
                """
                SELECT id, status, credential_refs, credential_ref
                FROM provider_accounts
                WHERE project_id = :project_id
                  AND provider = :provider
                  AND account_label = :account_label
                """
            ),
            {
                "project_id": project_id,
                "provider": normalized_provider,
                "account_label": normalized_label,
            },
        ).mappings().first()

        replaced = False
        rotated = False
        if existing_row:
            existing_status = str(existing_row["status"] or "")
            if existing_status in {"disabled", "revoked"}:
                raise ValueError(
                    f"Provider account is {existing_status}; "
                    "use the explicit lifecycle endpoint."
                )
            existing_refs = dict(
                existing_row["credential_refs"]
                or (
                    {"primary": existing_row["credential_ref"]}
                    if existing_row["credential_ref"]
                    else {}
                )
            )
            if existing_refs.get(slot_key) == pointer and all(
                key == slot_key or existing_refs.get(key)
                for key in existing_refs
            ):
                connection.execute(
                    text(
                        """
                        UPDATE provider_accounts
                        SET credential_version = credential_version + 1,
                            credential_rotated_at = now(),
                            connection_metadata = CASE
                                WHEN CAST(:has_metadata AS BOOLEAN)
                                THEN CAST(:connection_metadata AS JSONB)
                                ELSE connection_metadata
                            END,
                            last_test_status = NULL,
                            last_tested_at = NULL,
                            last_error = NULL,
                            updated_at = now()
                        WHERE id = :provider_account_id
                          AND project_id = :project_id
                        """
                    ),
                    {
                        "provider_account_id": existing_row["id"],
                        "project_id": project_id,
                        "has_metadata": bool(metadata),
                        "connection_metadata": json.dumps(metadata),
                    },
                )
                account = self.get_provider_account(
                    connection, project_id, existing_row["id"]
                )
                replaced = True
            else:
                next_refs = dict(existing_refs)
                next_refs[slot_key] = pointer
                rotated_payload = self.rotate_provider_credentials(
                    connection,
                    project_id=project_id,
                    provider_account_id=existing_row["id"],
                    credential_ref=None,
                    credential_refs=next_refs,
                    overlap_minutes=0,
                    actor=actor,
                )
                account = rotated_payload["provider_account"]
                if metadata:
                    account = self.update_provider_account(
                        connection,
                        project_id=project_id,
                        provider_account_id=account["id"],
                        account_label=None,
                        scopes=None,
                        read_only=None,
                        deploy_capable=None,
                        billing_readable=None,
                        destructive_actions_disabled=None,
                        connection_metadata=metadata,
                        owner_actor=None,
                        actor=actor,
                    )
                rotated = True
        else:
            account = self.create_provider_account(
                connection,
                project_id=project_id,
                provider=normalized_provider,
                account_label=normalized_label,
                auth_method=method_key,
                credential_refs={slot_key: pointer},
                connection_metadata=metadata or None,
                scopes=[],
                read_only=bool(read_only),
                deploy_capable=False,
                billing_readable=False,
                destructive_actions_disabled=True,
                actor=actor,
            )

        emit_event(
            connection,
            project_id,
            "provider.easy_connected",
            actor,
            "provider_account",
            account["id"],
            {
                "provider": normalized_provider,
                "account_label": normalized_label,
                "backend": backend_key,
                "slot": slot_key,
                "secret_ref_id": stored["secret_ref_id"],
                "last4": stored.get("last4"),
                "raw_secret_stored": False,
                "has_base_url": bool(cleaned_base),
                "replaced": replaced,
                "rotated": rotated,
                "credential_version": int(account.get("credential_version") or 1),
            },
        )
        test_payload: Optional[dict[str, Any]] = None
        if test_after:
            try:
                test_payload = self.test_provider_account(
                    connection,
                    project_id=project_id,
                    provider_account_id=account["id"],
                    actor=actor,
                )
                account = test_payload.get("provider_account") or account
            except Exception as error:
                test_payload = {
                    "status": "failed",
                    "detail": self._safe_provider_error(error),
                }
                account = self.get_provider_account(
                    connection, project_id, account["id"]
                )
        secret_summary = {
            "secret_ref_id": stored["secret_ref_id"],
            "last4": stored.get("last4"),
            "fingerprint": stored.get("fingerprint"),
            "raw_secret_stored": False,
            "backend": backend_key,
        }
        return {
            "provider_account": account,
            "secret": secret_summary,
            "secret_ref_id": stored["secret_ref_id"],
            "raw_secret_stored": False,
            "replaced": replaced,
            "rotated": rotated,
            "test": test_payload,
        }

    def update_provider_account(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: str,
        account_label: Optional[str],
        scopes: Optional[list[str]],
        read_only: Optional[bool],
        deploy_capable: Optional[bool],
        billing_readable: Optional[bool],
        destructive_actions_disabled: Optional[bool],
        connection_metadata: Optional[dict[str, Any]],
        owner_actor: Optional[str],
        actor: str,
    ) -> dict[str, Any]:
        existing = self.get_provider_account(
            connection, project_id, provider_account_id
        )
        if existing["status"] == "revoked":
            raise ValueError("Revoked provider accounts are immutable.")
        next_label = (
            self._require_text(account_label, "account_label")
            if account_label is not None
            else existing["account_label"]
        )
        duplicate_label = connection.execute(
            text(
                """
                SELECT id
                FROM provider_accounts
                WHERE project_id = :project_id
                  AND provider = :provider
                  AND account_label = :account_label
                  AND id <> :provider_account_id
                """
            ),
            {
                "project_id": project_id,
                "provider": existing["provider"],
                "account_label": next_label,
                "provider_account_id": provider_account_id,
            },
        ).scalar_one_or_none()
        if duplicate_label:
            raise ValueError(
                "A provider account with this provider and account_label already exists."
            )
        next_scopes = (
            self._normalize_string_list(scopes)
            if scopes is not None
            else existing["scopes"]
        )
        next_metadata = (
            self.secret_refs._validate_metadata(connection_metadata)
            if connection_metadata is not None
            else existing["connection_metadata"]
        )
        next_owner = (
            self._require_text(owner_actor, "owner_actor")
            if owner_actor is not None
            else existing["owner_actor"]
        )
        values = {
            "account_label": next_label,
            "scopes": next_scopes,
            "read_only": existing["read_only"] if read_only is None else read_only,
            "deploy_capable": (
                existing["deploy_capable"]
                if deploy_capable is None
                else deploy_capable
            ),
            "billing_readable": (
                existing["billing_readable"]
                if billing_readable is None
                else billing_readable
            ),
            "destructive_actions_disabled": (
                existing["destructive_actions_disabled"]
                if destructive_actions_disabled is None
                else destructive_actions_disabled
            ),
            "connection_metadata": next_metadata,
            "owner_actor": next_owner,
        }
        connection.execute(
            text(
                """
                UPDATE provider_accounts
                SET account_label = :account_label,
                    scopes = CAST(:scopes AS JSONB),
                    read_only = :read_only,
                    deploy_capable = :deploy_capable,
                    billing_readable = :billing_readable,
                    destructive_actions_disabled = :destructive_actions_disabled,
                    connection_metadata = CAST(:connection_metadata AS JSONB),
                    owner_actor = :owner_actor,
                    updated_at = now()
                WHERE id = :provider_account_id AND project_id = :project_id
                """
            ),
            {
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "account_label": values["account_label"],
                "scopes": json.dumps(values["scopes"]),
                "read_only": values["read_only"],
                "deploy_capable": values["deploy_capable"],
                "billing_readable": values["billing_readable"],
                "destructive_actions_disabled": values[
                    "destructive_actions_disabled"
                ],
                "connection_metadata": json.dumps(values["connection_metadata"]),
                "owner_actor": values["owner_actor"],
            },
        )
        changed = sorted(key for key, value in values.items() if value != existing[key])
        emit_event(
            connection,
            project_id,
            "provider.account_updated",
            actor,
            "provider_account",
            provider_account_id,
            {"provider": existing["provider"], "changed_fields": changed},
        )
        return self.get_provider_account(
            connection, project_id, provider_account_id
        )

    def transition_provider_account(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: str,
        action: str,
        actor: str,
    ) -> dict[str, Any]:
        account = self.get_provider_account(
            connection, project_id, provider_account_id
        )
        normalized_action = action.strip().lower()
        if normalized_action not in {"enable", "disable", "revoke"}:
            raise ValueError("action must be enable, disable, or revoke.")
        if account["status"] == "revoked":
            if normalized_action == "revoke":
                return account
            raise ValueError("Revoked provider accounts cannot be re-enabled.")
        if normalized_action == "enable":
            self._require_account_secret_refs_usable(
                connection, account, check_account_state=False
            )
            status = (
                "active"
                if self._provider_adapter_status(account["provider"]) == "available"
                else "pending_adapter"
            )
        elif normalized_action == "disable":
            status = "disabled"
        else:
            status = "revoked"
        connection.execute(
            text(
                """
                UPDATE provider_accounts
                SET status = :status,
                    read_only = CASE WHEN :status = 'revoked' THEN true ELSE read_only END,
                    deploy_capable = CASE
                        WHEN :status = 'revoked' THEN false ELSE deploy_capable
                    END,
                    destructive_actions_disabled = CASE
                        WHEN :status = 'revoked' THEN true
                        ELSE destructive_actions_disabled
                    END,
                    disabled_at = CASE
                        WHEN :status = 'disabled' THEN now()
                        WHEN :status IN ('active', 'pending_adapter') THEN NULL
                        ELSE disabled_at
                    END,
                    disabled_by = CASE
                        WHEN :status = 'disabled' THEN :actor
                        WHEN :status IN ('active', 'pending_adapter') THEN NULL
                        ELSE disabled_by
                    END,
                    revoked_at = CASE WHEN :status = 'revoked' THEN now() ELSE revoked_at END,
                    revoked_by = CASE
                        WHEN :status = 'revoked' THEN :actor ELSE revoked_by
                    END,
                    updated_at = now()
                WHERE id = :provider_account_id AND project_id = :project_id
                """
            ),
            {
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "status": status,
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            f"provider.account_{normalized_action}d",
            actor,
            "provider_account",
            provider_account_id,
            {
                "provider": account["provider"],
                "from_status": account["status"],
                "to_status": status,
            },
        )
        return self.get_provider_account(
            connection, project_id, provider_account_id
        )

    def rotate_provider_credentials(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: str,
        credential_ref: Optional[str],
        credential_refs: dict[str, str],
        overlap_minutes: int,
        actor: str,
    ) -> dict[str, Any]:
        account = self.get_provider_account(
            connection, project_id, provider_account_id
        )
        if account["status"] in {"disabled", "revoked"}:
            raise ValueError(
                f"Provider account is {account['status']}; credentials cannot be rotated."
            )
        auth_method, next_refs, primary_ref, slot_kinds = (
            self._normalize_provider_credentials(
                account["provider"],
                auth_method=account["auth_method"],
                credential_ref=credential_ref,
                credential_refs=credential_refs,
                apply_defaults=False,
            )
        )
        current_refs = dict(account["credential_refs"])
        if set(next_refs) != set(current_refs):
            raise ValueError(
                "Rotation must provide exactly the currently configured credential slots."
            )
        if next_refs == current_refs:
            raise ValueError("Rotation requires at least one new SecretRef.")
        for slot, pointer in next_refs.items():
            self._ensure_secret_ref(
                connection,
                project_id=project_id,
                provider=account["provider"],
                credential_ref=pointer,
                secret_kind=slot_kinds[slot],
                actor=actor,
            )

        overlap_expires_at = now_utc() + timedelta(minutes=overlap_minutes)
        prior_items: dict[str, dict[str, Any]] = {}
        active_items: dict[str, dict[str, Any]] = {}
        for slot, old_pointer in current_refs.items():
            new_pointer = next_refs[slot]
            old_item = self.secret_refs.get_by_credential_ref(
                connection,
                project_id=project_id,
                credential_ref=old_pointer,
            )
            new_item = self.secret_refs.get_by_credential_ref(
                connection,
                project_id=project_id,
                credential_ref=new_pointer,
            )
            prior_items[old_item["id"]] = old_item
            active_items[new_item["id"]] = new_item
            if old_pointer == new_pointer:
                continue
            connection.execute(
                text(
                    """
                    UPDATE secret_refs
                    SET status = 'rotating',
                        overlap_expires_at = :overlap_expires_at,
                        updated_at = now()
                    WHERE id = :secret_ref_id AND project_id = :project_id
                    """
                ),
                {
                    "project_id": project_id,
                    "secret_ref_id": old_item["id"],
                    "overlap_expires_at": overlap_expires_at,
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE secret_refs
                    SET supersedes_secret_ref_id = :prior_secret_ref_id,
                        updated_at = now()
                    WHERE id = :secret_ref_id AND project_id = :project_id
                    """
                ),
                {
                    "project_id": project_id,
                    "secret_ref_id": new_item["id"],
                    "prior_secret_ref_id": old_item["id"],
                },
            )

        account_status = (
            "active"
            if self._provider_adapter_status(account["provider"]) == "available"
            else "pending_adapter"
        )
        connection.execute(
            text(
                """
                UPDATE provider_accounts
                SET auth_method = :auth_method,
                    credential_ref = :credential_ref,
                    credential_refs = CAST(:credential_refs AS JSONB),
                    credential_version = credential_version + 1,
                    credential_rotated_at = now(),
                    status = :status,
                    last_test_status = NULL,
                    last_tested_at = NULL,
                    missing_permissions = '[]'::jsonb,
                    last_error = NULL,
                    updated_at = now()
                WHERE id = :provider_account_id AND project_id = :project_id
                """
            ),
            {
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "auth_method": auth_method,
                "credential_ref": primary_ref,
                "credential_refs": json.dumps(next_refs),
                "status": account_status,
            },
        )
        updated = self.get_provider_account(
            connection, project_id, provider_account_id
        )
        emit_event(
            connection,
            project_id,
            "provider.credentials_rotated",
            actor,
            "provider_account",
            provider_account_id,
            {
                "provider": account["provider"],
                "credential_version": updated["credential_version"],
                "rotated_slots": sorted(
                    slot
                    for slot, pointer in next_refs.items()
                    if pointer != current_refs[slot]
                ),
                "overlap_expires_at": overlap_expires_at.isoformat(),
            },
        )
        return {
            "provider_account": updated,
            "prior_secret_refs": [
                self.secret_refs.get_secret_ref(connection, project_id, secret_id)
                for secret_id in prior_items
            ],
            "active_secret_refs": [
                self.secret_refs.get_secret_ref(connection, project_id, secret_id)
                for secret_id in active_items
            ],
            "overlap_expires_at": overlap_expires_at.isoformat(),
        }

    def test_provider_account(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: str,
        actor: str,
    ) -> dict[str, Any]:
        account = self.get_provider_account(connection, project_id, provider_account_id)
        if account["status"] in {"disabled", "revoked"}:
            raise ProviderCredentialError(
                f"Provider account is {account['status']}; connection test blocked."
            )
        adapter_status = self._provider_adapter_status(account["provider"])
        if adapter_status != "available":
            status = "unsupported"
            detail = {
                "code": "provider_adapter_unavailable",
                "message": (
                    f"{account['provider']} credentials are recorded as SecretRefs, "
                    "but its runtime adapter is not available yet."
                ),
            }
            missing_permissions = ["provider_adapter_unavailable"]
        else:
            try:
                self._require_account_secret_refs_usable(connection, account)
                credential = self._credential_value(account["credential_ref"])
                result = self._test_connection(
                    account["provider"],
                    credential,
                    account.get("connection_metadata"),
                )
                status = "passed"
                detail = result
                missing_permissions = []
            except Exception as error:
                status = "failed"
                detail = self._safe_provider_error(error)
                missing_permissions = ["provider_credentials_or_permissions"]
        account_status = {
            "passed": "active",
            "failed": "degraded",
            "unsupported": "pending_adapter",
        }[status]

        connection.execute(
            text(
                """
                UPDATE provider_accounts
                SET status = :account_status,
                    last_test_status = :status,
                    last_tested_at = now(),
                    missing_permissions = CAST(:missing_permissions AS JSONB),
                    last_error = CAST(:last_error AS JSONB),
                    updated_at = now()
                WHERE id = :provider_account_id AND project_id = :project_id
                """
            ),
            {
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "account_status": account_status,
                "status": status,
                "missing_permissions": json.dumps(missing_permissions),
                "last_error": json.dumps(None if status == "passed" else detail),
            },
        )
        if status == "passed":
            for credential_pointer in set(account["credential_refs"].values()):
                ref_kind, ref_key = self.secret_refs.parse_credential_ref(
                    credential_pointer
                )
                connection.execute(
                    text(
                        """
                        UPDATE secret_refs
                        SET last_verified_at = now(),
                            updated_at = now()
                        WHERE project_id = :project_id
                          AND ref_kind = :ref_kind
                          AND ref_key = :ref_key
                        """
                    ),
                    {
                        "project_id": project_id,
                        "ref_kind": ref_kind,
                        "ref_key": ref_key,
                    },
                )
        health = self._record_provider_health(
            connection,
            project_id=project_id,
            provider_account_id=provider_account_id,
            provider=account["provider"],
            status=(
                "healthy"
                if status == "passed"
                else "unknown"
                if status == "unsupported"
                else "degraded"
            ),
            api_available=status == "passed",
            detail=detail,
        )
        emit_event(
            connection,
            project_id,
            "provider.connection_tested",
            actor,
            "provider_account",
            provider_account_id,
            {
                "provider": account["provider"],
                "status": status,
                "provider_health_id": health["id"],
                "detail": detail,
            },
        )
        return {
            "provider_account": self.get_provider_account(
                connection,
                project_id,
                provider_account_id,
            ),
            "provider_health": health,
            "status": status,
            "detail": detail,
        }

    def sync_resources(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        provider_account_id: Optional[str] = None,
    ) -> dict[str, Any]:
        accounts = (
            [self.get_provider_account(connection, project_id, provider_account_id)]
            if provider_account_id
            else self.list_provider_accounts(connection, project_id)
        )
        run_summaries: list[dict[str, Any]] = []
        resources_seen = 0
        resources_changed = 0
        costs_seen = 0
        for account in accounts:
            run_id = make_id("recon")
            connection.execute(
                text(
                    """
                    INSERT INTO reconciliation_runs (
                        id, project_id, provider_account_id, provider, status, started_by
                    )
                    VALUES (
                        :id, :project_id, :provider_account_id, :provider, 'running', :actor
                    )
                    """
                ),
                {
                    "id": run_id,
                    "project_id": project_id,
                    "provider_account_id": account["id"],
                    "provider": account["provider"],
                    "actor": actor,
                },
            )
            try:
                self._require_account_secret_refs_usable(connection, account)
                credential = self._credential_value(account["credential_ref"])
                inventory = self._sync_inventory(account["provider"], credential)
                changed = 0
                for resource in inventory:
                    if self._upsert_external_resource(
                        connection,
                        project_id,
                        account["id"],
                        resource,
                    ):
                        changed += 1
                billing_status = "not_requested"
                billing_message: Optional[str] = None
                if bool(account["billing_readable"]):
                    if self._provider_supports_billing(account["provider"]):
                        cost_records = self._sync_billing(
                            account["provider"], credential
                        )
                        billing_status = "synced"
                    else:
                        cost_records = []
                        billing_status = "unavailable"
                        billing_message = (
                            f"Exact billing sync is not available for {account['provider']}; "
                            "resource costs remain unknown until provider billing support or invoice reconciliation is configured."
                        )
                        emit_event(
                            connection,
                            project_id,
                            "provider.billing_unavailable",
                            actor,
                            "provider_account",
                            account["id"],
                            {
                                "provider": account["provider"],
                                "cost_confidence": "unknown",
                                "message": billing_message,
                            },
                        )
                else:
                    cost_records = []
                costs_recorded = 0
                for cost_record in cost_records:
                    if self._record_provider_billing_cost_event(
                        connection,
                        project_id=project_id,
                        provider_account_id=account["id"],
                        cost_record=cost_record,
                    ):
                        costs_recorded += 1
                self._finish_reconciliation(
                    connection,
                    run_id,
                    status="succeeded",
                    resources_seen=len(inventory),
                    resources_changed=changed,
                    costs_seen=costs_recorded,
                    error=None,
                )
                health = self._record_provider_health(
                    connection,
                    project_id=project_id,
                    provider_account_id=account["id"],
                    provider=account["provider"],
                    status="healthy",
                    api_available=True,
                    detail={
                        "resources_seen": len(inventory),
                        "costs_seen": costs_recorded,
                        "billing_status": billing_status,
                        "billing_confidence": (
                            "unknown"
                            if billing_status == "unavailable"
                            else "provider_reported"
                            if costs_recorded
                            else "unknown"
                        ),
                        "billing_message": billing_message,
                    },
                )
                drift = self._create_drift_report(
                    connection,
                    project_id,
                    account["id"],
                )
                inventory_snapshot_artifact_id = (
                    self._create_reconciliation_snapshot_artifact(
                        connection,
                        project_id=project_id,
                        run_id=run_id,
                        account=account,
                        status="succeeded",
                        inventory=inventory,
                        cost_records=cost_records,
                        costs_recorded=costs_recorded,
                        provider_health_id=health["id"],
                        drift_report_id=drift["id"],
                        deadletter_id=None,
                        error=None,
                        billing_status=billing_status,
                        billing_message=billing_message,
                    )
                )
                self._attach_reconciliation_metadata(
                    connection,
                    run_id,
                    {
                        "inventory_snapshot_artifact_id": inventory_snapshot_artifact_id,
                        "provider_health_id": health["id"],
                        "drift_report_id": drift["id"],
                        "billing_status": billing_status,
                        "billing_message": billing_message,
                    },
                )
                resources_seen += len(inventory)
                resources_changed += changed
                costs_seen += costs_recorded
                run_summaries.append(
                    {
                        "id": run_id,
                        "status": "succeeded",
                        "provider": account["provider"],
                        "provider_account_id": account["id"],
                        "resources_seen": len(inventory),
                        "resources_changed": changed,
                        "costs_seen": costs_recorded,
                        "provider_health_id": health["id"],
                        "drift_report_id": drift["id"],
                        "inventory_snapshot_artifact_id": inventory_snapshot_artifact_id,
                        "billing_status": billing_status,
                        "billing_message": billing_message,
                    }
                )
            except Exception as error:
                safe_error = self._redact_provider_error(error, account)
                error_payload = {"message": safe_error}
                self._finish_reconciliation(
                    connection,
                    run_id,
                    status="failed",
                    resources_seen=0,
                    resources_changed=0,
                    costs_seen=0,
                    error=error_payload,
                )
                health = self._record_provider_health(
                    connection,
                    project_id=project_id,
                    provider_account_id=account["id"],
                    provider=account["provider"],
                    status="degraded",
                    api_available=False,
                    detail=error_payload,
                )
                deadletter_id = self._create_deadletter(
                    connection,
                    project_id=project_id,
                    source_kind="provider_sync",
                    source_id=run_id,
                    reason=safe_error,
                    payload={
                        "provider": account["provider"],
                        "provider_account_id": account["id"],
                        "provider_health_id": health["id"],
                    },
                )
                inventory_snapshot_artifact_id = (
                    self._create_reconciliation_snapshot_artifact(
                        connection,
                        project_id=project_id,
                        run_id=run_id,
                        account=account,
                        status="failed",
                        inventory=[],
                        cost_records=[],
                        costs_recorded=0,
                        provider_health_id=health["id"],
                        drift_report_id=None,
                        deadletter_id=deadletter_id,
                        error=error_payload,
                        billing_status="failed",
                        billing_message=safe_error,
                    )
                )
                self._attach_reconciliation_metadata(
                    connection,
                    run_id,
                    {
                        "inventory_snapshot_artifact_id": inventory_snapshot_artifact_id,
                        "provider_health_id": health["id"],
                        "deadletter_id": deadletter_id,
                    },
                )
                emit_event(
                    connection,
                    project_id,
                    "provider.sync_failed",
                    actor,
                    "reconciliation_run",
                    run_id,
                    {
                        "provider": account["provider"],
                        "provider_account_id": account["id"],
                        "deadletter_id": deadletter_id,
                        "message": safe_error,
                    },
                )
                run_summaries.append(
                    {
                        "id": run_id,
                        "status": "failed",
                        "provider": account["provider"],
                        "provider_account_id": account["id"],
                        "resources_seen": 0,
                        "resources_changed": 0,
                        "costs_seen": 0,
                        "provider_health_id": health["id"],
                        "deadletter_id": deadletter_id,
                        "inventory_snapshot_artifact_id": inventory_snapshot_artifact_id,
                        "error": error_payload,
                    }
                )
        emit_event(
            connection,
            project_id,
            "provider.sync_completed",
            actor,
            "project",
            project_id,
            {
                "runs": len(run_summaries),
                "resources_seen": resources_seen,
                "resources_changed": resources_changed,
                "costs_seen": costs_seen,
            },
        )
        return {
            "project_id": project_id,
            "runs": run_summaries,
            "resources_seen": resources_seen,
            "resources_changed": resources_changed,
            "costs_seen": costs_seen,
        }

    def list_resources(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider: Optional[str] = None,
        ownership: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit, "offset": offset}
        if provider:
            clauses.append("provider = :provider")
            params["provider"] = provider.strip().lower()
        if ownership:
            clauses.append("ownership = :ownership")
            params["ownership"] = ownership.strip()
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, provider_account_id, provider, resource_type,
                       external_id, name, state, ownership, environment, owner_role,
                       task_id, deployment_target_id, receipt_id, budget_allocation_id,
                       health_status, estimated_monthly_usd_micros,
                       actual_monthly_usd_micros, cost_confidence, last_seen_at,
                       tags, metadata, created_at, updated_at
                FROM external_resources
                WHERE {' AND '.join(clauses)}
                ORDER BY last_seen_at DESC, provider ASC, name ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_external_resource(row) for row in rows]

    def list_cost_events(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, provider_account_id, external_resource_id,
                       billing_account_id, task_id, role_key, provider, category,
                       amount_usd_micros, cost_layer, confidence, source_ref,
                       period_start, period_end, receipt_id, metadata, created_at
                FROM cost_events
                WHERE project_id = :project_id
                ORDER BY created_at DESC, provider ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_cost_event(row) for row in rows]

    def list_cost_anomalies(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, provider_account_id, cost_event_id,
                       decision_id, provider, anomaly_type, severity, status,
                       amount_usd_micros, threshold_usd_micros, summary,
                       recommended_actions, created_at, resolved_at
                FROM cost_anomalies
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_cost_anomaly(row) for row in rows]

    def list_provider_health(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider_account_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit, "offset": offset}
        if provider_account_id:
            clauses.append("provider_account_id = :provider_account_id")
            params["provider_account_id"] = provider_account_id
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, provider_account_id, provider, status,
                       api_available, rate_limit_status, usage_cap_status,
                       last_checked_at, detail, created_at
                FROM provider_health
                WHERE {' AND '.join(clauses)}
                ORDER BY last_checked_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_provider_health(row) for row in rows]

    def list_drift_reports(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, provider_account_id, classification, summary,
                       unmanaged_count, orphaned_count, security_impacting_count,
                       production_impacting_count, recommendations, detail, created_at
                FROM drift_reports
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_drift_report(row) for row in rows]

    def list_reconciliation_runs(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, provider_account_id, provider, status,
                       resources_seen, resources_changed, costs_seen, started_by,
                       started_at, finished_at, error, metadata
                FROM reconciliation_runs
                WHERE project_id = :project_id
                ORDER BY started_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_reconciliation_run(row) for row in rows]

    def create_deployment_target(
        self,
        connection: Connection,
        *,
        project_id: str,
        app_name: str,
        repo: str,
        environment: str,
        provider_account_id: Optional[str],
        target_kind: str,
        target_ref: str,
        domains: list[str],
        required_resources: list[dict[str, Any]],
        secret_refs: list[str | dict[str, Any]],
        health_checks: list[dict[str, Any]],
        rollback_points: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        if provider_account_id:
            self.get_provider_account(connection, project_id, provider_account_id)
        normalized_environment = self._normalize_deployment_environment(environment)
        target_id = make_id("deptgt")
        # Preserve dict bindings ({name, secret_ref_id}). _normalize_string_list used to
        # strip them, so POST /v1/deployments could never store a Phase 1 credential binding.
        normalized_secret_refs = self._normalize_secret_ref_bindings(secret_refs)
        connection.execute(
            text(
                """
                INSERT INTO deployment_targets (
                    id, project_id, app_name, repo, environment, provider_account_id,
                    target_kind, target_ref, domains, required_resources, secret_refs,
                    health_checks, rollback_points, status, created_by
                )
                VALUES (
                    :id, :project_id, :app_name, :repo, :environment,
                    :provider_account_id, :target_kind, :target_ref,
                    CAST(:domains AS JSONB), CAST(:required_resources AS JSONB),
                    CAST(:secret_refs AS JSONB), CAST(:health_checks AS JSONB),
                    CAST(:rollback_points AS JSONB), 'created', :actor
                )
                ON CONFLICT (project_id, app_name, environment) DO UPDATE
                SET repo = EXCLUDED.repo,
                    provider_account_id = EXCLUDED.provider_account_id,
                    target_kind = EXCLUDED.target_kind,
                    target_ref = EXCLUDED.target_ref,
                    domains = EXCLUDED.domains,
                    required_resources = EXCLUDED.required_resources,
                    secret_refs = EXCLUDED.secret_refs,
                    health_checks = EXCLUDED.health_checks,
                    rollback_points = EXCLUDED.rollback_points,
                    updated_at = now()
                RETURNING id
                """
            ),
            {
                "id": target_id,
                "project_id": project_id,
                "app_name": self._require_text(app_name, "app_name"),
                "repo": self._require_text(repo, "repo"),
                "environment": normalized_environment,
                "provider_account_id": provider_account_id,
                "target_kind": self._require_text(target_kind, "target_kind"),
                "target_ref": self._require_text(target_ref, "target_ref"),
                "domains": json.dumps(self._normalize_string_list(domains)),
                "required_resources": json.dumps(required_resources),
                "secret_refs": json.dumps(normalized_secret_refs),
                "health_checks": json.dumps(health_checks),
                "rollback_points": json.dumps(rollback_points),
                "actor": actor,
            },
        )
        row = connection.execute(
            text(
                """
                SELECT id, project_id, app_name, repo, environment, provider_account_id,
                       target_kind, target_ref, domains, required_resources, secret_refs,
                       health_checks, rollback_points, status, created_by, created_at, updated_at
                FROM deployment_targets
                WHERE project_id = :project_id AND app_name = :app_name AND environment = :environment
                """
            ),
            {
                "project_id": project_id,
                "app_name": app_name.strip(),
                "environment": normalized_environment,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "deployment_target.upserted",
            actor,
            "deployment_target",
            row["id"],
            {
                "app_name": row["app_name"],
                "environment": row["environment"],
                "provider_account_id": row["provider_account_id"],
            },
        )
        return self._serialize_deployment_target(row)

    def list_deployment_targets(
        self,
        connection: Connection,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, app_name, repo, environment, provider_account_id,
                       target_kind, target_ref, domains, required_resources, secret_refs,
                       health_checks, rollback_points, status, created_by, created_at, updated_at
                FROM deployment_targets
                WHERE project_id = :project_id
                ORDER BY app_name ASC, environment ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_deployment_target(row) for row in rows]

    def get_deployment_target(
        self,
        connection: Connection,
        project_id: str,
        deployment_target_id: str,
    ) -> dict[str, Any]:
        return self._get_deployment_target(
            connection,
            project_id,
            deployment_target_id,
        )

    def get_deployment_run(
        self,
        connection: Connection,
        project_id: str,
        deployment_run_id: str,
    ) -> dict[str, Any]:
        return self._get_deployment_run(
            connection,
            project_id,
            deployment_run_id,
        )

    def deployment_rollout_policy_for_target(
        self,
        connection: Connection,
        project_id: str,
        target: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        return self._deployment_rollout_policy_for_target(
            connection,
            project_id,
            target,
        )

    def deployment_rollout_policy_block_reason(
        self,
        target: dict[str, Any],
        policy: dict[str, Any],
        rollback_plan: list[str],
        reservation_id: Optional[str],
    ) -> Optional[str]:
        return self._deployment_rollout_policy_block_reason(
            target,
            policy,
            rollback_plan,
            reservation_id,
        )

    def deployment_rollback_plan(self, target: dict[str, Any]) -> list[str]:
        return self._deployment_rollback_plan(target)

    def list_deployment_environment_policies(
        self,
        connection: Connection,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, environment, runtime_mode,
                       allowed_target_kinds, requires_change_set,
                       requires_budget_reservation, requires_approval,
                       requires_health_check, requires_rollback_plan,
                       rollout_notes, status, created_by, metadata,
                       created_at, updated_at
                FROM deployment_environment_policies
                WHERE project_id = :project_id
                ORDER BY environment ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_deployment_environment_policy(row) for row in rows]

    def upsert_deployment_environment_policy(
        self,
        connection: Connection,
        *,
        project_id: str,
        environment: str,
        runtime_mode: str,
        actor: str,
        allowed_target_kinds: Optional[list[str]] = None,
        requires_change_set: bool = True,
        requires_budget_reservation: bool = True,
        requires_approval: bool = True,
        requires_health_check: bool = True,
        requires_rollback_plan: bool = True,
        rollout_notes: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_environment = self._normalize_deployment_environment(environment)
        normalized_mode = runtime_mode.strip().lower()
        if normalized_mode not in DEPLOYMENT_ROLLOUT_MODES:
            raise ValueError(f"Unsupported deployment rollout runtime mode: {runtime_mode}")
        target_kinds = self._normalize_string_list(allowed_target_kinds or [])
        policy_id = f"deppol_{project_id}_{normalized_environment}"
        connection.execute(
            text(
                """
                INSERT INTO deployment_environment_policies (
                    id, project_id, environment, runtime_mode,
                    allowed_target_kinds, requires_change_set,
                    requires_budget_reservation, requires_approval,
                    requires_health_check, requires_rollback_plan,
                    rollout_notes, status, created_by, metadata
                )
                VALUES (
                    :id, :project_id, :environment, :runtime_mode,
                    CAST(:allowed_target_kinds AS JSONB), :requires_change_set,
                    :requires_budget_reservation, :requires_approval,
                    :requires_health_check, :requires_rollback_plan,
                    :rollout_notes, 'active', :actor, CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, environment) DO UPDATE
                SET runtime_mode = EXCLUDED.runtime_mode,
                    allowed_target_kinds = EXCLUDED.allowed_target_kinds,
                    requires_change_set = EXCLUDED.requires_change_set,
                    requires_budget_reservation = EXCLUDED.requires_budget_reservation,
                    requires_approval = EXCLUDED.requires_approval,
                    requires_health_check = EXCLUDED.requires_health_check,
                    requires_rollback_plan = EXCLUDED.requires_rollback_plan,
                    rollout_notes = EXCLUDED.rollout_notes,
                    status = 'active',
                    created_by = EXCLUDED.created_by,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": policy_id,
                "project_id": project_id,
                "environment": normalized_environment,
                "runtime_mode": normalized_mode,
                "allowed_target_kinds": json.dumps(target_kinds),
                "requires_change_set": requires_change_set,
                "requires_budget_reservation": requires_budget_reservation,
                "requires_approval": requires_approval,
                "requires_health_check": requires_health_check,
                "requires_rollback_plan": requires_rollback_plan,
                "rollout_notes": rollout_notes.strip() if rollout_notes else None,
                "actor": actor,
                "metadata": json.dumps(metadata or {}),
            },
        )
        row = self._get_deployment_environment_policy(
            connection,
            project_id,
            normalized_environment,
            active_only=False,
        )
        assert row is not None
        emit_event(
            connection,
            project_id,
            "deployment_environment_policy.upserted",
            actor,
            "deployment_environment_policy",
            row["id"],
            {
                "environment": normalized_environment,
                "runtime_mode": normalized_mode,
                "requires_budget_reservation": requires_budget_reservation,
                "requires_approval": requires_approval,
            },
        )
        return row

    def list_deployment_runs(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, deployment_target_id, changeset_id, decision_id,
                       release_artifact_id, health_artifact_id, rollback_artifact_id,
                       app_name, environment, repo, target_kind, target_ref,
                       commit_ref, status, release_packet, health_check_results,
                       rollback_plan, release_receipt_ids, created_by, created_at,
                       updated_at, started_at, finished_at, failure_reason
                FROM deployment_runs
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_deployment_run(row) for row in rows]

    def plan_deployment_run(
        self,
        connection: Connection,
        *,
        project_id: str,
        deployment_target_id: str,
        commit_ref: Optional[str],
        release_packet: dict[str, Any],
        expected_cost_usd_micros: int,
        reservation_id: Optional[str],
        actor: str,
    ) -> dict[str, Any]:
        if expected_cost_usd_micros < 0:
            raise ValueError("expected_cost_usd_micros must be non-negative.")
        target = self._get_deployment_target(
            connection,
            project_id,
            deployment_target_id,
        )
        if expected_cost_usd_micros > 0 and not reservation_id:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Deployment run requires a BudgetReservation",
                {
                    "action": "reserve_budget_before_deployment",
                    "deployment_target_id": deployment_target_id,
                },
                {
                    "expected_cost_usd_micros": expected_cost_usd_micros,
                    "budget_guardrail": "spendy_action_requires_budget_reservation",
                },
                "deployment_missing_budget_reservation",
                risk_tier=2,
            )
            emit_event(
                connection,
                project_id,
                "deployment_run.blocked",
                actor,
                "decision",
                decision_id,
                {
                    "deployment_target_id": deployment_target_id,
                    "reason": "missing_budget_reservation",
                },
            )
            deployment_run_id = make_id("deprun")
            target = self._get_deployment_target(
                connection,
                project_id,
                deployment_target_id,
            )
            connection.execute(
                text(
                    """
                    INSERT INTO deployment_runs (
                        id, project_id, deployment_target_id, decision_id,
                        app_name, environment, repo, target_kind, target_ref,
                        commit_ref, status, release_packet, rollback_plan,
                        release_receipt_ids, created_by, started_at,
                        failure_reason
                    )
                    VALUES (
                        :id, :project_id, :deployment_target_id, :decision_id,
                        :app_name, :environment, :repo, :target_kind,
                        :target_ref, :commit_ref, 'failed',
                        CAST(:release_packet AS JSONB), CAST(:rollback_plan AS JSONB),
                        '[]'::jsonb, :actor, now(), :failure_reason
                    )
                    """
                ),
                {
                    "id": deployment_run_id,
                    "project_id": project_id,
                    "deployment_target_id": deployment_target_id,
                    "decision_id": decision_id,
                    "app_name": target["app_name"],
                    "environment": target["environment"],
                    "repo": target["repo"],
                    "target_kind": target["target_kind"],
                    "target_ref": target["target_ref"],
                    "commit_ref": commit_ref,
                    "release_packet": json.dumps(release_packet or {}),
                    "rollback_plan": json.dumps(self._deployment_rollback_plan(target)),
                    "actor": actor,
                    "failure_reason": "Deployment run requires a BudgetReservation.",
                },
            )
            return self._get_deployment_run(connection, project_id, deployment_run_id)

        rollback_plan = self._deployment_rollback_plan(target)
        rollout_policy = self._deployment_rollout_policy_for_target(
            connection,
            project_id,
            target,
        )
        if rollout_policy:
            policy_block_reason = self._deployment_rollout_policy_block_reason(
                target,
                rollout_policy,
                rollback_plan,
                reservation_id,
            )
            if policy_block_reason:
                return self._record_blocked_deployment_run(
                    connection,
                    project_id=project_id,
                    target=target,
                    commit_ref=commit_ref,
                    release_packet=release_packet or {},
                    rollback_plan=rollback_plan,
                    actor=actor,
                    title="Deployment run blocked by environment rollout policy",
                    recommendation={
                        "action": "update_deployment_environment_policy",
                        "deployment_target_id": deployment_target_id,
                        "environment": target["environment"],
                        "policy_id": rollout_policy.get("id"),
                    },
                    evidence={
                        "reason": policy_block_reason,
                        "policy": {
                            "id": rollout_policy.get("id"),
                            "runtime_mode": rollout_policy.get("runtime_mode"),
                            "allowed_target_kinds": rollout_policy.get(
                                "allowed_target_kinds",
                            ),
                            "requires_budget_reservation": rollout_policy.get(
                                "requires_budget_reservation",
                            ),
                            "requires_health_check": rollout_policy.get(
                                "requires_health_check",
                            ),
                            "requires_rollback_plan": rollout_policy.get(
                                "requires_rollback_plan",
                            ),
                        },
                    },
                    reason_code="deployment_rollout_policy_blocked",
                    failure_reason=(
                        "Deployment run blocked by environment rollout policy: "
                        f"{policy_block_reason}."
                    ),
                    event_reason=policy_block_reason,
                )
        from .assurance import AssuranceService

        surface_key = f"deployment_target:{target['id']}"
        ownership_result = AssuranceService(self.settings).acquire_surface(
            connection,
            project_id=project_id,
            surface_key=surface_key,
            environment=target["environment"],
            owner_ref=actor,
            coordination_route=f"deployment-target:{target['id']}",
            reason="Deployment planning requires one fenced surface owner.",
            dedupe_key=f"deployment-surface:{target['id']}:{actor}",
            actor=actor,
            # A LEASE, NOT A DEED. Deployment surfaces were claimed with no expiry, so a
            # deployment that FAILED kept the surface for good: the failed run never
            # relinquished it, and every later attempt by anybody else was refused with
            # "Deployment surface is owned by another deployer" — forever, on a target whose
            # last deploy had blocked minutes after being created. One failure permanently
            # disabled deployment for that app.
            #
            # The claim exists to stop two deployers writing to one surface AT THE SAME TIME.
            # That is a bounded hazard, so it gets a bounded lease: a deploy that is still
            # running re-acquires under its own dedupe key, and one that died lets go on its
            # own instead of needing a human with database access.
            expires_at=now_utc() + timedelta(seconds=_SURFACE_LEASE_SECONDS),
            # And reclaim one abandoned BEFORE leases existed. Those rows have expires_at
            # NULL, so they are active forever and no API can release them — without this the
            # fix above would only protect targets that had not already been wedged.
            stale_after=timedelta(seconds=_SURFACE_LEASE_SECONDS),
        )
        if not ownership_result["allowed"]:
            return self._record_blocked_deployment_run(
                connection,
                project_id=project_id,
                target=target,
                commit_ref=commit_ref,
                release_packet=release_packet or {},
                rollback_plan=rollback_plan,
                actor=actor,
                title="Deployment run blocked by surface ownership",
                recommendation={
                    "action": "coordinate_with_surface_owner",
                    "deployment_target_id": deployment_target_id,
                    "owner_ref": ownership_result["ownership"]["owner_ref"],
                    "coordination_route": ownership_result["ownership"][
                        "coordination_route"
                    ],
                },
                evidence={
                    "surface_key": surface_key,
                    "environment": target["environment"],
                    "ownership_id": ownership_result["ownership"]["id"],
                },
                reason_code="deployment_surface_ownership_blocked",
                failure_reason="Deployment surface is owned by another deployer.",
                event_reason="surface_ownership_conflict",
            )
        ownership = ownership_result["ownership"]
        ownership_guard = {
            "ownership_id": ownership["id"],
            "surface_key": surface_key,
            "environment": target["environment"],
            "owner_ref": ownership["owner_ref"],
            "version": int(ownership["version"]),
        }
        release_packet = {
            **(release_packet or {}),
            "surface_ownership": ownership_guard,
        }
        release_content = {
            "deployment_target_id": target["id"],
            "app_name": target["app_name"],
            "environment": target["environment"],
            "repo": target["repo"],
            "target_kind": target["target_kind"],
            "target_ref": target["target_ref"],
            "commit_ref": commit_ref,
            "release_packet": release_packet or {},
            "health_checks": target["health_checks"],
            "rollback_plan": rollback_plan,
            "data_boundary": "internal",
        }
        release_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "deployment_release_packet",
            f"Deployment release packet for {target['app_name']} {target['environment']}",
            release_content,
            data_boundary="internal",
        )

        changeset = None
        operation = self._deployment_operation_for_target(
            connection,
            target,
            release_artifact_id,
            commit_ref,
        )
        if operation:
            operation["desired_state"] = {
                **(operation.get("desired_state") or {}),
                "surface_ownership": ownership_guard,
            }
            risk_tier = 3 if target["environment"] == "production" else 2
            changeset = self.create_changeset(
                connection,
                project_id=project_id,
                provider_account_id=target["provider_account_id"],
                title=f"Deploy {target['app_name']} to {target['environment']}",
                summary=(
                    "Deployment Registry planned provider deployment through "
                    "Resource Plane ChangeSet gates."
                ),
                operations=[operation],
                expected_cost_usd_micros=expected_cost_usd_micros,
                reservation_id=reservation_id,
                rollback_plan=rollback_plan,
                risk_tier=risk_tier,
                approval_required=(
                    target["environment"] in {"staging", "production"}
                    or bool(
                        rollout_policy
                        and rollout_policy.get("requires_approval", True)
                    )
                ),
                actor=actor,
            )

        deployment_run_id = make_id("deprun")
        connection.execute(
            text(
                """
                INSERT INTO deployment_runs (
                    id, project_id, deployment_target_id, changeset_id,
                    release_artifact_id, app_name, environment, repo, target_kind,
                    target_ref, commit_ref, status, release_packet, rollback_plan,
                    release_receipt_ids, created_by, started_at
                )
                VALUES (
                    :id, :project_id, :deployment_target_id, :changeset_id,
                    :release_artifact_id, :app_name, :environment, :repo,
                    :target_kind, :target_ref, :commit_ref, 'created',
                    CAST(:release_packet AS JSONB), CAST(:rollback_plan AS JSONB),
                    CAST(:release_receipt_ids AS JSONB), :actor, now()
                )
                """
            ),
            {
                "id": deployment_run_id,
                "project_id": project_id,
                "deployment_target_id": target["id"],
                "changeset_id": changeset["id"] if changeset else None,
                "release_artifact_id": release_artifact_id,
                "app_name": target["app_name"],
                "environment": target["environment"],
                "repo": target["repo"],
                "target_kind": target["target_kind"],
                "target_ref": target["target_ref"],
                "commit_ref": commit_ref,
                "release_packet": json.dumps(release_packet or {}),
                "rollback_plan": json.dumps(rollback_plan),
                "release_receipt_ids": json.dumps(
                    changeset["expected_receipts"] if changeset else []
                ),
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "deployment_run.planned",
            actor,
            "deployment_run",
            deployment_run_id,
            {
                "deployment_target_id": target["id"],
                "changeset_id": changeset["id"] if changeset else None,
                "release_artifact_id": release_artifact_id,
                "environment": target["environment"],
            },
        )
        return self._get_deployment_run(connection, project_id, deployment_run_id)

    def run_deployment_health_checks(
        self,
        connection: Connection,
        *,
        project_id: str,
        deployment_run_id: str,
        actor: str,
    ) -> dict[str, Any]:
        deployment_run = self._get_deployment_run(
            connection,
            project_id,
            deployment_run_id,
        )
        if deployment_run["changeset_id"]:
            changeset = self.get_changeset(
                connection,
                project_id,
                deployment_run["changeset_id"],
            )
            if changeset["status"] != "applied":
                raise ValueError(
                    "Deployment ChangeSet must be applied before health checks run."
                )
        target = self._get_deployment_target(
            connection,
            project_id,
            deployment_run["deployment_target_id"],
        )
        checks = target["health_checks"]
        if not checks:
            results = [
                {
                    "name": "deployment_health_checks",
                    "status": "failed",
                    "error": "no_health_checks_configured",
                }
            ]
        else:
            results = [self._run_http_health_check(check) for check in checks]

        all_passed = all(result["status"] == "passed" for result in results)
        health_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "deployment_health_report",
            f"Deployment health report for {deployment_run['app_name']} {deployment_run['environment']}",
            {
                "deployment_run_id": deployment_run_id,
                "deployment_target_id": target["id"],
                "results": results,
                "status": "healthy" if all_passed else "unhealthy",
                "checked_at": now_utc().isoformat(),
                "data_boundary": "internal",
            },
            data_boundary="internal",
        )
        rollback_artifact_id = None
        decision_id = None
        final_status = "healthy" if all_passed else "rollback_pending"
        failure_reason = None if all_passed else "Deployment health checks failed."
        if not all_passed:
            rollback_artifact_id = create_artifact(
                connection,
                self.settings,
                project_id,
                None,
                "deployment_rollback_plan",
                f"Rollback plan for {deployment_run['app_name']} {deployment_run['environment']}",
                {
                    "deployment_run_id": deployment_run_id,
                    "deployment_target_id": target["id"],
                    "rollback_plan": deployment_run["rollback_plan"],
                    "failed_health_checks": [
                        result for result in results if result["status"] != "passed"
                    ],
                    "data_boundary": "internal",
                },
                data_boundary="internal",
            )
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"Deployment health failed for {deployment_run['app_name']} {deployment_run['environment']}",
                {
                    "action": "review_rollback_or_retry",
                    "deployment_run_id": deployment_run_id,
                    "health_artifact_id": health_artifact_id,
                    "rollback_artifact_id": rollback_artifact_id,
                },
                {
                    "environment": deployment_run["environment"],
                    "health_check_results": results,
                },
                "deployment_health_failed",
                risk_tier=3 if deployment_run["environment"] == "production" else 2,
            )

        connection.execute(
            text(
                """
                UPDATE deployment_runs
                SET status = :status,
                    decision_id = COALESCE(:decision_id, decision_id),
                    health_artifact_id = :health_artifact_id,
                    rollback_artifact_id = COALESCE(:rollback_artifact_id, rollback_artifact_id),
                    health_check_results = CAST(:health_check_results AS JSONB),
                    failure_reason = :failure_reason,
                    finished_at = CASE WHEN :status = 'healthy' THEN now() ELSE finished_at END,
                    updated_at = now()
                WHERE id = :deployment_run_id AND project_id = :project_id
                """
            ),
            {
                "status": final_status,
                "health_artifact_id": health_artifact_id,
                "rollback_artifact_id": rollback_artifact_id,
                "decision_id": decision_id,
                "health_check_results": json.dumps(results),
                "failure_reason": failure_reason,
                "deployment_run_id": deployment_run_id,
                "project_id": project_id,
            },
        )
        connection.execute(
            text(
                """
                UPDATE deployment_targets
                SET status = :status,
                    updated_at = now()
                WHERE id = :deployment_target_id AND project_id = :project_id
                """
            ),
            {
                "status": "healthy" if all_passed else "unhealthy",
                "deployment_target_id": target["id"],
                "project_id": project_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "deployment_run.health_checked",
            actor,
            "deployment_run",
            deployment_run_id,
            {
                "status": final_status,
                "health_artifact_id": health_artifact_id,
                "rollback_artifact_id": rollback_artifact_id,
                "decision_id": decision_id,
            },
        )
        return {
            "deployment_run": self._get_deployment_run(
                connection,
                project_id,
                deployment_run_id,
            ),
            "health_artifact_id": health_artifact_id,
            "rollback_artifact_id": rollback_artifact_id,
            "decision_id": decision_id,
        }

    def create_changeset(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: Optional[str],
        title: str,
        summary: str,
        operations: list[dict[str, Any]],
        expected_cost_usd_micros: int,
        reservation_id: Optional[str],
        rollback_plan: list[str],
        risk_tier: int,
        approval_required: bool,
        actor: str,
    ) -> dict[str, Any]:
        if expected_cost_usd_micros < 0:
            raise ValueError("expected_cost_usd_micros must be non-negative.")
        if not operations:
            raise ValueError("operations must contain at least one operation.")
        account = (
            self.get_provider_account(connection, project_id, provider_account_id)
            if provider_account_id
            else None
        )
        normalized_ops = self._validate_operations(
            account,
            operations,
            rollback_plan,
        )
        reservation = None
        if reservation_id:
            reservation = self.budget.assert_active_reservation(
                connection,
                project_id,
                reservation_id,
            )
            if not reservation:
                raise ValueError("reservation_id must reference an active reservation.")
        self._validate_deployment_changeset_environment_policies(
            connection,
            project_id=project_id,
            provider_account_id=provider_account_id,
            operations=normalized_ops,
            rollback_plan=rollback_plan,
            reservation_id=reservation_id,
        )
        status = "budget_reserved" if reservation else "quoted"
        if approval_required or risk_tier >= 2 or self._operations_need_approval(normalized_ops):
            status = "pending_approval"
        elif reservation:
            status = "policy_checked"
        changeset_id = make_id("chg")
        expected_receipts = [
            {
                "dedupe_key": self._operation_dedupe_key(changeset_id, idx, operation),
                "skill_id": operation["skill_id"],
                "action": operation["action"],
                "resource_type": operation["resource_type"],
            }
            for idx, operation in enumerate(normalized_ops)
        ]
        cost_quote = self.budget.quote(
            connection,
            project_id,
            "provider",
            max(expected_cost_usd_micros, 1),
            f"ProviderOps ChangeSet quote: {title.strip()}",
        )
        policy_result = {
            "allowed": True,
            "requires_approval": status == "pending_approval",
            "requires_budget_reservation": expected_cost_usd_micros > 0,
            "provider_adapters_are_plugins": True,
            "resource_plane_kernel_is_core": True,
        }
        connection.execute(
            text(
                """
                INSERT INTO changesets (
                    id, project_id, provider_account_id, title, summary, status,
                    risk_tier, operations, expected_cost_usd_micros, cost_quote,
                    reservation_id, rollback_plan, expected_receipts, policy_result,
                    created_by
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :title, :summary,
                    :status, :risk_tier, CAST(:operations AS JSONB),
                    :expected_cost_usd_micros, CAST(:cost_quote AS JSONB),
                    :reservation_id, CAST(:rollback_plan AS JSONB),
                    CAST(:expected_receipts AS JSONB), CAST(:policy_result AS JSONB),
                    :actor
                )
                """
            ),
            {
                "id": changeset_id,
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "title": self._require_text(title, "title"),
                "summary": self._require_text(summary, "summary"),
                "status": status,
                "risk_tier": risk_tier,
                "operations": json.dumps(normalized_ops),
                "expected_cost_usd_micros": expected_cost_usd_micros,
                "cost_quote": json.dumps(cost_quote),
                "reservation_id": reservation_id,
                "rollback_plan": json.dumps(self._normalize_string_list(rollback_plan)),
                "expected_receipts": json.dumps(expected_receipts),
                "policy_result": json.dumps(policy_result),
                "actor": actor,
            },
        )
        for idx, operation in enumerate(normalized_ops):
            connection.execute(
                text(
                    """
                    INSERT INTO changeset_operations (
                        id, project_id, changeset_id, provider_account_id,
                        operation_index, skill_id, action, resource_type, resource_ref,
                        desired_state, risk_tier, destructive, production_affecting
                    )
                    VALUES (
                        :id, :project_id, :changeset_id, :provider_account_id,
                        :operation_index, :skill_id, :action, :resource_type,
                        :resource_ref, CAST(:desired_state AS JSONB), :risk_tier,
                        :destructive, :production_affecting
                    )
                    """
                ),
                {
                    "id": make_id("chgo"),
                    "project_id": project_id,
                    "changeset_id": changeset_id,
                    "provider_account_id": provider_account_id,
                    "operation_index": idx,
                    "skill_id": operation["skill_id"],
                    "action": operation["action"],
                    "resource_type": operation["resource_type"],
                    "resource_ref": operation.get("resource_ref"),
                    "desired_state": json.dumps(operation.get("desired_state") or {}),
                    "risk_tier": int(operation.get("risk_tier") or risk_tier),
                    "destructive": bool(operation.get("destructive")),
                    "production_affecting": bool(operation.get("production_affecting")),
                },
            )
        decision_id = None
        if status == "pending_approval":
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                f"Approve ProviderOps ChangeSet: {title.strip()}",
                {
                    "action": "approve_changeset",
                    "changeset_id": changeset_id,
                    "provider_account_id": provider_account_id,
                    "operations": len(normalized_ops),
                    "rollback_plan": rollback_plan,
                },
                {
                    "expected_cost_usd_micros": expected_cost_usd_micros,
                    "reservation_id": reservation_id,
                    "cost_confidence": "estimated_from_price_catalog",
                },
                "provider_changeset_requires_approval",
                risk_tier=max(risk_tier, 2),
                default_action="hold",
            )
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET external_ref = :changeset_id
                    WHERE id = :decision_id
                    """
                ),
                {"changeset_id": changeset_id, "decision_id": decision_id},
            )
            connection.execute(
                text(
                    """
                    UPDATE changesets
                    SET approval_decision_id = :decision_id,
                        updated_at = now()
                    WHERE id = :changeset_id
                    """
                ),
                {"decision_id": decision_id, "changeset_id": changeset_id},
            )
        emit_event(
            connection,
            project_id,
            "changeset.created",
            actor,
            "changeset",
            changeset_id,
            {
                "status": status,
                "risk_tier": risk_tier,
                "provider_account_id": provider_account_id,
                "expected_cost_usd_micros": expected_cost_usd_micros,
                "reservation_id": reservation_id,
                "approval_decision_id": decision_id,
            },
        )
        return self.get_changeset(connection, project_id, changeset_id)

    def list_changesets(
        self,
        connection: Connection,
        project_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit, "offset": offset}
        if status:
            clauses.append("status = :status")
            params["status"] = status
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, provider_account_id, title, summary, status,
                       risk_tier, operations, expected_cost_usd_micros, cost_quote,
                       reservation_id, approval_decision_id, rollback_plan,
                       expected_receipts, policy_result, created_by, approved_by,
                       approved_at, applied_by, applied_at, failure_reason,
                       created_at, updated_at
                FROM changesets
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_changeset(row) for row in rows]

    def get_changeset(
        self,
        connection: Connection,
        project_id: str,
        changeset_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider_account_id, title, summary, status,
                       risk_tier, operations, expected_cost_usd_micros, cost_quote,
                       reservation_id, approval_decision_id, rollback_plan,
                       expected_receipts, policy_result, created_by, approved_by,
                       approved_at, applied_by, applied_at, failure_reason,
                       created_at, updated_at
                FROM changesets
                WHERE id = :changeset_id AND project_id = :project_id
                """
            ),
            {"changeset_id": changeset_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ChangeSetNotFoundError("ChangeSet not found.")
        return self._serialize_changeset(row)

    def _approval_broker(self):
        """Lazily build the skill broker (reads skill specs from yaml; no DB)."""
        if getattr(self, "_entitlement_broker", None) is None:
            from .entitlements import EntitlementBrokerService

            self._entitlement_broker = EntitlementBrokerService(self.settings)
        return self._entitlement_broker

    def _required_approvers_for_operations(
        self, operations: list[dict[str, Any]]
    ) -> list[str]:
        """Union of the per-skill `policy.requires_approval` approver roles across a
        ChangeSet's operations. Only LIST-valued requirements impose multi-approver
        (dual-control) gating; string/absent values preserve single-approval behavior."""
        try:
            policies = {
                skill["id"]: (skill.get("policy") or {})
                for skill in self._approval_broker().list_skills()
            }
        except Exception:
            policies = {}
        required: set[str] = set()
        for operation in operations or []:
            skill_id = str(operation.get("skill_id") or "")
            approvers = (policies.get(skill_id) or {}).get("requires_approval")
            if isinstance(approvers, list):
                for role in approvers:
                    if isinstance(role, str) and role.strip():
                        required.add(role.strip())
        return sorted(required)

    def required_approvers_for_changeset(
        self, connection: Connection, changeset_id: str
    ) -> list[str]:
        """Approver roles required (dual-control) for a ChangeSet, derived from its
        operations' skills. Empty list => no per-skill approver list (single-approval)."""
        row = self._get_changeset_any_project(connection, changeset_id)
        return self._required_approvers_for_operations(row.get("operations") or [])

    def approve_changeset(
        self,
        connection: Connection,
        *,
        changeset_id: str,
        actor: str,
    ) -> dict[str, Any]:
        row = self._get_changeset_any_project(connection, changeset_id)
        if row["status"] not in {"pending_approval", "policy_checked", "budget_reserved"}:
            raise ValueError(f"ChangeSet is {row['status']} and cannot be approved.")

        # Dual-control: accumulate approvals and only finalize once every approver role
        # named by the operations' skills has approved.
        required = self._required_approvers_for_operations(row.get("operations") or [])
        policy_result = dict(row.get("policy_result") or {})
        approvals = list(policy_result.get("approvals") or [])
        if actor not in approvals:
            approvals.append(actor)
        policy_result["required_approvers"] = required
        policy_result["approvals"] = approvals
        missing = [role for role in required if role not in set(approvals)]

        if missing:
            connection.execute(
                text(
                    """
                    UPDATE changesets
                    SET policy_result = CAST(:policy_result AS JSONB),
                        updated_at = now()
                    WHERE id = :changeset_id
                    """
                ),
                {
                    "changeset_id": changeset_id,
                    "policy_result": json.dumps(policy_result),
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "changeset.approval_recorded",
                actor,
                "changeset",
                changeset_id,
                {
                    "approvals": approvals,
                    "required_approvers": required,
                    "missing_approvers": missing,
                },
            )
            return self.get_changeset(connection, row["project_id"], changeset_id)

        final_approver = ", ".join(approvals) if required else actor
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = 'approved',
                    approved_by = :approved_by,
                    approved_at = now(),
                    policy_result = CAST(:policy_result AS JSONB),
                    updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {
                "changeset_id": changeset_id,
                "approved_by": final_approver,
                "policy_result": json.dumps(policy_result),
            },
        )
        if row["approval_decision_id"]:
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'approved',
                        approved_by = :actor,
                        approved_at = now(),
                        updated_at = now(),
                        resolution_note = 'Approved through ProviderOps ChangeSet endpoint.'
                    WHERE id = :decision_id AND status = 'pending'
                    """
                ),
                {"decision_id": row["approval_decision_id"], "actor": actor},
            )
        emit_event(
            connection,
            row["project_id"],
            "changeset.approved",
            actor,
            "changeset",
            changeset_id,
            {
                "approval_decision_id": row["approval_decision_id"],
                "approvals": approvals,
                "required_approvers": required,
            },
        )
        return self.get_changeset(connection, row["project_id"], changeset_id)

    def apply_changeset(
        self,
        connection: Connection,
        *,
        changeset_id: str,
        actor: str,
        created_by_job_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        changeset = self._get_changeset_any_project(connection, changeset_id)
        project_id = changeset["project_id"]
        if changeset["status"] != "approved":
            emit_event(
                connection,
                project_id,
                "changeset.apply_blocked",
                actor,
                "changeset",
                changeset_id,
                {"reason": "changeset_not_approved", "status": changeset["status"]},
            )
            raise ValueError("ChangeSet must be approved before apply.")
        if int(changeset["expected_cost_usd_micros"]) > 0:
            reservation = self.budget.assert_active_reservation(
                connection,
                project_id,
                changeset["reservation_id"],
            )
            if not reservation:
                decision_id = create_blocking_decision(
                    connection,
                    project_id,
                    None,
                    "Missing active BudgetReservation for ProviderOps ChangeSet",
                    {"action": "reserve_provider_budget", "changeset_id": changeset_id},
                    {
                        "changeset_id": changeset_id,
                        "reservation_id": changeset["reservation_id"],
                    },
                    "provider_changeset_missing_reservation",
                    risk_tier=3,
                )
                emit_event(
                    connection,
                    project_id,
                    "changeset.apply_blocked",
                    actor,
                    "decision",
                    decision_id,
                    {"reason": "missing_active_reservation", "changeset_id": changeset_id},
                )
                return {
                    "status": "blocked",
                    "changeset": self.get_changeset(connection, project_id, changeset_id),
                    "resource_receipts": [],
                    "decision_id": decision_id,
                }
        operations = connection.execute(
            text(
                """
                SELECT id, project_id, changeset_id, provider_account_id, operation_index,
                       skill_id, action, resource_type, resource_ref, desired_state,
                       risk_tier, destructive, production_affecting, status,
                       resource_receipt_id
                FROM changeset_operations
                WHERE changeset_id = :changeset_id
                ORDER BY operation_index ASC
                """
            ),
            {"changeset_id": changeset_id},
        ).mappings().all()
        if dry_run:
            return {
                "status": "dry_run",
                "changeset": self.get_changeset(connection, project_id, changeset_id),
                "resource_receipts": [],
                "expected_receipts": changeset["expected_receipts"],
            }
        from .assurance import AssuranceService

        assurance = AssuranceService(self.settings)
        for operation in operations:
            guard = (operation.get("desired_state") or {}).get("surface_ownership")
            if not guard:
                continue
            preflight = assurance.preflight_surface(
                connection,
                project_id=project_id,
                surface_key=guard["surface_key"],
                environment=guard["environment"],
                actor_ref=actor,
                expected_version=int(guard["version"]),
                effect_ref=f"changeset:{changeset_id}:operation:{operation['id']}",
                dedupe_key=f"changeset-ownership:{changeset_id}:{operation['id']}",
            )
            if not preflight["allowed"]:
                emit_event(
                    connection,
                    project_id,
                    "changeset.apply_blocked",
                    actor,
                    "decision",
                    preflight["decision_id"],
                    {
                        "reason": "surface_ownership_preflight_failed",
                        "changeset_id": changeset_id,
                        "operation_id": operation["id"],
                    },
                )
                return {
                    "status": "blocked",
                    "changeset": self.get_changeset(
                        connection, project_id, changeset_id
                    ),
                    "resource_receipts": [],
                    "decision_id": preflight["decision_id"],
                }
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = 'applying',
                    applied_by = :actor,
                    updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {"changeset_id": changeset_id, "actor": actor},
        )
        resource_receipts: list[dict[str, Any]] = []
        failed = False
        for operation in operations:
            resource_receipt = self._record_resource_action_attempt(
                connection,
                changeset=dict(changeset),
                operation=dict(operation),
                actor=actor,
                created_by_job_id=created_by_job_id,
            )
            resource_receipts.append(resource_receipt)
            if resource_receipt["status"] != "succeeded":
                failed = True
                self._create_deadletter(
                    connection,
                    project_id=project_id,
                    source_kind="resource_receipt",
                    source_id=resource_receipt["id"],
                    reason=resource_receipt.get("error", {}).get(
                        "message",
                        "provider action failed",
                    ),
                    payload={
                        "changeset_id": changeset_id,
                        "operation_id": operation["id"],
                        "skill_id": operation["skill_id"],
                    },
                )
        final_status = "deadlettered" if failed else "applied"
        if failed and changeset["reservation_id"]:
            self.budget.release(
                connection,
                project_id,
                changeset["reservation_id"],
                "Released reservation because ProviderOps ChangeSet actuation failed.",
            )
        elif not failed and changeset["reservation_id"]:
            self.budget.settle(
                connection,
                project_id,
                changeset["reservation_id"],
                resource_receipts[0]["receipt_id"],
                "provider",
                "resource_plane.changeset.apply",
                int(changeset["expected_cost_usd_micros"]),
                None,
            )
            self._record_changeset_cost_event(
                connection,
                changeset=dict(changeset),
                resource_receipts=resource_receipts,
            )
        if not failed:
            self._mark_deployment_runs_deployed(
                connection,
                project_id=project_id,
                changeset_id=changeset_id,
                resource_receipts=resource_receipts,
            )
            self._register_elastic_workers_from_apply(
                connection,
                project_id=project_id,
                changeset_id=changeset_id,
                actor=actor,
                resource_receipts=resource_receipts,
                operations=[dict(op) for op in operations],
            )
        connection.execute(
            text(
                """
                UPDATE changesets
                SET status = :status,
                    applied_at = now(),
                    failure_reason = :failure_reason,
                    updated_at = now()
                WHERE id = :changeset_id
                """
            ),
            {
                "status": final_status,
                "failure_reason": (
                    "One or more provider actions failed; see resource receipts and deadletters."
                    if failed
                    else None
                ),
                "changeset_id": changeset_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "changeset.applied" if not failed else "changeset.deadlettered",
            actor,
            "changeset",
            changeset_id,
            {
                "status": final_status,
                "resource_receipt_ids": [item["id"] for item in resource_receipts],
            },
        )
        return {
            "status": final_status,
            "changeset": self.get_changeset(connection, project_id, changeset_id),
            "resource_receipts": resource_receipts,
        }

    def list_resource_receipts(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, changeset_id, operation_id, provider_account_id,
                       receipt_id, dedupe_key, provider, action, resource_type,
                       resource_ref, status, provider_ref, request_hash, result,
                       error, created_by_job_id, created_at, updated_at
                FROM resource_receipts
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
        ).mappings().all()
        return [self._serialize_resource_receipt(row) for row in rows]

    def list_worker_pools(
        self,
        connection: Connection,
        project_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, pool_key, kind, provider_account_id, provider,
                       status, current_workers, desired_workers, min_workers,
                       max_workers, max_queue_depth_per_worker,
                       cost_per_worker_hour_usd_micros, capabilities, metadata,
                       created_by, created_at, updated_at
                FROM worker_pools
                WHERE project_id = :project_id
                ORDER BY kind ASC, pool_key ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize_worker_pool(row) for row in rows]

    def upsert_worker_pool(
        self,
        connection: Connection,
        *,
        project_id: str,
        pool_key: str,
        kind: str,
        actor: str,
        provider_account_id: Optional[str] = None,
        current_workers: int = 0,
        desired_workers: int = 0,
        min_workers: int = 0,
        max_workers: int = 1,
        max_queue_depth_per_worker: int = 5,
        cost_per_worker_hour_usd_micros: int = 0,
        capabilities: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_kind = self._require_text(kind, "kind").lower()
        if normalized_kind not in {"local", "cloud"}:
            raise ValueError("kind must be local or cloud.")
        if min_workers < 0 or max_workers < 0 or current_workers < 0 or desired_workers < 0:
            raise ValueError("worker counts must be non-negative.")
        if min_workers > max_workers:
            raise ValueError("min_workers cannot exceed max_workers.")
        if desired_workers > max_workers:
            raise ValueError("desired_workers cannot exceed max_workers.")
        if max_queue_depth_per_worker < 1:
            raise ValueError("max_queue_depth_per_worker must be at least 1.")
        if cost_per_worker_hour_usd_micros < 0:
            raise ValueError("cost_per_worker_hour_usd_micros cannot be negative.")

        provider = None
        if provider_account_id:
            account = self.get_provider_account(connection, project_id, provider_account_id)
            provider = account["provider"]
        elif normalized_kind == "cloud":
            raise ValueError("cloud worker pools require provider_account_id.")

        pool_id = make_id("wp")
        connection.execute(
            text(
                """
                INSERT INTO worker_pools (
                    id, project_id, pool_key, kind, provider_account_id, provider,
                    status, current_workers, desired_workers, min_workers, max_workers,
                    max_queue_depth_per_worker, cost_per_worker_hour_usd_micros,
                    capabilities, metadata, created_by
                )
                VALUES (
                    :id, :project_id, :pool_key, :kind, :provider_account_id, :provider,
                    'active', :current_workers, :desired_workers, :min_workers,
                    :max_workers, :max_queue_depth_per_worker,
                    :cost_per_worker_hour_usd_micros, CAST(:capabilities AS JSONB),
                    CAST(:metadata AS JSONB), :actor
                )
                ON CONFLICT (project_id, pool_key) DO UPDATE
                SET kind = EXCLUDED.kind,
                    provider_account_id = EXCLUDED.provider_account_id,
                    provider = EXCLUDED.provider,
                    status = 'active',
                    current_workers = EXCLUDED.current_workers,
                    desired_workers = EXCLUDED.desired_workers,
                    min_workers = EXCLUDED.min_workers,
                    max_workers = EXCLUDED.max_workers,
                    max_queue_depth_per_worker = EXCLUDED.max_queue_depth_per_worker,
                    cost_per_worker_hour_usd_micros = EXCLUDED.cost_per_worker_hour_usd_micros,
                    capabilities = EXCLUDED.capabilities,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": pool_id,
                "project_id": project_id,
                "pool_key": self._require_text(pool_key, "pool_key"),
                "kind": normalized_kind,
                "provider_account_id": provider_account_id,
                "provider": provider,
                "current_workers": current_workers,
                "desired_workers": desired_workers,
                "min_workers": min_workers,
                "max_workers": max_workers,
                "max_queue_depth_per_worker": max_queue_depth_per_worker,
                "cost_per_worker_hour_usd_micros": cost_per_worker_hour_usd_micros,
                "capabilities": json.dumps(
                    self._normalize_string_list(capabilities or [])
                ),
                "metadata": json.dumps(metadata or {}),
                "actor": actor,
            },
        )
        pool = self._get_worker_pool_by_key(connection, project_id, pool_key)
        emit_event(
            connection,
            project_id,
            "worker_pool.upserted",
            actor,
            "worker_pool",
            pool["id"],
            {
                "pool_key": pool["pool_key"],
                "kind": pool["kind"],
                "provider_account_id": pool["provider_account_id"],
                "desired_workers": pool["desired_workers"],
                "max_workers": pool["max_workers"],
            },
        )
        return pool

    def list_scale_plans(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, worker_pool_id, status, trigger_reason,
                       queue_depth, running_jobs, leased_jobs, retryable_jobs,
                       current_workers, desired_workers_before,
                       desired_workers_after, target_workers, budget_state,
                       budget_available_usd_micros,
                       estimated_hourly_delta_usd_micros,
                       provider_health_status, actions, recommendations,
                       changeset_id, decision_id, created_by, created_at, applied_at
                FROM scale_plans
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [self._serialize_scale_plan(row) for row in rows]

    def run_autoscaler_plan(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        worker_pool_id: Optional[str] = None,
        cloud_spillover_enabled: bool = False,
    ) -> dict[str, Any]:
        pool = (
            self._get_worker_pool(connection, project_id, worker_pool_id)
            if worker_pool_id
            else self._ensure_default_worker_pool(connection, project_id, actor)
        )
        budget_snapshot = self.budget.status_for_project(connection, project_id)
        budget_policy = self._latest_budget_policy(connection, project_id)
        queue_snapshot = self._queue_snapshot(connection, project_id)
        provider_health = self._latest_provider_health(
            connection,
            project_id,
            provider_account_id=pool["provider_account_id"],
            provider=pool["provider"],
        )

        queue_depth = queue_snapshot["queue_depth"]
        per_worker = max(int(pool["max_queue_depth_per_worker"]), 1)
        needed_workers = 0 if queue_depth <= 0 else (
            queue_depth + per_worker - 1
        ) // per_worker
        policy_max_workers = int(
            budget_policy.get("max_workers_total") or pool["max_workers"] or 1
        )
        allowed_max_workers = max(
            0,
            min(int(pool["max_workers"]), policy_max_workers),
        )
        target_workers = max(
            int(pool["min_workers"]),
            min(needed_workers, allowed_max_workers),
        )
        desired_before = int(pool["desired_workers"])
        current_workers = int(pool["current_workers"])
        desired_after = desired_before
        status = "no_change"
        actions: list[dict[str, Any]] = []
        recommendations: list[dict[str, Any]] = []
        changeset_id: Optional[str] = None
        decision_id: Optional[str] = None
        trigger_reason = self._autoscaler_trigger_reason(
            budget_snapshot["budget_state"],
            queue_depth,
            desired_before,
            target_workers,
        )

        estimated_hourly_delta = max(
            target_workers - desired_before,
            0,
        ) * int(pool["cost_per_worker_hour_usd_micros"])

        if allowed_max_workers < needed_workers:
            recommendations.append(
                {
                    "action": "request_capacity_limit_review",
                    "reason": "queue_pressure_exceeds_policy_max_workers",
                    "needed_workers": needed_workers,
                    "allowed_max_workers": allowed_max_workers,
                }
            )

        budget_state = budget_snapshot["budget_state"]
        if budget_state == "red":
            desired_after = min(desired_before, int(pool["min_workers"]))
            actions.extend(
                [
                    {
                        "action": "enter_degraded_mode",
                        "reason": "red_budget_state",
                    },
                    {
                        "action": "pause_noncritical_work",
                        "reason": "budget_governor_wins_over_autoscaler",
                    },
                ]
            )
            if desired_after < desired_before:
                actions.append(
                    {
                        "action": "drain_workers",
                        "from": desired_before,
                        "to": desired_after,
                    }
                )
                self._set_worker_pool_desired(
                    connection,
                    project_id,
                    pool["id"],
                    desired_after,
                    actor,
                )
                status = "applied"
            else:
                status = "blocked"
            decision_id = self._create_autoscaler_decision(
                connection,
                project_id,
                "Budget state red blocked autoscaling",
                "increase_budget_or_defer_work",
                {
                    "budget_state": budget_state,
                    "queue_depth": queue_depth,
                    "worker_pool_id": pool["id"],
                },
                actor,
            )
        elif budget_state == "orange" and estimated_hourly_delta > 0:
            status = "blocked"
            target_workers = desired_before
            desired_after = desired_before
            actions.append(
                {
                    "action": "pause_noncritical_work",
                    "reason": "orange_budget_state_blocks_spendy_scale_up",
                }
            )
            decision_id = self._create_autoscaler_decision(
                connection,
                project_id,
                "Budget state orange blocked spendy autoscaling",
                "approve_budget_increase_or_accept_degraded_mode",
                {
                    "budget_state": budget_state,
                    "estimated_hourly_delta_usd_micros": estimated_hourly_delta,
                    "worker_pool_id": pool["id"],
                },
                actor,
            )
        elif pool["kind"] == "cloud" and provider_health["status"] not in {
            "healthy",
            "unknown",
        } and target_workers > desired_before:
            status = "blocked"
            target_workers = desired_before
            desired_after = desired_before
            actions.append(
                {
                    "action": "pause_cloud_spillover",
                    "reason": "provider_health_not_healthy",
                    "provider_health_status": provider_health["status"],
                }
            )
        elif target_workers > desired_before:
            if pool["kind"] == "local" or estimated_hourly_delta == 0:
                desired_after = target_workers
                self._set_worker_pool_desired(
                    connection,
                    project_id,
                    pool["id"],
                    desired_after,
                    actor,
                )
                status = "applied"
                actions.append(
                    {
                        "action": "scale_local_workers",
                        "from": desired_before,
                        "to": desired_after,
                    }
                )
            elif not cloud_spillover_enabled:
                status = "planned"
                recommendations.append(
                    {
                        "action": "enable_cloud_spillover_or_add_local_capacity",
                        "reason": "queue_pressure_exceeds_current_cloud_desired_capacity",
                        "target_workers": target_workers,
                    }
                )
            else:
                reservation = self.budget.reserve(
                    connection,
                    project_id,
                    "provider_autoscaler",
                    estimated_hourly_delta,
                    "Reserve one-hour cloud spillover estimate for autoscaler ChangeSet.",
                    60,
                )
                if reservation["status"] != "reserved":
                    status = "blocked"
                    decision_id = reservation.get("decision_id")
                    recommendations.append(
                        {
                            "action": "request_budget_increase",
                            "reason": "autoscaler_reservation_denied",
                            "budget_state": reservation["budget_state"],
                        }
                    )
                else:
                    changeset = self.create_changeset(
                        connection,
                        project_id=project_id,
                        provider_account_id=pool["provider_account_id"],
                        title=f"Scale cloud worker pool {pool['pool_key']}",
                        summary=(
                            "Provision cloud worker capacity requested by the "
                            "Budget-Aware Autoscaler."
                        ),
                        expected_cost_usd_micros=estimated_hourly_delta,
                        reservation_id=reservation["reservation_id"],
                        rollback_plan=[
                            "Drain the new worker.",
                            "Apply an approved Resource Janitor cleanup ChangeSet if provider resources were created.",
                        ],
                        risk_tier=3,
                        approval_required=True,
                        actor=actor,
                        operations=[
                            self._cloud_worker_create_operation(
                                pool,
                                target_workers - desired_before,
                            )
                        ],
                    )
                    status = "pending_approval"
                    changeset_id = changeset["id"]
                    decision_id = changeset["approval_decision_id"]
                    actions.append(
                        {
                            "action": "propose_cloud_workers",
                            "from": desired_before,
                            "to": target_workers,
                            "changeset_id": changeset_id,
                        }
                    )
        elif target_workers < desired_before:
            desired_after = target_workers
            self._set_worker_pool_desired(
                connection,
                project_id,
                pool["id"],
                desired_after,
                actor,
            )
            status = "applied"
            actions.append(
                {
                    "action": "drain_workers",
                    "from": desired_before,
                    "to": desired_after,
                }
            )
        else:
            desired_after = desired_before

        plan = self._insert_scale_plan(
            connection,
            project_id=project_id,
            worker_pool_id=pool["id"],
            status=status,
            trigger_reason=trigger_reason,
            queue_snapshot=queue_snapshot,
            current_workers=current_workers,
            desired_workers_before=desired_before,
            desired_workers_after=desired_after,
            target_workers=target_workers,
            budget_state=budget_state,
            budget_available_usd_micros=int(budget_snapshot["available_usd_micros"]),
            estimated_hourly_delta_usd_micros=estimated_hourly_delta,
            provider_health_status=provider_health["status"],
            actions=actions,
            recommendations=recommendations,
            changeset_id=changeset_id,
            decision_id=decision_id,
            actor=actor,
            applied=status == "applied",
        )
        emit_event(
            connection,
            project_id,
            "autoscaler.plan_created",
            actor,
            "scale_plan",
            plan["id"],
            {
                "status": status,
                "trigger_reason": trigger_reason,
                "worker_pool_id": pool["id"],
                "queue_depth": queue_depth,
                "desired_workers_before": desired_before,
                "desired_workers_after": desired_after,
                "budget_state": budget_state,
                "changeset_id": changeset_id,
                "decision_id": decision_id,
            },
        )
        return {
            "scale_plan": plan,
            "worker_pool": self._get_worker_pool(connection, project_id, pool["id"]),
            "budget_state": budget_state,
            "queue": queue_snapshot,
            "provider_health": provider_health,
            "changeset_id": changeset_id,
            "decision_id": decision_id,
        }

    def run_resource_janitor(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        provider_account_id: Optional[str] = None,
        max_candidates: int = 10,
    ) -> dict[str, Any]:
        if max_candidates < 1 or max_candidates > 100:
            raise ValueError("max_candidates must be between 1 and 100.")
        if provider_account_id:
            self.get_provider_account(connection, project_id, provider_account_id)

        resources = self._list_janitor_candidate_resources(
            connection,
            project_id=project_id,
            provider_account_id=provider_account_id,
            limit=max_candidates,
        )
        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        operations_by_provider: dict[Optional[str], list[dict[str, Any]]] = {}
        savings_by_provider: dict[Optional[str], int] = {}

        for row in resources:
            resource = self._serialize_external_resource(row)
            operation = self._cleanup_operation_for_resource(resource)
            if resource["environment"] == "production":
                skipped.append(
                    {
                        "external_resource_id": resource["id"],
                        "provider": resource["provider"],
                        "external_id": resource["external_id"],
                        "reason": "production_resource_requires_manual_review",
                    }
                )
                continue
            if not operation:
                skipped.append(
                    {
                        "external_resource_id": resource["id"],
                        "provider": resource["provider"],
                        "resource_type": resource["resource_type"],
                        "external_id": resource["external_id"],
                        "reason": "unsupported_cleanup_skill",
                    }
                )
                continue
            account = (
                self.get_provider_account(
                    connection,
                    project_id,
                    resource["provider_account_id"],
                )
                if resource["provider_account_id"]
                else None
            )
            if not account:
                skipped.append(
                    {
                        "external_resource_id": resource["id"],
                        "provider": resource["provider"],
                        "resource_type": resource["resource_type"],
                        "external_id": resource["external_id"],
                        "reason": "missing_provider_account",
                    }
                )
                continue
            if account["status"] not in {"active", "degraded"}:
                skipped.append(
                    {
                        "external_resource_id": resource["id"],
                        "provider": resource["provider"],
                        "resource_type": resource["resource_type"],
                        "external_id": resource["external_id"],
                        "reason": f"provider_account_{account['status']}",
                    }
                )
                continue
            if account["read_only"]:
                skipped.append(
                    {
                        "external_resource_id": resource["id"],
                        "provider": resource["provider"],
                        "resource_type": resource["resource_type"],
                        "external_id": resource["external_id"],
                        "reason": "provider_account_read_only",
                    }
                )
                continue
            if operation["destructive"] and account["destructive_actions_disabled"]:
                skipped.append(
                    {
                        "external_resource_id": resource["id"],
                        "provider": resource["provider"],
                        "resource_type": resource["resource_type"],
                        "external_id": resource["external_id"],
                        "reason": "provider_destructive_actions_disabled",
                    }
                )
                continue
            if self._has_open_cleanup_changeset(
                connection,
                project_id=project_id,
                resource=resource,
                operation=operation,
            ):
                skipped.append(
                    {
                        "external_resource_id": resource["id"],
                        "provider": resource["provider"],
                        "resource_type": resource["resource_type"],
                        "external_id": resource["external_id"],
                        "reason": "open_cleanup_changeset_exists",
                    }
                )
                continue

            savings = self._resource_monthly_savings(resource)
            candidate = {
                "external_resource_id": resource["id"],
                "provider_account_id": resource["provider_account_id"],
                "provider": resource["provider"],
                "resource_type": resource["resource_type"],
                "external_id": resource["external_id"],
                "name": resource["name"],
                "environment": resource["environment"],
                "ownership": resource["ownership"],
                "health_status": resource["health_status"],
                "estimated_monthly_savings_usd_micros": savings,
                "cost_confidence": resource["cost_confidence"],
                "cleanup_skill_id": operation["skill_id"],
                "destructive": operation["destructive"],
            }
            desired_state = dict(operation.get("desired_state") or {})
            desired_state.update(
                {
                    "external_resource_id": resource["id"],
                    "provider": resource["provider"],
                    "external_id": resource["external_id"],
                    "name": resource["name"],
                    "environment": resource["environment"],
                    "estimated_monthly_savings_usd_micros": savings,
                    "cost_confidence": resource["cost_confidence"],
                    "janitor_reason": self._janitor_reason(resource),
                }
            )
            operation["desired_state"] = desired_state
            operation["resource_ref"] = resource["external_id"]
            key = resource["provider_account_id"]
            operations_by_provider.setdefault(key, []).append(operation)
            savings_by_provider[key] = savings_by_provider.get(key, 0) + savings
            candidates.append(candidate)

        changesets = []
        for key, operations in operations_by_provider.items():
            provider_label = candidates[0]["provider"] if candidates else "provider"
            provider_candidates = [
                item for item in candidates if item["provider_account_id"] == key
            ]
            if provider_candidates:
                provider_label = provider_candidates[0]["provider"]
            title = f"Resource Janitor cleanup proposal for {provider_label}"
            savings = savings_by_provider.get(key, 0)
            rollback_plan = [
                "Review provider console snapshots before approval.",
                "Restore from provider snapshot/backup or recreate from recorded desired_state if cleanup is applied in error.",
                "Re-run ProviderOps inventory sync after cleanup to verify drift is resolved.",
            ]
            changeset = self.create_changeset(
                connection,
                project_id=project_id,
                provider_account_id=key,
                title=title,
                summary=(
                    "Resource Janitor proposed cleanup for orphaned or expired "
                    f"non-production resources with estimated monthly savings of {savings} micros."
                ),
                operations=operations,
                expected_cost_usd_micros=0,
                reservation_id=None,
                rollback_plan=rollback_plan,
                risk_tier=3 if any(op.get("destructive") for op in operations) else 2,
                approval_required=True,
                actor=actor,
            )
            policy_result = dict(changeset.get("policy_result") or {})
            policy_result.update(
                {
                    "janitor_proposal": True,
                    "never_auto_delete_production_resources": True,
                    "requires_explicit_approval": True,
                    "estimated_monthly_savings_usd_micros": savings,
                }
            )
            cost_quote = dict(changeset.get("cost_quote") or {})
            cost_quote.update(
                {
                    "savings_estimate": {
                        "estimated_monthly_savings_usd_micros": savings,
                        "confidence": "resource_inventory_estimate",
                    }
                }
            )
            connection.execute(
                text(
                    """
                    UPDATE changesets
                    SET policy_result = CAST(:policy_result AS JSONB),
                        cost_quote = CAST(:cost_quote AS JSONB),
                        updated_at = now()
                    WHERE id = :changeset_id
                    """
                ),
                {
                    "changeset_id": changeset["id"],
                    "policy_result": json.dumps(policy_result),
                    "cost_quote": json.dumps(cost_quote),
                },
            )
            changesets.append(
                self.get_changeset(connection, project_id, changeset["id"])
            )

        estimated_savings = sum(
            int(item["estimated_monthly_savings_usd_micros"]) for item in candidates
        )
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "resource_janitor_report",
            "Resource Janitor cleanup proposal report",
            {
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "candidate_count": len(candidates),
                "skipped_count": len(skipped),
                "estimated_monthly_savings_usd_micros": estimated_savings,
                "candidates": candidates,
                "skipped": skipped,
                "changeset_ids": [changeset["id"] for changeset in changesets],
                "data_boundary": "internal",
            },
            data_boundary="internal",
        )
        emit_event(
            connection,
            project_id,
            "resource_janitor.completed",
            actor,
            "artifact",
            artifact_id,
            {
                "candidate_count": len(candidates),
                "skipped_count": len(skipped),
                "changeset_ids": [changeset["id"] for changeset in changesets],
                "estimated_monthly_savings_usd_micros": estimated_savings,
            },
        )
        return {
            "status": "completed",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "candidate_count": len(candidates),
            "skipped_count": len(skipped),
            "estimated_monthly_savings_usd_micros": estimated_savings,
            "candidates": candidates,
            "skipped": skipped,
            "changesets": changesets,
        }

    def _get_worker_pool(
        self,
        connection: Connection,
        project_id: str,
        worker_pool_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, pool_key, kind, provider_account_id, provider,
                       status, current_workers, desired_workers, min_workers,
                       max_workers, max_queue_depth_per_worker,
                       cost_per_worker_hour_usd_micros, capabilities, metadata,
                       created_by, created_at, updated_at
                FROM worker_pools
                WHERE id = :worker_pool_id AND project_id = :project_id
                """
            ),
            {"worker_pool_id": worker_pool_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ValueError("worker pool not found.")
        return self._serialize_worker_pool(row)

    def _get_worker_pool_by_key(
        self,
        connection: Connection,
        project_id: str,
        pool_key: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, pool_key, kind, provider_account_id, provider,
                       status, current_workers, desired_workers, min_workers,
                       max_workers, max_queue_depth_per_worker,
                       cost_per_worker_hour_usd_micros, capabilities, metadata,
                       created_by, created_at, updated_at
                FROM worker_pools
                WHERE project_id = :project_id AND pool_key = :pool_key
                """
            ),
            {"project_id": project_id, "pool_key": pool_key},
        ).mappings().one()
        return self._serialize_worker_pool(row)

    def _ensure_default_worker_pool(
        self,
        connection: Connection,
        project_id: str,
        actor: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, pool_key, kind, provider_account_id, provider,
                       status, current_workers, desired_workers, min_workers,
                       max_workers, max_queue_depth_per_worker,
                       cost_per_worker_hour_usd_micros, capabilities, metadata,
                       created_by, created_at, updated_at
                FROM worker_pools
                WHERE project_id = :project_id AND pool_key = 'local-default'
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row:
            return self._serialize_worker_pool(row)
        budget_policy = self._latest_budget_policy(connection, project_id)
        return self.upsert_worker_pool(
            connection,
            project_id=project_id,
            pool_key="local-default",
            kind="local",
            actor=actor,
            current_workers=0,
            desired_workers=0,
            min_workers=0,
            max_workers=max(int(budget_policy.get("max_workers_total") or 1), 1),
            max_queue_depth_per_worker=5,
            cost_per_worker_hour_usd_micros=0,
            capabilities=["*"],
            metadata={"created_by_autoscaler": True},
        )

    def _latest_budget_policy(
        self,
        connection: Connection,
        project_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT max_concurrent_jobs, max_workers_total,
                       freeze_provisioning_pct, auto_lite_mode_pct
                FROM budget_policies
                WHERE scope_type = 'project' AND scope_id = :project_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        return dict(row) if row else {
            "max_concurrent_jobs": 1,
            "max_workers_total": 1,
            "freeze_provisioning_pct": 0.9,
            "auto_lite_mode_pct": 0.85,
        }

    def _queue_snapshot(
        self,
        connection: Connection,
        project_id: str,
    ) -> dict[str, int]:
        row = connection.execute(
            text(
                """
                SELECT
                  COUNT(*) FILTER (
                    WHERE status IN ('queued', 'failed_retryable') AND run_after <= now()
                  ) AS queue_depth,
                  COUNT(*) FILTER (WHERE status = 'running') AS running_jobs,
                  COUNT(*) FILTER (WHERE status = 'leased') AS leased_jobs,
                  COUNT(*) FILTER (WHERE status = 'failed_retryable') AS retryable_jobs
                FROM jobs
                WHERE project_id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        return {
            "queue_depth": int(row["queue_depth"] or 0),
            "running_jobs": int(row["running_jobs"] or 0),
            "leased_jobs": int(row["leased_jobs"] or 0),
            "retryable_jobs": int(row["retryable_jobs"] or 0),
        }

    def _latest_provider_health(
        self,
        connection: Connection,
        project_id: str,
        *,
        provider_account_id: Optional[str],
        provider: Optional[str],
    ) -> dict[str, Any]:
        if provider_account_id:
            row = connection.execute(
                text(
                    """
                    SELECT status, rate_limit_status, usage_cap_status, detail
                    FROM provider_health
                    WHERE project_id = :project_id
                      AND provider_account_id = :provider_account_id
                    ORDER BY last_checked_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "project_id": project_id,
                    "provider_account_id": provider_account_id,
                },
            ).mappings().first()
        elif provider:
            row = connection.execute(
                text(
                    """
                    SELECT status, rate_limit_status, usage_cap_status, detail
                    FROM provider_health
                    WHERE project_id = :project_id AND provider = :provider
                    ORDER BY last_checked_at DESC
                    LIMIT 1
                    """
                ),
                {"project_id": project_id, "provider": provider},
            ).mappings().first()
        else:
            row = None
        if not row:
            return {
                "status": "unknown",
                "rate_limit_status": "unknown",
                "usage_cap_status": "unknown",
                "detail": {},
            }
        return {
            "status": row["status"],
            "rate_limit_status": row["rate_limit_status"],
            "usage_cap_status": row["usage_cap_status"],
            "detail": row["detail"] or {},
        }

    def _set_worker_pool_desired(
        self,
        connection: Connection,
        project_id: str,
        worker_pool_id: str,
        desired_workers: int,
        actor: str,
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE worker_pools
                SET desired_workers = :desired_workers,
                    updated_at = now()
                WHERE id = :worker_pool_id AND project_id = :project_id
                """
            ),
            {
                "desired_workers": desired_workers,
                "worker_pool_id": worker_pool_id,
                "project_id": project_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "worker_pool.desired_capacity_changed",
            actor,
            "worker_pool",
            worker_pool_id,
            {"desired_workers": desired_workers},
        )

    def _insert_scale_plan(
        self,
        connection: Connection,
        *,
        project_id: str,
        worker_pool_id: str,
        status: str,
        trigger_reason: str,
        queue_snapshot: dict[str, int],
        current_workers: int,
        desired_workers_before: int,
        desired_workers_after: int,
        target_workers: int,
        budget_state: str,
        budget_available_usd_micros: int,
        estimated_hourly_delta_usd_micros: int,
        provider_health_status: str,
        actions: list[dict[str, Any]],
        recommendations: list[dict[str, Any]],
        changeset_id: Optional[str],
        decision_id: Optional[str],
        actor: str,
        applied: bool,
    ) -> dict[str, Any]:
        plan_id = make_id("scp")
        connection.execute(
            text(
                """
                INSERT INTO scale_plans (
                    id, project_id, worker_pool_id, status, trigger_reason,
                    queue_depth, running_jobs, leased_jobs, retryable_jobs,
                    current_workers, desired_workers_before, desired_workers_after,
                    target_workers, budget_state, budget_available_usd_micros,
                    estimated_hourly_delta_usd_micros, provider_health_status,
                    actions, recommendations, changeset_id, decision_id,
                    created_by, applied_at
                )
                VALUES (
                    :id, :project_id, :worker_pool_id, :status, :trigger_reason,
                    :queue_depth, :running_jobs, :leased_jobs, :retryable_jobs,
                    :current_workers, :desired_workers_before, :desired_workers_after,
                    :target_workers, :budget_state, :budget_available_usd_micros,
                    :estimated_hourly_delta_usd_micros, :provider_health_status,
                    CAST(:actions AS JSONB), CAST(:recommendations AS JSONB),
                    :changeset_id, :decision_id, :actor,
                    CASE WHEN :applied THEN now() ELSE NULL END
                )
                """
            ),
            {
                "id": plan_id,
                "project_id": project_id,
                "worker_pool_id": worker_pool_id,
                "status": status,
                "trigger_reason": trigger_reason,
                "queue_depth": queue_snapshot["queue_depth"],
                "running_jobs": queue_snapshot["running_jobs"],
                "leased_jobs": queue_snapshot["leased_jobs"],
                "retryable_jobs": queue_snapshot["retryable_jobs"],
                "current_workers": current_workers,
                "desired_workers_before": desired_workers_before,
                "desired_workers_after": desired_workers_after,
                "target_workers": target_workers,
                "budget_state": budget_state,
                "budget_available_usd_micros": budget_available_usd_micros,
                "estimated_hourly_delta_usd_micros": estimated_hourly_delta_usd_micros,
                "provider_health_status": provider_health_status,
                "actions": json.dumps(actions),
                "recommendations": json.dumps(recommendations),
                "changeset_id": changeset_id,
                "decision_id": decision_id,
                "actor": actor,
                "applied": applied,
            },
        )
        row = connection.execute(
            text(
                """
                SELECT id, project_id, worker_pool_id, status, trigger_reason,
                       queue_depth, running_jobs, leased_jobs, retryable_jobs,
                       current_workers, desired_workers_before,
                       desired_workers_after, target_workers, budget_state,
                       budget_available_usd_micros,
                       estimated_hourly_delta_usd_micros,
                       provider_health_status, actions, recommendations,
                       changeset_id, decision_id, created_by, created_at, applied_at
                FROM scale_plans
                WHERE id = :plan_id
                """
            ),
            {"plan_id": plan_id},
        ).mappings().one()
        return self._serialize_scale_plan(row)

    def _autoscaler_trigger_reason(
        self,
        budget_state: str,
        queue_depth: int,
        desired_before: int,
        target_workers: int,
    ) -> str:
        if budget_state in {"orange", "red"}:
            return f"budget_state_{budget_state}"
        if target_workers > desired_before:
            return "queue_pressure"
        if target_workers < desired_before:
            return "idle_capacity"
        if queue_depth > 0:
            return "steady_queue"
        return "steady_state"

    def _create_autoscaler_decision(
        self,
        connection: Connection,
        project_id: str,
        question: str,
        action: str,
        evidence: dict[str, Any],
        actor: str,
    ) -> str:
        decision_id = create_blocking_decision(
            connection,
            project_id,
            None,
            question,
            {
                "action": action,
                "actor": actor,
                "autoscaler_guardrail": "budget_governor_wins_over_autoscaler",
            },
            evidence,
            "autoscaler_requires_decision",
            risk_tier=2,
        )
        emit_event(
            connection,
            project_id,
            "autoscaler.decision_required",
            actor,
            "decision",
            decision_id,
            evidence,
        )
        return decision_id

    def _cloud_worker_create_operation(
        self,
        pool: dict[str, Any],
        count: int,
    ) -> dict[str, Any]:
        provider = (pool.get("provider") or "").strip().lower()
        base_desired = {
            "worker_pool_id": pool["id"],
            "pool_key": pool["pool_key"],
            "count": count,
            "tags": ["boss", "worker", f"pool:{pool['pool_key']}"],
            "elastic_fabric": True,
        }
        if provider == "digitalocean":
            return {
                "skill_id": "digitalocean.droplet.create",
                "action": "create",
                "resource_type": "droplet",
                "risk_tier": 3,
                "destructive": False,
                "production_affecting": False,
                "desired_state": base_desired,
            }
        if provider == "aws":
            return {
                "skill_id": "aws.ec2.instance.create",
                "action": "create",
                "resource_type": "ec2_instance",
                "risk_tier": 3,
                "destructive": False,
                "production_affecting": False,
                "desired_state": {
                    **base_desired,
                    "instance_type": pool.get("size") or "t3.medium",
                },
            }
        if provider == "azure":
            return {
                "skill_id": "azure.vm.create",
                "action": "create",
                "resource_type": "vm",
                "risk_tier": 3,
                "destructive": False,
                "production_affecting": False,
                "desired_state": {
                    **base_desired,
                    "vm_size": pool.get("size") or "Standard_B2s",
                },
            }
        if provider in {"ovhcloud", "ovh"}:
            return {
                "skill_id": "ovh.cloud.instance.create",
                "action": "create",
                "resource_type": "cloud_instance",
                "risk_tier": 3,
                "destructive": False,
                "production_affecting": False,
                "desired_state": base_desired,
            }
        raise ValueError(f"Provider {provider} does not support cloud worker creation.")

    def _register_elastic_workers_from_apply(
        self,
        connection: Connection,
        *,
        project_id: str,
        changeset_id: str,
        actor: str,
        resource_receipts: list[dict[str, Any]],
        operations: list[dict[str, Any]],
    ) -> None:
        """After a successful apply, register VM instances into the elastic fabric."""
        try:
            from .elastic_fabric import ElasticFabricService
        except Exception:
            return
        fabric = ElasticFabricService(self.settings, resource_plane=self)
        ops_by_id = {op["id"]: op for op in operations}
        for receipt in resource_receipts:
            if receipt.get("status") != "succeeded":
                continue
            # Prefer resource_receipt payload fields
            external_id = (
                receipt.get("provider_resource_id")
                or receipt.get("resource_external_id")
                or (receipt.get("metadata") or {}).get("resource_external_id")
            )
            if not external_id:
                # Fall back to ResourceReceipt table shape if present
                external_id = (receipt.get("result") or {}).get("resource_external_id")
            if not external_id:
                continue
            op = ops_by_id.get(receipt.get("operation_id") or "")
            desired = {}
            if op:
                desired = op.get("desired_state") or {}
            else:
                desired = (receipt.get("desired_state") or {}) if isinstance(
                    receipt.get("desired_state"), dict
                ) else {}
            pool_id = desired.get("worker_pool_id")
            if not pool_id and not desired.get("elastic_fabric"):
                # Still register if skill is droplet create and tags mention worker
                skill = (op or {}).get("skill_id") or receipt.get("skill_id") or ""
                if skill != "digitalocean.droplet.create":
                    continue
            if not pool_id:
                # Attach to default cloud pool if any
                pools = self.list_worker_pools(connection, project_id)
                cloud_pools = [p for p in pools if p.get("kind") == "cloud"]
                if not cloud_pools:
                    continue
                pool_id = cloud_pools[0]["id"]
            try:
                fabric.register_provider_instance(
                    connection,
                    project_id=project_id,
                    worker_pool_id=str(pool_id),
                    actor=actor,
                    external_id=str(external_id),
                    provider=str(
                        receipt.get("provider")
                        or (op or {}).get("provider")
                        or "digitalocean"
                    ),
                    provider_account_id=receipt.get("provider_account_id")
                    or (op or {}).get("provider_account_id"),
                    kind="vm",
                    region=desired.get("region")
                    or (receipt.get("result") or {}).get("region"),
                    size=desired.get("size")
                    or (receipt.get("result") or {}).get("size"),
                    changeset_id=changeset_id,
                    receipt_id=receipt.get("receipt_id") or receipt.get("id"),
                    metadata={
                        "from_changeset_apply": True,
                        "skill_id": (op or {}).get("skill_id"),
                    },
                )
            except Exception:
                # Never fail apply because elastic registration had a problem —
                # surface via event only.
                emit_event(
                    connection,
                    project_id,
                    "elastic.instance_register_failed",
                    actor,
                    "changeset",
                    changeset_id,
                    {
                        "external_id": str(external_id),
                        "worker_pool_id": str(pool_id),
                    },
                )

    def _get_deployment_target(
        self,
        connection: Connection,
        project_id: str,
        deployment_target_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, app_name, repo, environment, provider_account_id,
                       target_kind, target_ref, domains, required_resources, secret_refs,
                       health_checks, rollback_points, status, created_by, created_at, updated_at
                FROM deployment_targets
                WHERE id = :deployment_target_id AND project_id = :project_id
                """
            ),
            {
                "deployment_target_id": deployment_target_id,
                "project_id": project_id,
            },
        ).mappings().first()
        if not row:
            raise ValueError("Deployment target not found.")
        return self._serialize_deployment_target(row)

    def _get_deployment_environment_policy(
        self,
        connection: Connection,
        project_id: str,
        environment: str,
        *,
        active_only: bool = True,
    ) -> Optional[dict[str, Any]]:
        query = """
            SELECT id, project_id, environment, runtime_mode,
                   allowed_target_kinds, requires_change_set,
                   requires_budget_reservation, requires_approval,
                   requires_health_check, requires_rollback_plan,
                   rollout_notes, status, created_by, metadata,
                   created_at, updated_at
            FROM deployment_environment_policies
            WHERE project_id = :project_id
              AND environment = :environment
        """
        if active_only:
            query += " AND status = 'active'"
        row = connection.execute(
            text(query),
            {
                "project_id": project_id,
                "environment": self._normalize_deployment_environment(environment),
            },
        ).mappings().first()
        return self._serialize_deployment_environment_policy(row) if row else None

    def _get_deployment_run(
        self,
        connection: Connection,
        project_id: str,
        deployment_run_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, deployment_target_id, changeset_id, decision_id,
                       release_artifact_id, health_artifact_id, rollback_artifact_id,
                       app_name, environment, repo, target_kind, target_ref,
                       commit_ref, status, release_packet, health_check_results,
                       rollback_plan, release_receipt_ids, created_by, created_at,
                       updated_at, started_at, finished_at, failure_reason
                FROM deployment_runs
                WHERE id = :deployment_run_id AND project_id = :project_id
                """
            ),
            {
                "deployment_run_id": deployment_run_id,
                "project_id": project_id,
            },
        ).mappings().first()
        if not row:
            raise ValueError("Deployment run not found.")
        return self._serialize_deployment_run(row)

    def _normalize_deployment_environment(self, environment: str) -> str:
        return self._require_text(environment, "environment").lower().replace(" ", "_")

    def _deployment_rollout_policy_for_target(
        self,
        connection: Connection,
        project_id: str,
        target: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        environment = self._normalize_deployment_environment(target["environment"])
        if (
            not target.get("provider_account_id")
            or environment in DEPLOYMENT_SANDBOX_ENVIRONMENTS
        ):
            return None
        policy = self._get_deployment_environment_policy(
            connection,
            project_id,
            environment,
        )
        if policy:
            return policy
        return {
            "id": None,
            "project_id": project_id,
            "environment": environment,
            "runtime_mode": "metadata_only",
            "allowed_target_kinds": [],
            "requires_change_set": True,
            "requires_budget_reservation": True,
            "requires_approval": True,
            "requires_health_check": environment == "production",
            "requires_rollback_plan": True,
            "rollout_notes": None,
            "status": "missing",
            "created_by": "system",
            "metadata": {"reason": "missing_explicit_environment_policy"},
            "created_at": None,
            "updated_at": None,
        }

    def _deployment_rollout_policy_block_reason(
        self,
        target: dict[str, Any],
        policy: dict[str, Any],
        rollback_plan: list[str],
        reservation_id: Optional[str],
    ) -> Optional[str]:
        environment = self._normalize_deployment_environment(target["environment"])
        mode = str(policy.get("runtime_mode") or "metadata_only")
        if policy.get("status") != "active":
            return "missing_active_environment_policy"
        if mode in {"disabled", "metadata_only"}:
            return f"environment_{environment}_not_enabled_for_provider_rollout"
        if environment == "production" and mode != "production_allowed":
            return "production_rollout_requires_production_allowed_policy"
        if environment == "staging" and mode not in {
            "staging_allowed",
            "production_allowed",
        }:
            return "staging_rollout_requires_staging_allowed_policy"
        if environment not in {"staging", "production"} and mode not in {
            "preview_allowed",
            "staging_allowed",
            "production_allowed",
        }:
            return f"environment_{environment}_not_allowed_by_policy"
        allowed_target_kinds = set(policy.get("allowed_target_kinds") or [])
        if allowed_target_kinds and target["target_kind"] not in allowed_target_kinds:
            return "target_kind_not_allowed_by_environment_policy"
        if policy.get("requires_change_set", True) and not target.get("provider_account_id"):
            return "provider_change_set_required"
        if policy.get("requires_budget_reservation", True) and not reservation_id:
            return "budget_reservation_required_by_environment_policy"
        if policy.get("requires_health_check", True) and not target.get("health_checks"):
            return "health_check_required_by_environment_policy"
        if policy.get("requires_rollback_plan", True) and not rollback_plan:
            return "rollback_plan_required_by_environment_policy"
        return None

    def _record_blocked_deployment_run(
        self,
        connection: Connection,
        *,
        project_id: str,
        target: dict[str, Any],
        commit_ref: Optional[str],
        release_packet: dict[str, Any],
        rollback_plan: list[str],
        actor: str,
        title: str,
        recommendation: dict[str, Any],
        evidence: dict[str, Any],
        reason_code: str,
        failure_reason: str,
        event_reason: str,
    ) -> dict[str, Any]:
        decision_id = create_blocking_decision(
            connection,
            project_id,
            None,
            title,
            recommendation,
            evidence,
            reason_code,
            risk_tier=3 if target["environment"] == "production" else 2,
        )
        emit_event(
            connection,
            project_id,
            "deployment_run.blocked",
            actor,
            "decision",
            decision_id,
            {
                "deployment_target_id": target["id"],
                "environment": target["environment"],
                "reason": event_reason,
            },
        )
        deployment_run_id = make_id("deprun")
        connection.execute(
            text(
                """
                INSERT INTO deployment_runs (
                    id, project_id, deployment_target_id, decision_id,
                    app_name, environment, repo, target_kind, target_ref,
                    commit_ref, status, release_packet, rollback_plan,
                    release_receipt_ids, created_by, started_at,
                    failure_reason
                )
                VALUES (
                    :id, :project_id, :deployment_target_id, :decision_id,
                    :app_name, :environment, :repo, :target_kind,
                    :target_ref, :commit_ref, 'failed',
                    CAST(:release_packet AS JSONB), CAST(:rollback_plan AS JSONB),
                    '[]'::jsonb, :actor, now(), :failure_reason
                )
                """
            ),
            {
                "id": deployment_run_id,
                "project_id": project_id,
                "deployment_target_id": target["id"],
                "decision_id": decision_id,
                "app_name": target["app_name"],
                "environment": target["environment"],
                "repo": target["repo"],
                "target_kind": target["target_kind"],
                "target_ref": target["target_ref"],
                "commit_ref": commit_ref,
                "release_packet": json.dumps(release_packet),
                "rollback_plan": json.dumps(rollback_plan),
                "actor": actor,
                "failure_reason": failure_reason,
            },
        )
        return self._get_deployment_run(connection, project_id, deployment_run_id)

    def _deployment_rollback_plan(self, target: dict[str, Any]) -> list[str]:
        steps: list[str] = []
        for rollback_point in target["rollback_points"]:
            if isinstance(rollback_point, str) and rollback_point.strip():
                steps.append(rollback_point.strip())
            elif isinstance(rollback_point, dict):
                for key in ("step", "instruction", "ref"):
                    value = rollback_point.get(key)
                    if isinstance(value, str) and value.strip():
                        steps.append(value.strip())
                        break
        if steps:
            return steps
        return [
            f"Revert {target['app_name']} {target['environment']} to the previous known-good release.",
            "Re-run deployment health checks after rollback.",
        ]

    def _deployment_operation_for_target(
        self,
        connection: Connection,
        target: dict[str, Any],
        release_artifact_id: str,
        commit_ref: Optional[str],
    ) -> Optional[dict[str, Any]]:
        target_kind = target["target_kind"].lower().replace("-", "_")
        # `provider_account_id` identifies a CLOUD PROVIDER ACCOUNT — an AWS/GCP/Azure identity
        # whose API this deployment will call. A host reached over SSH is not that: its
        # credential is a key, declared in the target's secret_refs and checked by the
        # connector's validate() before anything is attempted. Demanding a provider account
        # for it blocked a target that was fully credentialed and ready, with an error naming
        # a concept that does not apply.
        #
        # Kept narrow deliberately: cloud kinds still require an account, because there the
        # account IS the credential and the quota boundary.
        if target_kind in {"local", "local_process", "local_runner", "ssh", "container"}:
            return None
        provider_account_id = target["provider_account_id"]
        if not provider_account_id:
            raise ValueError(
                f"Cloud deployment targets require provider_account_id "
                f"(target_kind={target_kind!r}).")
        account = self.get_provider_account(
            connection,
            target["project_id"],
            provider_account_id,
        )
        if account["read_only"]:
            raise ValueError("Provider account is read-only; deployment is blocked.")
        if not account["deploy_capable"]:
            raise ValueError("Provider account is not marked deploy-capable.")

        provider = account["provider"]
        skill_id: Optional[str] = None
        resource_type = "deployment"
        if provider == "cloudflare" and "pages" in target_kind:
            skill_id = "cloudflare.pages.deploy"
            resource_type = "pages_deployment"
        elif provider == "cloudflare" and "worker" in target_kind:
            skill_id = "cloudflare.worker.deploy"
            resource_type = "worker"
        elif provider == "digitalocean" and "app" in target_kind:
            skill_id = "digitalocean.app.deploy"
            resource_type = "app"
        elif provider == "supabase" and (
            "migration" in target_kind or "database" in target_kind
        ):
            skill_id = "supabase.migration.apply"
            resource_type = "migration"
        if not skill_id:
            raise ValueError(
                f"No registered deployment skill for provider {provider} and target kind {target['target_kind']}."
            )

        return {
            "skill_id": skill_id,
            "action": skill_id,
            "resource_type": resource_type,
            "resource_ref": target["target_ref"],
            "risk_tier": 3 if target["environment"] == "production" else 2,
            "destructive": False,
            "production_affecting": target["environment"] == "production",
            "desired_state": {
                "deployment_target_id": target["id"],
                "target_kind": target["target_kind"],
                "release_artifact_id": release_artifact_id,
                "commit_ref": commit_ref,
                "environment": target["environment"],
                "target_ref": target["target_ref"],
                "domains": target["domains"],
                "secret_refs": target["secret_refs"],
                "required_resources": target["required_resources"],
                "health_checks": target["health_checks"],
            },
        }

    def _run_http_health_check(self, check: dict[str, Any]) -> dict[str, Any]:
        name = str(check.get("name") or check.get("url") or "health_check")
        url = str(check.get("url") or "").strip()
        if not url:
            return {"name": name, "status": "failed", "error": "missing_url"}
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return {
                "name": name,
                "url": url,
                "status": "failed",
                "error": "unsupported_health_check_scheme",
            }
        method = str(check.get("method") or "GET").upper()
        if method not in {"GET", "HEAD"}:
            return {
                "name": name,
                "url": url,
                "status": "failed",
                "error": "unsupported_health_check_method",
            }
        expected_status = int(check.get("expected_status") or 200)
        timeout_seconds = float(check.get("timeout_seconds") or 5)
        try:
            response = httpx.request(
                method,
                url,
                timeout=min(max(timeout_seconds, 0.5), 30.0),
                follow_redirects=True,
            )
        except httpx.HTTPError as error:
            return {
                "name": name,
                "url": url,
                "status": "failed",
                "error": str(error),
            }
        passed = response.status_code == expected_status
        return {
            "name": name,
            "url": url,
            "status": "passed" if passed else "failed",
            "status_code": response.status_code,
            "expected_status": expected_status,
        }

    def _validate_deployment_changeset_environment_policies(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: Optional[str],
        operations: list[dict[str, Any]],
        rollback_plan: list[str],
        reservation_id: Optional[str],
    ) -> None:
        for operation in operations:
            skill_id = str(operation.get("skill_id") or "")
            if skill_id not in DEPLOYMENT_SKILL_IDS:
                continue
            desired_state = operation.get("desired_state") or {}
            environment = str(
                desired_state.get("environment")
                or ("production" if operation.get("production_affecting") else "")
            ).strip()
            if not environment:
                raise ValueError(
                    "Deployment ChangeSet operations require desired_state.environment."
                )
            target_kind = str(
                desired_state.get("target_kind")
                or operation.get("resource_type")
                or ""
            ).strip()
            if not target_kind:
                raise ValueError(
                    "Deployment ChangeSet operations require desired_state.target_kind."
                )
            target = {
                "id": desired_state.get("deployment_target_id")
                or operation.get("resource_ref"),
                "environment": environment,
                "target_kind": target_kind,
                "provider_account_id": provider_account_id,
                "health_checks": desired_state.get("health_checks") or [],
            }
            policy = self._deployment_rollout_policy_for_target(
                connection,
                project_id,
                target,
            )
            if not policy:
                continue
            block_reason = self._deployment_rollout_policy_block_reason(
                target,
                policy,
                rollback_plan,
                reservation_id,
            )
            if block_reason:
                raise ValueError(
                    "Deployment ChangeSet blocked by environment rollout policy: "
                    f"{block_reason}."
                )

    def get_provider_account(
        self, connection: Connection, project_id: str, provider_account_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider, account_label, auth_method,
                       credential_ref, credential_refs, connection_metadata, scopes,
                       status, owner_actor, credential_version, credential_rotated_at,
                       disabled_at, disabled_by, revoked_at, revoked_by,
                       read_only, deploy_capable, billing_readable,
                       destructive_actions_disabled, capabilities, missing_permissions,
                       last_test_status, last_tested_at, last_error, created_by,
                       created_at, updated_at
                FROM provider_accounts
                WHERE id = :provider_account_id AND project_id = :project_id
                """
            ),
            {"provider_account_id": provider_account_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ProviderAccountNotFoundError("Provider account not found.")
        return self._serialize_provider_account(row)

    def _get_provider_account_by_label(
        self,
        connection: Connection,
        project_id: str,
        provider: str,
        account_label: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider, account_label, auth_method,
                       credential_ref, credential_refs, connection_metadata, scopes,
                       status, owner_actor, credential_version, credential_rotated_at,
                       disabled_at, disabled_by, revoked_at, revoked_by,
                       read_only, deploy_capable, billing_readable,
                       destructive_actions_disabled, capabilities, missing_permissions,
                       last_test_status, last_tested_at, last_error, created_by,
                       created_at, updated_at
                FROM provider_accounts
                WHERE project_id = :project_id AND provider = :provider
                  AND account_label = :account_label
                """
            ),
            {
                "project_id": project_id,
                "provider": provider,
                "account_label": account_label,
            },
        ).mappings().one()
        return dict(row)

    def _get_changeset_any_project(
        self, connection: Connection, changeset_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, provider_account_id, title, summary, status,
                       risk_tier, operations, expected_cost_usd_micros, cost_quote,
                       reservation_id, approval_decision_id, rollback_plan,
                       expected_receipts, policy_result, created_by, approved_by,
                       approved_at, applied_by, applied_at, failure_reason,
                       created_at, updated_at
                FROM changesets
                WHERE id = :changeset_id
                """
            ),
            {"changeset_id": changeset_id},
        ).mappings().first()
        if not row:
            raise ChangeSetNotFoundError("ChangeSet not found.")
        return dict(row)

    def _ensure_secret_ref(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider: str,
        credential_ref: str,
        secret_kind: str,
        actor: str,
    ) -> dict[str, Any]:
        ref_kind, ref_key = self.secret_refs.parse_credential_ref(credential_ref)
        existing = connection.execute(
            text(
                """
                SELECT id, provider, status
                FROM secret_refs
                WHERE project_id = :project_id
                  AND ref_kind = :ref_kind
                  AND ref_key = :ref_key
                """
            ),
            {
                "project_id": project_id,
                "ref_kind": ref_kind,
                "ref_key": ref_key,
            },
        ).mappings().first()
        if existing:
            if existing["provider"] not in {None, provider}:
                raise ValueError(
                    f"SecretRef {credential_ref!r} already belongs to provider "
                    f"{existing['provider']}."
                )
            if existing["status"] in {"disabled", "rotating", "revoked"}:
                raise ValueError(
                    f"SecretRef {credential_ref!r} is {existing['status']} and "
                    "cannot be silently reactivated."
                )
            return self.secret_refs.get_secret_ref(
                connection, project_id, existing["id"]
            )
        secret_ref_id = f"secret_{project_id}_{provider}_{ref_kind}_{ref_key}".replace(
            "-",
            "_",
        )
        connection.execute(
            text(
                """
                INSERT INTO secret_refs (
                    id, project_id, scope_type, scope_id, ref_kind, ref_key,
                    provider, secret_kind, storage_backend, status, metadata, created_by
                )
                VALUES (
                    :id, :project_id, 'project', :project_id, :ref_kind, :ref_key,
                    :provider, :secret_kind, :storage_backend, 'active',
                    CAST(:metadata AS JSONB), :actor
                )
                """
            ),
            {
                "id": secret_ref_id[:96],
                "project_id": project_id,
                "ref_kind": ref_kind,
                "ref_key": ref_key,
                "provider": provider,
                "secret_kind": secret_kind,
                "storage_backend": self.secret_refs._storage_backend_for_ref_kind(
                    ref_kind
                ),
                "metadata": json.dumps({"raw_secret_stored": False}),
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "secret_ref.created",
            actor,
            "secret_ref",
            secret_ref_id[:96],
            {
                "provider": provider,
                "ref_kind": ref_kind,
                "ref_key": ref_key,
                "secret_kind": secret_kind,
            },
        )
        return self.secret_refs.get_secret_ref(
            connection, project_id, secret_ref_id[:96]
        )

    def _upsert_capabilities(
        self,
        connection: Connection,
        project_id: str,
        provider_account_id: str,
        capabilities: list[str],
    ) -> None:
        for capability in capabilities:
            connection.execute(
                text(
                    """
                    INSERT INTO capabilities (
                        id, project_id, provider_account_id, capability_key, status
                    )
                    VALUES (
                        :id, :project_id, :provider_account_id, :capability_key, 'available'
                    )
                    ON CONFLICT (provider_account_id, capability_key) DO UPDATE
                    SET status = 'available',
                        detected_at = now()
                    """
                ),
                {
                    "id": make_id("cap"),
                    "project_id": project_id,
                    "provider_account_id": provider_account_id,
                    "capability_key": capability,
                },
            )

    def _list_janitor_candidate_resources(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = [
            "project_id = :project_id",
            """
            (
              ownership = 'orphaned'
              OR metadata->>'ttl_expired' = 'true'
              OR metadata->>'janitor_candidate' = 'true'
            )
            """,
        ]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if provider_account_id:
            clauses.append("provider_account_id = :provider_account_id")
            params["provider_account_id"] = provider_account_id
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, provider_account_id, provider, resource_type,
                       external_id, name, state, ownership, environment, owner_role,
                       task_id, deployment_target_id, receipt_id, budget_allocation_id,
                       health_status, estimated_monthly_usd_micros,
                       actual_monthly_usd_micros, cost_confidence, last_seen_at,
                       tags, metadata, created_at, updated_at
                FROM external_resources
                WHERE {' AND '.join(clauses)}
                ORDER BY
                  CASE WHEN environment = 'production' THEN 1 ELSE 0 END ASC,
                  COALESCE(actual_monthly_usd_micros, estimated_monthly_usd_micros, 0) DESC,
                  last_seen_at ASC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [dict(row) for row in rows]

    def _cleanup_operation_for_resource(
        self,
        resource: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        provider = resource["provider"]
        resource_type = resource["resource_type"]
        if provider == "digitalocean" and resource_type == "droplet":
            return {
                "skill_id": "digitalocean.droplet.destroy",
                "action": "destroy",
                "resource_type": "droplet",
                "risk_tier": 3,
                "destructive": True,
                "production_affecting": False,
                "desired_state": {"state": "deleted"},
            }
        if provider == "supabase" and resource_type in {"project", "database"}:
            return {
                "skill_id": "supabase.project.pause",
                "action": "pause",
                "resource_type": resource_type,
                "risk_tier": 2,
                "destructive": False,
                "production_affecting": False,
                "desired_state": {"state": "paused"},
            }
        return None

    def _resource_monthly_savings(self, resource: dict[str, Any]) -> int:
        for key in ("actual_monthly_usd_micros", "estimated_monthly_usd_micros"):
            value = resource.get(key)
            if isinstance(value, int) and value > 0:
                return value
        metadata = resource.get("metadata") or {}
        value = metadata.get("estimated_monthly_usd_micros")
        return int(value) if isinstance(value, int) and value > 0 else 0

    def _janitor_reason(self, resource: dict[str, Any]) -> str:
        metadata = resource.get("metadata") or {}
        if resource["ownership"] == "orphaned":
            return "orphaned_resource"
        if metadata.get("ttl_expired") is True or metadata.get("ttl_expired") == "true":
            return "ttl_expired"
        if metadata.get("janitor_candidate") is True or metadata.get("janitor_candidate") == "true":
            return "explicit_janitor_candidate"
        return "resource_waste_candidate"

    def _has_open_cleanup_changeset(
        self,
        connection: Connection,
        *,
        project_id: str,
        resource: dict[str, Any],
        operation: dict[str, Any],
    ) -> bool:
        row = connection.execute(
            text(
                """
                SELECT 1
                FROM changeset_operations co
                JOIN changesets c ON c.id = co.changeset_id
                WHERE co.project_id = :project_id
                  AND co.skill_id = :skill_id
                  AND co.resource_type = :resource_type
                  AND co.resource_ref = :resource_ref
                  AND c.status IN (
                    'draft', 'quoted', 'policy_checked', 'budget_reserved',
                    'pending_approval', 'approved', 'applying'
                  )
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "skill_id": operation["skill_id"],
                "resource_type": operation["resource_type"],
                "resource_ref": resource["external_id"],
            },
        ).first()
        return row is not None

    def _record_resource_action_attempt(
        self,
        connection: Connection,
        *,
        changeset: dict[str, Any],
        operation: dict[str, Any],
        actor: str,
        created_by_job_id: Optional[str],
    ) -> dict[str, Any]:
        project_id = changeset["project_id"]
        account = (
            self.get_provider_account(
                connection,
                project_id,
                operation["provider_account_id"],
            )
            if operation["provider_account_id"]
            else None
        )
        provider = account["provider"] if account else self._provider_from_skill(operation["skill_id"])
        dedupe_key = self._operation_dedupe_key(
            changeset["id"],
            int(operation["operation_index"]),
            dict(operation),
        )
        existing = connection.execute(
            text(
                """
                SELECT id, project_id, changeset_id, operation_id, provider_account_id,
                       receipt_id, dedupe_key, provider, action, resource_type,
                       resource_ref, status, provider_ref, request_hash, result,
                       error, created_by_job_id, created_at, updated_at
                FROM resource_receipts
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": dedupe_key},
        ).mappings().first()
        if existing:
            return self._serialize_resource_receipt(existing)

        scope = self._scope(connection, project_id)
        effect_scope = {
            "project_id": project_id,
            "changeset_id": changeset["id"],
            "operation_id": operation["id"],
            "provider_account_id": operation["provider_account_id"],
            "provider": provider,
            "resource_type": operation["resource_type"],
            "resource_ref": operation["resource_ref"],
        }
        payload = {
            "skill_id": operation["skill_id"],
            "action": operation["action"],
            "desired_state": operation.get("desired_state") or {},
            "actor": actor,
        }
        receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            operation["skill_id"],
            effect_scope,
            payload,
            created_by_job_id=created_by_job_id,
            data_boundary="internal",
        )
        resource_receipt_id = make_id("rrcpt")
        status = "succeeded"
        provider_ref: Optional[str] = None
        result: Optional[dict[str, Any]] = None
        error: Optional[dict[str, Any]] = None
        try:
            result = self._execute_provider_operation(
                connection,
                account,
                dict(operation),
            )
            provider_ref = result.get("provider_ref")
        except Exception as exc:
            status = "failed"
            error = {
                "message": str(exc),
                "skill_id": operation["skill_id"],
                "error_type": exc.__class__.__name__,
            }
        connection.execute(
            text(
                """
                INSERT INTO resource_receipts (
                    id, project_id, changeset_id, operation_id, provider_account_id,
                    receipt_id, dedupe_key, provider, action, resource_type,
                    resource_ref, status, provider_ref, request_hash, result, error,
                    created_by_job_id
                )
                VALUES (
                    :id, :project_id, :changeset_id, :operation_id,
                    :provider_account_id, :receipt_id, :dedupe_key, :provider,
                    :action, :resource_type, :resource_ref, :status,
                    :provider_ref, :request_hash, CAST(:result AS JSONB),
                    CAST(:error AS JSONB), :created_by_job_id
                )
                """
            ),
            {
                "id": resource_receipt_id,
                "project_id": project_id,
                "changeset_id": changeset["id"],
                "operation_id": operation["id"],
                "provider_account_id": operation["provider_account_id"],
                "receipt_id": receipt["id"],
                "dedupe_key": dedupe_key,
                "provider": provider,
                "action": operation["action"],
                "resource_type": operation["resource_type"],
                "resource_ref": operation["resource_ref"],
                "status": status,
                "provider_ref": provider_ref,
                "request_hash": json_hash(payload),
                "result": json.dumps(result),
                "error": json.dumps(error),
                "created_by_job_id": created_by_job_id,
            },
        )
        if status == "succeeded":
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                provider_ref,
                result or {},
            )
            self._persist_managed_resource_from_result(
                connection,
                project_id=project_id,
                provider_account_id=operation["provider_account_id"],
                operation=dict(operation),
                resource_receipt_id=resource_receipt_id,
                receipt_id=receipt["id"],
                result=result or {},
            )
            if account:
                self._record_provider_health(
                    connection,
                    project_id=project_id,
                    provider_account_id=account["id"],
                    provider=provider,
                    status="healthy",
                    api_available=True,
                    detail={
                        "operation": operation["skill_id"],
                        "changeset_id": changeset["id"],
                    },
                )
        else:
            self.receipts.mark_failed(connection, receipt["id"], error or {})
            if account:
                self._record_provider_health(
                    connection,
                    project_id=project_id,
                    provider_account_id=account["id"],
                    provider=provider,
                    status="degraded",
                    api_available=False,
                    detail={
                        "operation": operation["skill_id"],
                        "changeset_id": changeset["id"],
                        "error": error,
                    },
                )
        connection.execute(
            text(
                """
                UPDATE changeset_operations
                SET status = :status,
                    resource_receipt_id = :resource_receipt_id,
                    updated_at = now()
                WHERE id = :operation_id
                """
            ),
            {
                "status": status,
                "resource_receipt_id": resource_receipt_id,
                "operation_id": operation["id"],
            },
        )
        emit_event(
            connection,
            project_id,
            "resource_receipt.succeeded"
            if status == "succeeded"
            else "resource_receipt.failed",
            actor,
            "resource_receipt",
            resource_receipt_id,
            {
                "changeset_id": changeset["id"],
                "operation_id": operation["id"],
                "dedupe_key": dedupe_key,
                "error": error,
                "provider_ref": provider_ref,
            },
        )
        return self._serialize_resource_receipt(
            connection.execute(
                text(
                    """
                    SELECT id, project_id, changeset_id, operation_id, provider_account_id,
                           receipt_id, dedupe_key, provider, action, resource_type,
                           resource_ref, status, provider_ref, request_hash, result,
                           error, created_by_job_id, created_at, updated_at
                    FROM resource_receipts
                    WHERE id = :resource_receipt_id
                    """
                ),
                {"resource_receipt_id": resource_receipt_id},
            ).mappings().one()
        )

    def _execute_provider_operation(
        self,
        connection: Connection,
        account: Optional[dict[str, Any]],
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        skill_id = operation["skill_id"]
        provider = self._provider_from_skill(skill_id)
        if not account:
            raise ProviderAdapterError("Provider write operation requires a ProviderAccount.")
        if account["provider"] != provider:
            raise ProviderAdapterError(
                f"ProviderAccount is for {account['provider']}, not {provider}."
            )
        self._require_account_secret_refs_usable(connection, account)
        if account["read_only"] and operation["action"] not in {"read", "sync"}:
            raise ProviderAdapterError("Provider account is read-only; provider write blocked.")
        if operation["destructive"] and account["destructive_actions_disabled"]:
            raise ProviderAdapterError("Provider account blocks destructive provider actions.")

        credential = self._credential_value(account["credential_ref"])
        if skill_id == "cloudflare.dns.record.upsert":
            return self._cloudflare_dns_record_upsert(credential, operation)
        if skill_id == "cloudflare.worker.deploy":
            return self._cloudflare_worker_deploy(connection, credential, operation)
        if skill_id == "cloudflare.pages.deploy":
            return self._cloudflare_pages_deploy(credential, operation)
        if skill_id == "cloudflare.r2.bucket.create":
            return self._cloudflare_r2_bucket_create(credential, operation)
        if skill_id == "digitalocean.droplet.create":
            return self._digitalocean_droplet_create(operation)
        if skill_id == "digitalocean.droplet.destroy":
            return self._digitalocean_droplet_destroy(operation)
        if skill_id == "digitalocean.app.deploy":
            return self._digitalocean_app_deploy(operation)
        if skill_id == "supabase.migration.apply":
            return self._supabase_migration_apply(connection, credential, operation)
        if skill_id == "supabase.project.pause":
            return self._supabase_project_pause(credential, operation)
        if skill_id == "supabase.project.create":
            return self._supabase_project_create(credential, operation)
        if skill_id == "ssh_host.workload.deploy":
            return self._ssh_host_workload_deploy(operation)
        if skill_id == "ssh_host.workload.teardown":
            return self._ssh_host_workload_teardown(operation)
        if skill_id == "ssh_host.workload.health_check":
            return self._ssh_host_workload_health_check(operation)
        if skill_id == "ssh_host.job.run":
            return self._ssh_host_job_run(operation)
        if skill_id == "aws.ec2.instance.create":
            return self._aws_ec2_instance_create(credential, operation)
        if skill_id == "aws.ec2.instance.destroy":
            return self._aws_ec2_instance_destroy(credential, operation)
        if skill_id == "aws.s3.bucket.create":
            return self._aws_s3_bucket_create(credential, operation)
        if skill_id == "aws.rds.instance.create":
            return self._aws_rds_instance_create(credential, operation)
        if skill_id == "aws.iam.role.upsert":
            return self._aws_iam_role_upsert(credential, operation)
        if skill_id == "aws.route53.record.upsert":
            return self._aws_route53_record_upsert(credential, operation)
        if skill_id == "aws.bedrock.converse":
            return self._aws_bedrock_converse(credential, operation)
        if skill_id == "aws.bedrock.list_models":
            return self._aws_bedrock_list_models(credential, operation)
        if skill_id == "azure.resource_group.ensure":
            return self._azure_resource_group_ensure(credential, operation)
        if skill_id == "azure.vm.create":
            return self._azure_vm_create(credential, operation)
        if skill_id == "azure.vm.destroy":
            return self._azure_vm_destroy(credential, operation)
        if skill_id == "azure.openai.chat":
            return self._azure_openai_chat(credential, operation)
        if skill_id == "ovh.cloud.instance.create":
            return self._ovh_cloud_instance_create(credential, operation)
        if skill_id == "ovh.cloud.instance.destroy":
            return self._ovh_cloud_instance_destroy(credential, operation)
        if skill_id == "ovh.dedicated.server.list":
            return self._ovh_dedicated_server_list(credential, operation)
        if skill_id == "digitalocean.firewall.ensure":
            return self._digitalocean_firewall_ensure(operation)
        if skill_id == "digitalocean.floating_ip.create":
            return self._digitalocean_floating_ip_create(operation)
        if skill_id == "digitalocean.inference.chat":
            return self._digitalocean_inference_chat(operation)
        raise ProviderAdapterError(
            "Provider write adapter execution is not enabled for this skill. "
            "The action was recorded and deadlettered without calling the provider."
        )

    def _cloudflare_dns_record_upsert(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        zone_id = self._require_text(str(desired.get("zone_id") or ""), "zone_id")
        record_type = self._require_text(
            str(desired.get("record_type") or desired.get("type") or ""),
            "record_type",
        ).upper()
        name = self._require_text(str(desired.get("name") or ""), "name")
        content = self._require_text(
            str(desired.get("content") or desired.get("value") or ""),
            "content",
        )
        payload: dict[str, Any] = {
            "type": record_type,
            "name": name,
            "content": content,
            "ttl": int(desired.get("ttl") or 1),
        }
        if "proxied" in desired:
            payload["proxied"] = bool(desired["proxied"])
        if desired.get("comment"):
            payload["comment"] = str(desired["comment"])
        if desired.get("priority") is not None:
            payload["priority"] = int(desired["priority"])

        headers = self._provider_headers("cloudflare", credential)
        base_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
        with httpx.Client(timeout=20) as client:
            existing_response = client.get(
                base_url,
                headers=headers,
                params={"type": record_type, "name": name},
            )
            existing_response.raise_for_status()
            existing_payload = existing_response.json()
            existing_records = (
                existing_payload.get("result")
                if isinstance(existing_payload, dict)
                else []
            ) or []
            existing = existing_records[0] if existing_records else None
            if existing:
                response = client.put(
                    f"{base_url}/{existing['id']}",
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                )
                provider_operation = "updated"
            else:
                response = client.post(
                    base_url,
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                )
                provider_operation = "created"
            response.raise_for_status()
            response_payload = response.json()

        result = response_payload.get("result") if isinstance(response_payload, dict) else {}
        if not isinstance(result, dict) or not result.get("id"):
            raise ProviderAdapterError("Cloudflare DNS upsert response did not include a record id.")
        return {
            "provider_ref": f"cloudflare:dns_record:{result['id']}",
            "resource_external_id": str(result["id"]),
            "resource_name": str(result.get("name") or name),
            "resource_type": "dns_record",
            "state": "active",
            "health_status": "active",
            "operation": provider_operation,
            "zone_id": zone_id,
            "record": {
                "id": result.get("id"),
                "type": result.get("type") or record_type,
                "name": result.get("name") or name,
                "content": result.get("content") or content,
                "ttl": result.get("ttl") or payload["ttl"],
                "proxied": result.get("proxied", payload.get("proxied")),
            },
            "api_success": response_payload.get("success")
            if isinstance(response_payload, dict)
            else None,
        }

    def _cloudflare_worker_deploy(
        self,
        connection: Connection,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        descriptor = self._deployment_resource_descriptor(
            desired,
            {"cloudflare_worker", "worker", "worker_script"},
        )
        account_id = self._require_text(
            str(descriptor.get("account_id") or desired.get("account_id") or ""),
            "account_id",
        )
        script_name = self._require_text(
            str(
                descriptor.get("script_name")
                or desired.get("script_name")
                or desired.get("target_ref")
                or operation.get("resource_ref")
                or ""
            ),
            "script_name",
        )
        main_module = self._require_text(
            str(descriptor.get("main_module") or desired.get("main_module") or "worker.js"),
            "main_module",
        )
        script_content = self._deployment_text_payload(
            connection,
            project_id=operation["project_id"],
            descriptor=descriptor,
            direct_fields=("script_content", "worker_script", "content"),
            artifact_fields=("script_artifact_id", "artifact_id"),
            preferred_file=main_module,
            payload_label="Cloudflare Worker script",
        )
        compatibility_date = str(
            descriptor.get("compatibility_date")
            or desired.get("compatibility_date")
            or "2026-06-03"
        )
        metadata: dict[str, Any] = {
            "main_module": main_module,
            "compatibility_date": compatibility_date,
        }
        compatibility_flags = descriptor.get("compatibility_flags") or desired.get(
            "compatibility_flags"
        )
        if isinstance(compatibility_flags, list):
            metadata["compatibility_flags"] = [
                str(flag) for flag in compatibility_flags if str(flag).strip()
            ]
        bindings = descriptor.get("bindings") or desired.get("bindings")
        if isinstance(bindings, list):
            metadata["bindings"] = bindings

        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/workers/scripts/{script_name}"
        )
        headers = self._provider_headers("cloudflare", credential)
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            main_module: (
                main_module,
                script_content,
                "application/javascript+module",
            ),
        }
        with httpx.Client(timeout=30) as client:
            response = client.put(url, headers=headers, files=files)
            response.raise_for_status()
            response_payload = response.json()

        result = response_payload.get("result") if isinstance(response_payload, dict) else {}
        if not isinstance(result, dict):
            result = {}
        script_id = str(result.get("id") or script_name)
        etag = result.get("etag")
        return {
            "provider_ref": f"cloudflare:worker:{script_id}",
            "resource_external_id": script_id,
            "resource_name": script_name,
            "resource_type": "worker",
            "state": "active",
            "health_status": "deployed",
            "operation": "deployed",
            "account_id": account_id,
            "script_name": script_name,
            "main_module": main_module,
            "compatibility_date": compatibility_date,
            "etag": etag,
            "api_success": response_payload.get("success")
            if isinstance(response_payload, dict)
            else None,
        }

    def _cloudflare_pages_deploy(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        descriptor = self._deployment_resource_descriptor(
            desired,
            {"cloudflare_pages", "pages", "pages_project"},
        )
        if descriptor.get("direct_upload") is True or descriptor.get("manifest"):
            raise ProviderAdapterError(
                "Cloudflare Pages direct-upload deployment requires an asset upload "
                "pipeline and is not enabled in this runtime slice."
            )
        account_id = self._require_text(
            str(descriptor.get("account_id") or desired.get("account_id") or ""),
            "account_id",
        )
        project_name = self._require_text(
            str(
                descriptor.get("project_name")
                or desired.get("project_name")
                or desired.get("target_ref")
                or operation.get("resource_ref")
                or ""
            ),
            "project_name",
        )
        form_fields: dict[str, tuple[None, str]] = {}
        branch = descriptor.get("branch") or desired.get("branch")
        if isinstance(branch, str) and branch.strip():
            form_fields["branch"] = (None, branch.strip())
        commit_hash = descriptor.get("commit_hash") or desired.get("commit_ref")
        if isinstance(commit_hash, str) and commit_hash.strip():
            form_fields["commit_hash"] = (None, commit_hash.strip())
        commit_message = descriptor.get("commit_message") or desired.get(
            "commit_message"
        )
        if isinstance(commit_message, str) and commit_message.strip():
            form_fields["commit_message"] = (None, commit_message.strip())
        commit_dirty = descriptor.get("commit_dirty")
        if commit_dirty is not None:
            form_fields["commit_dirty"] = (
                None,
                "true" if bool(commit_dirty) else "false",
            )
        build_output_dir = descriptor.get("pages_build_output_dir")
        if isinstance(build_output_dir, str) and build_output_dir.strip():
            form_fields["pages_build_output_dir"] = (None, build_output_dir.strip())

        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/pages/projects/{project_name}/deployments"
        )
        headers = self._provider_headers("cloudflare", credential)
        with httpx.Client(timeout=30) as client:
            response = client.post(url, headers=headers, files=form_fields)
            response.raise_for_status()
            response_payload = response.json()

        result = response_payload.get("result") if isinstance(response_payload, dict) else {}
        if not isinstance(result, dict):
            result = {}
        deployment_id = self._require_text(
            str(result.get("id") or ""),
            "deployment_id",
        )
        latest_stage = result.get("latest_stage") if isinstance(result, dict) else {}
        stage_status = (
            latest_stage.get("status")
            if isinstance(latest_stage, dict)
            else result.get("environment")
        )
        return {
            "provider_ref": f"cloudflare:pages_deployment:{deployment_id}",
            "resource_external_id": deployment_id,
            "resource_name": str(result.get("project_name") or project_name),
            "resource_type": "pages_deployment",
            "state": "active",
            "health_status": str(stage_status or "queued"),
            "operation": "deployed",
            "account_id": account_id,
            "project_name": project_name,
            "deployment_id": deployment_id,
            "deployment_url": result.get("url"),
            "environment": result.get("environment"),
            "api_success": response_payload.get("success")
            if isinstance(response_payload, dict)
            else None,
        }

    def _cloudflare_r2_bucket_create(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        account_id = self._require_text(str(desired.get("account_id") or ""), "account_id")
        bucket_name = self._require_text(
            str(
                desired.get("bucket_name")
                or desired.get("name")
                or operation.get("resource_ref")
                or ""
            ),
            "bucket_name",
        )
        payload: dict[str, Any] = {"name": bucket_name}
        location_hint = desired.get("locationHint") or desired.get("location")
        if isinstance(location_hint, str) and location_hint.strip():
            payload["locationHint"] = location_hint.strip()
        storage_class = desired.get("storageClass") or desired.get("storage_class")
        if isinstance(storage_class, str) and storage_class.strip():
            payload["storageClass"] = storage_class.strip()

        headers = {
            **self._provider_headers("cloudflare", credential),
            "Content-Type": "application/json",
        }
        jurisdiction = desired.get("jurisdiction")
        if isinstance(jurisdiction, str) and jurisdiction.strip():
            headers["cf-r2-jurisdiction"] = jurisdiction.strip()

        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets"
        with httpx.Client(timeout=30) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            response_payload = response.json()

        result = response_payload.get("result") if isinstance(response_payload, dict) else {}
        if not isinstance(result, dict):
            result = {}
        result_name = str(result.get("name") or bucket_name)
        return {
            "provider_ref": f"cloudflare:r2_bucket:{result_name}",
            "resource_external_id": result_name,
            "resource_name": result_name,
            "resource_type": "r2_bucket",
            "state": "active",
            "health_status": "active",
            "operation": "created",
            "account_id": account_id,
            "bucket_name": result_name,
            "location": result.get("location") or payload.get("locationHint"),
            "jurisdiction": result.get("jurisdiction") or jurisdiction,
            "storage_class": result.get("storage_class") or payload.get("storageClass"),
            "api_success": response_payload.get("success")
            if isinstance(response_payload, dict)
            else None,
        }

    def _aws_ec2_instance_create(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        image_id = self._require_text(
            str(desired.get("image_id") or desired.get("ami") or ""),
            "image_id",
        )
        instance_type = self._require_text(
            str(desired.get("instance_type") or ""),
            "instance_type",
        )
        name = self._require_text(
            str(
                desired.get("name")
                or operation.get("resource_ref")
                or f"boss-providerops-{operation['id'][-8:]}"
            ),
            "name",
        )
        aws_tags = [
            {"Key": "Name", "Value": name},
            {"Key": "boss-managed", "Value": "true"},
            {"Key": "boss-providerops", "Value": "true"},
        ]
        reserved_keys = {tag["Key"] for tag in aws_tags}
        for tag in desired.get("tags") or []:
            if not isinstance(tag, str) or not tag.strip():
                continue
            key, _, value = tag.strip().partition(":")
            if key and key not in reserved_keys:
                aws_tags.append({"Key": key, "Value": value})
                reserved_keys.add(key)
        params: dict[str, Any] = {
            "ImageId": image_id,
            "InstanceType": instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": aws_tags}
            ],
        }
        subnet_id = desired.get("subnet_id")
        if isinstance(subnet_id, str) and subnet_id.strip():
            params["SubnetId"] = subnet_id.strip()
        key_name = desired.get("key_name")
        if isinstance(key_name, str) and key_name.strip():
            params["KeyName"] = key_name.strip()
        security_group_ids = desired.get("security_group_ids")
        if isinstance(security_group_ids, list):
            cleaned_groups = [
                str(group).strip()
                for group in security_group_ids
                if str(group).strip()
            ]
            if cleaned_groups:
                params["SecurityGroupIds"] = cleaned_groups

        ec2 = self._aws_client("ec2", credential)
        response = ec2.run_instances(**params)
        instances = response.get("Instances") or []
        instance = instances[0] if instances else {}
        if not isinstance(instance, dict) or not instance.get("InstanceId"):
            raise ProviderAdapterError(
                "AWS run_instances response did not include an instance id."
            )
        instance_id = str(instance["InstanceId"])
        status = str((instance.get("State") or {}).get("Name") or "pending")
        return {
            "provider_ref": f"aws:ec2_instance:{instance_id}",
            "resource_external_id": instance_id,
            "resource_name": name,
            "resource_type": "ec2_instance",
            "state": self._resource_state_from_provider_status(status),
            "health_status": status,
            "operation": "created",
            "region": self.settings.aws_region,
            "instance_type": instance_type,
            "image_id": image_id,
            "availability_zone": (instance.get("Placement") or {}).get(
                "AvailabilityZone"
            ),
        }

    def _aws_ec2_instance_destroy(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        instance_id = self._require_text(
            str(
                desired.get("instance_id")
                or desired.get("external_id")
                or operation.get("resource_ref")
                or ""
            ),
            "instance_id",
        )
        ec2 = self._aws_client("ec2", credential)
        response = ec2.terminate_instances(InstanceIds=[instance_id])
        terminating = response.get("TerminatingInstances") or []
        current = terminating[0] if terminating else {}
        status = str(
            ((current.get("CurrentState") or {}).get("Name") if isinstance(current, dict) else None)
            or "shutting-down"
        )
        return {
            "provider_ref": f"aws:ec2_instance:{instance_id}",
            "resource_external_id": instance_id,
            "resource_name": str(desired.get("name") or instance_id),
            "resource_type": "ec2_instance",
            "state": "deleted",
            "health_status": status,
            "operation": "destroyed",
            "region": self.settings.aws_region,
        }

    def _aws_s3_bucket_create(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        bucket_name = self._require_text(
            str(
                desired.get("bucket_name")
                or desired.get("name")
                or operation.get("resource_ref")
                or ""
            ),
            "bucket_name",
        )
        region = self.settings.aws_region
        params: dict[str, Any] = {"Bucket": bucket_name}
        if region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": region}

        s3 = self._aws_client("s3", credential)
        s3.create_bucket(**params)
        versioning_enabled = bool(desired.get("versioning"))
        if versioning_enabled:
            s3.put_bucket_versioning(
                Bucket=bucket_name,
                VersioningConfiguration={"Status": "Enabled"},
            )
        encryption = desired.get("encryption")
        sse_algorithm: Optional[str] = None
        if encryption:
            sse_algorithm = (
                "aws:kms"
                if str(encryption).lower() in {"kms", "aws:kms"}
                else "AES256"
            )
            s3.put_bucket_encryption(
                Bucket=bucket_name,
                ServerSideEncryptionConfiguration={
                    "Rules": [
                        {
                            "ApplyServerSideEncryptionByDefault": {
                                "SSEAlgorithm": sse_algorithm
                            }
                        }
                    ]
                },
            )
        return {
            "provider_ref": f"aws:s3_bucket:{bucket_name}",
            "resource_external_id": bucket_name,
            "resource_name": bucket_name,
            "resource_type": "s3_bucket",
            "state": "active",
            "health_status": "active",
            "operation": "created",
            "region": region,
            "versioning_enabled": versioning_enabled,
            "sse_algorithm": sse_algorithm,
        }

    def _aws_rds_instance_create(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        for literal_key in ("master_password", "master_user_password", "password"):
            if desired.get(literal_key):
                raise ProviderAdapterError(
                    "aws.rds.instance.create does not accept a literal master "
                    "password in desired_state; provide "
                    "master_password_secret_ref as an env SecretRef instead."
                )
        db_identifier = self._require_text(
            str(
                desired.get("db_instance_identifier")
                or desired.get("name")
                or operation.get("resource_ref")
                or ""
            ),
            "db_instance_identifier",
        )
        engine = self._require_text(str(desired.get("engine") or ""), "engine")
        instance_class = self._require_text(
            str(
                desired.get("db_instance_class")
                or desired.get("instance_class")
                or ""
            ),
            "db_instance_class",
        )
        allocated_storage = int(
            desired.get("allocated_storage_gb")
            or desired.get("allocated_storage")
            or 0
        )
        if allocated_storage <= 0:
            raise ValueError("allocated_storage_gb must be a positive integer.")
        master_username = self._require_text(
            str(desired.get("master_username") or ""),
            "master_username",
        )
        secret_ref = self._require_text(
            str(desired.get("master_password_secret_ref") or ""),
            "master_password_secret_ref",
        )
        master_password = self._env_secret_value(secret_ref)

        params: dict[str, Any] = {
            "DBInstanceIdentifier": db_identifier,
            "Engine": engine,
            "DBInstanceClass": instance_class,
            "AllocatedStorage": allocated_storage,
            "MasterUsername": master_username,
            "MasterUserPassword": master_password,
        }
        db_name = desired.get("db_name")
        if isinstance(db_name, str) and db_name.strip():
            params["DBName"] = db_name.strip()
        if desired.get("multi_az") is not None:
            params["MultiAZ"] = bool(desired["multi_az"])
        if desired.get("publicly_accessible") is not None:
            params["PubliclyAccessible"] = bool(desired["publicly_accessible"])
        subnet_group = desired.get("db_subnet_group_name")
        if isinstance(subnet_group, str) and subnet_group.strip():
            params["DBSubnetGroupName"] = subnet_group.strip()
        security_group_ids = desired.get("vpc_security_group_ids")
        if isinstance(security_group_ids, list):
            cleaned_groups = [
                str(group).strip()
                for group in security_group_ids
                if str(group).strip()
            ]
            if cleaned_groups:
                params["VpcSecurityGroupIds"] = cleaned_groups

        rds = self._aws_client("rds", credential)
        response = rds.create_db_instance(**params)
        db_instance = response.get("DBInstance") or {}
        status = str(db_instance.get("DBInstanceStatus") or "creating")
        return {
            "provider_ref": f"aws:rds_instance:{db_identifier}",
            "resource_external_id": str(
                db_instance.get("DBInstanceIdentifier") or db_identifier
            ),
            "resource_name": db_identifier,
            "resource_type": "rds_instance",
            "state": "active"
            if status == "available"
            else self._resource_state_from_provider_status(status),
            "health_status": status,
            "operation": "created",
            "region": self.settings.aws_region,
            "engine": engine,
            "db_instance_class": instance_class,
            "allocated_storage_gb": allocated_storage,
        }

    def _aws_iam_role_upsert(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        role_name = self._require_text(
            str(
                desired.get("role_name")
                or desired.get("name")
                or operation.get("resource_ref")
                or ""
            ),
            "role_name",
        )
        assume_document = (
            desired.get("assume_role_policy_document")
            or desired.get("assume_role_policy")
        )
        if isinstance(assume_document, dict):
            assume_document_text = json.dumps(assume_document)
        else:
            assume_document_text = self._require_text(
                str(assume_document or ""),
                "assume_role_policy_document",
            )

        iam = self._aws_client("iam", credential)
        existing: Optional[dict[str, Any]]
        try:
            existing = iam.get_role(RoleName=role_name)
        except Exception as error:
            if self._aws_error_code(error) != "NoSuchEntity":
                raise
            existing = None
        if existing:
            iam.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=assume_document_text,
            )
            role = existing.get("Role") or {}
            provider_operation = "updated"
        else:
            create_params: dict[str, Any] = {
                "RoleName": role_name,
                "AssumeRolePolicyDocument": assume_document_text,
            }
            description = desired.get("description")
            if isinstance(description, str) and description.strip():
                create_params["Description"] = description.strip()
            response = iam.create_role(**create_params)
            role = response.get("Role") or {}
            provider_operation = "created"

        attached_policy_arns: list[str] = []
        for policy_arn in (
            desired.get("attach_policy_arns")
            or desired.get("policy_arns")
            or []
        ):
            if isinstance(policy_arn, str) and policy_arn.strip():
                iam.attach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy_arn.strip(),
                )
                attached_policy_arns.append(policy_arn.strip())
        return {
            "provider_ref": f"aws:iam_role:{role_name}",
            "resource_external_id": str(role.get("RoleId") or role_name),
            "resource_name": role_name,
            "resource_type": "iam_role",
            "state": "active",
            "health_status": "active",
            "operation": provider_operation,
            "role_arn": role.get("Arn"),
            "attached_policy_arns": attached_policy_arns,
        }

    def _aws_route53_record_upsert(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        hosted_zone_id = self._require_text(
            str(desired.get("hosted_zone_id") or desired.get("zone_id") or ""),
            "hosted_zone_id",
        )
        record_type = self._require_text(
            str(desired.get("record_type") or desired.get("type") or ""),
            "record_type",
        ).upper()
        name = self._require_text(str(desired.get("name") or ""), "name")
        raw_values = desired.get("values")
        if not isinstance(raw_values, list):
            raw_values = [desired.get("content") or desired.get("value")]
        values = [str(value).strip() for value in raw_values if value and str(value).strip()]
        if not values:
            raise ValueError("values (or content) must not be blank.")
        ttl = int(desired.get("ttl") or 300)

        route53 = self._aws_client("route53", credential)
        response = route53.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                "Comment": "boss-managed route53 upsert",
                "Changes": [
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": name,
                            "Type": record_type,
                            "TTL": ttl,
                            "ResourceRecords": [
                                {"Value": value} for value in values
                            ],
                        },
                    }
                ],
            },
        )
        change_info = response.get("ChangeInfo") or {}
        external_id = f"{hosted_zone_id}:{record_type}:{name}"
        return {
            "provider_ref": f"aws:route53_record:{external_id}",
            "resource_external_id": external_id,
            "resource_name": name,
            "resource_type": "route53_record",
            "state": "active",
            "health_status": str(change_info.get("Status") or "PENDING").lower(),
            "operation": "updated",
            "hosted_zone_id": hosted_zone_id,
            "change_id": change_info.get("Id"),
            "record": {
                "type": record_type,
                "name": name,
                "ttl": ttl,
                "values": values,
            },
        }

    def _digitalocean_droplet_create(self, operation: dict[str, Any]) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        # Elastic fabric + pool metadata may supply defaults when operators omit them.
        region = self._require_text(
            str(desired.get("region") or "nyc3"),
            "region",
        )
        size = self._require_text(
            str(desired.get("size") or "s-1vcpu-1gb"),
            "size",
        )
        name = self._require_text(
            str(
                desired.get("name")
                or operation.get("resource_ref")
                or f"boss-providerops-{operation['id'][-8:]}"
            ),
            "name",
        )
        user_tags = [
            str(tag).strip()
            for tag in desired.get("tags", [])
            if isinstance(tag, str) and tag.strip()
        ]
        result = self.digitalocean.create_droplet(
            name=name,
            region=region,
            size=size,
            tags=["boss-managed", "boss-providerops"],
            user_tags=user_tags,
        )
        return {
            "provider_ref": result.api_url,
            "resource_external_id": result.droplet_id,
            "resource_name": result.name,
            "resource_type": "droplet",
            "state": self._resource_state_from_provider_status(result.status),
            "health_status": result.status,
            "operation": "created",
            "region": result.region,
            "size": result.size,
            "api_url": result.api_url,
        }

    def _digitalocean_droplet_destroy(self, operation: dict[str, Any]) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        droplet_id = self._require_text(
            str(
                desired.get("droplet_id")
                or desired.get("external_id")
                or operation.get("resource_ref")
                or ""
            ),
            "droplet_id",
        )
        result = self.digitalocean.delete_droplet(droplet_id)
        return {
            "provider_ref": result.api_url,
            "resource_external_id": result.droplet_id,
            "resource_name": str(desired.get("name") or droplet_id),
            "resource_type": "droplet",
            "state": "deleted",
            "health_status": "deleted",
            "operation": "deleted",
            "api_url": result.api_url,
        }

    def _digitalocean_app_deploy(self, operation: dict[str, Any]) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        app_id = self._require_text(
            str(
                desired.get("app_id")
                or desired.get("target_ref")
                or operation.get("resource_ref")
                or ""
            ),
            "app_id",
        )
        result = self.digitalocean.create_app_deployment(
            app_id=app_id,
            force_build=bool(desired.get("force_build", True)),
        )
        return {
            "provider_ref": result.api_url,
            "resource_external_id": result.deployment_id,
            "resource_name": f"{result.app_id}:{result.deployment_id}",
            "resource_type": "app_deployment",
            "state": "active",
            "health_status": result.phase,
            "operation": "deployed",
            "app_id": result.app_id,
            "deployment_id": result.deployment_id,
            "status": result.status,
            "phase": result.phase,
            "api_url": result.api_url,
        }

    # ------------------------------------------------------------------
    # Multi-cloud expand: Azure, OVH, Bedrock, DO extras
    # ------------------------------------------------------------------

    def _azure_creds(self, credential: str, desired: dict[str, Any]) -> dict[str, str]:
        return {
            "tenant_id": str(
                desired.get("tenant_id") or self.settings.azure_tenant_id or ""
            ),
            "client_id": str(
                desired.get("client_id") or self.settings.azure_client_id or ""
            ),
            "client_secret": str(
                credential
                or desired.get("client_secret")
                or self.settings.azure_client_secret
                or ""
            ),
            "subscription_id": str(
                desired.get("subscription_id")
                or self.settings.azure_subscription_id
                or ""
            ),
        }

    def _azure_adapter(self):
        try:
            from .azure_adapter import AzureAdapter
        except ImportError as exc:
            raise ProviderAdapterError(
                "Azure adapter is not installed (azure_adapter.py missing)."
            ) from exc
        return AzureAdapter(self.settings)

    def _azure_resource_group_ensure(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        creds = self._azure_creds(credential, desired)
        name = self._require_text(str(desired.get("name") or desired.get("resource_group") or ""), "name")
        location = self._require_text(str(desired.get("location") or "eastus"), "location")
        result = self._azure_adapter().ensure_resource_group(
            name=name, location=location, **creds
        )
        return {
            "provider_ref": result.get("id") or f"azure:rg:{name}",
            "resource_external_id": result.get("id") or name,
            "resource_name": name,
            "resource_type": "resource_group",
            "state": "active",
            "operation": "ensured",
            "location": location,
            **{k: v for k, v in result.items() if k not in {"client_secret"}},
        }

    def _azure_vm_create(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        creds = self._azure_creds(credential, desired)
        name = self._require_text(
            str(desired.get("name") or operation.get("resource_ref") or ""),
            "name",
        )
        resource_group = self._require_text(
            str(desired.get("resource_group") or desired.get("resourceGroup") or ""),
            "resource_group",
        )
        location = str(desired.get("location") or "eastus")
        size = str(desired.get("vm_size") or desired.get("size") or "Standard_B2s")
        result = self._azure_adapter().create_vm(
            name=name,
            resource_group=resource_group,
            location=location,
            size=size,
            admin_username=str(desired.get("admin_username") or "bossadmin"),
            ssh_public_key=desired.get("ssh_public_key"),
            image_ref=desired.get("image_ref") or desired.get("image"),
            tags=desired.get("tags") if isinstance(desired.get("tags"), dict) else None,
            **creds,
        )
        return {
            "provider_ref": result.get("provider_ref")
            or result.get("vm_id")
            or f"azure:vm:{resource_group}/{name}",
            "resource_external_id": result.get("vm_id") or name,
            "resource_name": name,
            "resource_type": "vm",
            "state": self._resource_state_from_provider_status(
                str(result.get("status") or "creating")
            ),
            "operation": "created",
            "region": location,
            "size": size,
            **{k: v for k, v in result.items() if "secret" not in k.lower()},
        }

    def _azure_vm_destroy(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        creds = self._azure_creds(credential, desired)
        name = self._require_text(
            str(desired.get("name") or operation.get("resource_ref") or ""),
            "name",
        )
        resource_group = self._require_text(
            str(desired.get("resource_group") or ""),
            "resource_group",
        )
        result = self._azure_adapter().destroy_vm(
            resource_group=resource_group, name=name, **creds
        )
        return {
            "provider_ref": result.get("provider_ref")
            or f"azure:vm:{resource_group}/{name}",
            "resource_external_id": result.get("vm_id") or name,
            "resource_name": name,
            "resource_type": "vm",
            "state": "deleted",
            "operation": "deleted",
        }

    def _azure_openai_chat(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        endpoint = str(
            desired.get("endpoint")
            or self.settings.azure_openai_endpoint
            or ""
        )
        api_key = str(
            credential
            or desired.get("api_key")
            or self.settings.azure_openai_key
            or ""
        )
        deployment = str(
            desired.get("deployment")
            or self.settings.azure_openai_deployment
            or ""
        )
        messages = desired.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise ProviderAdapterError("azure.openai.chat requires desired_state.messages")
        result = self._azure_adapter().azure_openai_chat(
            endpoint=endpoint,
            api_key=api_key,
            deployment=deployment,
            api_version=str(
                desired.get("api_version")
                or getattr(self.settings, "azure_openai_api_version", None)
                or "2024-02-15-preview"
            ),
            messages=messages,
            max_tokens=desired.get("max_tokens"),
        )
        return {
            "provider_ref": result.get("provider_ref") or f"azure:openai:{deployment}",
            "resource_external_id": deployment,
            "resource_name": deployment,
            "resource_type": "model_invocation",
            "state": "succeeded",
            "operation": "invoked",
            "content": result.get("content"),
            "usage": result.get("usage") or {},
            "model": result.get("model") or deployment,
        }

    def _ovh_adapter(self):
        try:
            from .ovh_adapter import OvhAdapter
        except ImportError as exc:
            raise ProviderAdapterError(
                "OVH adapter is not installed (ovh_adapter.py missing)."
            ) from exc
        return OvhAdapter(self.settings)

    def _ovh_auth_kwargs(
        self, credential: str, desired: dict[str, Any]
    ) -> dict[str, str]:
        return {
            "application_key": str(
                desired.get("application_key")
                or self.settings.ovh_application_key
                or ""
            ),
            "application_secret": str(
                credential
                or desired.get("application_secret")
                or self.settings.ovh_application_secret
                or ""
            ),
            "consumer_key": str(
                desired.get("consumer_key") or self.settings.ovh_consumer_key or ""
            ),
            "endpoint": str(
                desired.get("endpoint") or self.settings.ovh_endpoint or ""
            ),
        }

    def _ovh_dedicated_server_list(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        servers = self._ovh_adapter().list_dedicated_servers(
            **self._ovh_auth_kwargs(credential, desired)
        )
        return {
            "provider_ref": "ovh:dedicated:servers",
            "resource_external_id": "dedicated-servers",
            "resource_name": "dedicated-servers",
            "resource_type": "inventory",
            "state": "active",
            "operation": "listed",
            "items": servers,
            "count": len(servers),
        }

    def _ovh_cloud_instance_create(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        auth = self._ovh_auth_kwargs(credential, desired)
        project_id = str(
            desired.get("project_id")
            or self.settings.ovh_cloud_project_id
            or ""
        )
        if not project_id:
            raise ProviderAdapterError(
                "ovh.cloud.instance.create requires project_id "
                "(desired_state or BOSS_OVH_CLOUD_PROJECT_ID)."
            )
        name = self._require_text(
            str(desired.get("name") or operation.get("resource_ref") or ""),
            "name",
        )
        result = self._ovh_adapter().create_cloud_instance(
            project_id,
            name=name,
            flavor_id=str(desired.get("flavor_id") or desired.get("size") or ""),
            image_id=str(desired.get("image_id") or desired.get("image") or ""),
            region=str(desired.get("region") or ""),
            **auth,
        )
        instance_id = str(result.get("instance_id") or result.get("id") or name)
        return {
            "provider_ref": result.get("provider_ref")
            or f"ovh:cloud:{project_id}:{instance_id}",
            "resource_external_id": instance_id,
            "resource_name": name,
            "resource_type": "cloud_instance",
            "state": self._resource_state_from_provider_status(
                str(result.get("status") or "BUILD")
            ),
            "operation": "created",
            "region": desired.get("region"),
            "size": desired.get("flavor_id") or desired.get("size"),
        }

    def _ovh_cloud_instance_destroy(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        auth = self._ovh_auth_kwargs(credential, desired)
        project_id = str(
            desired.get("project_id") or self.settings.ovh_cloud_project_id or ""
        )
        instance_id = self._require_text(
            str(
                desired.get("instance_id")
                or desired.get("external_id")
                or operation.get("resource_ref")
                or ""
            ),
            "instance_id",
        )
        result = self._ovh_adapter().destroy_cloud_instance(
            project_id, instance_id, **auth
        )
        return {
            "provider_ref": result.get("provider_ref")
            or f"ovh:cloud:{project_id}:{instance_id}",
            "resource_external_id": instance_id,
            "resource_name": str(desired.get("name") or instance_id),
            "resource_type": "cloud_instance",
            "state": "deleted",
            "operation": "deleted",
        }

    def _aws_bedrock_converse(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            from .aws_bedrock_adapter import AwsBedrockAdapter
        except ImportError as exc:
            raise ProviderAdapterError(
                "AWS Bedrock adapter is not installed (aws_bedrock_adapter.py missing)."
            ) from exc
        desired = operation.get("desired_state") or {}
        model_id = self._require_text(
            str(desired.get("model_id") or desired.get("model") or ""),
            "model_id",
        )
        messages = desired.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise ProviderAdapterError(
                "aws.bedrock.converse requires desired_state.messages"
            )
        adapter = AwsBedrockAdapter(self.settings)
        result = adapter.converse(
            model_id=model_id,
            messages=messages,
            max_tokens=int(desired.get("max_tokens") or 1024),
            temperature=desired.get("temperature"),
            system=desired.get("system"),
            access_key_id=str(
                desired.get("access_key_id") or self.settings.aws_access_key_id or ""
            ),
            secret_access_key=str(credential or ""),
            region=str(desired.get("region") or self.settings.aws_region),
            session_token=desired.get("session_token"),
        )
        return {
            "provider_ref": result.get("provider_ref") or f"aws:bedrock:{model_id}",
            "resource_external_id": model_id,
            "resource_name": model_id,
            "resource_type": "model_invocation",
            "state": "succeeded",
            "operation": "invoked",
            "content": result.get("content"),
            "usage": result.get("usage") or {},
            "model": model_id,
        }

    def _aws_bedrock_list_models(
        self, credential: str, operation: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            from .aws_bedrock_adapter import AwsBedrockAdapter
        except ImportError as exc:
            raise ProviderAdapterError(
                "AWS Bedrock adapter is not installed (aws_bedrock_adapter.py missing)."
            ) from exc
        desired = operation.get("desired_state") or {}
        adapter = AwsBedrockAdapter(self.settings)
        models = adapter.list_foundation_models(
            access_key_id=str(
                desired.get("access_key_id") or self.settings.aws_access_key_id or ""
            ),
            secret_access_key=str(credential or ""),
            region=str(desired.get("region") or self.settings.aws_region),
            session_token=desired.get("session_token"),
        )
        return {
            "provider_ref": "aws:bedrock:models",
            "resource_external_id": "foundation-models",
            "resource_name": "foundation-models",
            "resource_type": "inventory",
            "state": "active",
            "operation": "listed",
            "items": models,
            "count": len(models),
        }

    def _digitalocean_firewall_ensure(self, operation: dict[str, Any]) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        name = self._require_text(str(desired.get("name") or ""), "name")
        ensure = getattr(self.digitalocean, "ensure_firewall", None)
        if ensure is None:
            raise ProviderAdapterError(
                "DigitalOcean adapter does not implement ensure_firewall yet."
            )
        result = ensure(
            name=name,
            inbound_rules=desired.get("inbound_rules") or [],
            outbound_rules=desired.get("outbound_rules") or [],
            tags=desired.get("tags"),
            droplet_ids=desired.get("droplet_ids"),
        )
        return {
            "provider_ref": result.get("provider_ref")
            or f"do:firewall:{result.get('id') or name}",
            "resource_external_id": str(result.get("id") or name),
            "resource_name": name,
            "resource_type": "firewall",
            "state": "active",
            "operation": "ensured",
        }

    def _digitalocean_floating_ip_create(
        self, operation: dict[str, Any]
    ) -> dict[str, Any]:
        create = getattr(self.digitalocean, "create_floating_ip", None)
        if create is None:
            raise ProviderAdapterError(
                "DigitalOcean adapter does not implement create_floating_ip yet."
            )
        desired = operation.get("desired_state") or {}
        result = create(
            region=desired.get("region"),
            droplet_id=desired.get("droplet_id"),
        )
        ip = str(result.get("ip") or result.get("floating_ip") or "")
        return {
            "provider_ref": result.get("provider_ref") or f"do:floating_ip:{ip}",
            "resource_external_id": ip,
            "resource_name": ip,
            "resource_type": "floating_ip",
            "state": "active",
            "operation": "created",
        }

    def _digitalocean_inference_chat(
        self, operation: dict[str, Any]
    ) -> dict[str, Any]:
        chat = getattr(self.digitalocean, "inference_chat", None)
        if chat is None:
            raise ProviderAdapterError(
                "DigitalOcean adapter does not implement inference_chat yet."
            )
        desired = operation.get("desired_state") or {}
        messages = desired.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise ProviderAdapterError(
                "digitalocean.inference.chat requires desired_state.messages"
            )
        result = chat(
            messages=messages,
            model=str(desired.get("model") or "default"),
            base_url=desired.get("base_url")
            or getattr(self.settings, "do_inference_base_url", None),
            api_key=desired.get("api_key")
            or getattr(self.settings, "do_inference_api_key", None),
        )
        return {
            "provider_ref": result.get("provider_ref") or "do:inference",
            "resource_external_id": str(desired.get("model") or "default"),
            "resource_name": str(desired.get("model") or "default"),
            "resource_type": "model_invocation",
            "state": "succeeded",
            "operation": "invoked",
            "content": result.get("content"),
            "usage": result.get("usage") or {},
        }

    def _ssh_host_desired(self, operation: dict[str, Any]) -> tuple[dict, str, str, str, int]:
        desired = operation.get("desired_state") or {}
        host = self._require_text(str(desired.get("host") or ""), "host")
        workload = self._require_text(
            str(desired.get("workload") or operation.get("resource_ref") or ""),
            "workload",
        )
        user = str(desired.get("user") or "root").strip() or "root"
        port = int(desired.get("port") or 22)
        return desired, host, workload, user, port

    def _ssh_host_workload_deploy(self, operation: dict[str, Any]) -> dict[str, Any]:
        desired, host, workload, user, port = self._ssh_host_desired(operation)
        image = self._require_text(str(desired.get("image") or ""), "image")
        env = {str(k): str(v) for k, v in (desired.get("env") or {}).items()}
        ports = [str(p) for p in (desired.get("ports") or []) if str(p).strip()]
        volumes = [str(v) for v in (desired.get("volumes") or []) if str(v).strip()]
        command = desired.get("command")
        result = self.ssh_host.deploy_workload(
            host=host,
            workload=workload,
            image=image,
            user=user,
            port=port,
            env=env,
            ports=ports,
            volumes=volumes,
            command=str(command) if command else None,
        )
        return {
            "provider_ref": f"ssh://{user}@{host}",
            "resource_external_id": result.container_id or workload,
            "resource_name": workload,
            "resource_type": "container",
            "state": "active",
            "health_status": result.status,
            "operation": "deployed",
            "host": host,
            "image": image,
            "container_id": result.container_id,
            "ports": result.ports,
        }

    def _ssh_host_workload_teardown(self, operation: dict[str, Any]) -> dict[str, Any]:
        _desired, host, workload, user, port = self._ssh_host_desired(operation)
        result = self.ssh_host.teardown_workload(
            host=host, workload=workload, user=user, port=port
        )
        return {
            "provider_ref": f"ssh://{user}@{host}",
            "resource_external_id": workload,
            "resource_name": workload,
            "resource_type": "container",
            "state": "deleted" if result.removed else "active",
            "health_status": "deleted" if result.removed else "unknown",
            "operation": "deleted",
            "host": host,
            "removed": result.removed,
        }

    def _ssh_host_workload_health_check(self, operation: dict[str, Any]) -> dict[str, Any]:
        _desired, host, workload, user, port = self._ssh_host_desired(operation)
        result = self.ssh_host.health_check(
            host=host, workload=workload, user=user, port=port
        )
        return {
            "provider_ref": f"ssh://{user}@{host}",
            "resource_external_id": workload,
            "resource_name": workload,
            "resource_type": "container",
            "state": "active" if result.running else "stopped",
            "health_status": result.status,
            "operation": "read",
            "host": host,
            "running": result.running,
        }

    def _ssh_host_job_run(self, operation: dict[str, Any]) -> dict[str, Any]:
        desired, host, job, user, port = self._ssh_host_desired(operation)
        image = self._require_text(str(desired.get("image") or ""), "image")
        env = {str(k): str(v) for k, v in (desired.get("env") or {}).items()}
        volumes = [str(v) for v in (desired.get("volumes") or []) if str(v).strip()]
        command = desired.get("command")
        timeout = int(desired.get("timeout") or 900)
        result = self.ssh_host.run_job_to_completion(
            host=host,
            job=job,
            image=image,
            user=user,
            port=port,
            env=env,
            volumes=volumes,
            command=str(command) if command else None,
            timeout=timeout,
        )
        succeeded = result.finished and result.exit_code == 0
        return {
            "provider_ref": f"ssh://{user}@{host}",
            "resource_external_id": job,
            "resource_name": job,
            "resource_type": "job",
            "state": "completed" if succeeded else "failed",
            "health_status": (
                f"exit={result.exit_code}" if result.finished else "timeout"
            ),
            "operation": "job_run",
            "host": host,
            "image": image,
            "exit_code": result.exit_code,
            "finished": result.finished,
            "logs_tail": result.logs[-2000:],
        }

    def _supabase_migration_apply(
        self,
        connection: Connection,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        descriptor = self._deployment_resource_descriptor(
            desired,
            {"database_migration", "migration", "supabase_migration"},
        )
        project_ref = self._require_text(
            str(
                descriptor.get("project_ref")
                or desired.get("project_ref")
                or desired.get("target_ref")
                or operation.get("resource_ref")
                or ""
            ),
            "project_ref",
        )
        migration_name = self._require_text(
            str(
                descriptor.get("migration_name")
                or descriptor.get("name")
                or desired.get("migration_name")
                or desired.get("commit_ref")
                or ""
            ),
            "migration_name",
        )
        migration_sql = self._deployment_text_payload(
            connection,
            project_id=operation["project_id"],
            descriptor=descriptor,
            direct_fields=("migration_sql", "sql", "query", "content"),
            artifact_fields=("migration_artifact_id", "artifact_id"),
            preferred_file=f"{migration_name}.sql",
            payload_label="Supabase migration",
        )
        rollback_sql = descriptor.get("rollback") or descriptor.get("rollback_sql")
        payload: dict[str, Any] = {
            "name": migration_name,
            "query": migration_sql,
        }
        if isinstance(rollback_sql, str) and rollback_sql.strip():
            payload["rollback"] = rollback_sql

        url = (
            "https://api.supabase.com/v1/projects/"
            f"{project_ref}/database/migrations"
        )
        headers = {
            **self._provider_headers("supabase", credential),
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            response_payload = response.json()

        result = response_payload if isinstance(response_payload, dict) else {}
        migration_ref = str(
            result.get("version")
            or result.get("id")
            or result.get("name")
            or migration_name
        )
        return {
            "provider_ref": f"supabase:migration:{project_ref}:{migration_ref}",
            "resource_external_id": migration_ref,
            "resource_name": migration_name,
            "resource_type": "migration",
            "state": "active",
            "health_status": "applied",
            "operation": "applied",
            "project_ref": project_ref,
            "migration_name": migration_name,
            "api_success": True,
            "response": result,
        }

    def _supabase_project_pause(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        project_ref = self._require_text(
            str(
                desired.get("project_ref")
                or desired.get("ref")
                or operation.get("resource_ref")
                or ""
            ),
            "project_ref",
        )
        url = f"https://api.supabase.com/v1/projects/{project_ref}/pause"
        headers = self._provider_headers("supabase", credential)
        with httpx.Client(timeout=30) as client:
            response = client.post(url, headers=headers)
            response.raise_for_status()
            response_payload = response.json()
        result = response_payload if isinstance(response_payload, dict) else {}
        return {
            "provider_ref": f"supabase:project:{project_ref}:paused",
            "resource_external_id": project_ref,
            "resource_name": str(desired.get("name") or project_ref),
            "resource_type": "project",
            "state": "draining",
            "health_status": "paused",
            "operation": "paused",
            "project_ref": project_ref,
            "reason": desired.get("reason"),
            "api_success": True,
            "response": result,
        }

    def _supabase_project_create(
        self,
        credential: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        desired = operation.get("desired_state") or {}
        name = self._require_text(str(desired.get("name") or ""), "name")
        organization_slug = self._require_text(
            str(
                desired.get("organization_slug")
                or desired.get("org_ref")
                or desired.get("organization_ref")
                or ""
            ),
            "organization_slug",
        )
        db_pass_secret_ref = self._require_text(
            str(
                desired.get("db_pass_secret_ref")
                or desired.get("database_password_ref")
                or ""
            ),
            "db_pass_secret_ref",
        )
        payload: dict[str, Any] = {
            "name": name,
            "organization_slug": organization_slug,
            "db_pass": self._env_secret_value(db_pass_secret_ref),
        }
        for field in ("region", "plan", "desired_instance_size", "template_url"):
            value = desired.get(field)
            if isinstance(value, str) and value.strip():
                payload[field] = value.strip()
        if desired.get("kps_enabled") is not None:
            payload["kps_enabled"] = bool(desired["kps_enabled"])

        headers = {
            **self._provider_headers("supabase", credential),
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            response = client.post(
                "https://api.supabase.com/v1/projects",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()
        result = response_payload if isinstance(response_payload, dict) else {}
        project_ref = self._require_text(str(result.get("ref") or ""), "project_ref")
        status = str(result.get("status") or "provisioning")
        return {
            "provider_ref": f"supabase:project:{project_ref}",
            "resource_external_id": project_ref,
            "resource_name": str(result.get("name") or name),
            "resource_type": "project",
            "state": "provisioning",
            "health_status": status,
            "operation": "created",
            "project_ref": project_ref,
            "organization_slug": result.get("organization_slug") or organization_slug,
            "region": result.get("region") or payload.get("region"),
            "status": status,
            "api_success": True,
            "response": result,
        }

    def _deployment_resource_descriptor(
        self,
        desired: dict[str, Any],
        accepted_types: set[str],
    ) -> dict[str, Any]:
        for item in desired.get("required_resources") or []:
            if not isinstance(item, dict):
                continue
            labels = {
                str(item.get(key) or "").lower().replace("-", "_").strip()
                for key in ("type", "resource_type", "kind", "id")
            }
            labels.discard("")
            if labels & accepted_types:
                return item
        return {}

    def _deployment_text_payload(
        self,
        connection: Connection,
        *,
        project_id: str,
        descriptor: dict[str, Any],
        direct_fields: tuple[str, ...],
        artifact_fields: tuple[str, ...],
        preferred_file: str,
        payload_label: str,
    ) -> str:
        for field in direct_fields:
            value = descriptor.get(field)
            if isinstance(value, str) and value.strip():
                return value

        artifact_id = ""
        for field in artifact_fields:
            value = descriptor.get(field)
            if isinstance(value, str) and value.strip():
                artifact_id = value.strip()
                break
        if not artifact_id:
            raise ProviderAdapterError(
                f"{payload_label} deployment requires inline content or an artifact reference."
            )

        row = connection.execute(
            text(
                """
                SELECT content
                FROM artifacts
                WHERE id = :artifact_id AND project_id = :project_id
                """
            ),
            {"artifact_id": artifact_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise ProviderAdapterError(f"{payload_label} artifact was not found.")
        content = row["content"]
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, dict):
            for field in direct_fields:
                value = content.get(field)
                if isinstance(value, str) and value.strip():
                    return value
            files = content.get("files")
            if isinstance(files, list):
                for file_payload in files:
                    if not isinstance(file_payload, dict):
                        continue
                    path = str(
                        file_payload.get("path")
                        or file_payload.get("name")
                        or ""
                    ).strip()
                    value = file_payload.get("content")
                    if path == preferred_file and isinstance(value, str) and value.strip():
                        return value
                for file_payload in files:
                    if not isinstance(file_payload, dict):
                        continue
                    value = file_payload.get("content")
                    if isinstance(value, str) and value.strip():
                        return value
        raise ProviderAdapterError(
            f"{payload_label} artifact did not contain deployable content."
        )

    def _persist_managed_resource_from_result(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: Optional[str],
        operation: dict[str, Any],
        resource_receipt_id: str,
        receipt_id: str,
        result: dict[str, Any],
    ) -> None:
        provider = self._provider_from_skill(operation["skill_id"])
        external_id = result.get("resource_external_id")
        if not external_id:
            return
        desired = operation.get("desired_state") or {}
        metadata = {
            "changeset_id": operation["changeset_id"],
            "operation_id": operation["id"],
            "resource_receipt_id": resource_receipt_id,
            "provider_ref": result.get("provider_ref"),
            "desired_state": desired,
            "provider_result": result,
        }
        connection.execute(
            text(
                """
                INSERT INTO external_resources (
                    id, project_id, provider_account_id, provider, resource_type,
                    external_id, name, state, ownership, environment, health_status,
                    receipt_id, cost_confidence, last_seen_at, tags, metadata
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :provider, :resource_type,
                    :external_id, :name, :state, 'managed_by_boss', :environment,
                    :health_status, :receipt_id, 'unknown', now(),
                    CAST(:tags AS JSONB), CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, provider, external_id) DO UPDATE
                SET provider_account_id = EXCLUDED.provider_account_id,
                    resource_type = EXCLUDED.resource_type,
                    name = EXCLUDED.name,
                    state = EXCLUDED.state,
                    ownership = 'managed_by_boss',
                    environment = EXCLUDED.environment,
                    health_status = EXCLUDED.health_status,
                    receipt_id = EXCLUDED.receipt_id,
                    last_seen_at = now(),
                    tags = EXCLUDED.tags,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": make_id("res"),
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "provider": provider,
                "resource_type": str(
                    result.get("resource_type") or operation["resource_type"]
                ),
                "external_id": str(external_id),
                "name": str(
                    result.get("resource_name")
                    or operation.get("resource_ref")
                    or external_id
                ),
                "state": str(result.get("state") or "active"),
                "environment": str(desired.get("environment") or "unknown"),
                "health_status": str(result.get("health_status") or "unknown"),
                "receipt_id": receipt_id,
                "tags": json.dumps(["boss-managed", provider]),
                "metadata": json.dumps(metadata),
            },
        )

    def _mark_deployment_runs_deployed(
        self,
        connection: Connection,
        *,
        project_id: str,
        changeset_id: str,
        resource_receipts: list[dict[str, Any]],
    ) -> None:
        row = connection.execute(
            text(
                """
                UPDATE deployment_runs
                SET status = 'deployed',
                    started_at = COALESCE(started_at, now()),
                    updated_at = now()
                WHERE project_id = :project_id
                  AND changeset_id = :changeset_id
                  AND status IN ('created', 'building', 'deploying')
                RETURNING id
                """
            ),
            {"project_id": project_id, "changeset_id": changeset_id},
        ).mappings().first()
        if not row:
            return
        emit_event(
            connection,
            project_id,
            "deployment_run.deployed",
            "resource_plane",
            "deployment_run",
            row["id"],
            {
                "changeset_id": changeset_id,
                "resource_receipt_ids": [item["id"] for item in resource_receipts],
            },
        )

    def _record_provider_health(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: Optional[str],
        provider: str,
        status: str,
        api_available: bool,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        health_id = make_id("ph")
        connection.execute(
            text(
                """
                INSERT INTO provider_health (
                    id, project_id, provider_account_id, provider, status,
                    api_available, detail
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :provider, :status,
                    :api_available, CAST(:detail AS JSONB)
                )
                """
            ),
            {
                "id": health_id,
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "provider": provider,
                "status": status,
                "api_available": api_available,
                "detail": json.dumps(detail),
            },
        )
        return self._serialize_provider_health(
            connection.execute(
                text(
                    """
                    SELECT id, project_id, provider_account_id, provider, status,
                           api_available, rate_limit_status, usage_cap_status,
                           last_checked_at, detail, created_at
                    FROM provider_health
                    WHERE id = :id
                    """
                ),
                {"id": health_id},
            ).mappings().one()
        )

    def _record_changeset_cost_event(
        self,
        connection: Connection,
        *,
        changeset: dict[str, Any],
        resource_receipts: list[dict[str, Any]],
    ) -> None:
        amount_usd_micros = int(changeset["expected_cost_usd_micros"] or 0)
        if amount_usd_micros <= 0 or not resource_receipts:
            return
        primary_receipt = resource_receipts[0]
        external_resource_id = connection.execute(
            text(
                """
                SELECT id
                FROM external_resources
                WHERE project_id = :project_id AND receipt_id = :receipt_id
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {
                "project_id": changeset["project_id"],
                "receipt_id": primary_receipt["receipt_id"],
            },
        ).scalar_one_or_none()
        cost_event_id = make_id("cevt")
        metadata = {
            "changeset_id": changeset["id"],
            "reservation_id": changeset["reservation_id"],
            "cost_quote": changeset.get("cost_quote") or {},
            "confidence_reason": (
                "Provider write completed, but provider billing has not reconciled "
                "this estimate to invoice or provider-reported actuals yet."
            ),
            "resource_receipt_ids": [item["id"] for item in resource_receipts],
        }
        connection.execute(
            text(
                """
                INSERT INTO cost_events (
                    id, project_id, provider_account_id, external_resource_id,
                    provider, category, amount_usd_micros, cost_layer, confidence,
                    source_ref, receipt_id, metadata
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :external_resource_id,
                    :provider, 'provider', :amount_usd_micros, 'observed',
                    'estimated_from_price_catalog', :source_ref, :receipt_id,
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": cost_event_id,
                "project_id": changeset["project_id"],
                "provider_account_id": primary_receipt["provider_account_id"],
                "external_resource_id": external_resource_id,
                "provider": primary_receipt["provider"],
                "amount_usd_micros": amount_usd_micros,
                "source_ref": changeset["id"],
                "receipt_id": primary_receipt["receipt_id"],
                "metadata": json.dumps(metadata),
            },
        )
        self._evaluate_cost_anomaly(
            connection,
            project_id=changeset["project_id"],
            cost_event_id=cost_event_id,
            provider_account_id=primary_receipt["provider_account_id"],
            provider=primary_receipt["provider"],
            amount_usd_micros=amount_usd_micros,
        )
        emit_event(
            connection,
            changeset["project_id"],
            "provider.cost_event_recorded",
            "resource_plane",
            "cost_event",
            cost_event_id,
            {
                "changeset_id": changeset["id"],
                "amount_usd_micros": amount_usd_micros,
                "confidence": "estimated_from_price_catalog",
                "receipt_id": primary_receipt["receipt_id"],
            },
        )

    def _record_provider_billing_cost_event(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: str,
        cost_record: ProviderCostRecord,
    ) -> bool:
        existing_id = connection.execute(
            text(
                """
                SELECT id
                FROM cost_events
                WHERE project_id = :project_id
                  AND provider_account_id = :provider_account_id
                  AND provider = :provider
                  AND source_ref = :source_ref
                  AND cost_layer = :cost_layer
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "provider": cost_record.provider,
                "source_ref": cost_record.source_ref,
                "cost_layer": cost_record.cost_layer,
            },
        ).scalar_one_or_none()
        if existing_id:
            return False

        billing_account_id = self._upsert_billing_account(
            connection,
            project_id=project_id,
            provider_account_id=provider_account_id,
            provider=cost_record.provider,
            external_billing_id=cost_record.billing_account_external_id,
            label=cost_record.billing_account_label,
            metadata={
                "source_ref": cost_record.source_ref,
                "confidence": cost_record.confidence,
            },
        )

        external_resource_id = None
        if cost_record.external_resource_external_id:
            external_resource_id = connection.execute(
                text(
                    """
                    SELECT id
                    FROM external_resources
                    WHERE project_id = :project_id
                      AND provider = :provider
                      AND external_id = :external_id
                    LIMIT 1
                    """
                ),
                {
                    "project_id": project_id,
                    "provider": cost_record.provider,
                    "external_id": cost_record.external_resource_external_id,
                },
            ).scalar_one_or_none()

        cost_event_id = make_id("cevt")
        connection.execute(
            text(
                """
                INSERT INTO cost_events (
                    id, project_id, provider_account_id, external_resource_id,
                    billing_account_id, provider, category, amount_usd_micros,
                    cost_layer, confidence, source_ref, period_start, period_end,
                    metadata
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :external_resource_id,
                    :billing_account_id, :provider, :category, :amount_usd_micros,
                    :cost_layer, :confidence, :source_ref, :period_start,
                    :period_end, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": cost_event_id,
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "external_resource_id": external_resource_id,
                "billing_account_id": billing_account_id,
                "provider": cost_record.provider,
                "category": cost_record.category,
                "amount_usd_micros": cost_record.amount_usd_micros,
                "cost_layer": cost_record.cost_layer,
                "confidence": cost_record.confidence,
                "source_ref": cost_record.source_ref,
                "period_start": cost_record.period_start,
                "period_end": cost_record.period_end,
                "metadata": json.dumps(cost_record.metadata),
            },
        )

        if cost_record.amount_usd_micros > 0:
            self._evaluate_cost_anomaly(
                connection,
                project_id=project_id,
                cost_event_id=cost_event_id,
                provider_account_id=provider_account_id,
                provider=cost_record.provider,
                amount_usd_micros=cost_record.amount_usd_micros,
            )
        emit_event(
            connection,
            project_id,
            "provider.cost_event_recorded",
            "resource_plane",
            "cost_event",
            cost_event_id,
            {
                "provider": cost_record.provider,
                "provider_account_id": provider_account_id,
                "source_ref": cost_record.source_ref,
                "cost_layer": cost_record.cost_layer,
                "confidence": cost_record.confidence,
                "billing_account_id": billing_account_id,
            },
        )
        return True

    def _upsert_billing_account(
        self,
        connection: Connection,
        *,
        project_id: str,
        provider_account_id: str,
        provider: str,
        external_billing_id: Optional[str],
        label: Optional[str],
        metadata: dict[str, Any],
    ) -> str:
        account_ref = external_billing_id or f"{provider}:{provider_account_id}:default"
        existing_id = connection.execute(
            text(
                """
                SELECT id
                FROM billing_accounts
                WHERE project_id = :project_id
                  AND provider_account_id = :provider_account_id
                  AND provider = :provider
                  AND external_billing_id = :external_billing_id
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "provider": provider,
                "external_billing_id": account_ref,
            },
        ).scalar_one_or_none()
        if existing_id:
            connection.execute(
                text(
                    """
                    UPDATE billing_accounts
                    SET label = :label,
                        metadata = CAST(:metadata AS JSONB),
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {
                    "id": existing_id,
                    "label": label or account_ref,
                    "metadata": json.dumps(metadata),
                },
            )
            return existing_id

        billing_account_id = make_id("bill")
        connection.execute(
            text(
                """
                INSERT INTO billing_accounts (
                    id, project_id, provider_account_id, provider,
                    external_billing_id, label, metadata
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :provider,
                    :external_billing_id, :label, CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": billing_account_id,
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "provider": provider,
                "external_billing_id": account_ref,
                "label": label or account_ref,
                "metadata": json.dumps(metadata),
            },
        )
        return billing_account_id

    def _evaluate_cost_anomaly(
        self,
        connection: Connection,
        *,
        project_id: str,
        cost_event_id: str,
        provider_account_id: Optional[str],
        provider: str,
        amount_usd_micros: int,
    ) -> None:
        policy = connection.execute(
            text(
                """
                SELECT daily_cap_usd_micros, freeze_provisioning_pct
                FROM budget_policies
                WHERE scope_type = 'project'
                  AND scope_id = :project_id
                ORDER BY version DESC, created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not policy or not int(policy["daily_cap_usd_micros"] or 0):
            return
        threshold = int(
            int(policy["daily_cap_usd_micros"])
            * float(policy["freeze_provisioning_pct"])
        )
        if threshold <= 0:
            return
        observed_today = connection.execute(
            text(
                """
                SELECT COALESCE(SUM(amount_usd_micros), 0) AS observed
                FROM cost_events
                WHERE project_id = :project_id
                  AND cost_layer IN ('observed', 'reconciled')
                  AND created_at >= date_trunc('day', now())
                """
            ),
            {"project_id": project_id},
        ).scalar_one()
        observed_today = int(observed_today or 0)
        if observed_today < threshold:
            return
        existing = connection.execute(
            text(
                """
                SELECT id
                FROM cost_anomalies
                WHERE project_id = :project_id
                  AND cost_event_id = :cost_event_id
                  AND anomaly_type = 'provider_spend_freeze_threshold_crossed'
                """
            ),
            {"project_id": project_id, "cost_event_id": cost_event_id},
        ).scalar_one_or_none()
        if existing:
            return

        severity = (
            "red"
            if observed_today >= int(policy["daily_cap_usd_micros"])
            else "orange"
        )
        recommended_actions = [
            {
                "action": "provider_read_only_mode_applied",
                "provider_account_id": provider_account_id,
            },
            {"action": "review_budget_or_defer_provider_work"},
            {"action": "reconcile_provider_invoice_before_unfreezing"},
        ]
        decision_id = create_blocking_decision(
            connection,
            project_id,
            None,
            f"Provider spend anomaly for {provider}",
            {
                "action": "review_provider_spend_kill_switch",
                "provider": provider,
                "provider_account_id": provider_account_id,
                "cost_event_id": cost_event_id,
                "kill_switches_applied": ["read_only_provider_mode"],
            },
            {
                "observed_today_usd_micros": observed_today,
                "threshold_usd_micros": threshold,
                "trigger_amount_usd_micros": amount_usd_micros,
                "confidence": "estimated_from_price_catalog",
            },
            "provider_cost_anomaly",
            risk_tier=3,
        )
        if provider_account_id:
            connection.execute(
                text(
                    """
                    UPDATE provider_accounts
                    SET read_only = true,
                        last_error = CAST(:last_error AS JSONB),
                        updated_at = now()
                    WHERE id = :provider_account_id AND project_id = :project_id
                    """
                ),
                {
                    "project_id": project_id,
                    "provider_account_id": provider_account_id,
                    "last_error": json.dumps(
                        {
                            "reason": "provider_spend_freeze_threshold_crossed",
                            "cost_event_id": cost_event_id,
                            "observed_today_usd_micros": observed_today,
                            "threshold_usd_micros": threshold,
                        }
                    ),
                },
            )
        anomaly_id = make_id("canom")
        summary = (
            f"{provider} observed provider cost {observed_today} micros crossed "
            f"freeze threshold {threshold} micros; provider writes were switched "
            "to read-only mode pending review."
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_anomalies (
                    id, project_id, provider_account_id, cost_event_id,
                    decision_id, provider, anomaly_type, severity, status,
                    amount_usd_micros, threshold_usd_micros, summary,
                    recommended_actions
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :cost_event_id,
                    :decision_id, :provider,
                    'provider_spend_freeze_threshold_crossed', :severity, 'open',
                    :amount_usd_micros, :threshold_usd_micros, :summary,
                    CAST(:recommended_actions AS JSONB)
                )
                ON CONFLICT (project_id, cost_event_id, anomaly_type) DO NOTHING
                """
            ),
            {
                "id": anomaly_id,
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "cost_event_id": cost_event_id,
                "decision_id": decision_id,
                "provider": provider,
                "severity": severity,
                "amount_usd_micros": observed_today,
                "threshold_usd_micros": threshold,
                "summary": summary,
                "recommended_actions": json.dumps(recommended_actions),
            },
        )
        emit_event(
            connection,
            project_id,
            "provider.cost_anomaly_detected",
            "resource_plane",
            "cost_anomaly",
            anomaly_id,
            {
                "provider": provider,
                "provider_account_id": provider_account_id,
                "cost_event_id": cost_event_id,
                "decision_id": decision_id,
                "severity": severity,
                "observed_today_usd_micros": observed_today,
                "threshold_usd_micros": threshold,
                "kill_switches_applied": ["read_only_provider_mode"],
            },
        )
        if provider_account_id:
            emit_event(
                connection,
                project_id,
                "provider.read_only_kill_switch_applied",
                "resource_plane",
                "provider_account",
                provider_account_id,
                {
                    "cost_anomaly_id": anomaly_id,
                    "decision_id": decision_id,
                    "reason": "provider_spend_freeze_threshold_crossed",
                },
            )

    def _finish_reconciliation(
        self,
        connection: Connection,
        run_id: str,
        *,
        status: str,
        resources_seen: int,
        resources_changed: int,
        costs_seen: int,
        error: Optional[dict[str, Any]],
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE reconciliation_runs
                SET status = :status,
                    resources_seen = :resources_seen,
                    resources_changed = :resources_changed,
                    costs_seen = :costs_seen,
                    finished_at = now(),
                    error = CAST(:error AS JSONB)
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "status": status,
                "resources_seen": resources_seen,
                "resources_changed": resources_changed,
                "costs_seen": costs_seen,
                "error": json.dumps(error),
            },
        )

    def _attach_reconciliation_metadata(
        self,
        connection: Connection,
        run_id: str,
        metadata: dict[str, Any],
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE reconciliation_runs
                SET metadata = metadata || CAST(:metadata AS JSONB)
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "metadata": json.dumps(metadata),
            },
        )

    def _create_reconciliation_snapshot_artifact(
        self,
        connection: Connection,
        *,
        project_id: str,
        run_id: str,
        account: dict[str, Any],
        status: str,
        inventory: list[InventoryResource],
        cost_records: list[ProviderCostRecord],
        costs_recorded: int,
        provider_health_id: Optional[str],
        drift_report_id: Optional[str],
        deadletter_id: Optional[str],
        error: Optional[dict[str, Any]],
        billing_status: str = "not_requested",
        billing_message: Optional[str] = None,
    ) -> str:
        content = {
            "object_type": "providerops_inventory_snapshot",
            "reconciliation_run_id": run_id,
            "project_id": project_id,
            "provider_account_id": account["id"],
            "provider": account["provider"],
            "status": status,
            "resources_seen": len(inventory),
            "cost_records_seen": len(cost_records),
            "costs_recorded": costs_recorded,
            "billing_status": billing_status,
            "billing_confidence": (
                "unknown"
                if billing_status in {"unavailable", "failed", "not_requested"}
                else "provider_reported"
                if costs_recorded
                else "unknown"
            ),
            "billing_message": billing_message,
            "provider_health_id": provider_health_id,
            "drift_report_id": drift_report_id,
            "deadletter_id": deadletter_id,
            "resources": [
                self._inventory_resource_snapshot(resource)
                for resource in sorted(
                    inventory,
                    key=lambda item: (
                        item.provider,
                        item.resource_type,
                        item.external_id,
                    ),
                )
            ],
            "cost_records": [
                self._cost_record_snapshot(record)
                for record in sorted(cost_records, key=lambda item: item.source_ref)
            ],
            "error": error,
            "data_boundary": "internal",
        }
        return create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "providerops_inventory_snapshot",
            f"ProviderOps inventory snapshot: {account['provider']} {run_id}",
            content,
            data_boundary="internal",
        )

    def _inventory_resource_snapshot(
        self,
        resource: InventoryResource,
    ) -> dict[str, Any]:
        return {
            "provider": resource.provider,
            "resource_type": resource.resource_type,
            "external_id": resource.external_id,
            "name": resource.name,
            "state": resource.state,
            "ownership": resource.ownership,
            "environment": resource.environment,
            "health_status": resource.health_status,
            "tags": sorted(resource.tags),
            "cost_confidence": resource.cost_confidence,
            "estimated_monthly_usd_micros": resource.estimated_monthly_usd_micros,
            "actual_monthly_usd_micros": resource.actual_monthly_usd_micros,
            "metadata": resource.metadata,
        }

    def _cost_record_snapshot(self, record: ProviderCostRecord) -> dict[str, Any]:
        return {
            "provider": record.provider,
            "category": record.category,
            "amount_usd_micros": record.amount_usd_micros,
            "cost_layer": record.cost_layer,
            "confidence": record.confidence,
            "source_ref": record.source_ref,
            "period_start": (
                record.period_start.isoformat() if record.period_start else None
            ),
            "period_end": record.period_end.isoformat()
            if record.period_end
            else None,
            "billing_account_external_id": record.billing_account_external_id,
            "billing_account_label": record.billing_account_label,
            "external_resource_external_id": record.external_resource_external_id,
            "metadata": record.metadata,
        }

    def _upsert_external_resource(
        self,
        connection: Connection,
        project_id: str,
        provider_account_id: str,
        resource: InventoryResource,
    ) -> bool:
        existing = connection.execute(
            text(
                """
                SELECT id, metadata, state, ownership, health_status
                FROM external_resources
                WHERE project_id = :project_id AND provider = :provider
                  AND external_id = :external_id
                """
            ),
            {
                "project_id": project_id,
                "provider": resource.provider,
                "external_id": resource.external_id,
            },
        ).mappings().first()
        resource_id = existing["id"] if existing else make_id("res")
        connection.execute(
            text(
                """
                INSERT INTO external_resources (
                    id, project_id, provider_account_id, provider, resource_type,
                    external_id, name, state, ownership, environment, health_status,
                    estimated_monthly_usd_micros, actual_monthly_usd_micros,
                    cost_confidence, last_seen_at, tags, metadata
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :provider, :resource_type,
                    :external_id, :name, :state, :ownership, :environment,
                    :health_status, :estimated_monthly_usd_micros,
                    :actual_monthly_usd_micros, :cost_confidence, now(),
                    CAST(:tags AS JSONB), CAST(:metadata AS JSONB)
                )
                ON CONFLICT (project_id, provider, external_id) DO UPDATE
                SET provider_account_id = EXCLUDED.provider_account_id,
                    resource_type = EXCLUDED.resource_type,
                    name = EXCLUDED.name,
                    state = EXCLUDED.state,
                    ownership = EXCLUDED.ownership,
                    environment = EXCLUDED.environment,
                    health_status = EXCLUDED.health_status,
                    estimated_monthly_usd_micros = EXCLUDED.estimated_monthly_usd_micros,
                    actual_monthly_usd_micros = EXCLUDED.actual_monthly_usd_micros,
                    cost_confidence = EXCLUDED.cost_confidence,
                    last_seen_at = now(),
                    tags = EXCLUDED.tags,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """
            ),
            {
                "id": resource_id,
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "provider": resource.provider,
                "resource_type": resource.resource_type,
                "external_id": resource.external_id,
                "name": resource.name,
                "state": resource.state,
                "ownership": resource.ownership,
                "environment": resource.environment,
                "health_status": resource.health_status,
                "estimated_monthly_usd_micros": resource.estimated_monthly_usd_micros,
                "actual_monthly_usd_micros": resource.actual_monthly_usd_micros,
                "cost_confidence": resource.cost_confidence,
                "tags": json.dumps(resource.tags),
                "metadata": json.dumps(resource.metadata),
            },
        )
        return not existing

    def _create_drift_report(
        self,
        connection: Connection,
        project_id: str,
        provider_account_id: Optional[str],
        *,
        classification: Optional[str] = None,
        summary: Optional[str] = None,
        unmanaged_count: Optional[int] = None,
        orphaned_count: Optional[int] = None,
        recommendations: Optional[list[dict[str, Any]]] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Persist a drift report.

        Called without keyword overrides, this is the reconciliation-loop path:
        it classifies the synced external_resources inventory.  The Terraform
        drift-sync path (BOSS-69) precomputes its own three-way comparison and
        passes classification/summary/counts/detail explicitly while reusing
        the same table, vocabulary, and serializer.
        """
        if classification is None:
            counts = connection.execute(
                text(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE ownership = 'discovered_unmanaged') AS unmanaged_count,
                        COUNT(*) FILTER (WHERE ownership = 'orphaned') AS orphaned_count
                    FROM external_resources
                    WHERE project_id = :project_id AND provider_account_id = :provider_account_id
                    """
                ),
                {
                    "project_id": project_id,
                    "provider_account_id": provider_account_id,
                },
            ).mappings().one()
            unmanaged_count = int(counts["unmanaged_count"] or 0)
            orphaned_count = int(counts["orphaned_count"] or 0)
            classification = (
                "cost-impacting" if unmanaged_count or orphaned_count else "benign"
            )
            recommendations = []
            if unmanaged_count:
                recommendations.append(
                    {
                        "action": "classify_or_import_resources",
                        "count": unmanaged_count,
                    }
                )
            if orphaned_count:
                recommendations.append(
                    {
                        "action": "propose_cleanup_changeset",
                        "count": orphaned_count,
                        "requires_approval": True,
                    }
                )
            summary = (
                f"{unmanaged_count} unmanaged and {orphaned_count} orphaned resources found."
            )
        report_id = make_id("drift")
        connection.execute(
            text(
                """
                INSERT INTO drift_reports (
                    id, project_id, provider_account_id, classification, summary,
                    unmanaged_count, orphaned_count, recommendations, detail
                )
                VALUES (
                    :id, :project_id, :provider_account_id, :classification, :summary,
                    :unmanaged_count, :orphaned_count, CAST(:recommendations AS JSONB),
                    CAST(:detail AS JSONB)
                )
                """
            ),
            {
                "id": report_id,
                "project_id": project_id,
                "provider_account_id": provider_account_id,
                "classification": classification,
                "summary": summary,
                "unmanaged_count": int(unmanaged_count or 0),
                "orphaned_count": int(orphaned_count or 0),
                "recommendations": json.dumps(recommendations or []),
                "detail": json.dumps(detail or {}),
            },
        )
        return self._serialize_drift_report(
            connection.execute(
                text(
                    """
                    SELECT id, project_id, provider_account_id, classification, summary,
                           unmanaged_count, orphaned_count, security_impacting_count,
                           production_impacting_count, recommendations, detail, created_at
                    FROM drift_reports
                    WHERE id = :id
                    """
                ),
                {"id": report_id},
            ).mappings().one()
        )

    def _create_deadletter(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_kind: str,
        source_id: str,
        reason: str,
        payload: dict[str, Any],
    ) -> str:
        deadletter_id = make_id("dlq")
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (:id, :project_id, :source_kind, :source_id, :reason, CAST(:payload AS JSONB))
                """
            ),
            {
                "id": deadletter_id,
                "project_id": project_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "reason": reason,
                "payload": json.dumps(payload),
            },
        )
        emit_event(
            connection,
            project_id,
            "deadletter.created",
            "resource_plane",
            source_kind,
            source_id,
            {"deadletter_id": deadletter_id, "reason": reason},
        )
        return deadletter_id

    def _test_connection(
        self,
        provider: str,
        credential: str,
        connection_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if provider == "aws":
            sts = self._aws_client("sts", credential)
            payload = sts.get_caller_identity()
            status_code = int(
                ((payload.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 200)
                if isinstance(payload, dict)
                else 200
            )
            return {
                "provider": provider,
                "status_code": status_code,
                "summary": self._connection_summary(provider, payload),
            }
        if provider == "azure":
            try:
                from .azure_adapter import AzureAdapter
            except ImportError as exc:
                raise ProviderAdapterError(
                    "Azure adapter is not installed (azure_adapter.py missing)."
                ) from exc
            adapter = AzureAdapter(self.settings)
            payload = adapter.test_connection(
                tenant_id=str(self.settings.azure_tenant_id or ""),
                client_id=str(self.settings.azure_client_id or ""),
                client_secret=str(credential or self.settings.azure_client_secret or ""),
                subscription_id=str(self.settings.azure_subscription_id or ""),
            )
            return {
                "provider": provider,
                "status_code": int(payload.get("status_code") or 200),
                "summary": self._connection_summary(provider, payload),
            }
        if provider in {"ovhcloud", "ovh"}:
            try:
                from .ovh_adapter import OvhAdapter
            except ImportError as exc:
                raise ProviderAdapterError(
                    "OVH adapter is not installed (ovh_adapter.py missing)."
                ) from exc
            adapter = OvhAdapter(self.settings)
            payload = adapter.test_connection(
                application_key=str(self.settings.ovh_application_key or ""),
                application_secret=str(
                    credential or self.settings.ovh_application_secret or ""
                ),
                consumer_key=str(self.settings.ovh_consumer_key or ""),
                endpoint=str(self.settings.ovh_endpoint or ""),
            )
            return {
                "provider": provider,
                "status_code": int(payload.get("status_code") or 200),
                "summary": self._connection_summary(provider, payload),
            }
        headers = self._provider_headers(provider, credential)
        with httpx.Client(timeout=10) as client:
            if provider == "digitalocean":
                response = client.get(
                    "https://api.digitalocean.com/v2/account",
                    headers=headers,
                )
            elif provider == "cloudflare":
                response = client.get(
                    "https://api.cloudflare.com/client/v4/user/tokens/verify",
                    headers=headers,
                )
            elif provider == "supabase":
                response = client.get(
                    "https://api.supabase.com/v1/projects",
                    headers=headers,
                )
            elif provider in {"openai", "openai_compatible"}:
                metadata = connection_metadata or {}
                base = str(
                    metadata.get("base_url") or "https://api.openai.com"
                ).rstrip("/")
                models_path = "/models" if base.endswith("/v1") else "/v1/models"
                response = client.get(f"{base}{models_path}", headers=headers)
            elif provider == "anthropic":
                metadata = connection_metadata or {}
                base = str(
                    metadata.get("base_url") or "https://api.anthropic.com"
                ).rstrip("/")
                models_path = "/models" if base.endswith("/v1") else "/v1/models"
                response = client.get(f"{base}{models_path}", headers=headers)
            else:
                raise ProviderAdapterError(f"Provider '{provider}' is not supported.")
            response.raise_for_status()
            payload = response.json()
        return {
            "provider": provider,
            "status_code": response.status_code,
            "summary": self._connection_summary(provider, payload),
        }

    def _sync_inventory(
        self,
        provider: str,
        credential: str,
    ) -> list[InventoryResource]:
        if provider == "aws":
            return self._aws_resources(credential)
        if provider == "azure":
            try:
                from .azure_adapter import AzureAdapter
            except ImportError as exc:
                raise ProviderAdapterError(
                    "Azure adapter is not installed (azure_adapter.py missing)."
                ) from exc
            raw = AzureAdapter(self.settings).list_resources(
                tenant_id=str(self.settings.azure_tenant_id or ""),
                client_id=str(self.settings.azure_client_id or ""),
                client_secret=str(
                    credential or self.settings.azure_client_secret or ""
                ),
                subscription_id=str(self.settings.azure_subscription_id or ""),
            )
            out: list[InventoryResource] = []
            for item in raw:
                if not (item.get("id") or item.get("external_id") or item.get("name")):
                    continue
                tags: list[str]
                if isinstance(item.get("tags"), dict):
                    tags = [f"{k}:{v}" for k, v in item["tags"].items()]
                else:
                    tags = [str(t) for t in (item.get("tags") or [])]
                meta = dict(item) if isinstance(item, dict) else {}
                meta.setdefault(
                    "region", str(item.get("location") or item.get("region") or "")
                )
                out.append(
                    InventoryResource(
                        provider="azure",
                        resource_type=str(
                            item.get("type")
                            or item.get("resource_type")
                            or "azure_resource"
                        ),
                        external_id=str(
                            item.get("id") or item.get("external_id") or ""
                        ),
                        name=str(item.get("name") or item.get("id") or "azure"),
                        state=str(item.get("state") or "active"),
                        ownership=str(item.get("ownership") or "unknown"),
                        environment=str(item.get("environment") or "unknown"),
                        health_status=str(
                            item.get("health_status") or item.get("status") or "unknown"
                        ),
                        tags=tags,
                        metadata=meta,
                    )
                )
            return out
        if provider in {"ovhcloud", "ovh"}:
            try:
                from .ovh_adapter import OvhAdapter
            except ImportError as exc:
                raise ProviderAdapterError(
                    "OVH adapter is not installed (ovh_adapter.py missing)."
                ) from exc
            raw = OvhAdapter(self.settings).inventory(
                application_key=str(self.settings.ovh_application_key or ""),
                application_secret=str(
                    credential or self.settings.ovh_application_secret or ""
                ),
                consumer_key=str(self.settings.ovh_consumer_key or ""),
                endpoint=str(self.settings.ovh_endpoint or ""),
            )
            out = []
            for item in raw:
                if not (
                    item.get("id") or item.get("external_id") or item.get("name")
                ):
                    continue
                meta = dict(item) if isinstance(item, dict) else {}
                meta.setdefault(
                    "region",
                    str(item.get("region") or item.get("datacenter") or ""),
                )
                out.append(
                    InventoryResource(
                        provider="ovhcloud",
                        resource_type=str(
                            item.get("type")
                            or item.get("resource_type")
                            or "ovh_resource"
                        ),
                        external_id=str(
                            item.get("id")
                            or item.get("external_id")
                            or item.get("name")
                            or ""
                        ),
                        name=str(item.get("name") or item.get("id") or "ovh"),
                        state=str(
                            item.get("state") or item.get("status") or "active"
                        ),
                        ownership=str(item.get("ownership") or "unknown"),
                        environment=str(item.get("environment") or "unknown"),
                        health_status=str(
                            item.get("health_status")
                            or item.get("status")
                            or "unknown"
                        ),
                        tags=[str(t) for t in (item.get("tags") or [])],
                        metadata=meta,
                    )
                )
            return out
        headers = self._provider_headers(provider, credential)
        with httpx.Client(timeout=20) as client:
            if provider == "digitalocean":
                response = client.get(
                    "https://api.digitalocean.com/v2/droplets?per_page=200",
                    headers=headers,
                )
                response.raise_for_status()
                return self._digitalocean_resources(response.json())
            if provider == "cloudflare":
                response = client.get(
                    "https://api.cloudflare.com/client/v4/zones?per_page=50",
                    headers=headers,
                )
                response.raise_for_status()
                return self._cloudflare_resources(response.json())
            if provider == "supabase":
                response = client.get(
                    "https://api.supabase.com/v1/projects",
                    headers=headers,
                )
                response.raise_for_status()
                return self._supabase_resources(response.json())
            if provider in {"openai", "anthropic", "openai_compatible"}:
                self._test_connection(provider, credential)
                return []
        raise ProviderAdapterError(
            f"Inventory sync is not supported by the configured ProviderAdapter for '{provider}'."
        )

    def _sync_billing(
        self,
        provider: str,
        credential: str,
    ) -> list[ProviderCostRecord]:
        if provider == "aws":
            return self._aws_cost_records(credential)
        headers = self._provider_headers(provider, credential)
        start, end = self._default_billing_window()
        with httpx.Client(timeout=20) as client:
            if provider == "digitalocean":
                response = client.get(
                    "https://api.digitalocean.com/v2/customers/my/billing_history",
                    headers=headers,
                    params={"per_page": 200},
                )
                response.raise_for_status()
                return self._digitalocean_cost_records(response.json())
            if provider == "openai":
                response = client.get(
                    "https://api.openai.com/v1/organization/costs",
                    headers=headers,
                    params={
                        "start_time": int(start.timestamp()),
                        "end_time": int(end.timestamp()),
                        "bucket_width": "1d",
                        "limit": 31,
                    },
                )
                response.raise_for_status()
                return self._openai_cost_records(response.json())
            if provider == "anthropic":
                response = client.get(
                    "https://api.anthropic.com/v1/organizations/cost_report",
                    headers=headers,
                    params=[
                        ("starting_at", self._rfc3339(start)),
                        ("ending_at", self._rfc3339(end)),
                        ("bucket_width", "1d"),
                        ("limit", "31"),
                        ("group_by[]", "model"),
                        ("group_by[]", "workspace_id"),
                        ("group_by[]", "cost_type"),
                    ],
                )
                response.raise_for_status()
                return self._anthropic_cost_records(response.json())
        raise ProviderAdapterError(
            f"Billing sync is not supported by the configured ProviderAdapter for '{provider}'."
        )

    def _provider_supports_billing(self, provider: str) -> bool:
        return provider in {"aws", "digitalocean", "openai", "anthropic"}

    def _provider_headers(self, provider: str, credential: str) -> dict[str, str]:
        if provider == "anthropic":
            return {
                "x-api-key": credential,
                "anthropic-version": "2023-06-01",
                "Accept": "application/json",
            }
        return {
            "Authorization": f"Bearer {credential}",
            "Accept": "application/json",
        }

    def _aws_client(self, service: str, credential: str) -> Any:
        try:
            import boto3  # noqa: PLC0415 — lazy import; optional provider dependency
        except ImportError as error:
            raise ProviderAdapterError(
                "The AWS provider adapter requires the 'boto3' package. "
                "Install it (pip install boto3) to enable aws.* capabilities."
            ) from error
        access_key_id = self.settings.aws_access_key_id or os.environ.get(
            "BOSS_AWS_ACCESS_KEY_ID"
        )
        if not access_key_id:
            raise ProviderCredentialError(
                "Missing AWS access key id (BOSS_AWS_ACCESS_KEY_ID)."
            )
        region = self.settings.aws_region
        session = boto3.session.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=credential,
            region_name=region,
        )
        role_arn = self.settings.aws_role_arn
        if role_arn:
            assumed = session.client("sts").assume_role(
                RoleArn=role_arn,
                RoleSessionName="boss-resource-plane",
            )
            assumed_credentials = assumed.get("Credentials") or {}
            session = boto3.session.Session(
                aws_access_key_id=assumed_credentials.get("AccessKeyId"),
                aws_secret_access_key=assumed_credentials.get("SecretAccessKey"),
                aws_session_token=assumed_credentials.get("SessionToken"),
                region_name=region,
            )
        return session.client(service)

    def _aws_error_code(self, error: Exception) -> Optional[str]:
        response = getattr(error, "response", None)
        if isinstance(response, dict):
            error_payload = response.get("Error")
            if isinstance(error_payload, dict):
                code = error_payload.get("Code")
                return str(code) if code else None
        return None

    def _aws_tag_strings(self, aws_tags: Any) -> list[str]:
        tags: list[str] = []
        for tag in aws_tags if isinstance(aws_tags, list) else []:
            if not isinstance(tag, dict):
                continue
            key = str(tag.get("Key") or "").strip()
            if not key:
                continue
            value = str(tag.get("Value") or "").strip()
            if value and value.lower() not in {"true", "1", "yes"}:
                tags.append(f"{key}:{value}")
            else:
                # Flag-like tags (boss-managed=true) collapse to the bare key so
                # ownership/environment tag heuristics match them.
                tags.append(key)
        return tags

    def _connection_summary(self, provider: str, payload: Any) -> dict[str, Any]:
        if provider == "aws":
            identity = payload if isinstance(payload, dict) else {}
            return {
                "account_id": identity.get("Account"),
                "arn": identity.get("Arn"),
            }
        if provider == "digitalocean":
            account = payload.get("account") if isinstance(payload, dict) else {}
            return {
                "account_status": account.get("status"),
                "email_verified": account.get("email_verified"),
            }
        if provider == "cloudflare":
            result = payload.get("result") if isinstance(payload, dict) else {}
            return {
                "token_status": result.get("status"),
                "token_id_present": bool(result.get("id")),
            }
        if provider == "supabase":
            return {"projects_visible": len(payload) if isinstance(payload, list) else None}
        if provider in {"openai", "anthropic"}:
            data = payload.get("data") if isinstance(payload, dict) else []
            return {"models_visible": len(data) if isinstance(data, list) else None}
        return {}

    def _digitalocean_resources(self, payload: dict[str, Any]) -> list[InventoryResource]:
        resources: list[InventoryResource] = []
        for droplet in payload.get("droplets") or []:
            tags = [str(tag) for tag in droplet.get("tags") or []]
            resources.append(
                InventoryResource(
                    provider="digitalocean",
                    resource_type="droplet",
                    external_id=str(droplet.get("id")),
                    name=str(droplet.get("name") or droplet.get("id")),
                    state=self._resource_state_from_provider_status(
                        str(droplet.get("status") or "active")
                    ),
                    ownership=self._ownership_from_tags(tags),
                    environment=self._environment_from_tags(tags),
                    health_status=str(droplet.get("status") or "unknown"),
                    tags=tags,
                    metadata={
                        "region": (droplet.get("region") or {}).get("slug"),
                        "size": (droplet.get("size") or {}).get("slug"),
                        "image": (droplet.get("image") or {}).get("slug"),
                        "networks": droplet.get("networks") or {},
                        # Surface VPC for topology inference (BOSS-240).
                        "vpc_uuid": droplet.get("vpc_uuid"),
                        "vpc_id": droplet.get("vpc_uuid"),
                    },
                )
            )
        return resources

    def _cloudflare_resources(self, payload: dict[str, Any]) -> list[InventoryResource]:
        resources: list[InventoryResource] = []
        for zone in payload.get("result") or []:
            resources.append(
                InventoryResource(
                    provider="cloudflare",
                    resource_type="zone",
                    external_id=str(zone.get("id")),
                    name=str(zone.get("name") or zone.get("id")),
                    state="active" if zone.get("status") == "active" else "degraded",
                    ownership="discovered_unmanaged",
                    environment="unknown",
                    health_status=str(zone.get("status") or "unknown"),
                    tags=[],
                    metadata={
                        "account": zone.get("account") or {},
                        "plan": zone.get("plan") or {},
                    },
                )
            )
        return resources

    def _supabase_resources(self, payload: Any) -> list[InventoryResource]:
        resources: list[InventoryResource] = []
        for project in payload if isinstance(payload, list) else []:
            resources.append(
                InventoryResource(
                    provider="supabase",
                    resource_type="project",
                    external_id=str(project.get("id") or project.get("ref")),
                    name=str(project.get("name") or project.get("id") or project.get("ref")),
                    state="active",
                    ownership="discovered_unmanaged",
                    environment=str(project.get("region") or "unknown"),
                    health_status=str(project.get("status") or "unknown"),
                    tags=[],
                    metadata={
                        "organization_id": project.get("organization_id"),
                        "region": project.get("region"),
                        "created_at": project.get("created_at"),
                    },
                )
            )
        return resources

    def _aws_resources(self, credential: str) -> list[InventoryResource]:
        region = self.settings.aws_region
        resources: list[InventoryResource] = []

        ec2 = self._aws_client("ec2", credential)
        ec2_payload = ec2.describe_instances()
        for reservation in ec2_payload.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                tags = self._aws_tag_strings(instance.get("Tags"))
                name = next(
                    (
                        str(tag.get("Value"))
                        for tag in instance.get("Tags") or []
                        if isinstance(tag, dict) and tag.get("Key") == "Name"
                    ),
                    None,
                )
                status = str(
                    (instance.get("State") or {}).get("Name") or "unknown"
                )
                resources.append(
                    InventoryResource(
                        provider="aws",
                        resource_type="ec2_instance",
                        external_id=str(instance.get("InstanceId")),
                        name=str(name or instance.get("InstanceId")),
                        state=self._resource_state_from_provider_status(status),
                        ownership=self._ownership_from_tags(tags),
                        environment=self._environment_from_tags(tags),
                        health_status=status,
                        tags=tags,
                        metadata={
                            "region": region,
                            "instance_type": instance.get("InstanceType"),
                            "availability_zone": (
                                instance.get("Placement") or {}
                            ).get("AvailabilityZone"),
                            "image_id": instance.get("ImageId"),
                            "vpc_id": instance.get("VpcId"),
                            "subnet_id": instance.get("SubnetId"),
                            "private_ip": instance.get("PrivateIpAddress"),
                            "public_ip": instance.get("PublicIpAddress"),
                        },
                    )
                )

        s3 = self._aws_client("s3", credential)
        s3_payload = s3.list_buckets()
        for bucket in s3_payload.get("Buckets") or []:
            bucket_name = str(bucket.get("Name"))
            creation_date = bucket.get("CreationDate")
            resources.append(
                InventoryResource(
                    provider="aws",
                    resource_type="s3_bucket",
                    external_id=bucket_name,
                    name=bucket_name,
                    state="active",
                    ownership="discovered_unmanaged",
                    environment="unknown",
                    health_status="active",
                    tags=[],
                    metadata={
                        "region": region,
                        "creation_date": creation_date.isoformat()
                        if hasattr(creation_date, "isoformat")
                        else creation_date,
                    },
                )
            )

        rds = self._aws_client("rds", credential)
        rds_payload = rds.describe_db_instances()
        for db_instance in rds_payload.get("DBInstances") or []:
            tags = self._aws_tag_strings(db_instance.get("TagList"))
            status = str(db_instance.get("DBInstanceStatus") or "unknown")
            resources.append(
                InventoryResource(
                    provider="aws",
                    resource_type="rds_instance",
                    external_id=str(db_instance.get("DBInstanceIdentifier")),
                    name=str(db_instance.get("DBInstanceIdentifier")),
                    state="active"
                    if status == "available"
                    else self._resource_state_from_provider_status(status),
                    ownership=self._ownership_from_tags(tags),
                    environment=self._environment_from_tags(tags),
                    health_status=status,
                    tags=tags,
                    metadata={
                        "region": region,
                        "engine": db_instance.get("Engine"),
                        "engine_version": db_instance.get("EngineVersion"),
                        "instance_class": db_instance.get("DBInstanceClass"),
                        "allocated_storage_gb": db_instance.get(
                            "AllocatedStorage"
                        ),
                        "endpoint": (db_instance.get("Endpoint") or {}).get(
                            "Address"
                        ),
                        "multi_az": db_instance.get("MultiAZ"),
                    },
                )
            )
        return resources

    def _aws_cost_records(self, credential: str) -> list[ProviderCostRecord]:
        start, end = self._default_billing_window()
        cost_explorer = self._aws_client("ce", credential)
        payload = cost_explorer.get_cost_and_usage(
            TimePeriod={
                "Start": start.date().isoformat(),
                "End": end.date().isoformat(),
            },
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        records: list[ProviderCostRecord] = []
        for bucket_index, bucket in enumerate(payload.get("ResultsByTime") or []):
            time_period = bucket.get("TimePeriod") or {}
            period_start = self._coerce_datetime(time_period.get("Start"))
            period_end = self._coerce_datetime(time_period.get("End"))
            for group_index, group in enumerate(bucket.get("Groups") or []):
                keys = group.get("Keys") or []
                service = str(keys[0]) if keys else f"group-{group_index}"
                metric = (group.get("Metrics") or {}).get("UnblendedCost") or {}
                unit = str(metric.get("Unit") or "USD").upper()
                if unit != "USD":
                    raise ProviderAdapterError(
                        f"AWS cost unit '{unit}' cannot be stored as USD."
                    )
                amount = self._amount_to_usd_micros(metric.get("Amount"))
                if amount is None or amount == 0:
                    continue
                records.append(
                    ProviderCostRecord(
                        provider="aws",
                        category=service,
                        amount_usd_micros=amount,
                        cost_layer="observed",
                        confidence="provider_reported",
                        source_ref=(
                            "aws.cost_explorer:"
                            f"{time_period.get('Start') or bucket_index}:{service}"
                        ),
                        period_start=period_start,
                        period_end=period_end,
                        billing_account_external_id="aws:cost_explorer",
                        billing_account_label="AWS Cost Explorer",
                        metadata={
                            "provider_source": "cost_explorer",
                            "granularity": "DAILY",
                            "metric": "UnblendedCost",
                            "service": service,
                            "amount": metric.get("Amount"),
                            "unit": unit,
                            "estimated": bucket.get("Estimated"),
                        },
                    )
                )
        return records

    def _digitalocean_cost_records(
        self,
        payload: dict[str, Any],
    ) -> list[ProviderCostRecord]:
        records: list[ProviderCostRecord] = []
        for index, entry in enumerate(payload.get("billing_history") or []):
            amount = self._amount_to_usd_micros(entry.get("amount"))
            if amount is None:
                continue
            period_start = self._coerce_datetime(
                entry.get("date") or entry.get("invoice_period")
            )
            invoice_uuid = (
                entry.get("invoice_uuid")
                or entry.get("invoice_id")
                or entry.get("id")
                or f"history-{index}"
            )
            records.append(
                ProviderCostRecord(
                    provider="digitalocean",
                    category="infra",
                    amount_usd_micros=amount,
                    cost_layer="reconciled"
                    if str(entry.get("type") or "").lower() == "invoice"
                    else "observed",
                    confidence="invoice_reconciled"
                    if str(entry.get("type") or "").lower() == "invoice"
                    else "provider_reported",
                    source_ref=f"digitalocean.billing_history:{invoice_uuid}",
                    period_start=period_start,
                    period_end=None,
                    billing_account_external_id="digitalocean:customer:my",
                    billing_account_label="DigitalOcean Customer Billing",
                    metadata={
                        "provider_source": "billing_history",
                        "type": entry.get("type"),
                        "description": entry.get("description"),
                        "invoice_uuid": entry.get("invoice_uuid"),
                        "raw_amount": entry.get("amount"),
                    },
                )
            )
        return records

    def _openai_cost_records(
        self,
        payload: dict[str, Any],
    ) -> list[ProviderCostRecord]:
        records: list[ProviderCostRecord] = []
        for bucket_index, bucket in enumerate(payload.get("data") or []):
            period_start = self._coerce_datetime(bucket.get("start_time"))
            period_end = self._coerce_datetime(bucket.get("end_time"))
            for result_index, result in enumerate(bucket.get("results") or []):
                amount_payload = result.get("amount") or {}
                currency = str(amount_payload.get("currency") or "usd").lower()
                if currency != "usd":
                    raise ProviderAdapterError(
                        f"OpenAI cost currency '{currency}' cannot be stored as USD."
                    )
                amount = self._amount_to_usd_micros(amount_payload.get("value"))
                if amount is None or amount == 0:
                    continue
                discriminator = (
                    result.get("line_item")
                    or result.get("project_id")
                    or result.get("api_key_id")
                    or result.get("model")
                    or result_index
                )
                records.append(
                    ProviderCostRecord(
                        provider="openai",
                        category="model",
                        amount_usd_micros=amount,
                        cost_layer="observed",
                        confidence="provider_reported",
                        source_ref=(
                            "openai.organization.costs:"
                            f"{period_start.isoformat() if period_start else bucket_index}:"
                            f"{discriminator}"
                        ),
                        period_start=period_start,
                        period_end=period_end,
                        billing_account_external_id="openai:organization",
                        billing_account_label="OpenAI Organization Costs",
                        metadata={
                            "provider_source": "organization_costs",
                            "currency": currency,
                            "amount_value": amount_payload.get("value"),
                            "api_key_id": result.get("api_key_id"),
                            "project_id": result.get("project_id"),
                            "model": result.get("model"),
                            "line_item": result.get("line_item"),
                        },
                    )
                )
        return records

    def _anthropic_cost_records(
        self,
        payload: dict[str, Any],
    ) -> list[ProviderCostRecord]:
        records: list[ProviderCostRecord] = []
        for bucket_index, bucket in enumerate(payload.get("data") or []):
            period_start = self._coerce_datetime(bucket.get("starting_at"))
            period_end = self._coerce_datetime(bucket.get("ending_at"))
            for result_index, result in enumerate(bucket.get("results") or []):
                currency = str(result.get("currency") or "USD").lower()
                if currency != "usd":
                    raise ProviderAdapterError(
                        f"Anthropic cost currency '{currency}' cannot be stored as USD."
                    )
                amount = self._amount_to_usd_micros(
                    result.get("amount"),
                    amount_unit="minor",
                )
                if amount is None or amount == 0:
                    continue
                discriminator = (
                    result.get("workspace_id")
                    or result.get("api_key_id")
                    or result.get("model")
                    or result.get("cost_type")
                    or result_index
                )
                records.append(
                    ProviderCostRecord(
                        provider="anthropic",
                        category="model",
                        amount_usd_micros=amount,
                        cost_layer="observed",
                        confidence="provider_reported",
                        source_ref=(
                            "anthropic.organizations.cost_report:"
                            f"{period_start.isoformat() if period_start else bucket_index}:"
                            f"{discriminator}:{result.get('cost_type') or 'cost'}"
                        ),
                        period_start=period_start,
                        period_end=period_end,
                        billing_account_external_id="anthropic:organization",
                        billing_account_label="Anthropic Organization Costs",
                        metadata={
                            "provider_source": "organizations_cost_report",
                            "currency": currency,
                            "amount": result.get("amount"),
                            "amount_unit": "lowest_currency_unit",
                            "workspace_id": result.get("workspace_id"),
                            "api_key_id": result.get("api_key_id"),
                            "model": result.get("model"),
                            "cost_type": result.get("cost_type"),
                            "service_tier": result.get("service_tier"),
                            "context_window": result.get("context_window"),
                        },
                    )
                )
        return records

    def _default_billing_window(self) -> tuple[datetime, datetime]:
        end = now_utc().replace(microsecond=0)
        start = end - timedelta(days=1)
        return start, end

    def _rfc3339(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _coerce_datetime(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        text_value = str(value).strip()
        if not text_value:
            return None
        if text_value.isdigit():
            return datetime.fromtimestamp(int(text_value), tz=timezone.utc)
        normalized = text_value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(f"{text_value}T00:00:00+00:00")
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _amount_to_usd_micros(
        self,
        value: Any,
        *,
        amount_unit: str = "major",
    ) -> Optional[int]:
        if value is None:
            return None
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if amount_unit == "minor":
            amount = amount / Decimal("100")
        return int(
            (amount * Decimal("1000000")).to_integral_value(rounding=ROUND_HALF_UP)
        )

    def _validate_operations(
        self,
        account: Optional[dict[str, Any]],
        operations: list[dict[str, Any]],
        rollback_plan: list[str],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in operations:
            skill_id = self._require_text(str(raw.get("skill_id") or ""), "skill_id")
            action = self._require_text(str(raw.get("action") or ""), "action")
            resource_type = self._require_text(
                str(raw.get("resource_type") or ""),
                "resource_type",
            )
            destructive = bool(raw.get("destructive"))
            production_affecting = bool(raw.get("production_affecting"))
            if account and account["read_only"] and action not in {"read", "sync"}:
                raise ValueError("Provider account is read-only; provider writes are blocked.")
            if account and account["destructive_actions_disabled"] and destructive:
                raise ValueError(
                    "Provider account has destructive actions disabled."
                )
            if (destructive or production_affecting) and not rollback_plan:
                raise ValueError(
                    "Destructive or production-affecting operations require a rollback plan."
                )
            normalized.append(
                {
                    "skill_id": skill_id,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_ref": raw.get("resource_ref"),
                    "desired_state": raw.get("desired_state") or {},
                    "risk_tier": int(raw.get("risk_tier") or 2),
                    "destructive": destructive,
                    "production_affecting": production_affecting,
                }
            )
        return normalized

    def _operations_need_approval(self, operations: list[dict[str, Any]]) -> bool:
        return any(
            bool(operation.get("destructive"))
            or bool(operation.get("production_affecting"))
            or int(operation.get("risk_tier") or 0) >= 2
            for operation in operations
        )

    def _operation_dedupe_key(
        self,
        changeset_id: str,
        operation_index: int,
        operation: dict[str, Any],
    ) -> str:
        return (
            "resource_plane:"
            f"{changeset_id}:"
            f"{operation_index}:"
            f"{operation.get('skill_id')}:"
            f"{operation.get('action')}:"
            f"{operation.get('resource_ref') or operation.get('resource_type')}"
        )

    def _scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            raise ValueError("Project not found.")
        return dict(row)

    def _credential_value(self, credential_ref: Optional[str]) -> str:
        if not credential_ref:
            raise ProviderCredentialError("Provider account has no credential_ref.")
        try:
            ref_kind, ref_key = self.secret_refs.parse_credential_ref(credential_ref)
        except ValueError as error:
            raise ProviderCredentialError(str(error)) from error
        if ref_kind == "env":
            value_map = {
                "OPENAI_API_KEY": self.settings.openai_api_key,
                "ANTHROPIC_API_KEY": self.settings.anthropic_api_key,
                "BOSS_DIGITALOCEAN_TOKEN": self.settings.digitalocean_token,
                "BOSS_CLOUDFLARE_API_TOKEN": self.settings.cloudflare_api_token,
                "BOSS_SUPABASE_ACCESS_TOKEN": self.settings.supabase_access_token,
            }
            value = value_map.get(ref_key) or os.environ.get(ref_key)
            if not value:
                raise ProviderCredentialError(
                    f"Missing credential for SecretRef env:{ref_key}."
                )
            return value
        from .secret_backends import SecretUnavailable, backend_for

        try:
            return backend_for(ref_kind, self.settings).get(ref_key)
        except SecretUnavailable as error:
            raise ProviderCredentialError(str(error)) from error

    def _env_secret_value(self, secret_ref: str) -> str:
        ref_kind, _, ref_key = secret_ref.partition(":")
        if ref_kind != "env" or not ref_key:
            raise ProviderCredentialError(
                "Only env SecretRef values are supported in this runtime slice."
            )
        value = os.environ.get(ref_key)
        if not value:
            raise ProviderCredentialError(
                f"Missing secret for SecretRef env:{ref_key}."
            )
        return value

    def _normalize_provider(self, provider: str) -> str:
        normalized = self._require_text(provider, "provider").lower()
        if normalized not in SUPPORTED_PROVIDERS:
            raise ValueError(
                "provider must be one of: "
                + ", ".join(sorted(SUPPORTED_PROVIDERS))
            )
        return normalized

    def _normalize_provider_credentials(
        self,
        provider: str,
        *,
        auth_method: Optional[str],
        credential_ref: Optional[str],
        credential_refs: dict[str, str],
        apply_defaults: bool = True,
    ) -> tuple[str, dict[str, str], Optional[str], dict[str, str]]:
        profile = PROVIDER_CONNECTION_PROFILES.get(provider)
        if not profile:
            method_key = (auth_method or "api_token").strip().lower()
            if method_key != "api_token":
                raise ValueError(
                    f"{provider} supports only auth_method='api_token' in this runtime."
                )
            primary = (
                credential_ref or SUPPORTED_PROVIDERS[provider]["credential_ref"]
            ).strip()
            refs = dict(credential_refs or {})
            if refs and set(refs) != {"primary"}:
                raise ValueError(
                    f"{provider} credential_refs supports only the 'primary' slot."
                )
            refs["primary"] = refs.get("primary") or primary
            self.secret_refs.parse_credential_ref(refs["primary"])
            return method_key, refs, refs["primary"], {"primary": "api_token"}

        method_key = (
            auth_method or profile["default_auth_method"]
        ).strip().lower()
        method = profile["auth_methods"].get(method_key)
        if not method:
            raise ValueError(
                f"{provider} auth_method must be one of: "
                + ", ".join(sorted(profile["auth_methods"]))
                + "."
            )
        slots = {slot["key"]: slot for slot in method["credential_slots"]}
        refs = {
            str(key).strip(): str(value).strip()
            for key, value in (credential_refs or {}).items()
            if str(key).strip() and str(value).strip()
        }
        unknown = sorted(set(refs) - set(slots))
        if unknown:
            raise ValueError(
                f"Unknown credential slot(s) for {provider}/{method_key}: "
                + ", ".join(unknown)
                + "."
            )
        primary_slot = next(
            (slot["key"] for slot in method["credential_slots"] if slot.get("primary")),
            next(iter(slots), None),
        )
        if credential_ref:
            if not primary_slot:
                raise ValueError(
                    f"{provider}/{method_key} does not accept a primary credential_ref."
                )
            refs[primary_slot] = credential_ref.strip()
        if apply_defaults:
            for slot in method["credential_slots"]:
                if slot.get("default_ref") and slot["key"] not in refs:
                    refs[slot["key"]] = slot["default_ref"]
        missing = sorted(
            slot["key"]
            for slot in method["credential_slots"]
            if slot["required"] and slot["key"] not in refs
        )
        if missing:
            raise ValueError(
                f"Missing required credential slot(s) for {provider}/{method_key}: "
                + ", ".join(missing)
                + "."
            )
        for pointer in refs.values():
            self.secret_refs.parse_credential_ref(pointer)
        primary_ref = refs.get(primary_slot) if primary_slot else None
        slot_kinds = {
            slot["key"]: slot["secret_kind"] for slot in method["credential_slots"]
        }
        return method_key, refs, primary_ref, slot_kinds

    def _provider_adapter_status(self, provider: str) -> str:
        profile = PROVIDER_CONNECTION_PROFILES.get(provider)
        return str(profile.get("adapter_status") if profile else "available")

    def _require_account_secret_refs_usable(
        self,
        connection: Connection,
        account: dict[str, Any],
        *,
        check_account_state: bool = True,
    ) -> None:
        account_status = str(account.get("status") or "active")
        if check_account_state and account_status in {
            "disabled",
            "revoked",
            "pending_adapter",
        }:
            raise ProviderCredentialError(
                f"Provider account is {account_status}; provider operation blocked."
            )
        if connection is None:
            return
        refs = dict(account.get("credential_refs") or {})
        if account.get("credential_ref") and not refs:
            refs = {"primary": account["credential_ref"]}
        for pointer in set(refs.values()):
            try:
                item = self.secret_refs.require_usable(
                    connection,
                    project_id=account["project_id"],
                    credential_ref=pointer,
                )
            except (SecretRefNotFoundError, ValueError) as error:
                raise ProviderCredentialError(str(error)) from error
            if item["provider"] not in {None, account["provider"]}:
                raise ProviderCredentialError(
                    f"SecretRef {pointer!r} belongs to {item['provider']}, "
                    f"not {account['provider']}."
                )

    @staticmethod
    def _safe_provider_error(error: Exception) -> dict[str, str]:
        return {
            "code": error.__class__.__name__,
            "message": "Provider connection test failed; inspect SecretRef availability and provider permissions.",
        }

    def _redact_provider_error(
        self,
        error: Exception,
        account: dict[str, Any],
    ) -> str:
        message = str(error)
        refs = dict(account.get("credential_refs") or {})
        if account.get("credential_ref") and not refs:
            refs = {"primary": account["credential_ref"]}
        configured_values = {
            "OPENAI_API_KEY": self.settings.openai_api_key,
            "ANTHROPIC_API_KEY": self.settings.anthropic_api_key,
            "BOSS_DIGITALOCEAN_TOKEN": self.settings.digitalocean_token,
            "BOSS_CLOUDFLARE_API_TOKEN": self.settings.cloudflare_api_token,
            "BOSS_SUPABASE_ACCESS_TOKEN": self.settings.supabase_access_token,
            "BOSS_AWS_ACCESS_KEY_ID": self.settings.aws_access_key_id,
        }
        for pointer in refs.values():
            try:
                ref_kind, ref_key = self.secret_refs.parse_credential_ref(pointer)
            except ValueError:
                continue
            if ref_kind != "env":
                continue
            value = configured_values.get(ref_key) or os.environ.get(ref_key)
            if value:
                message = message.replace(value, "[REDACTED]")
        return message

    def _normalize_string_list(self, values: list[str]) -> list[str]:
        return [item.strip() for item in values if isinstance(item, str) and item.strip()]

    def _normalize_secret_ref_bindings(
        self, values: list[str | dict[str, Any]]
    ) -> list[str | dict[str, Any]]:
        """Keep Phase 1 dict bindings and bare string refs; drop empties only."""
        out: list[str | dict[str, Any]] = []
        for item in values or []:
            if isinstance(item, str):
                token = item.strip()
                if token:
                    out.append(token)
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                ref_id = str(item.get("secret_ref_id") or "").strip()
                legacy = str(item.get("ref") or "").strip()
                if not name:
                    continue
                binding: dict[str, str] = {"name": name}
                if ref_id:
                    binding["secret_ref_id"] = ref_id
                if legacy:
                    binding["ref"] = legacy
                if "secret_ref_id" in binding or "ref" in binding:
                    out.append(binding)
        return out

    def _require_text(self, value: str, field_name: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} must not be blank.")
        return cleaned

    def _resource_state_from_provider_status(self, status: str) -> str:
        normalized = status.lower()
        if normalized in {"active", "ok", "running"}:
            return "active"
        if normalized in {"off", "stopped"}:
            return "draining"
        return "degraded"

    def _ownership_from_tags(self, tags: list[str]) -> str:
        lowered = {tag.lower() for tag in tags}
        if "boss" in lowered or "boss-managed" in lowered:
            return "managed_by_boss"
        return "discovered_unmanaged"

    def _environment_from_tags(self, tags: list[str]) -> str:
        for tag in tags:
            lower = tag.lower()
            if lower.startswith("env:"):
                return lower.partition(":")[2] or "unknown"
            if lower in {"local", "preview", "staging", "production"}:
                return lower
        return "unknown"

    def _provider_from_skill(self, skill_id: str) -> str:
        provider, _, _ = skill_id.partition(".")
        return provider or "unknown"

    def _serialize_provider_account(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in (
            "created_at",
            "updated_at",
            "last_tested_at",
            "credential_rotated_at",
            "disabled_at",
            "revoked_at",
        ):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["auth_method"] = payload.get("auth_method") or "api_token"
        payload["credential_refs"] = payload.get("credential_refs") or (
            {"primary": payload["credential_ref"]}
            if payload.get("credential_ref")
            else {}
        )
        payload["connection_metadata"] = payload.get("connection_metadata") or {}
        payload["scopes"] = payload.get("scopes") or []
        payload["capabilities"] = payload.get("capabilities") or []
        payload["missing_permissions"] = payload.get("missing_permissions") or []
        payload["last_error"] = payload.get("last_error")
        return payload

    def _serialize_external_resource(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at", "last_seen_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["tags"] = payload.get("tags") or []
        payload["metadata"] = payload.get("metadata") or {}
        return payload

    def _serialize_cost_event(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "period_start", "period_end"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["metadata"] = payload.get("metadata") or {}
        return payload

    def _serialize_cost_anomaly(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "resolved_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["recommended_actions"] = payload.get("recommended_actions") or []
        return payload

    def _serialize_provider_health(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "last_checked_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["detail"] = payload.get("detail") or {}
        return payload

    def _serialize_drift_report(self, row) -> dict[str, Any]:
        payload = dict(row)
        payload["created_at"] = payload["created_at"].isoformat()
        payload["recommendations"] = payload.get("recommendations") or []
        payload["detail"] = payload.get("detail") or {}
        return payload

    def _serialize_reconciliation_run(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("started_at", "finished_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["error"] = payload.get("error")
        payload["metadata"] = payload.get("metadata") or {}
        return payload

    def _serialize_deployment_target(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        for key in (
            "domains",
            "required_resources",
            "secret_refs",
            "health_checks",
            "rollback_points",
        ):
            payload[key] = payload.get(key) or []
        return payload

    def _serialize_deployment_environment_policy(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["allowed_target_kinds"] = payload.get("allowed_target_kinds") or []
        payload["metadata"] = payload.get("metadata") or {}
        return payload

    def _serialize_deployment_run(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at", "started_at", "finished_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        for key in ("health_check_results", "rollback_plan", "release_receipt_ids"):
            payload[key] = payload.get(key) or []
        payload["release_packet"] = payload.get("release_packet") or {}
        return payload

    def _serialize_changeset(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at", "approved_at", "applied_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        for key in (
            "operations",
            "rollback_plan",
            "expected_receipts",
        ):
            payload[key] = payload.get(key) or []
        for key in ("cost_quote", "policy_result"):
            payload[key] = payload.get(key) or {}
        return payload

    def _serialize_resource_receipt(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["result"] = payload.get("result")
        payload["error"] = payload.get("error")
        return payload

    def _serialize_worker_pool(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["capabilities"] = payload.get("capabilities") or []
        payload["metadata"] = payload.get("metadata") or {}
        return payload

    def _serialize_scale_plan(self, row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "applied_at"):
            payload[key] = payload[key].isoformat() if payload.get(key) else None
        payload["actions"] = payload.get("actions") or []
        payload["recommendations"] = payload.get("recommendations") or []
        return payload
