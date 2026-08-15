from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .github_adapter import GitHubAdapter
from .receipts import ReceiptService
from .secret_backends import SecretUnavailable, backend_for, resolve_secret_ref
from .secret_refs import SecretRefService


class GitHubConnectionNotFoundError(RuntimeError):
    pass


class GitHubRepositoryBindingNotFoundError(RuntimeError):
    pass


class GitHubConnectionStateError(RuntimeError):
    pass


class GitHubConnectionService:
    """Project-scoped GitHub connection and repository target registry.

    The registry stores only SecretRef pointers. Secret values are resolved at
    execution time and passed directly to the deterministic GitHub adapter.
    """

    AUTH_METHODS = {"github_app", "personal_access_token"}
    CONNECTION_STATUSES = {"configured", "active", "error", "disabled", "revoked"}
    REPOSITORY_ROLES = {"primary", "source", "upstream", "mirror"}
    REPOSITORY_STATUSES = {"active", "disabled"}
    REPOSITORY_PERMISSIONS = {"pull", "triage", "push", "maintain", "admin"}
    _REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]+$")

    def __init__(
        self,
        settings: Settings,
        *,
        adapter_factory: Optional[Callable[..., GitHubAdapter]] = None,
        secret_resolver: Optional[Callable[[dict[str, Any]], str]] = None,
    ) -> None:
        self.settings = settings
        self.receipts = ReceiptService()
        self.secret_refs = SecretRefService()
        self.adapter_factory = adapter_factory or GitHubAdapter
        self._custom_secret_resolver = secret_resolver
        self.secret_resolver = secret_resolver or self._resolve_secret
        self._active_connection: Optional[Connection] = None

    def list_connections(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, connection_label, auth_method,
                       credential_ref, app_id, installation_id, api_base_url,
                       status, required_permissions, granted_permissions,
                       missing_permissions, account_login, metadata,
                       last_test_status, last_tested_at, last_error,
                       disabled_at, disabled_by, revoked_at, revoked_by,
                       created_by, created_at, updated_at
                FROM github_connections
                WHERE project_id = :project_id
                ORDER BY connection_label, created_at
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize_connection(row) for row in rows]

    def get_connection(
        self,
        connection: Connection,
        project_id: str,
        connection_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, connection_label, auth_method,
                       credential_ref, app_id, installation_id, api_base_url,
                       status, required_permissions, granted_permissions,
                       missing_permissions, account_login, metadata,
                       last_test_status, last_tested_at, last_error,
                       disabled_at, disabled_by, revoked_at, revoked_by,
                       created_by, created_at, updated_at
                FROM github_connections
                WHERE project_id = :project_id AND id = :connection_id
                """
            ),
            {"project_id": project_id, "connection_id": connection_id},
        ).mappings().first()
        if row is None:
            raise GitHubConnectionNotFoundError("GitHub connection not found.")
        return self._serialize_connection(row)

    def create_connection(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_label: str,
        auth_method: str,
        credential_ref: str,
        app_id: Optional[str],
        installation_id: Optional[str],
        api_base_url: str,
        required_permissions: list[str],
        metadata: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        label = self._required_text(connection_label, "connection_label")
        method = auth_method.strip().lower()
        if method not in self.AUTH_METHODS:
            raise ValueError(
                "auth_method must be one of: "
                + ", ".join(sorted(self.AUTH_METHODS))
                + "."
            )
        safe_api_base = self._normalize_api_base_url(api_base_url)
        secret_ref = self.secret_refs.require_usable(
            connection,
            project_id=project_id,
            credential_ref=credential_ref,
        )
        if secret_ref["provider"] not in {None, "github"}:
            raise ValueError("GitHub connections require a GitHub SecretRef.")
        if method == "github_app":
            if not (app_id and app_id.strip()):
                raise ValueError("github_app connections require app_id.")
            if not (installation_id and installation_id.strip()):
                raise ValueError("github_app connections require installation_id.")
            if secret_ref["secret_kind"] not in {"private_key", "client_secret"}:
                raise ValueError(
                    "github_app connections require a private_key SecretRef."
                )
        elif secret_ref["secret_kind"] not in {"api_token", "access_key", "session"}:
            raise ValueError(
                "personal_access_token connections require an api_token SecretRef."
            )
        permissions = self._normalize_permissions(required_permissions or ["pull"])
        safe_metadata = self.secret_refs._validate_metadata(metadata)
        duplicate = connection.execute(
            text(
                """
                SELECT id
                FROM github_connections
                WHERE project_id = :project_id
                  AND lower(connection_label) = lower(:connection_label)
                """
            ),
            {"project_id": project_id, "connection_label": label},
        ).first()
        if duplicate:
            raise ValueError(
                "Project already has a GitHub connection with this label."
            )
        connection_id = make_id("ghconn")
        connection.execute(
            text(
                """
                INSERT INTO github_connections (
                    id, project_id, connection_label, auth_method, credential_ref,
                    app_id, installation_id, api_base_url, status,
                    required_permissions, metadata, created_by
                )
                VALUES (
                    :id, :project_id, :connection_label, :auth_method,
                    :credential_ref, :app_id, :installation_id, :api_base_url,
                    'configured', CAST(:required_permissions AS JSONB),
                    CAST(:metadata AS JSONB), :actor
                )
                """
            ),
            {
                "id": connection_id,
                "project_id": project_id,
                "connection_label": label,
                "auth_method": method,
                "credential_ref": credential_ref,
                "app_id": app_id.strip() if app_id else None,
                "installation_id": (
                    installation_id.strip() if installation_id else None
                ),
                "api_base_url": safe_api_base,
                "required_permissions": json.dumps(permissions),
                "metadata": json.dumps(safe_metadata),
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.connection_created",
            actor,
            "github_connection",
            connection_id,
            {
                "connection_label": label,
                "auth_method": method,
                "api_base_url": safe_api_base,
                "required_permissions": permissions,
                "credential_configured": True,
            },
        )
        return self.get_connection(connection, project_id, connection_id)

    def transition_connection(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_id: str,
        action: str,
        actor: str,
    ) -> dict[str, Any]:
        existing = self.get_connection(connection, project_id, connection_id)
        normalized_action = action.strip().lower()
        if normalized_action not in {"enable", "disable", "revoke"}:
            raise ValueError("action must be enable, disable, or revoke.")
        if existing["status"] == "revoked":
            raise GitHubConnectionStateError(
                "Revoked GitHub connections cannot be re-enabled or changed."
            )
        if normalized_action == "enable":
            self.secret_refs.require_usable(
                connection,
                project_id=project_id,
                credential_ref=self._credential_ref(
                    connection, project_id, connection_id
                ),
            )
            next_status = "configured"
        elif normalized_action == "disable":
            next_status = "disabled"
        else:
            next_status = "revoked"
        connection.execute(
            text(
                """
                UPDATE github_connections
                SET status = :status,
                    last_test_status = CASE
                        WHEN :status = 'configured' THEN NULL
                        ELSE last_test_status
                    END,
                    disabled_at = CASE
                        WHEN :status = 'disabled' THEN now()
                        WHEN :status = 'configured' THEN NULL
                        ELSE disabled_at
                    END,
                    disabled_by = CASE
                        WHEN :status = 'disabled' THEN :actor
                        WHEN :status = 'configured' THEN NULL
                        ELSE disabled_by
                    END,
                    revoked_at = CASE
                        WHEN :status = 'revoked' THEN now()
                        ELSE revoked_at
                    END,
                    revoked_by = CASE
                        WHEN :status = 'revoked' THEN :actor
                        ELSE revoked_by
                    END,
                    updated_at = now()
                WHERE project_id = :project_id AND id = :connection_id
                """
            ),
            {
                "project_id": project_id,
                "connection_id": connection_id,
                "status": next_status,
                "actor": actor,
            },
        )
        if next_status == "revoked":
            connection.execute(
                text(
                    """
                    UPDATE github_repository_bindings
                    SET status = 'disabled', updated_at = now()
                    WHERE project_id = :project_id
                      AND connection_id = :connection_id
                    """
                ),
                {"project_id": project_id, "connection_id": connection_id},
            )
        emit_event(
            connection,
            project_id,
            f"github.connection_{normalized_action}d",
            actor,
            "github_connection",
            connection_id,
            {"previous_status": existing["status"], "status": next_status},
        )
        return self.get_connection(connection, project_id, connection_id)

    def list_repository_bindings(
        self,
        connection: Connection,
        project_id: str,
        *,
        connection_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id}
        if connection_id:
            filters.append("connection_id = :connection_id")
            params["connection_id"] = connection_id
        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, connection_id, repository_id, owner, name,
                       full_name, role, default_branch, visibility, archived,
                       status, permissions, capabilities, metadata, last_synced_at,
                       created_by, created_at, updated_at
                FROM github_repository_bindings
                WHERE {" AND ".join(filters)}
                ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END,
                         full_name, created_at
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_binding(row) for row in rows]

    def get_repository_binding(
        self,
        connection: Connection,
        project_id: str,
        binding_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, connection_id, repository_id, owner, name,
                       full_name, role, default_branch, visibility, archived,
                       status, permissions, capabilities, metadata, last_synced_at,
                       created_by, created_at, updated_at
                FROM github_repository_bindings
                WHERE project_id = :project_id AND id = :binding_id
                """
            ),
            {"project_id": project_id, "binding_id": binding_id},
        ).mappings().first()
        if row is None:
            raise GitHubRepositoryBindingNotFoundError(
                "GitHub repository binding not found."
            )
        return self._serialize_binding(row)

    def create_repository_binding(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_id: str,
        repository: str,
        role: str,
        default_branch: Optional[str],
        metadata: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        github_connection = self.get_connection(
            connection, project_id, connection_id
        )
        if github_connection["status"] in {"disabled", "revoked"}:
            raise GitHubConnectionStateError(
                f"GitHub connection is {github_connection['status']}."
            )
        owner, name, full_name = self._normalize_repository(repository)
        normalized_role = role.strip().lower()
        if normalized_role not in self.REPOSITORY_ROLES:
            raise ValueError(
                "role must be one of: "
                + ", ".join(sorted(self.REPOSITORY_ROLES))
                + "."
            )
        if normalized_role == "primary":
            primary = connection.execute(
                text(
                    """
                    SELECT id
                    FROM github_repository_bindings
                    WHERE project_id = :project_id
                      AND role = 'primary'
                      AND status = 'active'
                    """
                ),
                {"project_id": project_id},
            ).first()
            if primary:
                raise ValueError("Project already has an active primary repository.")
        duplicate = connection.execute(
            text(
                """
                SELECT id
                FROM github_repository_bindings
                WHERE project_id = :project_id
                  AND lower(full_name) = lower(:full_name)
                """
            ),
            {"project_id": project_id, "full_name": full_name},
        ).first()
        if duplicate:
            raise ValueError("Repository is already bound to this project.")
        safe_metadata = self.secret_refs._validate_metadata(metadata)
        binding_id = make_id("ghrepo")
        connection.execute(
            text(
                """
                INSERT INTO github_repository_bindings (
                    id, project_id, connection_id, owner, name, full_name, role,
                    default_branch, metadata, created_by
                )
                VALUES (
                    :id, :project_id, :connection_id, :owner, :name, :full_name,
                    :role, :default_branch, CAST(:metadata AS JSONB), :actor
                )
                """
            ),
            {
                "id": binding_id,
                "project_id": project_id,
                "connection_id": connection_id,
                "owner": owner,
                "name": name,
                "full_name": full_name,
                "role": normalized_role,
                "default_branch": (
                    self._required_text(default_branch, "default_branch")
                    if default_branch
                    else None
                ),
                "metadata": json.dumps(safe_metadata),
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.repository_bound",
            actor,
            "github_repository_binding",
            binding_id,
            {
                "connection_id": connection_id,
                "repository": full_name,
                "role": normalized_role,
            },
        )
        return self.get_repository_binding(connection, project_id, binding_id)

    def update_repository_binding(
        self,
        connection: Connection,
        *,
        project_id: str,
        binding_id: str,
        role: Optional[str],
        status: Optional[str],
        default_branch: Optional[str],
        metadata: Optional[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        existing = self.get_repository_binding(connection, project_id, binding_id)
        github_connection = self.get_connection(
            connection, project_id, existing["connection_id"]
        )
        next_role = (role or existing["role"]).strip().lower()
        next_status = (status or existing["status"]).strip().lower()
        if next_role not in self.REPOSITORY_ROLES:
            raise ValueError(
                "role must be one of: "
                + ", ".join(sorted(self.REPOSITORY_ROLES))
                + "."
            )
        if next_status not in self.REPOSITORY_STATUSES:
            raise ValueError("status must be active or disabled.")
        if next_status == "active" and github_connection["status"] == "revoked":
            raise GitHubConnectionStateError(
                "A binding on a revoked GitHub connection cannot be activated."
            )
        if next_role == "primary" and next_status == "active":
            primary = connection.execute(
                text(
                    """
                    SELECT id
                    FROM github_repository_bindings
                    WHERE project_id = :project_id
                      AND role = 'primary'
                      AND status = 'active'
                      AND id <> :binding_id
                    """
                ),
                {"project_id": project_id, "binding_id": binding_id},
            ).first()
            if primary:
                raise ValueError("Project already has an active primary repository.")
        next_metadata = (
            self.secret_refs._validate_metadata(metadata)
            if metadata is not None
            else existing["metadata"]
        )
        next_default_branch = (
            self._required_text(default_branch, "default_branch")
            if default_branch is not None
            else existing["default_branch"]
        )
        connection.execute(
            text(
                """
                UPDATE github_repository_bindings
                SET role = :role,
                    status = :status,
                    default_branch = :default_branch,
                    metadata = CAST(:metadata AS JSONB),
                    updated_at = now()
                WHERE project_id = :project_id AND id = :binding_id
                """
            ),
            {
                "project_id": project_id,
                "binding_id": binding_id,
                "role": next_role,
                "status": next_status,
                "default_branch": next_default_branch,
                "metadata": json.dumps(next_metadata),
            },
        )
        emit_event(
            connection,
            project_id,
            "github.repository_binding_updated",
            actor,
            "github_repository_binding",
            binding_id,
            {
                "role": next_role,
                "status": next_status,
                "default_branch": next_default_branch,
            },
        )
        return self.get_repository_binding(connection, project_id, binding_id)

    def test_connection(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_id: str,
        binding_id: str,
        dedupe_key: str,
        actor: str,
    ) -> dict[str, Any]:
        github_connection = self.get_connection(
            connection, project_id, connection_id
        )
        if github_connection["status"] in {"disabled", "revoked"}:
            raise GitHubConnectionStateError(
                f"GitHub connection is {github_connection['status']}."
            )
        binding = self.get_repository_binding(connection, project_id, binding_id)
        if binding["connection_id"] != connection_id:
            raise ValueError(
                "Repository binding does not belong to the GitHub connection."
            )
        if binding["status"] != "active":
            raise GitHubConnectionStateError("GitHub repository binding is disabled.")
        safe_dedupe_key = self._required_text(dedupe_key, "dedupe_key")
        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            safe_dedupe_key,
            "github.repository.read",
            {
                "connection_id": connection_id,
                "repository_binding_id": binding_id,
                "repository": binding["full_name"],
            },
            {"operation": "test_connection"},
            data_boundary="internal",
        )
        if not created:
            receipt_scope = receipt.get("effect_scope") or {}
            if (
                receipt.get("project_id") != project_id
                or receipt.get("effect_kind") != "github.repository.read"
                or receipt_scope.get("connection_id") != connection_id
                or receipt_scope.get("repository_binding_id") != binding_id
                or receipt_scope.get("repository") != binding["full_name"]
            ):
                raise ValueError(
                    "dedupe_key is already bound to a different GitHub connection test."
                )
        if not created and receipt["status"] == "succeeded":
            return {
                "status": (receipt.get("result") or {}).get("status", "passed"),
                "duplicate": True,
                "receipt_id": receipt["id"],
                "connection": github_connection,
                "repository_binding": binding,
                "detail": receipt.get("result") or {},
            }

        try:
            adapter = self._make_adapter(
                connection,
                project_id=project_id,
                github_connection=github_connection,
                binding=binding,
            )
            repository = adapter.repo_metadata()
            returned_full_name = str(repository.get("full_name") or "").strip()
            if returned_full_name.casefold() != binding["full_name"].casefold():
                raise RuntimeError("GitHub returned a different repository identity.")
            permission_map = {
                str(key): bool(value)
                for key, value in (repository.get("permissions") or {}).items()
                if str(key) in self.REPOSITORY_PERMISSIONS
            }
            granted = sorted(key for key, value in permission_map.items() if value)
            missing = sorted(
                permission
                for permission in github_connection["required_permissions"]
                if permission not in granted
            )
            capabilities = self._capabilities(granted)
            status = "passed" if not missing else "failed"
            safe_result = {
                "status": status,
                "repository": returned_full_name,
                "repository_id": repository.get("id"),
                "default_branch": repository.get("default_branch"),
                "visibility": repository.get("visibility"),
                "archived": bool(repository.get("archived", False)),
                "account_login": (
                    (repository.get("owner") or {}).get("login")
                    if isinstance(repository.get("owner"), dict)
                    else None
                ),
                "granted_permissions": granted,
                "missing_permissions": missing,
                "capabilities": capabilities,
            }
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                str(repository.get("html_url") or binding["full_name"]),
                safe_result,
            )
            self._record_test_result(
                connection,
                project_id=project_id,
                connection_id=connection_id,
                binding_id=binding_id,
                status=status,
                result=safe_result,
                actor=actor,
            )
            return {
                "status": status,
                "duplicate": False,
                "receipt_id": receipt["id"],
                "connection": self.get_connection(
                    connection, project_id, connection_id
                ),
                "repository_binding": self.get_repository_binding(
                    connection, project_id, binding_id
                ),
                "detail": safe_result,
            }
        except Exception as error:
            safe_error = {
                "type": error.__class__.__name__,
                "message": "GitHub connection test failed.",
            }
            self.receipts.mark_failed(connection, receipt["id"], safe_error)
            self._record_test_failure(
                connection,
                project_id=project_id,
                connection_id=connection_id,
                actor=actor,
                error=safe_error,
            )
            return {
                "status": "failed",
                "duplicate": False,
                "receipt_id": receipt["id"],
                "connection": self.get_connection(
                    connection, project_id, connection_id
                ),
                "repository_binding": binding,
                "detail": {"status": "failed", "message": safe_error["message"]},
            }

    def ensure_env_credential_ref(
        self,
        connection: Connection,
        *,
        project_id: str,
        env_key: str,
        auth_method: str,
        actor: str,
    ) -> str:
        """Ensure an env SecretRef exists and return credential_ref (env:KEY).

        Easy project link never accepts raw tokens — only environment names.
        """
        key = self._required_text(env_key, "env_credential_key").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(
                "env_credential_key must be an environment variable name "
                "(letters, digits, underscore)."
            )
        method = auth_method.strip().lower()
        secret_kind = (
            "private_key" if method == "github_app" else "api_token"
        )
        credential_ref = f"env:{key}"
        existing = self.secret_refs.list_secret_refs(
            connection, project_id, provider="github", status="active", limit=200
        )
        for item in existing:
            if item.get("ref_kind") == "env" and item.get("ref_key") == key:
                if item.get("secret_kind") not in {
                    secret_kind,
                    "api_token",
                    "private_key",
                    "client_secret",
                    "access_key",
                    "session",
                }:
                    raise ValueError(
                        f"SecretRef {credential_ref} exists with incompatible secret_kind."
                    )
                return credential_ref
        # Also accept any provider=None github-ish active env ref
        any_refs = self.secret_refs.list_secret_refs(
            connection, project_id, status="active", limit=200
        )
        for item in any_refs:
            if item.get("ref_kind") == "env" and item.get("ref_key") == key:
                if item.get("provider") not in {None, "github"}:
                    raise ValueError(
                        f"SecretRef {credential_ref} already belongs to provider "
                        f"{item.get('provider')}."
                    )
                return credential_ref
        self.secret_refs.create_secret_ref(
            connection,
            project_id=project_id,
            ref_kind="env",
            ref_key=key,
            provider="github",
            status="active",
            metadata={"purpose": "github_project_link"},
            actor=actor,
            secret_kind=secret_kind,
        )
        return credential_ref

    def easy_connect(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_label: str,
        auth_method: str,
        env_credential_key: str,
        app_id: Optional[str],
        installation_id: Optional[str],
        api_base_url: str,
        required_permissions: list[str],
        repository: Optional[str],
        role: str,
        test_after_link: bool,
        metadata: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        """One-shot project GitHub connect: SecretRef → connection → optional repo bind/test.

        Designed for Mission Control "easy auth": operator places a PAT or App private
        key in the runtime environment, names the env var, and links owner/repo.
        """
        credential_ref = self.ensure_env_credential_ref(
            connection,
            project_id=project_id,
            env_key=env_credential_key,
            auth_method=auth_method,
            actor=actor,
        )
        created_connection = self.create_connection(
            connection,
            project_id=project_id,
            connection_label=connection_label,
            auth_method=auth_method,
            credential_ref=credential_ref,
            app_id=app_id,
            installation_id=installation_id,
            api_base_url=api_base_url or "https://api.github.com",
            required_permissions=required_permissions
            or ["pull", "push", "triage"],
            metadata={
                **(metadata or {}),
                "setup": "easy_connect",
                "env_credential_key": env_credential_key.strip().upper()
                if env_credential_key
                else None,
            },
            actor=actor,
        )
        binding = None
        test_result = None
        if repository and repository.strip():
            binding = self.create_repository_binding(
                connection,
                project_id=project_id,
                connection_id=created_connection["id"],
                repository=repository,
                role=role or "primary",
                default_branch=None,
                metadata={"setup": "easy_connect"},
                actor=actor,
            )
            if test_after_link:
                test_result = self.test_connection(
                    connection,
                    project_id=project_id,
                    connection_id=created_connection["id"],
                    binding_id=binding["id"],
                    dedupe_key=(
                        f"github-easy-connect:{project_id}:"
                        f"{created_connection['id']}:{binding['id']}"
                    ),
                    actor=actor,
                )
                created_connection = test_result.get("connection") or self.get_connection(
                    connection, project_id, created_connection["id"]
                )
                binding = test_result.get("repository_binding") or binding
        emit_event(
            connection,
            project_id,
            "github.easy_connect_completed",
            actor,
            "github_connection",
            created_connection["id"],
            {
                "auth_method": created_connection["auth_method"],
                "repository": (binding or {}).get("full_name"),
                "tested": bool(test_result),
                "test_status": (test_result or {}).get("status"),
            },
        )
        return {
            "connection": created_connection,
            "repository_binding": binding,
            "test": test_result,
            "credential_ref": credential_ref,
        }

    def list_accessible_repositories(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List GitHub repositories visible to a project connection (for easy linking)."""
        github_connection = self.get_connection(
            connection, project_id, connection_id
        )
        if github_connection["status"] in {"disabled", "revoked"}:
            raise GitHubConnectionStateError(
                f"GitHub connection is {github_connection['status']}."
            )
        if github_connection["auth_method"] != "personal_access_token":
            raise ValueError(
                "Repository listing is available for personal_access_token "
                "connections. For GitHub App installs, bind repositories by "
                "owner/name from the installation."
            )
        page_limit = max(1, min(int(limit), 100))
        # Binding-less adapter: use a placeholder repo identity; list uses /user/repos.
        placeholder = {
            "owner": "_",
            "name": "_",
            "full_name": "_/_",
        }
        adapter = self._make_adapter(
            connection,
            project_id=project_id,
            github_connection=github_connection,
            binding=placeholder,
        )
        return adapter.list_accessible_repositories(
            per_page=min(page_limit, 50),
            max_pages=max(1, (page_limit + 49) // 50),
        )[:page_limit]

    def adapter_for_binding(
        self,
        connection: Connection,
        *,
        project_id: str,
        binding_id: str,
    ) -> tuple[GitHubAdapter, dict[str, Any], dict[str, Any]]:
        """Resolve a live project binding to an execution-scoped adapter."""
        binding = self.get_repository_binding(connection, project_id, binding_id)
        github_connection = self.get_connection(
            connection, project_id, binding["connection_id"]
        )
        if github_connection["status"] != "active":
            raise GitHubConnectionStateError(
                "GitHub connection must pass its connection test before branch operations."
            )
        if binding["status"] != "active":
            raise GitHubConnectionStateError("GitHub repository binding is disabled.")
        return (
            self._make_adapter(
                connection,
                project_id=project_id,
                github_connection=github_connection,
                binding=binding,
            ),
            github_connection,
            binding,
        )

    def _make_adapter(
        self,
        connection: Connection,
        *,
        project_id: str,
        github_connection: dict[str, Any],
        binding: dict[str, Any],
    ) -> GitHubAdapter:
        credential_ref = self._credential_ref(
            connection, project_id, github_connection["id"]
        )
        secret_ref = self.secret_refs.require_usable(
            connection,
            project_id=project_id,
            credential_ref=credential_ref,
        )
        if self.secret_resolver is self._resolve_secret:
            credential = self._resolve_secret(secret_ref, connection)
        else:
            credential = self.secret_resolver(secret_ref)
        if not credential:
            raise GitHubConnectionStateError(
                "GitHub SecretRef could not be resolved by this execution node."
            )
        if github_connection["auth_method"] == "github_app":
            scoped_settings = self.settings.model_copy(
                update={
                    "github_token": None,
                    "github_app_id": github_connection["app_id"],
                    "github_app_installation_id": github_connection[
                        "installation_id"
                    ],
                    "github_app_private_key": credential,
                    "github_app_private_key_path": None,
                }
            )
            return self.adapter_factory(
                scoped_settings,
                owner=binding["owner"],
                repo=binding["name"],
                api_base_url=github_connection["api_base_url"],
            )
        return self.adapter_factory(
            self.settings,
            owner=binding["owner"],
            repo=binding["name"],
            token=credential,
            api_base_url=github_connection["api_base_url"],
        )

    def _record_test_result(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_id: str,
        binding_id: str,
        status: str,
        result: dict[str, Any],
        actor: str,
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE github_connections
                SET status = :connection_status,
                    account_login = :account_login,
                    granted_permissions = CAST(:granted_permissions AS JSONB),
                    missing_permissions = CAST(:missing_permissions AS JSONB),
                    last_test_status = :test_status,
                    last_tested_at = now(),
                    last_error = NULL,
                    updated_at = now()
                WHERE project_id = :project_id AND id = :connection_id
                """
            ),
            {
                "project_id": project_id,
                "connection_id": connection_id,
                "connection_status": "active" if status == "passed" else "error",
                "account_login": result.get("account_login"),
                "granted_permissions": json.dumps(
                    result.get("granted_permissions") or []
                ),
                "missing_permissions": json.dumps(
                    result.get("missing_permissions") or []
                ),
                "test_status": status,
            },
        )
        connection.execute(
            text(
                """
                UPDATE github_repository_bindings
                SET repository_id = :repository_id,
                    default_branch = :default_branch,
                    visibility = :visibility,
                    archived = :archived,
                    permissions = CAST(:permissions AS JSONB),
                    capabilities = CAST(:capabilities AS JSONB),
                    last_synced_at = now(),
                    updated_at = now()
                WHERE project_id = :project_id AND id = :binding_id
                """
            ),
            {
                "project_id": project_id,
                "binding_id": binding_id,
                "repository_id": result.get("repository_id"),
                "default_branch": result.get("default_branch"),
                "visibility": result.get("visibility"),
                "archived": result.get("archived", False),
                "permissions": json.dumps(
                    {
                        key: True
                        for key in result.get("granted_permissions") or []
                    }
                ),
                "capabilities": json.dumps(result.get("capabilities") or []),
            },
        )
        emit_event(
            connection,
            project_id,
            f"github.connection_test_{status}",
            actor,
            "github_connection",
            connection_id,
            {
                "repository_binding_id": binding_id,
                "granted_permissions": result.get("granted_permissions") or [],
                "missing_permissions": result.get("missing_permissions") or [],
            },
        )

    def _record_test_failure(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_id: str,
        actor: str,
        error: dict[str, Any],
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE github_connections
                SET status = 'error',
                    last_test_status = 'failed',
                    last_tested_at = now(),
                    last_error = CAST(:error AS JSONB),
                    updated_at = now()
                WHERE project_id = :project_id AND id = :connection_id
                """
            ),
            {
                "project_id": project_id,
                "connection_id": connection_id,
                "error": json.dumps(error),
            },
        )
        emit_event(
            connection,
            project_id,
            "github.connection_test_failed",
            actor,
            "github_connection",
            connection_id,
            {"error": error},
        )

    def _credential_ref(
        self, connection: Connection, project_id: str, connection_id: str
    ) -> str:
        value = connection.execute(
            text(
                """
                SELECT credential_ref
                FROM github_connections
                WHERE project_id = :project_id AND id = :connection_id
                """
            ),
            {"project_id": project_id, "connection_id": connection_id},
        ).scalar_one_or_none()
        if value is None:
            raise GitHubConnectionNotFoundError("GitHub connection not found.")
        return str(value)

    def _resolve_secret(
        self,
        secret_ref: dict[str, Any],
        connection: Optional[Connection] = None,
    ) -> str:
        """Resolve a GitHub SecretRef at use time. Never logs secret material.

        Dicts with an id go through ``resolve_secret_ref`` (every backend).
        Env refs still resolve via ``backend_for("environment")``.
        Unusable refs fail closed as ``GitHubConnectionStateError``.
        """
        if not isinstance(secret_ref, dict):
            raise GitHubConnectionStateError(
                "GitHub SecretRef could not be resolved by this execution node."
            )
        conn = connection if connection is not None else self._active_connection
        try:
            ref_id = str(secret_ref.get("id") or "").strip()
            if ref_id and conn is not None:
                value = resolve_secret_ref(conn, ref_id, self.settings)
            elif str(secret_ref.get("ref_kind") or "").strip().lower() in {
                "env",
                "environment",
            } and secret_ref.get("ref_key"):
                value = backend_for("environment", self.settings).get(
                    str(secret_ref["ref_key"])
                )
            else:
                raise GitHubConnectionStateError(
                    "GitHub SecretRef could not be resolved by this execution node."
                )
        except SecretUnavailable as error:
            raise GitHubConnectionStateError(
                "GitHub SecretRef could not be resolved by this execution node."
            ) from error
        if not str(value or "").strip():
            raise GitHubConnectionStateError(
                "GitHub SecretRef could not be resolved by this execution node."
            )
        return value

    @staticmethod
    def _scope(connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT w.id AS workspace_id, w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            raise ValueError("Project not found.")
        return dict(row)

    @classmethod
    def _normalize_permissions(cls, permissions: list[str]) -> list[str]:
        normalized = sorted(
            {
                str(permission).strip().lower()
                for permission in permissions
                if str(permission).strip()
            }
        )
        invalid = [
            permission
            for permission in normalized
            if permission not in cls.REPOSITORY_PERMISSIONS
        ]
        if invalid:
            raise ValueError(
                "required_permissions contains unsupported values: "
                + ", ".join(invalid)
                + "."
            )
        if not normalized:
            raise ValueError("required_permissions must contain at least one value.")
        return normalized

    @classmethod
    def _normalize_repository(cls, repository: str) -> tuple[str, str, str]:
        owner, separator, name = repository.strip().partition("/")
        if (
            not separator
            or "/" in name
            or not cls._REPOSITORY_PART.fullmatch(owner)
            or not cls._REPOSITORY_PART.fullmatch(name)
        ):
            raise ValueError("repository must use a valid 'owner/name' format.")
        return owner, name, f"{owner}/{name}"

    @staticmethod
    def _normalize_api_base_url(value: str) -> str:
        parsed = urlparse(value.strip().rstrip("/"))
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "api_base_url must be an HTTPS origin without credentials, query, or fragment."
            )
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")

    @staticmethod
    def _capabilities(granted_permissions: list[str]) -> list[str]:
        granted = set(granted_permissions)
        capabilities = ["repository.read"]
        if granted.intersection({"push", "maintain", "admin"}):
            capabilities.extend(
                [
                    "contents.write",
                    "branches.write",
                    "pull_requests.write",
                    "issues.write",
                ]
            )
        if granted.intersection({"triage", "push", "maintain", "admin"}):
            capabilities.append("issues.triage")
        if granted.intersection({"maintain", "admin"}):
            capabilities.append("repository.maintain")
        if "admin" in granted:
            capabilities.append("repository.admin")
        return sorted(set(capabilities))

    @staticmethod
    def _required_text(value: Optional[str], field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty.")
        return normalized

    @staticmethod
    def _serialize_connection(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "connection_label": row["connection_label"],
            "auth_method": row["auth_method"],
            "credential_configured": bool(row["credential_ref"]),
            "app_id": row["app_id"],
            "installation_id": row["installation_id"],
            "api_base_url": row["api_base_url"],
            "status": row["status"],
            "required_permissions": row["required_permissions"] or [],
            "granted_permissions": row["granted_permissions"] or [],
            "missing_permissions": row["missing_permissions"] or [],
            "account_login": row["account_login"],
            "metadata": row["metadata"] or {},
            "last_test_status": row["last_test_status"],
            "last_tested_at": (
                row["last_tested_at"].isoformat() if row["last_tested_at"] else None
            ),
            "last_error": row["last_error"],
            "disabled_at": (
                row["disabled_at"].isoformat() if row["disabled_at"] else None
            ),
            "disabled_by": row["disabled_by"],
            "revoked_at": (
                row["revoked_at"].isoformat() if row["revoked_at"] else None
            ),
            "revoked_by": row["revoked_by"],
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    @staticmethod
    def _serialize_binding(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "connection_id": row["connection_id"],
            "repository_id": row["repository_id"],
            "owner": row["owner"],
            "name": row["name"],
            "full_name": row["full_name"],
            "role": row["role"],
            "default_branch": row["default_branch"],
            "visibility": row["visibility"],
            "archived": bool(row["archived"]),
            "status": row["status"],
            "permissions": row["permissions"] or {},
            "capabilities": row["capabilities"] or [],
            "metadata": row["metadata"] or {},
            "last_synced_at": (
                row["last_synced_at"].isoformat() if row["last_synced_at"] else None
            ),
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
