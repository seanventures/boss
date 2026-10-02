from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import DATA_BOUNDARIES, emit_event, make_id, normalize_data_boundary


class SkillNotFoundError(Exception):
    pass


class EntitlementSourceNotFoundError(Exception):
    pass


class EntitlementPoolNotFoundError(Exception):
    pass


class InvalidEntitlementConnectionError(Exception):
    pass


class EntitlementConnectionSessionNotFoundError(Exception):
    pass


class EntitlementConnectionSessionStateError(Exception):
    pass


#: Who owns a subscription connected to this workspace: its operator, whoever pressed Connect.
WORKSPACE_OPERATOR_ID = "founder"


class EntitlementBrokerService:
    OFFICIAL_OAUTH_METHODS = {
        "official_cli_oauth",
        "official_device_oauth",
        "official_browser_oauth",
    }
    CAPACITY_DENIAL_STATES = {
        "blocked",
        "disabled",
        "exhausted",
        "rate_limited",
        "suspended",
        "unavailable",
    }
    GATEWAY_SOURCE_KINDS = {"managed_cloud_model_gateway"}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        edition = str(settings.boss_edition or "community").strip().lower()
        self._paid_gateway = None
        if edition in {
            "cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted"
        }:
            extension = import_module("boss_engine.services.enterprise_gateway")
            self._paid_gateway = extension.EnterpriseGatewayPolicy(settings)
        self._gateway_source_kinds = set(self.GATEWAY_SOURCE_KINDS)
        if self._paid_gateway is not None:
            self._gateway_source_kinds.add(self._paid_gateway.source_kind)

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path} did not contain an object payload")
        return payload

    def _provider_registry(self) -> dict[str, Any]:
        registry = self._load_yaml(self.settings.specpack_root / "models" / "providers.yaml")
        if self._paid_gateway is not None:
            return self._paid_gateway.merge_registry(registry)
        return registry

    def _source_record_defaults(self) -> dict[str, dict[str, Any]]:
        defaults: dict[str, dict[str, Any]] = {}
        registry = self._provider_registry()
        for provider in registry.get("providers", []):
            provider_id = provider.get("id")
            if not isinstance(provider_id, str):
                continue
            for source_record in provider.get("source_records", []):
                source_id = source_record.get("source_id")
                if not isinstance(source_id, str):
                    continue
                defaults[source_id] = {
                    **source_record,
                    "provider": provider_id,
                    "provider_description": provider.get("description", ""),
                }
        return defaults

    def _skill_files(self) -> list[Path]:
        return sorted((self.settings.specpack_root / "skills").glob("*.yaml"))

    def _edition_skill_bundles(
        self,
        connection: Connection,
        project_id: str,
    ) -> set[str]:
        # The skill catalog is edition-scoped, but selecting its pack must not
        # import the hosted billing/connection service. Edition status uses the
        # same configured edition for its source pack; the database link only
        # affects whether that edition is active, not which skills it installs.
        configured = str(self.settings.boss_edition or "community").strip().lower()
        configured = configured.replace("-", "_")
        if configured in {"enterprise", "enterprise_cloud", "enterprise_self_hosted"}:
            edition = "enterprise"
        elif configured == "cloud":
            edition = "cloud"
        else:
            edition = "community"
        packs = [
            path
            for path in sorted((self.settings.specpack_root / "packs").glob("*_edition.yaml"))
            if str(self._load_yaml(path).get("edition") or "").strip().lower()
            == edition
        ]
        if len(packs) != 1:
            raise ValueError(f"Expected one skill pack for edition {edition!r}; found {len(packs)}")
        return self._pack_skill_bundles(packs[0].stem)

    def _pack_skill_bundles(
        self,
        pack_id: str,
        visited: Optional[set[str]] = None,
    ) -> set[str]:
        visited = visited or set()
        if pack_id in visited:
            return set()
        visited.add(pack_id)
        path = self.settings.specpack_root / "packs" / f"{pack_id}.yaml"
        if not path.exists():
            return set()
        pack = self._load_yaml(path)
        installs = pack.get("installs") or {}
        bundles = {
            str(item).strip()
            for item in (installs.get("skills") or [])
            if isinstance(item, str) and item.strip()
        }
        for requirement in pack.get("requires") or []:
            if not isinstance(requirement, str):
                continue
            required_path = (
                self.settings.specpack_root / "packs" / f"{requirement}.yaml"
            )
            if required_path.exists():
                bundles.update(self._pack_skill_bundles(requirement, visited))
        return bundles

    def list_skills(
        self,
        project_id: Optional[str] = None,
        *,
        connection: Optional[Connection] = None,
        include_authored: bool = True,
    ) -> list[dict[str, Any]]:
        if project_id is not None and connection is None:
            raise ValueError("connection is required when project_id is provided")

        allowed_bundles: Optional[set[str]] = None
        project_state: dict[str, dict[str, Any]] = {}
        if project_id is not None:
            assert connection is not None
            allowed_bundles = self._edition_skill_bundles(connection, project_id)
            rows = connection.execute(
                text(
                    """
                    SELECT skill_id, source, enabled, on_demand, config,
                           publisher, provenance
                    FROM project_skills
                    WHERE project_id = :project_id
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
            project_state = {str(row["skill_id"]): dict(row) for row in rows}

        items: list[dict[str, Any]] = []
        for path in self._skill_files():
            if allowed_bundles is not None and path.stem not in allowed_bundles:
                continue
            payload = self._load_yaml(path)
            for skill in payload.get("skills", []):
                if not isinstance(skill, dict):
                    continue
                self.validate_skill_spec(skill, str(path))
                skill_id = str(skill.get("id", path.stem))
                system = bool(skill.get("system", False))
                load = skill.get("load", "eager")
                state = project_state.get(skill_id)
                source = (
                    str(state["source"])
                    if state is not None
                    else ("system" if system else "module")
                )
                enabled = (
                    True
                    if system
                    else bool(state["enabled"])
                    if state is not None
                    else True
                )
                on_demand = (
                    bool(state["on_demand"])
                    if state is not None
                    else load == "on_demand"
                )
                items.append(
                    {
                        "id": skill_id,
                        "kind": skill.get("kind", "unknown"),
                        "system": system,
                        "load": load,
                        "source": source if project_id is not None else (
                            "system" if system else "catalog"
                        ),
                        "enabled": enabled if project_id is not None else system,
                        "on_demand": on_demand,
                        "risk_tier": int(skill.get("risk_tier", 0)),
                        "description": skill.get("description", ""),
                        "mock_mode": bool(skill.get("mock_mode", False)),
                        "input_schema": skill.get("input_schema", {}) or {},
                        "output_schema": skill.get("output_schema", {}) or {},
                        "cost": skill.get("cost", {}) or {},
                        "retry": skill.get("retry", {}) or {},
                        "permissions": skill.get("permissions", {}) or {},
                        "data_boundary": skill.get("data_boundary", "internal"),
                        "policy": skill.get("policy", {}) or {},
                        "publisher": (
                            (state or {}).get("publisher") or
                            ({"id": "boss", "name": "BOSS"} if system else {})
                        ),
                        "provenance": (
                            (state or {}).get("provenance") or
                            ({"source": "builtin", "version": "1", "trusted": True}
                             if system else {"source": "specpack", "trusted": True})
                        ),
                    }
                )
        catalog_ids = {str(item["id"]) for item in items}
        if project_id is not None:
            for skill_id, state in project_state.items():
                config = state.get("config") or {}
                custom = config.get("skill_spec") if isinstance(config, dict) else None
                if skill_id in catalog_ids or not isinstance(custom, dict):
                    continue
                self.validate_skill_spec(custom, f"project:{project_id}")
                provenance = state.get("provenance") or {}
                if (
                    self.settings.exclude_untrusted_skills
                    and provenance.get("trusted") is False
                ):
                    continue
                items.append(
                    {
                        **custom,
                        "id": skill_id,
                        "system": False,
                        "source": str(state.get("source") or "tenant"),
                        "enabled": bool(state.get("enabled")),
                        "on_demand": bool(state.get("on_demand")),
                        "publisher": state.get("publisher") or {},
                        "provenance": provenance,
                    }
                )
        if project_id is not None and include_authored:
            # THE SKILLS THIS PROJECT AUTHORED, in the same list. This is what
            # SkillRuntimeService, held_skills and the agent loop load specs from; without this
            # an authored skill was shown everywhere and could run nowhere — invoke raised
            # SkillNotFoundError. See skill_catalogue for the whole story and the risk rule.
            #
            # A key that is already in the catalogue is skipped rather than overriding it:
            # save_skill refuses a key that shadows a built-in, so a collision here is a row
            # that predates that refusal, and the shipped skill must stay reachable.
            from .skill_catalogue import authored_specs

            assert connection is not None
            by_id = {str(item["id"]): item for item in items}
            for spec in authored_specs(connection, project_id, by_id):
                if spec["id"] in by_id:
                    continue
                self.validate_skill_spec(spec, f"authored:{project_id}")
                items.append(spec)
        items.sort(key=lambda item: item["id"])
        return items

    def validate_skill_spec(self, skill: dict[str, Any], source: str) -> None:
        errors: list[str] = []
        for key in ("id", "kind", "description", "data_boundary"):
            if not isinstance(skill.get(key), str) or not skill[key].strip():
                errors.append(f"{key} must be a non-empty string")

        risk_tier = skill.get("risk_tier")
        if not isinstance(risk_tier, int) or risk_tier < 0 or risk_tier > 3:
            errors.append("risk_tier must be an integer between 0 and 3")

        if skill.get("mock_mode") is not False:
            errors.append("mock_mode must be false in real-only runtime specs")

        if not isinstance(skill.get("system", False), bool):
            errors.append("system must be a boolean when provided")
        if skill.get("load", "eager") not in {"eager", "on_demand"}:
            errors.append("load must be either eager or on_demand")

        for key in (
            "input_schema",
            "output_schema",
            "cost",
            "retry",
            "permissions",
            "policy",
        ):
            if not isinstance(skill.get(key), dict) or not skill[key]:
                errors.append(f"{key} must be a non-empty object")

        cost = skill.get("cost") if isinstance(skill.get("cost"), dict) else {}
        if not isinstance(cost.get("estimator"), str) or not cost["estimator"].strip():
            errors.append("cost.estimator must be a non-empty string")
        if not isinstance(cost.get("category"), str) or not cost["category"].strip():
            errors.append("cost.category must be a non-empty string")

        retry = skill.get("retry") if isinstance(skill.get("retry"), dict) else {}
        max_attempts = retry.get("max_attempts")
        if not isinstance(max_attempts, int) or max_attempts < 1:
            errors.append("retry.max_attempts must be a positive integer")
        if not isinstance(retry.get("backoff"), str) or not retry["backoff"].strip():
            errors.append("retry.backoff must be a non-empty string")

        permissions = (
            skill.get("permissions") if isinstance(skill.get("permissions"), dict) else {}
        )
        for key in ("allowed_callers", "required_permissions"):
            value = permissions.get(key)
            # AN EMPTY `allowed_callers` IS A STATEMENT, NOT AN OMISSION.
            #
            # It means "nobody reaches this by default": the only route is somebody giving the
            # skill to an agent, which `skill_runtime` honours through `role_skills` whatever this
            # list says. That is the right default for a skill powerful enough that holding it
            # should be a deliberate act — `code.write_and_run` is the case that needed it — and
            # the validator could not express it, so the alternative was naming a role that should
            # not have had it. The key must still be PRESENT: a missing one is an author who did
            # not think about reach, and that is the mistake this check is for.
            #
            # `required_permissions` stays non-empty. A skill answerable to no permission at all is
            # not a governed skill.
            if not isinstance(value, list) or (not value and key != "allowed_callers"):
                errors.append(f"permissions.{key} must be a non-empty list")
            elif any(not isinstance(item, str) or not item.strip() for item in value):
                errors.append(
                    f"permissions.{key} must contain only non-empty strings"
                )

        if errors:
            skill_id = skill.get("id", "<unknown>")
            raise ValueError(f"SkillSpec {skill_id} in {source} is invalid: {'; '.join(errors)}")

    def get_skill(
        self,
        skill_id: str,
        project_id: Optional[str] = None,
        *,
        connection: Optional[Connection] = None,
    ) -> dict[str, Any]:
        catalog = self.list_skills(project_id, connection=connection)
        for skill in catalog:
            if skill["id"] == skill_id:
                return skill
        # Preserve the global mock/canary API contract for catalog SkillSpecs
        # that are not installed in the project's effective edition pack.
        if project_id is not None:
            for skill in self.list_skills():
                if skill["id"] == skill_id:
                    return skill
        raise SkillNotFoundError(f"Skill '{skill_id}' not found")

    def _runtime_flags(self, source_id: str) -> tuple[bool, bool]:
        if source_id == "openai_codex_chatgpt":
            return (
                bool(self.settings.openai_codex_oauth_enabled),
                bool(
                    self.settings.openai_codex_oauth_enabled
                    and self.settings.openai_codex_oauth_session_ref
                ),
            )
        if source_id == "openai_api":
            return True, bool(self.settings.openai_api_key)
        if source_id == "anthropic_claude_code":
            return (
                bool(self.settings.anthropic_claude_oauth_enabled),
                bool(
                    self.settings.anthropic_claude_oauth_enabled
                    and self.settings.anthropic_claude_oauth_session_ref
                ),
            )
        if source_id == "anthropic_api":
            return False, bool(self.settings.anthropic_api_key)
        if source_id == "typesafe_jev":
            # Runs over HTTPS from the API and the worker; the key is the only prerequisite. A
            # key BOSS stored is a bound SecretRef, which _runtime_context counts by itself.
            return True, bool(self.settings.typesafe_api_key)
        if source_id == "google_api":
            return False, bool(self.settings.google_api_key)
        if source_id == "xai_api":
            return False, bool(self.settings.xai_api_key)
        if source_id == "xai_grok_build":
            return (
                bool(self.settings.xai_grok_oauth_enabled),
                bool(
                    self.settings.xai_grok_oauth_enabled
                    and self.settings.xai_grok_oauth_session_ref
                ),
            )
        if source_id == "cursor_cli":
            return False, bool(self.settings.cursor_cli_enabled)
        if source_id == "approved_local_model_host":
            local_ready = bool(
                self.settings.local_model_enabled and self.settings.local_model_id
            )
            return local_ready, local_ready
        if self._paid_gateway is not None and source_id == self._paid_gateway.source_id:
            return self._paid_gateway.runtime_flags()
        if source_id == "managed_cloud_model_gateway":
            runtime_supported = bool(
                self.settings.managed_cloud_model_gateway_enabled
                and self.settings.account_connect_url
            )
            return runtime_supported, bool(
                runtime_supported and self.settings.account_connect_token
            )
        return False, False

    def _credential_ref(self, source_id: str) -> Optional[str]:
        if source_id == "openai_codex_chatgpt":
            return "BOSS_OPENAI_CODEX_OAUTH_SESSION_REF"
        if source_id == "openai_api":
            return "OPENAI_API_KEY"
        if source_id == "anthropic_claude_code":
            return "BOSS_ANTHROPIC_CLAUDE_OAUTH_SESSION_REF"
        if source_id == "anthropic_api":
            return "ANTHROPIC_API_KEY"
        if source_id == "typesafe_jev":
            return "TYPESAFE_API_KEY"
        if source_id == "google_api":
            return "GOOGLE_API_KEY"
        if source_id == "xai_api":
            return "XAI_API_KEY"
        if source_id == "xai_grok_build":
            return "BOSS_XAI_GROK_OAUTH_SESSION_REF"
        if source_id == "cursor_cli":
            return "BOSS_CURSOR_CLI_ENABLED"
        if source_id == "approved_local_model_host":
            return "BOSS_LOCAL_MODEL_ID"
        if self._paid_gateway is not None and source_id == self._paid_gateway.source_id:
            return self._paid_gateway.credential_ref()
        if source_id == "managed_cloud_model_gateway":
            return "BOSS_ACCOUNT_CONNECT_TOKEN"
        return None

    def _runtime_supported_models(
        self, source_id: str, source_defaults: dict[str, Any]
    ) -> list[str]:
        if self._paid_gateway is not None and source_id == self._paid_gateway.source_id:
            return self._paid_gateway.supported_models(source_defaults)
        if source_id == "managed_cloud_model_gateway":
            configured = list(self.settings.managed_cloud_model_gateway_model_values)
            return configured or list(source_defaults.get("supported_models") or [])
        return list(source_defaults.get("supported_models") or [])

    def _requires_official_oauth(self, source: dict[str, Any]) -> bool:
        return (
            source.get("entitlement_kind") == "subscription"
            and source.get("source_kind") in {"official_cli", "official_coding_agent"}
        )

    def _effective_connection_status(self, source: dict[str, Any]) -> str:
        observed_capacity = source.get("observed_capacity") or {}
        if bool(observed_capacity.get("official_oauth_connected")):
            return "connected"
        _, credential_present = self._runtime_context(source)
        if self._requires_official_oauth(source) and credential_present:
            return "connected"
        return str(source.get("connection_status") or "not_connected")

    def _connection_policy_denial(self, source: dict[str, Any]) -> Optional[str]:
        if not self._requires_official_oauth(source):
            return None
        if source.get("auth_method") not in self.OFFICIAL_OAUTH_METHODS:
            return "official_oauth_connection_required"
        if self._effective_connection_status(source) != "connected":
            return "official_oauth_connection_not_connected"
        return None

    def _capacity_policy_denial(self, source: dict[str, Any]) -> Optional[str]:
        observed_capacity = source.get("observed_capacity") or {}
        capacity_state = str(observed_capacity.get("capacity_state") or "").strip()
        if capacity_state in self.CAPACITY_DENIAL_STATES:
            if capacity_state == "rate_limited":
                return "capacity_rate_limited"
            if capacity_state == "blocked":
                return "capacity_blocked"
            return "capacity_exhausted"
        if bool(observed_capacity.get("subscription_limit_reached")) or bool(
            observed_capacity.get("usage_cap_reached")
        ):
            return "capacity_exhausted"
        if bool(observed_capacity.get("provider_rate_limited")) or bool(
            observed_capacity.get("rate_limited")
        ):
            return "capacity_rate_limited"
        for key in ("remaining_units", "remaining_requests", "remaining_tokens"):
            value = observed_capacity.get(key)
            if isinstance(value, (int, float)) and value <= 0:
                return "capacity_exhausted"
        return None

    def _route_context_for_source(self, source: dict[str, Any]) -> dict[str, Any]:
        observed_capacity = source.get("observed_capacity") or {}
        endpoint = self.resolve_openai_compatible_endpoint(source)
        return {
            "source_id": source["source_id"],
            "provider": source.get("provider"),
            "source_kind": source.get("source_kind"),
            "auth_method": source.get("auth_method"),
            "connection_status": self._effective_connection_status(source),
            "connection_ref": source.get("connection_ref"),
            "capacity_state": observed_capacity.get("capacity_state", "unknown"),
            "capacity_reset_at": observed_capacity.get("reset_at")
            or observed_capacity.get("rate_limit_reset_at"),
            "observed_at": observed_capacity.get("observed_at")
            or observed_capacity.get("last_checked_at"),
            "base_url": endpoint.get("base_url"),
            "openai_compatible": endpoint.get("openai_compatible"),
        }

    def _reject_source(
        self,
        rejected_sources: list[dict[str, Any]],
        source: dict[str, Any],
        reason: str,
        **extra: Any,
    ) -> None:
        rejected_sources.append(
            {
                **self._route_context_for_source(source),
                "reason": reason,
                **extra,
            }
        )

    def _effective_status(self, row: dict[str, Any]) -> str:
        stored_status = row.get("status") or "declared"
        runtime_supported, credential_present = self._runtime_context(row)
        if stored_status in {"inactive", "disabled"}:
            return stored_status
        if runtime_supported and credential_present:
            connection_denial = self._connection_policy_denial(row)
            if connection_denial:
                return connection_denial
            return "active"
        if runtime_supported and not credential_present:
            if self._requires_official_oauth(row):
                return "missing_oauth_connection"
            return "missing_credentials"
        if stored_status == "active" and not runtime_supported:
            return "runtime_unavailable"
        if stored_status == "missing_credentials" and credential_present and runtime_supported:
            return "active"
        return stored_status

    def _runtime_context(self, row: dict[str, Any]) -> tuple[bool, bool]:
        computed_runtime_supported, computed_credential_present = self._runtime_flags(
            row["source_id"]
        )
        observed_capacity = row.get("observed_capacity") or {}
        bound_secret = bool(row.get("credential_secret_ref_id"))
        endpoint = self.resolve_openai_compatible_endpoint(row)
        local_endpoint = bool(endpoint.get("base_url"))
        runtime_supported = (
            bound_secret
            or local_endpoint
            or bool(row.get("runtime_supported", False))
            or bool(computed_runtime_supported)
        )
        credential_present = (
            bound_secret
            or local_endpoint
            or bool(observed_capacity.get("credential_present", False))
            or bool(computed_credential_present)
        )
        return runtime_supported, credential_present

    @staticmethod
    def _connection_metadata(row: dict[str, Any]) -> dict[str, Any]:
        meta = row.get("connection_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                return {}
        return dict(meta) if isinstance(meta, dict) else {}

    @classmethod
    def public_connection_metadata(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Return connection metadata with credential-like keys stripped."""
        safe: dict[str, Any] = {}
        for key, value in cls._connection_metadata(row).items():
            normalized = str(key).strip().lower()
            if any(
                term in normalized
                for term in ("secret", "token", "api_key", "password", "credential")
            ):
                continue
            safe[key] = value
        return safe

    @classmethod
    def resolve_openai_compatible_endpoint(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Routing view for a local/OpenAI-compatible entitlement source.

        Never includes the raw credential. ``base_url`` is the operator-configured
        OpenAI-compatible origin (e.g. http://127.0.0.1:11434/v1).
        """
        source_kind = str(row.get("source_kind") or "")
        source_id = str(row.get("source_id") or "")
        if source_kind != "approved_local_model" and source_id != "approved_local_model_host":
            return {
                "source_id": source_id,
                "base_url": None,
                "openai_compatible": False,
                "credential_bound": bool(row.get("credential_secret_ref_id")),
            }
        meta = cls.public_connection_metadata(row)
        base_url = str(meta.get("base_url") or "").strip() or None
        return {
            "source_id": source_id,
            "base_url": base_url,
            "openai_compatible": bool(base_url or meta.get("openai_compatible")),
            "credential_bound": bool(row.get("credential_secret_ref_id")),
            "auth_method": row.get("auth_method") or "local_host",
        }

    def configure_openai_compatible_source(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_id: str,
        base_url: str,
        actor: str,
        secret_ref_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Persist a local OpenAI-compatible base_url on an entitlement source."""
        from urllib.parse import urlparse

        source = self.get_source(connection, project_id, source_id)
        if (
            source.get("source_kind") != "approved_local_model"
            and source_id != "approved_local_model_host"
        ):
            raise ValueError(
                "Only approved local model sources accept an OpenAI-compatible base_url."
            )
        parsed = urlparse(str(base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) origin, e.g. http://127.0.0.1:11434/v1")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials.")
        cleaned = str(base_url).strip().rstrip("/")
        if secret_ref_id:
            secret = connection.execute(
                text(
                    """
                    SELECT id, provider, status, secret_kind
                    FROM secret_refs
                    WHERE id = :secret_ref_id AND project_id = :project_id
                    """
                ),
                {"secret_ref_id": secret_ref_id, "project_id": project_id},
            ).mappings().first()
            if not secret or secret["status"] not in {"active", "rotating", "rotation_due"}:
                raise ValueError("An active project SecretRef is required.")
            if secret["secret_kind"] not in {"api_token", "access_key", "client_secret"}:
                raise ValueError("SecretRef kind is not valid for a model credential.")
        meta = {
            **self._connection_metadata(source),
            "base_url": cleaned,
            "openai_compatible": True,
        }
        observed = {
            **(source.get("observed_capacity") or {}),
            "credential_present": True,
            "runtime_supported": True,
            "capacity_state": (source.get("observed_capacity") or {}).get(
                "capacity_state", "available"
            ),
            "local_openai_compatible": True,
        }
        connection.execute(
            text(
                """
                UPDATE entitlement_sources
                SET connection_metadata = CAST(:metadata AS JSONB),
                    credential_secret_ref_id = COALESCE(:secret_ref_id, credential_secret_ref_id),
                    connection_status = 'connected',
                    runtime_supported = true,
                    status = 'active',
                    auth_method = 'openai_compatible',
                    observed_capacity = CAST(:observed AS JSONB),
                    updated_at = now()
                WHERE project_id = :project_id AND source_id = :source_id
                """
            ),
            {
                "metadata": json.dumps(meta),
                "secret_ref_id": secret_ref_id,
                "observed": json.dumps(observed),
                "project_id": project_id,
                "source_id": source_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "entitlement.local_endpoint_configured",
            actor,
            "entitlement_source",
            source["id"],
            {
                "source_id": source_id,
                "base_url_host": parsed.hostname,
                "base_url_port": parsed.port,
                "credential_bound": bool(secret_ref_id or source.get("credential_secret_ref_id")),
            },
        )
        updated = self.get_source(connection, project_id, source_id)
        endpoint = self.resolve_openai_compatible_endpoint(updated)
        return {
            "source_id": source_id,
            "project_id": project_id,
            "effective_status": updated.get("effective_status"),
            "connection_status": updated.get("connection_status"),
            "credential_secret_ref_id": updated.get("credential_secret_ref_id"),
            "endpoint": endpoint,
            "connection_metadata": self.public_connection_metadata(updated),
        }

    def _access_class_for_source_kind(self, source_kind: str) -> str:
        if source_kind == "approved_local_model":
            return "local_model"
        if source_kind == "official_api":
            return "official_api"
        if source_kind in {"official_cli", "official_coding_agent"}:
            return "official_cli"
        if source_kind in self._gateway_source_kinds:
            return source_kind
        return source_kind

    def _capacity_kind_for_source(self, source: dict[str, Any]) -> str:
        source_kind = source.get("source_kind")
        entitlement_kind = source.get("entitlement_kind")
        if source_kind == "approved_local_model":
            return "local_model"
        if source_kind in self._gateway_source_kinds:
            return str(source_kind)
        if source_kind == "official_api":
            return "official_api"
        if entitlement_kind in {"subscription", "licensed_seat"}:
            return "subscription_cli"
        return "official_api"

    def _routing_modes_for_source(self, source: dict[str, Any]) -> list[str]:
        source_kind = source.get("source_kind")
        if source_kind == "approved_local_model":
            return ["local_first", "budget_safe"]
        if source_kind == "official_api":
            return ["budget_safe", "quality_first"]
        if source_kind in {"official_cli", "official_coding_agent"}:
            return ["subscription_first", "owner_sandbox"]
        if self._paid_gateway is not None and source_kind == self._paid_gateway.source_kind:
            return self._paid_gateway.routing_modes()
        if source_kind == "managed_cloud_model_gateway":
            return ["quality_first", "budget_safe"]
        return ["budget_safe"]

    def _pool_id(self, project_id: str, source_id: str) -> str:
        return f"pool_{project_id}_{source_id}"

    def _entitlement_pool_from_source(self, source: dict[str, Any]) -> dict[str, Any]:
        observed_capacity = source.get("observed_capacity") or {}
        capacity_state = str(observed_capacity.get("capacity_state") or "unknown")
        return {
            "id": self._pool_id(source["project_id"], source["source_id"]),
            "project_id": source["project_id"],
            "provider": source["provider"],
            "source_id": source["source_id"],
            "source_kind": source["source_kind"],
            "access_class": self._access_class_for_source_kind(source["source_kind"]),
            "auth_method": source.get("auth_method", "api_key"),
            "connection_status": source.get("connection_status", "not_connected"),
            "effective_status": source.get("effective_status", source.get("status", "declared")),
            "owner_type": source["owner_type"],
            "owner_id": source["owner_id"],
            "allowed_workload_classes": source.get("workload_classes_allowed") or [],
            "routing_modes": self._routing_modes_for_source(source),
            "capacity_state": capacity_state,
            "cooldown_until": observed_capacity.get("cooldown_until")
            or observed_capacity.get("reset_at")
            or observed_capacity.get("rate_limit_reset_at"),
            "multi_tenant_allowed": bool(source.get("multi_tenant_allowed", False)),
            "budget_policy_ref": f"budget_policy:{source['project_id']}",
            "runtime_supported": bool(source.get("runtime_supported", False)),
            "supported_models": source.get("supported_models") or [],
            "task_strengths": source.get("task_strengths") or [],
            "risk_tier_max": int(source.get("risk_tier_max", 0)),
            "observed_capacity": observed_capacity,
            "policy_notes": source.get("policy_notes") or [],
            "updated_at": source.get("updated_at"),
        }

    def _route_decision_from_log(self, log: dict[str, Any]) -> dict[str, Any]:
        decision = log.get("decision") or {}
        selected_source_id = log.get("selected_source_id")
        return {
            "id": log["id"],
            "project_id": log["project_id"],
            "task_id": decision.get("task_id"),
            "skill_id": log.get("skill_id"),
            "task_kind": log["task_kind"],
            "workload_class": log["workload_class"],
            "risk_tier": log["risk_tier"],
            "selected_entitlement_pool_id": self._pool_id(
                log["project_id"], selected_source_id
            )
            if selected_source_id
            else None,
            "selected_source_id": selected_source_id,
            "selected_provider": log.get("selected_provider"),
            "selected_model_id": log.get("selected_model_id"),
            "routing_mode": decision.get("routing_mode", "budget_safe"),
            "rejected_sources": log.get("rejected_sources") or [],
            "budget_state": log["budget_state"],
            "data_boundary": log.get("data_boundary", "internal"),
            "capacity_snapshot": decision.get("selected_source")
            or {"eligible_sources": decision.get("eligible_sources") or []},
            "policy_result": {
                "allowed": log.get("denial_reason") is None,
                "denial_reason": log.get("denial_reason"),
                "provider_rule_compliance": bool(
                    decision.get("provider_rule_compliance", False)
                ),
                "reservation_required": bool(
                    decision.get("reservation_required", False)
                ),
            },
            "created_at": log["created_at"],
        }

    def _capacity_receipt_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["created_at"] = payload["created_at"].isoformat()
        return payload

    def seed_default_sources(
        self, connection: Connection, project_id: str, actor: str = "bootstrap"
    ) -> None:
        for source_id, source_defaults in self._source_record_defaults().items():
            runtime_supported, credential_present = self._runtime_flags(source_id)
            source_kind = source_defaults.get("source_kind", "official_api")
            stored_status = "active" if runtime_supported and credential_present else "declared"
            if runtime_supported and not credential_present:
                stored_status = "missing_credentials"
            owner_type = source_defaults.get("owner_type")
            if not owner_type:
                owner_type = (
                    "project"
                    if source_kind in {"official_api", *self._gateway_source_kinds}
                    else "workspace_operator"
                )
            self.upsert_source(
                connection,
                {
                    "project_id": project_id,
                    "provider": source_defaults["provider"],
                    "source_id": source_id,
                    "source_kind": source_kind,
                    "entitlement_kind": source_defaults.get(
                        "entitlement_kind", "usage_based_api"
                    ),
                    "owner_type": owner_type,
                    "owner_id": (WORKSPACE_OPERATOR_ID if owner_type == "workspace_operator"
                                 else project_id),
                    "workload_classes_allowed": source_defaults.get(
                        "workload_classes_allowed", []
                    ),
                    "multi_tenant_allowed": bool(
                        source_defaults.get("multi_tenant_allowed", False)
                    ),
                    "credential_ref": self._credential_ref(source_id),
                    "auth_method": source_defaults.get("auth_method", "api_key"),
                    "connection_status": "connected"
                    if credential_present
                    else source_defaults.get("connection_status", "not_connected"),
                    "connection_ref": source_defaults.get(
                        "connection_ref", self._credential_ref(source_id)
                    ),
                    "oauth_scopes": source_defaults.get("oauth_scopes", []),
                    "connection_metadata": source_defaults.get(
                        "connection_metadata", {}
                    ),
                    "cost_mode": source_defaults.get("entitlement_kind", "usage_based_api"),
                    "limit_shape": source_defaults.get("limit_shape", {}),
                    "supported_models": self._runtime_supported_models(
                        source_id, source_defaults
                    ),
                    "task_strengths": source_defaults.get("task_strengths", []),
                    "risk_tier_max": int(
                        source_defaults.get(
                            "risk_tier_max",
                            3
                            if source_kind in {"official_api", *self._gateway_source_kinds}
                            else 1,
                        )
                    ),
                    "status": stored_status,
                    "policy_notes": source_defaults.get("policy_notes", []),
                    "runtime_supported": runtime_supported,
                    "observed_capacity": {
                        "credential_present": credential_present,
                        "runtime_supported": runtime_supported,
                        "official_oauth_connected": bool(
                            self._requires_official_oauth(source_defaults)
                            and credential_present
                        ),
                    },
                },
                actor=actor,
                emit_events=False,
                keep_connection=True,
            )

    def list_sources(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        declared_boss_sources = {
            source_id
            for source_id, defaults in self._source_record_defaults().items()
            if defaults.get("provider") == "boss"
        }
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, provider, source_id, source_kind, entitlement_kind,
                       owner_type, owner_id, workload_classes_allowed, multi_tenant_allowed,
                       credential_ref, credential_secret_ref_id, cost_mode, limit_shape, supported_models,
                       task_strengths, risk_tier_max, status, policy_notes,
                       auth_method, connection_status, connection_ref, oauth_scopes,
                       connection_metadata, observed_capacity, runtime_supported,
                       created_at, updated_at
                FROM entitlement_sources
                WHERE project_id = :project_id
                ORDER BY provider ASC, source_id ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            # A previously paid database can be started as Community. Never
            # route a BOSS-owned source absent from this edition's catalog.
            if (
                payload["provider"] == "boss"
                and payload["source_id"] not in declared_boss_sources
            ):
                continue
            payload["effective_status"] = self._effective_status(payload)
            payload["connection_status"] = self._effective_connection_status(payload)
            runtime_supported, credential_present = self._runtime_context(payload)
            payload["runtime_supported"] = runtime_supported
            payload["observed_capacity"] = {
                **(payload.get("observed_capacity") or {}),
                "credential_present": credential_present,
                "runtime_supported": runtime_supported,
                "capacity_state": (
                    (payload.get("observed_capacity") or {}).get("capacity_state")
                    or ("available" if credential_present else "unknown")
                ),
                "official_oauth_connected": bool(
                    self._requires_official_oauth(payload)
                    and self._effective_connection_status(payload) == "connected"
                ),
            }
            payload["created_at"] = payload["created_at"].isoformat()
            payload["updated_at"] = payload["updated_at"].isoformat()
            items.append(payload)
        return items

    def list_model_adapters(self) -> list[dict[str, Any]]:
        adapters: list[dict[str, Any]] = []
        for provider in self._provider_registry().get("providers", []):
            provider_id = provider.get("id")
            if not isinstance(provider_id, str):
                continue
            for source in provider.get("source_records", []):
                source_id = source.get("source_id")
                if not isinstance(source_id, str):
                    continue
                runtime_supported, credential_present = self._runtime_flags(source_id)
                source_kind = str(source.get("source_kind", "official_api"))
                adapters.append(
                    {
                        "id": f"adapter_{source_id}",
                        "provider": provider_id,
                        "source_id": source_id,
                        "source_kind": source_kind,
                        "access_class": self._access_class_for_source_kind(source_kind),
                        "entitlement_kind": source.get(
                            "entitlement_kind", "usage_based_api"
                        ),
                        "auth_method": source.get("auth_method", "api_key"),
                        "connection_ref": source.get(
                            "connection_ref", self._credential_ref(source_id)
                        ),
                        "supported_models": self._runtime_supported_models(
                            source_id, source
                        ),
                        "task_strengths": source.get("task_strengths", []) or [],
                        "allowed_workload_classes": source.get(
                            "workload_classes_allowed", []
                        )
                        or [],
                        "multi_tenant_allowed": bool(
                            source.get("multi_tenant_allowed", False)
                        ),
                        "runtime_supported": runtime_supported,
                        "credential_present": credential_present,
                        "official_connection_required": bool(
                            self._requires_official_oauth(source)
                        ),
                        "policy_notes": source.get("policy_notes", []) or [],
                    }
                )
        return sorted(adapters, key=lambda item: (item["provider"], item["source_id"]))

    def list_entitlement_pools(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        return [
            self._entitlement_pool_from_source(source)
            for source in self.list_sources(connection, project_id)
        ]

    def get_entitlement_pool(
        self, connection: Connection, project_id: str, pool_id: str
    ) -> dict[str, Any]:
        for pool in self.list_entitlement_pools(connection, project_id):
            if pool["id"] == pool_id or pool["source_id"] == pool_id:
                return pool
        raise EntitlementPoolNotFoundError(
            f"Entitlement pool '{pool_id}' not found for project '{project_id}'."
        )

    def record_capacity_probe(
        self,
        connection: Connection,
        project_id: str,
        pool_id: str,
        *,
        actor: str,
        observed_capacity: dict[str, Any],
        usage_units: Optional[dict[str, Any]] = None,
        provider_ref: Optional[str] = None,
        model_route_decision_id: Optional[str] = None,
        dedupe_key: Optional[str] = None,
        status: str = "recorded",
    ) -> dict[str, Any]:
        pool = self.get_entitlement_pool(connection, project_id, pool_id)
        resolved_dedupe_key = dedupe_key or (
            f"{pool['id']}:{provider_ref or 'manual'}:"
            f"{observed_capacity.get('observed_at') or observed_capacity.get('capacity_state') or 'probe'}"
        )
        existing = connection.execute(
            text(
                """
                SELECT id, project_id, entitlement_pool_id, source_id, provider,
                       capacity_kind, model_route_decision_id, provider_ref,
                       usage_units, capacity_state_after, dedupe_key, status,
                       observed_capacity, created_by, created_at
                FROM capacity_receipts
                WHERE project_id = :project_id
                  AND entitlement_pool_id = :entitlement_pool_id
                  AND dedupe_key = :dedupe_key
                """
            ),
            {
                "project_id": project_id,
                "entitlement_pool_id": pool["id"],
                "dedupe_key": resolved_dedupe_key,
            },
        ).mappings().first()
        if existing:
            return {
                "receipt": self._capacity_receipt_from_row(dict(existing)),
                "pool": self.get_entitlement_pool(connection, project_id, pool["id"]),
                "duplicate": True,
            }

        source = self.get_source(connection, project_id, pool["source_id"])
        merged_capacity = {
            **(source.get("observed_capacity") or {}),
            **observed_capacity,
        }
        capacity_state_after = str(
            merged_capacity.get("capacity_state")
            or observed_capacity.get("capacity_state")
            or "unknown"
        )
        merged_capacity["capacity_state"] = capacity_state_after
        updated_source = self.upsert_source(
            connection,
            {
                **source,
                "observed_capacity": merged_capacity,
            },
            actor=actor,
            emit_events=False,
        )
        receipt_id = make_id("caprec")
        connection.execute(
            text(
                """
                INSERT INTO capacity_receipts (
                    id, project_id, entitlement_pool_id, source_id, provider,
                    capacity_kind, model_route_decision_id, provider_ref,
                    usage_units, capacity_state_after, dedupe_key, status,
                    observed_capacity, created_by
                )
                VALUES (
                    :id, :project_id, :entitlement_pool_id, :source_id, :provider,
                    :capacity_kind, :model_route_decision_id, :provider_ref,
                    CAST(:usage_units AS JSONB), :capacity_state_after, :dedupe_key,
                    :status, CAST(:observed_capacity AS JSONB), :created_by
                )
                """
            ),
            {
                "id": receipt_id,
                "project_id": project_id,
                "entitlement_pool_id": pool["id"],
                "source_id": pool["source_id"],
                "provider": pool["provider"],
                "capacity_kind": self._capacity_kind_for_source(source),
                "model_route_decision_id": model_route_decision_id,
                "provider_ref": provider_ref,
                "usage_units": json.dumps(usage_units or {}),
                "capacity_state_after": capacity_state_after,
                "dedupe_key": resolved_dedupe_key,
                "status": status,
                "observed_capacity": json.dumps(observed_capacity),
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "capacity.receipt_recorded",
            actor,
            "capacity_receipt",
            receipt_id,
            {
                "entitlement_pool_id": pool["id"],
                "source_id": pool["source_id"],
                "provider": pool["provider"],
                "capacity_state_after": capacity_state_after,
                "dedupe_key": resolved_dedupe_key,
            },
        )
        receipt_row = connection.execute(
            text(
                """
                SELECT id, project_id, entitlement_pool_id, source_id, provider,
                       capacity_kind, model_route_decision_id, provider_ref,
                       usage_units, capacity_state_after, dedupe_key, status,
                       observed_capacity, created_by, created_at
                FROM capacity_receipts
                WHERE id = :id
                """
            ),
            {"id": receipt_id},
        ).mappings().one()
        return {
            "receipt": self._capacity_receipt_from_row(dict(receipt_row)),
            "pool": self._entitlement_pool_from_source(updated_source),
            "duplicate": False,
        }

    def upsert_source(
        self,
        connection: Connection,
        payload: dict[str, Any],
        *,
        actor: str,
        emit_events: bool = True,
        keep_connection: bool = False,
    ) -> dict[str, Any]:
        """Create or update one entitlement source.

        keep_connection: seeding refreshes the CATALOGUE (models, strengths, limits) on every
        install run, and must not touch a CONNECTION somebody made. Without it, re-running the
        installer reset every connected subscription to "declared / not_connected" — the Codex
        login recorded by `--connect-codex` was gone after the next upgrade. A real disconnect
        passes an explicit status ("revoked") and is unaffected.
        """
        defaults = self._source_record_defaults().get(payload["source_id"], {})
        if payload.get("provider") == "boss" and not defaults:
            raise ValueError("BOSS-owned model source is unavailable in this edition")
        record = {**defaults, **payload}
        computed_runtime_supported, computed_credential_present = self._runtime_flags(
            record["source_id"]
        )
        bound_secret = bool(record.get("credential_secret_ref_id"))
        runtime_supported = bound_secret or bool(record.get("runtime_supported", False)) or bool(
            computed_runtime_supported
        )
        observed_capacity = record.get("observed_capacity", {}) or {}
        credential_present = (
            bound_secret
            or bool(observed_capacity.get("credential_present", False))
            or bool(computed_credential_present)
        )
        source_pk = f"src_{record['project_id']}_{record['source_id']}"
        auth_method = record.get("auth_method", "api_key")
        connection_status = record.get("connection_status")
        if not connection_status:
            connection_status = "connected" if credential_present else "not_connected"
        connection.execute(
            text(
                """
                INSERT INTO entitlement_sources (
                    id, project_id, provider, source_id, source_kind, entitlement_kind,
                    owner_type, owner_id, workload_classes_allowed, multi_tenant_allowed,
                    credential_ref, credential_secret_ref_id, cost_mode, limit_shape, supported_models, task_strengths,
                    risk_tier_max, status, auth_method, connection_status, connection_ref,
                    oauth_scopes, connection_metadata, policy_notes, observed_capacity,
                    runtime_supported
                )
                VALUES (
                    :id, :project_id, :provider, :source_id, :source_kind, :entitlement_kind,
                    :owner_type, :owner_id, CAST(:workload_classes_allowed AS JSONB),
                    :multi_tenant_allowed, :credential_ref, :credential_secret_ref_id, :cost_mode,
                    CAST(:limit_shape AS JSONB), CAST(:supported_models AS JSONB),
                    CAST(:task_strengths AS JSONB), :risk_tier_max, :status,
                    :auth_method, :connection_status, :connection_ref,
                    CAST(:oauth_scopes AS JSONB), CAST(:connection_metadata AS JSONB),
                    CAST(:policy_notes AS JSONB), CAST(:observed_capacity AS JSONB),
                    :runtime_supported
                )
                ON CONFLICT (project_id, source_id) DO UPDATE
                SET provider = EXCLUDED.provider,
                    source_kind = EXCLUDED.source_kind,
                    entitlement_kind = EXCLUDED.entitlement_kind,
                    owner_type = EXCLUDED.owner_type,
                    owner_id = EXCLUDED.owner_id,
                    workload_classes_allowed = EXCLUDED.workload_classes_allowed,
                    multi_tenant_allowed = EXCLUDED.multi_tenant_allowed,
                    credential_ref = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.credential_ref ELSE EXCLUDED.credential_ref END,
                    credential_secret_ref_id = COALESCE(
                        EXCLUDED.credential_secret_ref_id,
                        entitlement_sources.credential_secret_ref_id
                    ),
                    cost_mode = EXCLUDED.cost_mode,
                    limit_shape = EXCLUDED.limit_shape,
                    supported_models = EXCLUDED.supported_models,
                    task_strengths = EXCLUDED.task_strengths,
                    risk_tier_max = EXCLUDED.risk_tier_max,
                    status = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.status ELSE EXCLUDED.status END,
                    auth_method = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.auth_method ELSE EXCLUDED.auth_method END,
                    connection_status = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.connection_status ELSE EXCLUDED.connection_status END,
                    connection_ref = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.connection_ref ELSE EXCLUDED.connection_ref END,
                    oauth_scopes = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.oauth_scopes ELSE EXCLUDED.oauth_scopes END,
                    connection_metadata = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.connection_metadata ELSE EXCLUDED.connection_metadata END,
                    policy_notes = EXCLUDED.policy_notes,
                    observed_capacity = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.observed_capacity ELSE EXCLUDED.observed_capacity END,
                    runtime_supported = CASE WHEN (:keep_connection AND entitlement_sources.connection_status <> 'not_connected') THEN entitlement_sources.runtime_supported ELSE EXCLUDED.runtime_supported END,
                    updated_at = now()
                """
            ),
            {
                "id": source_pk,
                "keep_connection": bool(keep_connection),
                "project_id": record["project_id"],
                "provider": record["provider"],
                "source_id": record["source_id"],
                "source_kind": record.get("source_kind", "official_api"),
                "entitlement_kind": record.get("entitlement_kind", "usage_based_api"),
                "owner_type": record.get("owner_type", "project"),
                "owner_id": record.get("owner_id", record["project_id"]),
                "workload_classes_allowed": json.dumps(
                    record.get("workload_classes_allowed", [])
                ),
                "multi_tenant_allowed": bool(
                    record.get("multi_tenant_allowed", False)
                ),
                "credential_ref": record.get("credential_ref"),
                "credential_secret_ref_id": record.get("credential_secret_ref_id"),
                "cost_mode": record.get("cost_mode", "usage_based_api"),
                "limit_shape": json.dumps(record.get("limit_shape", {})),
                "supported_models": json.dumps(record.get("supported_models", [])),
                "task_strengths": json.dumps(record.get("task_strengths", [])),
                "risk_tier_max": int(record.get("risk_tier_max", 1)),
                "status": record.get("status", "declared"),
                "auth_method": auth_method,
                "connection_status": connection_status,
                "connection_ref": record.get(
                    "connection_ref", record.get("credential_ref")
                ),
                "oauth_scopes": json.dumps(record.get("oauth_scopes", [])),
                "connection_metadata": json.dumps(
                    record.get("connection_metadata", {})
                ),
                "policy_notes": json.dumps(record.get("policy_notes", [])),
                "observed_capacity": json.dumps(
                    {
                        **observed_capacity,
                        "credential_present": credential_present,
                        "runtime_supported": runtime_supported,
                        "official_oauth_connected": bool(
                            self._requires_official_oauth(record)
                            and credential_present
                            and auth_method in self.OFFICIAL_OAUTH_METHODS
                            and connection_status == "connected"
                        ),
                    }
                ),
                "runtime_supported": runtime_supported,
            },
        )
        if emit_events:
            emit_event(
                connection,
                record["project_id"],
                "entitlement.source_upserted",
                actor,
                "entitlement_source",
                source_pk,
                {"source_id": record["source_id"], "provider": record["provider"]},
        )
        source = next(
            item
            for item in self.list_sources(connection, record["project_id"])
            if item["source_id"] == record["source_id"]
        )
        return source

    def get_source(
        self, connection: Connection, project_id: str, source_id: str
    ) -> dict[str, Any]:
        for source in self.list_sources(connection, project_id):
            if source["source_id"] == source_id:
                return source
        raise EntitlementSourceNotFoundError(
            f"Entitlement source '{source_id}' not found for project '{project_id}'."
        )

    def _connection_session_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        for key in ("created_at", "updated_at", "completed_at", "expires_at"):
            if payload.get(key) is not None and hasattr(payload[key], "isoformat"):
                payload[key] = payload[key].isoformat()
        return payload

    def list_connection_sessions(
        self,
        connection: Connection,
        project_id: str,
        *,
        source_id: Optional[str] = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        conditions = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if source_id:
            conditions.append("source_id = :source_id")
            params["source_id"] = source_id
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, source_id, provider, auth_method,
                       connection_mode, status, requested_scopes, authorization_url,
                       device_code, user_code, verification_uri, login_command,
                       connection_ref, instructions, metadata, error, requested_by,
                       completed_by, expires_at, created_at, updated_at, completed_at
                FROM entitlement_connection_sessions
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [self._connection_session_from_row(dict(row)) for row in rows]

    def get_connection_session(
        self, connection: Connection, project_id: str, session_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, source_id, provider, auth_method,
                       connection_mode, status, requested_scopes, authorization_url,
                       device_code, user_code, verification_uri, login_command,
                       connection_ref, instructions, metadata, error, requested_by,
                       completed_by, expires_at, created_at, updated_at, completed_at
                FROM entitlement_connection_sessions
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {"project_id": project_id, "id": session_id},
        ).mappings().first()
        if row is None:
            raise EntitlementConnectionSessionNotFoundError(
                f"Entitlement connection session '{session_id}' not found for project '{project_id}'."
            )
        return self._connection_session_from_row(dict(row))

    def create_connection_session(
        self,
        connection: Connection,
        project_id: str,
        source_id: str,
        *,
        actor: str,
        connection_mode: Optional[str] = None,
        requested_scopes: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        source = self.get_source(connection, project_id, source_id)
        if not self._requires_official_oauth(source):
            raise InvalidEntitlementConnectionError(
                "Only official OAuth-backed subscription or coding-agent sources use connection sessions."
            )
        source_metadata = source.get("connection_metadata") or {}
        supported_modes = source_metadata.get("supported_connection_modes") or []
        if not isinstance(supported_modes, list):
            supported_modes = []
        resolved_mode = connection_mode or (supported_modes[0] if supported_modes else "official_cli_oauth")
        if supported_modes and resolved_mode not in supported_modes:
            raise InvalidEntitlementConnectionError(
                f"connection_mode must be one of: {', '.join(str(item) for item in supported_modes)}"
            )
        login_command = source_metadata.get("login_command")
        if not isinstance(login_command, str) or not login_command.strip():
            raise InvalidEntitlementConnectionError(
                "Official-session connection is not configured with a provider login command."
            )
        scopes = requested_scopes if requested_scopes is not None else source.get("oauth_scopes", [])
        instructions = {
            "official_auth_surface": source_metadata.get("official_auth_surface"),
            "login_command": login_command,
            "supported_connection_modes": supported_modes,
            "operator_action": "Run the official provider login flow on the authorized local runner, then complete this session with a SecretRef or local session reference.",
            "secret_handling": "BOSS stores only SecretRefs or local session references; agents never receive raw provider credentials.",
            "provider_rules": source.get("policy_notes") or [],
        }
        session_id = make_id("entconn")
        connection.execute(
            text(
                """
                INSERT INTO entitlement_connection_sessions (
                    id, project_id, source_id, provider, auth_method, connection_mode,
                    status, requested_scopes, login_command, instructions, metadata,
                    requested_by, expires_at
                )
                VALUES (
                    :id, :project_id, :source_id, :provider, :auth_method,
                    :connection_mode, 'operator_action_required',
                    CAST(:requested_scopes AS JSONB), :login_command,
                    CAST(:instructions AS JSONB), CAST(:metadata AS JSONB),
                    :requested_by, now() + interval '30 minutes'
                )
                """
            ),
            {
                "id": session_id,
                "project_id": project_id,
                "source_id": source_id,
                "provider": source["provider"],
                "auth_method": source.get("auth_method", "official_cli_oauth"),
                "connection_mode": resolved_mode,
                "requested_scopes": json.dumps(scopes or []),
                "login_command": login_command,
                "instructions": json.dumps(instructions),
                "metadata": json.dumps(metadata or {}),
                "requested_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "entitlement.connection_session_started",
            actor,
            "entitlement_connection_session",
            session_id,
            {
                "source_id": source_id,
                "provider": source["provider"],
                "auth_method": source.get("auth_method"),
                "connection_mode": resolved_mode,
            },
        )
        return self.get_connection_session(connection, project_id, session_id)

    def complete_connection_session(
        self,
        connection: Connection,
        project_id: str,
        session_id: str,
        *,
        actor: str,
        connection_ref: str,
        oauth_scopes: Optional[list[str]] = None,
        connection_metadata: Optional[dict[str, Any]] = None,
        observed_capacity: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        session = self.get_connection_session(connection, project_id, session_id)
        if session["status"] != "operator_action_required":
            raise EntitlementConnectionSessionStateError(
                f"Connection session '{session_id}' is {session['status']} and cannot be completed."
            )
        expired = connection.execute(
            text(
                """
                SELECT expires_at IS NOT NULL AND expires_at < now() AS expired
                FROM entitlement_connection_sessions
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {"project_id": project_id, "id": session_id},
        ).scalar_one()
        if expired:
            connection.execute(
                text(
                    """
                    UPDATE entitlement_connection_sessions
                    SET status = 'expired',
                        error = CAST(:error AS JSONB),
                        updated_at = now()
                    WHERE project_id = :project_id AND id = :id
                    """
                ),
                {
                    "project_id": project_id,
                    "id": session_id,
                    "error": json.dumps({"reason": "connection_session_expired"}),
                },
            )
            raise EntitlementConnectionSessionStateError(
                f"Connection session '{session_id}' has expired."
            )
        source = self.connect_source(
            connection,
            project_id,
            session["source_id"],
            actor=actor,
            connection_ref=connection_ref,
            auth_method=session["auth_method"],
            oauth_scopes=oauth_scopes or session.get("requested_scopes") or [],
            connection_metadata={
                **(connection_metadata or {}),
                "connection_session_id": session_id,
                "connection_mode": session["connection_mode"],
            },
            observed_capacity={
                **(observed_capacity or {}),
                "official_connection_session_id": session_id,
            },
        )
        connection.execute(
            text(
                """
                UPDATE entitlement_connection_sessions
                SET status = 'completed',
                    connection_ref = :connection_ref,
                    metadata = metadata || CAST(:metadata AS JSONB),
                    completed_by = :completed_by,
                    completed_at = now(),
                    updated_at = now()
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {
                "project_id": project_id,
                "id": session_id,
                "connection_ref": connection_ref,
                "metadata": json.dumps(connection_metadata or {}),
                "completed_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "entitlement.connection_session_completed",
            actor,
            "entitlement_connection_session",
            session_id,
            {
                "source_id": session["source_id"],
                "provider": session["provider"],
                "connection_status": source["connection_status"],
            },
        )
        return {
            "session": self.get_connection_session(connection, project_id, session_id),
            "source": source,
        }

    def connect_source(
        self,
        connection: Connection,
        project_id: str,
        source_id: str,
        *,
        actor: str,
        connection_ref: str,
        auth_method: Optional[str] = None,
        oauth_scopes: Optional[list[str]] = None,
        connection_metadata: Optional[dict[str, Any]] = None,
        observed_capacity: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not connection_ref.strip():
            raise InvalidEntitlementConnectionError(
                "connection_ref is required and must point to a SecretRef or local official session reference."
            )
        source = self.get_source(connection, project_id, source_id)
        resolved_auth_method = auth_method or source.get("auth_method", "api_key")
        if self._requires_official_oauth(source) and (
            resolved_auth_method not in self.OFFICIAL_OAUTH_METHODS
        ):
            raise InvalidEntitlementConnectionError(
                "Subscription-backed public model sources must use an official OAuth connection method."
            )
        merged_capacity = {
            **(source.get("observed_capacity") or {}),
            **(observed_capacity or {}),
            "credential_present": True,
            "runtime_supported": True,
            "official_oauth_connected": bool(self._requires_official_oauth(source)),
        }
        merged_capacity.setdefault("capacity_state", "available")
        connected = self.upsert_source(
            connection,
            {
                **source,
                "auth_method": resolved_auth_method,
                "connection_status": "connected",
                "connection_ref": connection_ref,
                "oauth_scopes": oauth_scopes
                if oauth_scopes is not None
                else source.get("oauth_scopes", []),
                "connection_metadata": {
                    **(source.get("connection_metadata") or {}),
                    **(connection_metadata or {}),
                    "connection_recorded_by": actor,
                },
                "observed_capacity": merged_capacity,
                "runtime_supported": True,
                "status": "active",
            },
            actor=actor,
            emit_events=False,
        )
        emit_event(
            connection,
            project_id,
            "entitlement.source_connected",
            actor,
            "entitlement_source",
            connected["id"],
            {
                "source_id": source_id,
                "provider": connected["provider"],
                "auth_method": connected["auth_method"],
                "connection_status": connected["connection_status"],
                "capacity_state": connected["observed_capacity"].get("capacity_state"),
            },
        )
        return connected

    def disconnect_source(
        self,
        connection: Connection,
        project_id: str,
        source_id: str,
        *,
        actor: str,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        source = self.get_source(connection, project_id, source_id)
        observed_capacity = {
            **(source.get("observed_capacity") or {}),
            "credential_present": False,
            "official_oauth_connected": False,
            "capacity_state": "unknown",
        }
        disconnected = self.upsert_source(
            connection,
            {
                **source,
                "connection_status": "revoked",
                "observed_capacity": observed_capacity,
                "status": "missing_credentials",
            },
            actor=actor,
            emit_events=False,
        )
        emit_event(
            connection,
            project_id,
            "entitlement.source_disconnected",
            actor,
            "entitlement_source",
            disconnected["id"],
            {
                "source_id": source_id,
                "provider": disconnected["provider"],
                "reason": reason or "operator_disconnected",
            },
        )
        return disconnected

    def _allowed_tiers_for_budget_state(self, budget_state: str) -> list[str]:
        registry = self._provider_registry()
        return (
            ((registry.get("routing_policy") or {}).get("budget_states") or {})
            .get(budget_state, {})
            .get("allowed_tiers", [])
        )

    def _model_registry(self) -> list[dict[str, Any]]:
        return self._provider_registry().get("models", [])

    def _models_for_source(
        self, provider: str, supported_models: list[str], allowed_tiers: list[str]
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for model in self._model_registry():
            model_id = model.get("id")
            if not isinstance(model_id, str):
                continue
            if model.get("provider") != provider:
                continue
            suffix = model_id.split(":", 1)[-1]
            if supported_models and suffix not in supported_models and model_id not in supported_models:
                continue
            if allowed_tiers and model.get("tier") not in allowed_tiers:
                continue
            candidates.append(model)
        if provider == "boss":
            known_ids = {model["id"] for model in candidates}
            for model_name in supported_models:
                model_id = (
                    model_name if model_name.startswith("boss:") else f"boss:{model_name}"
                )
                if model_id in known_ids:
                    continue
                if allowed_tiers and "mid" not in allowed_tiers:
                    continue
                candidates.append(
                    {
                        "id": model_id,
                        "provider": "boss",
                        "tier": "mid",
                        "strengths": [
                            "planning",
                            "coding",
                            "reasoning",
                            "review",
                            "privacy_sensitive",
                        ],
                        "cost_class": "gateway_metered",
                        "enabled": True,
                    }
                )
        return candidates

    def _pick_model(
        self,
        models: list[dict[str, Any]],
        preferred_capabilities: list[str],
        task_kind: str,
    ) -> Optional[dict[str, Any]]:
        if not models:
            return None

        def score(model: dict[str, Any]) -> tuple[int, str]:
            strengths = set(model.get("strengths", []))
            capability_score = len(strengths.intersection(preferred_capabilities))
            task_score = 1 if task_kind in strengths else 0
            return (capability_score * 10 + task_score, model.get("id", ""))

        return sorted(models, key=score, reverse=True)[0]

    def _allowed_data_boundaries_for_source(
        self, source: dict[str, Any]
    ) -> set[str]:
        defaults = self._source_record_defaults().get(source["source_id"], {})
        configured = defaults.get("allowed_data_boundaries")
        if isinstance(configured, list) and configured:
            return {normalize_data_boundary(str(item)) for item in configured}
        boundary_policy = (
            (self._provider_registry().get("routing_policy") or {}).get(
                "data_boundaries"
            )
            or {}
        )
        if source.get("source_kind") == "approved_local_model":
            configured = boundary_policy.get("approved_local_model_default")
            if isinstance(configured, list) and configured:
                return {normalize_data_boundary(str(item)) for item in configured}
            return set(DATA_BOUNDARIES)
        if (
            self._paid_gateway is not None
            and source.get("source_kind") == self._paid_gateway.source_kind
        ):
            configured = boundary_policy.get(f"{self._paid_gateway.source_kind}_default")
            if not isinstance(configured, list) or not configured:
                configured = self._paid_gateway.data_boundaries()
            return {normalize_data_boundary(str(item)) for item in configured}
        if source.get("source_kind") == "managed_cloud_model_gateway":
            configured = boundary_policy.get("managed_cloud_model_gateway_default")
            if isinstance(configured, list) and configured:
                return {normalize_data_boundary(str(item)) for item in configured}
            return {"public", "internal", "confidential"}
        configured = boundary_policy.get("external_official_source_default")
        if isinstance(configured, list) and configured:
            return {normalize_data_boundary(str(item)) for item in configured}
        return {"public", "internal", "confidential"}

    def list_routing_logs(
        self,
        connection: Connection,
        project_id: str,
        limit: int = 25,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        filters = filters or {}
        conditions = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}

        equality_columns = {
            "skill_id": "skill_id",
            "task_kind": "task_kind",
            "workload_class": "workload_class",
            "budget_state": "budget_state",
            "selected_provider": "selected_provider",
            "selected_source_id": "selected_source_id",
            "selected_model_id": "selected_model_id",
            "denial_reason": "denial_reason",
        }
        for key, column in equality_columns.items():
            value = filters.get(key)
            if value is not None:
                conditions.append(f"{column} = :{key}")
                params[key] = value

        if filters.get("data_boundary") is not None:
            conditions.append("data_boundary = :data_boundary")
            params["data_boundary"] = normalize_data_boundary(filters["data_boundary"])

        if filters.get("risk_tier") is not None:
            conditions.append("risk_tier = :risk_tier")
            params["risk_tier"] = int(filters["risk_tier"])

        if filters.get("routing_mode") is not None:
            conditions.append(
                "COALESCE(NULLIF(decision->>'routing_mode', ''), 'budget_safe') = :routing_mode"
            )
            params["routing_mode"] = filters["routing_mode"]

        if filters.get("denied_only"):
            conditions.append("denial_reason IS NOT NULL")
        if filters.get("selected_only"):
            conditions.append("selected_source_id IS NOT NULL AND denial_reason IS NULL")

        where_clause = " AND ".join(conditions)
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, skill_id, task_kind, workload_class, risk_tier,
                       data_boundary, budget_state, selected_source_id, selected_provider,
                       selected_model_id, denial_reason, rejected_sources, decision,
                       created_at
                FROM routing_logs
                WHERE {where_clause}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [
            {
                **dict(row),
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    def get_routing_log(
        self, connection: Connection, project_id: str, route_decision_log_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, skill_id, task_kind, workload_class, risk_tier,
                       data_boundary, budget_state, selected_source_id, selected_provider,
                       selected_model_id, denial_reason, rejected_sources, decision,
                       created_at
                FROM routing_logs
                WHERE project_id = :project_id AND id = :id
                """
            ),
            {"project_id": project_id, "id": route_decision_log_id},
        ).mappings().first()
        if row is None:
            raise SkillNotFoundError(
                f"Route decision '{route_decision_log_id}' not found for project '{project_id}'."
            )
        payload = dict(row)
        payload["created_at"] = row["created_at"].isoformat()
        return payload

    def list_model_route_decisions(
        self,
        connection: Connection,
        project_id: str,
        limit: int = 25,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        return [
            self._route_decision_from_log(log)
            for log in self.list_routing_logs(connection, project_id, limit, filters)
        ]

    def simulate_model_route_decision(
        self,
        connection: Connection,
        project_id: str,
        *,
        skill_id: str,
        task_kind: str,
        workload_class: str,
        risk_tier: int,
        preferred_capabilities: Optional[list[str]] = None,
        budget_state: Optional[str] = None,
        data_boundary: str = "internal",
        actor: str = "system",
        only_source_id: Optional[str] = None,
    ) -> dict[str, Any]:
        route = self.preview_route(
            connection,
            project_id,
            skill_id=skill_id,
            task_kind=task_kind,
            workload_class=workload_class,
            risk_tier=risk_tier,
            preferred_capabilities=preferred_capabilities,
            budget_state=budget_state,
            data_boundary=data_boundary,
            actor=actor,
            only_source_id=only_source_id,
        )
        log = self.get_routing_log(
            connection, project_id, route["route_decision_log_id"]
        )
        return self._route_decision_from_log(log)

    def preview_route(
        self,
        connection: Connection,
        project_id: str,
        *,
        skill_id: str,
        task_kind: str,
        workload_class: str,
        risk_tier: int,
        preferred_capabilities: Optional[list[str]] = None,
        budget_state: Optional[str] = None,
        data_boundary: str = "internal",
        actor: str = "system",
        only_source_id: Optional[str] = None,
    ) -> dict[str, Any]:
        preferred_capabilities = preferred_capabilities or []
        skill = self.get_skill(skill_id, project_id, connection=connection)
        boundary = normalize_data_boundary(data_boundary)
        budget_snapshot = self.budget.status_for_project(connection, project_id)
        resolved_budget_state = budget_state or budget_snapshot["budget_state"]
        registry = self._provider_registry()
        routing_policy = registry.get("routing_policy", {}) or {}
        preferred_source_kinds = (
            ((routing_policy.get("workload_class_defaults") or {}).get(workload_class) or {})
            .get("preferred_source_kinds", [])
        )
        disallowed_combinations = routing_policy.get("disallowed_combinations", []) or []
        allowed_tiers = self._allowed_tiers_for_budget_state(resolved_budget_state)
        budget_state_policy = (
            ((routing_policy.get("budget_states") or {}).get(resolved_budget_state) or {})
        )
        model_tiers_explicitly_empty = (
            "allowed_tiers" in budget_state_policy and not allowed_tiers
        )

        candidates: list[tuple[int, dict[str, Any], Optional[dict[str, Any]]]] = []
        rejected_sources: list[dict[str, Any]] = []
        for source in self.list_sources(connection, project_id):
            # PINNED: the caller already knows which provider will answer (a typed decision on
            # TypeSafe, or on the Claude CLI), so the lease must be booked against that source
            # and no other. Every policy check below still applies to it.
            if only_source_id and source["source_id"] != only_source_id:
                continue
            # A decisions-only source (TypeSafe Jev) cannot write code; never offer it for
            # anything but a typed decision, however cheap it scores.
            if (self._connection_metadata(source).get("decisions_only")
                    and skill_id != "typed_decision"):
                self._reject_source(rejected_sources, source, "source_reserved_for_decisions")
                continue
            effective_status = source["effective_status"]
            if effective_status != "active":
                self._reject_source(rejected_sources, source, effective_status)
                continue
            if not source["runtime_supported"]:
                self._reject_source(rejected_sources, source, "runtime_unavailable")
                continue
            if workload_class not in (source.get("workload_classes_allowed") or []):
                self._reject_source(
                    rejected_sources, source, "workload_class_not_allowed"
                )
                continue
            if workload_class == "multi_tenant_customer" and not source.get(
                "multi_tenant_allowed", False
            ):
                self._reject_source(
                    rejected_sources, source, "multi_tenant_not_allowed"
                )
                continue
            if risk_tier > int(source.get("risk_tier_max", 0)):
                self._reject_source(
                    rejected_sources, source, "risk_tier_exceeds_source_limit"
                )
                continue
            if any(
                combo.get("source_kind") == source["source_kind"]
                and combo.get("workload_class") == workload_class
                for combo in disallowed_combinations
            ):
                self._reject_source(
                    rejected_sources, source, "provider_policy_disallows_workload"
                )
                continue
            connection_denial = self._connection_policy_denial(source)
            if connection_denial:
                self._reject_source(rejected_sources, source, connection_denial)
                continue
            capacity_denial = self._capacity_policy_denial(source)
            if capacity_denial:
                self._reject_source(rejected_sources, source, capacity_denial)
                continue
            if boundary not in self._allowed_data_boundaries_for_source(source):
                self._reject_source(
                    rejected_sources,
                    source,
                    "data_boundary_not_allowed",
                    data_boundary=boundary,
                )
                continue
            if model_tiers_explicitly_empty and source["source_kind"] != "approved_local_model":
                self._reject_source(
                    rejected_sources,
                    source,
                    "budget_state_disallows_model_spend",
                    budget_state=resolved_budget_state,
                )
                continue

            model_candidates = self._models_for_source(
                source["provider"],
                source.get("supported_models") or [],
                allowed_tiers,
            )
            if allowed_tiers and not model_candidates and source["source_kind"] == "official_api":
                self._reject_source(
                    rejected_sources, source, "no_budget_eligible_model_tier"
                )
                continue
            selected_model = self._pick_model(
                model_candidates, preferred_capabilities, task_kind
            )
            source_strengths = set(source.get("task_strengths") or [])
            capability_score = len(source_strengths.intersection(preferred_capabilities))
            if task_kind in source_strengths:
                capability_score += 1
            try:
                preference_score = 100 - preferred_source_kinds.index(source["source_kind"]) * 10
            except ValueError:
                preference_score = 0
            candidates.append(
                (
                    preference_score + capability_score,
                    source,
                    selected_model,
                )
            )

        selected_source: Optional[dict[str, Any]] = None
        selected_model: Optional[dict[str, Any]] = None
        denial_reason: Optional[str] = None
        if candidates:
            candidates.sort(
                key=lambda item: (
                    item[0],
                    item[2].get("id", "") if item[2] else "",
                    item[1]["source_id"],
                ),
                reverse=True,
            )
            _, selected_source, selected_model = candidates[0]
        else:
            denial_reason = "no_eligible_source"

        decision = {
            "skill_id": skill_id,
            "task_kind": task_kind,
            "data_boundary": boundary,
            "reservation_required": bool(
                (skill.get("policy") or {}).get("requires_budget_reservation", False)
            ),
            "preferred_capabilities": preferred_capabilities,
            "fallback_order": [item[1]["source_id"] for item in candidates[1:4]],
            "provider_rule_compliance": selected_source is not None,
            "connection_ref": selected_source.get("connection_ref")
            if selected_source
            else None,
            "auth_method": selected_source.get("auth_method")
            if selected_source
            else None,
            "selected_source": self._route_context_for_source(selected_source)
            if selected_source
            else None,
            "eligible_sources": [
                self._route_context_for_source(item[1]) for item in candidates[:5]
            ],
            "rejected_source_count": len(rejected_sources),
        }
        log_id = make_id("route")
        connection.execute(
            text(
                """
                INSERT INTO routing_logs (
                    id, project_id, skill_id, task_kind, workload_class, risk_tier,
                    data_boundary, budget_state, selected_source_id, selected_provider,
                    selected_model_id, denial_reason, rejected_sources, decision
                )
                VALUES (
                    :id, :project_id, :skill_id, :task_kind, :workload_class, :risk_tier,
                    :data_boundary, :budget_state, :selected_source_id, :selected_provider,
                    :selected_model_id, :denial_reason, CAST(:rejected_sources AS JSONB),
                    CAST(:decision AS JSONB)
                )
                """
            ),
            {
                "id": log_id,
                "project_id": project_id,
                "skill_id": skill_id,
                "task_kind": task_kind,
                "workload_class": workload_class,
                "risk_tier": risk_tier,
                "data_boundary": boundary,
                "budget_state": resolved_budget_state,
                "selected_source_id": selected_source["source_id"] if selected_source else None,
                "selected_provider": selected_source["provider"] if selected_source else None,
                "selected_model_id": selected_model.get("id") if selected_model else None,
                "denial_reason": denial_reason,
                "rejected_sources": json.dumps(rejected_sources),
                "decision": json.dumps(decision),
            },
        )
        emit_event(
            connection,
            project_id,
            "routing.selected" if selected_source else "routing.denied",
            actor,
            "routing_log",
            log_id,
            {
                "skill_id": skill_id,
                "task_kind": task_kind,
                "data_boundary": boundary,
                "selected_source_id": selected_source["source_id"] if selected_source else None,
                "selected_model_id": selected_model.get("id") if selected_model else None,
                "denial_reason": denial_reason,
            },
        )
        return {
            "route_decision_log_id": log_id,
            "skill_id": skill_id,
            "task_kind": task_kind,
            "workload_class": workload_class,
            "risk_tier": risk_tier,
            "data_boundary": boundary,
            "budget_state": resolved_budget_state,
            "source_id": selected_source["source_id"] if selected_source else None,
            "provider": selected_source["provider"] if selected_source else None,
            "model_id": selected_model.get("id") if selected_model else None,
            "denial_reason": denial_reason,
            "rejected_sources": rejected_sources,
            "decision": decision,
        }
