from __future__ import annotations

import json
import logging
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


logger = logging.getLogger(__name__)


class GitHubConnectionNotFoundError(RuntimeError):
    pass


class GitHubRepositoryBindingNotFoundError(RuntimeError):
    pass


#: Credential shapes that must never reach a stored message. A provider's own error body can
#: echo the request, and the request carried the token.
_CREDENTIAL_PATTERNS = (
    r"gh[pousr]_[A-Za-z0-9]{16,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}",
    r"-----BEGIN[^-]+PRIVATE KEY-----",
    r"[A-Za-z0-9_\-]{40,}",          # anything long enough to be a credential we do not know
)


def _redact_credentials(error: BaseException, limit: int = 300) -> str:
    """The reason a call failed, with anything credential-shaped removed.

    Discarding the reason entirely — which is what this replaced — protects the token and
    costs the operator the one fact they needed. Redacting keeps both: "GitHub has no
    paramg/boss-sandbox visible to this token" is safe to store and is the whole answer.
    """
    text_value = str(error or "").strip() or error.__class__.__name__
    for pattern in _CREDENTIAL_PATTERNS:
        text_value = re.sub(pattern, "[redacted]", text_value)
    return text_value[:limit]


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

    def _connection_by_label(
        self, connection: Connection, project_id: str, label: str,
    ) -> Optional[dict[str, Any]]:
        """The connection this project already has under that label, if any."""
        row = connection.execute(
            text("SELECT * FROM github_connections "
                 " WHERE project_id = :p AND lower(connection_label) = lower(:l) LIMIT 1"),
            {"p": project_id, "l": (label or "").strip()},
        ).mappings().first()
        return self._serialize_connection(row) if row else None

    def repoint_connection(
        self,
        connection: Connection,
        *,
        project_id: str,
        connection_id: str,
        credential_ref: str,
        auth_method: str,
        app_id: Optional[str],
        installation_id: Optional[str],
        api_base_url: str,
        actor: str,
    ) -> dict[str, Any]:
        """Point an existing connection at a new credential, keeping everything else.

        This is what "press Connect again with a better token" means. The id, the repository
        bindings and the history stay; the credential and the failed test state do not. The
        previous credential_ref is recorded in the event, because "which credential was this
        using when it worked" is a question somebody asks later.
        """
        current = self.get_connection(connection, project_id, connection_id)
        # STRAIGHT FROM THE TABLE. `_serialize_connection` does not expose credential_ref —
        # rightly, it is an internal pointer — so reading it from the serialized dict would
        # record "previous_credential_ref: null" in the event and lose the one fact the event
        # exists for.
        previous_ref = connection.execute(
            text("SELECT credential_ref FROM github_connections "
                 " WHERE project_id = :p AND id = :id"),
            {"p": project_id, "id": connection_id}).scalar()
        method = (auth_method or current["auth_method"]).strip().lower()
        if method not in self.AUTH_METHODS:
            raise ValueError(
                "auth_method must be one of: " + ", ".join(sorted(self.AUTH_METHODS)) + ".")
        self.secret_refs.parse_credential_ref(credential_ref)
        connection.execute(
            text("""
                UPDATE github_connections
                   SET credential_ref = :ref,
                       auth_method = :method,
                       app_id = :app_id,
                       installation_id = :installation_id,
                       api_base_url = :api_base_url,
                       -- 'configured' is the state BEFORE a passing test; a pass sets
                       -- 'active' and a failure 'error'. 'enabled' is not in this column's
                       -- CHECK constraint at all — see the note below on the query that
                       -- filtered by it.
                       status = 'configured',
                       last_test_status = NULL,
                       last_error = NULL,
                       updated_at = now()
                 WHERE project_id = :p AND id = :id
            """),
            {"ref": credential_ref, "method": method, "app_id": app_id,
             "installation_id": installation_id, "api_base_url": api_base_url,
             "p": project_id, "id": connection_id},
        )
        emit_event(
            connection, project_id, "github.connection_repointed", actor,
            "github_connection", connection_id,
            # The REFERENCE, never a value: these are `<kind>:<key>` pointers.
            {"previous_credential_ref": previous_ref,
             "credential_ref": credential_ref, "auth_method": method},
        )
        return self.get_connection(connection, project_id, connection_id)

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
            try:
                repository = adapter.repo_metadata()
            except Exception as probe_error:
                # A 404 ON A PRIVATE REPOSITORY IS A SCOPE PROBLEM WEARING A NOT-FOUND SIGN.
                # GitHub refuses to say a private repository exists to a token that cannot see
                # it, so "404" is what a correctly-scoped-to-the-wrong-repositories token
                # returns — and the operator, looking at the repository in their browser,
                # reasonably concludes BOSS is broken. Measured: a valid fine-grained token
                # belonging to the right account, scoped to two OTHER repositories.
                raise RuntimeError(self._explain_repository_failure(
                    connection, project_id=project_id,
                    github_connection=github_connection, binding=binding,
                    error=probe_error)) from probe_error
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
            # SAY WHY. This recorded a constant — "GitHub connection test failed." — and threw
            # the reason away, so a failed test was unactionable for the operator AND for
            # BOSS's own agent, which could only report the same four words back. The caution
            # was right and the remedy was too blunt: a GitHub error body can echo the token,
            # so the message is REDACTED rather than discarded.
            safe_error = {
                "type": error.__class__.__name__,
                # ROOM FOR THE EXPLANATION. The default cap is 300, which is right for an
                # unknown provider error and cut this one off mid-sentence — the message here
                # is one BOSS composed itself, naming the account and the repositories the
                # token can reach. Still bounded; just bounded above what it has to say.
                "message": ("GitHub connection test failed: "
                            f"{_redact_credentials(error, limit=1200)}"),
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

    def token_for_repository(
        self,
        connection: Connection,
        *,
        project_id: str,
        owner: str,
        repo: str,
    ) -> Optional[str]:
        """The token this PROJECT registered for owner/repo, or None.

        WHY THIS EXISTS. The setup wizard writes a connection and a repository binding, and
        `create_code_task` already resolves the repo NAME through that registry — but every path
        that actually pushes took `settings.github_token`, an environment variable. So a
        wizard-connected install reported "Connected — owner/name is the build target" and its
        first build died at push with "BOSS_GITHUB_TOKEN is not set". The registry recorded
        intent the runtime ignored.

        Exact binding first, then the project's primary: a project with one repo should not have
        to spell it out, and a project with several must not hand over the wrong account's token.
        Returns None rather than raising — the caller has an environment fallback, and a missing
        registry is not an error.
        """
        wanted = f"{owner}/{repo}".strip().lower()
        try:
            rows = connection.execute(
                text(
                    """
                    SELECT b.full_name, b.role, c.id AS connection_id
                    FROM github_repository_bindings b
                    JOIN github_connections c ON c.id = b.connection_id
                    WHERE b.project_id = :p AND b.status = 'active'
                      -- 'configured' and 'active' ARE the healthy states. This said
                      -- `c.status = 'enabled'`, a value the column's CHECK constraint does
                      -- not permit — so the condition could never be true and this lookup
                      -- always found nothing, silently, and fell through to whatever the
                      -- caller does without a repository.
                      AND c.status IN ('configured', 'active')
                    ORDER BY (LOWER(b.full_name) = :want) DESC,
                             (b.role = 'primary') DESC, b.updated_at DESC
                    LIMIT 1
                    """
                ),
                {"p": project_id, "want": wanted},
            ).mappings().first()
        except Exception as error:  # noqa: BLE001 — an older schema must not break the build
            logger.debug("github: registry lookup failed for %s: %s", wanted, error)
            return None
        if not rows:
            return None
        try:
            ref = self._credential_ref(connection, project_id, str(rows["connection_id"]))
            record = self.secret_refs.get_by_credential_ref(connection, project_id, ref) \
                if hasattr(self.secret_refs, "get_by_credential_ref") else None
            if record is None:
                kind, _, key = str(ref).partition(":")
                record = {"ref_kind": kind, "ref_key": key}
                for item in self.secret_refs.list_secret_refs(
                        connection, project_id, status="active", limit=200):
                    if item.get("ref_kind") == kind and item.get("ref_key") == key:
                        record = item
                        break
            return self._resolve_secret(record, connection)
        except (GitHubConnectionStateError, GitHubConnectionNotFoundError) as error:
            # A bound connection whose secret cannot be read here is not a reason to fail the
            # build: the environment may still have a usable token.
            logger.debug("github: could not resolve the registered credential: %s", error)
            return None

    def store_token_credential(
        self,
        connection: Connection,
        *,
        project_id: str,
        token: str,
        auth_method: str,
        label: str,
        actor: str,
    ) -> str:
        """Write a pasted token through to the secret backend; return its credential_ref.

        The value leaves BOSS inside `put_secret` and is never written to a column, logged, or
        returned. What comes back is a reference the connection stores, resolved at use time by
        `_resolve_secret` — which already handles every backend, so nothing downstream has to
        know this token arrived by hand rather than from the environment.
        """
        from .secrets_service import put_secret

        method = (auth_method or "").strip().lower()
        secret_kind = "private_key" if method == "github_app" else "api_token"
        # A NAME THAT SURVIVES A SECOND CONNECTION. `ref_key` is unique per project, so a fixed
        # name would make a second GitHub account silently overwrite the first one's token.
        slug = re.sub(r"[^a-z0-9]+", "_", (label or "github").strip().lower()).strip("_")
        stored = put_secret(
            connection,
            self.settings,
            project_id=project_id,
            name=f"github_{slug or 'connection'}",
            value=token.strip(),
            secret_kind=secret_kind,
            backend=self._preferred_backend(),
            actor=actor,
        )
        connection.execute(
            text("UPDATE secret_refs SET provider='github' WHERE id = :id"),
            {"id": stored["secret_ref_id"]},
        )
        return f"{stored['backend']}:{stored['ref_key']}"

    def _preferred_backend(self) -> str:
        """Where a pasted secret goes: the best store that works on this installation."""
        from .secret_backends import default_secret_backend

        return default_secret_backend(self.settings)

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
        #: A token pasted into the setup wizard. Stored through the secret backend; only the
        #: reference is kept. Absent, the env-var path is used exactly as before.
        token: Optional[str] = None,
    ) -> dict[str, Any]:
        """One-shot project GitHub connect: SecretRef → connection → optional repo bind/test.

        Two ways in, deliberately. An operator who owns the server names an environment
        variable. An operator setting up self-hosted BOSS for the first time pastes a token into
        the wizard — because "set an env var on the API container and restart it" is not a first
        step anybody should have to take to try a product, and it is the step that ends most
        open-source evaluations.
        """
        if token and token.strip():
            credential_ref = self.store_token_credential(
                connection,
                project_id=project_id,
                token=token,
                auth_method=auth_method,
                label=connection_label,
                actor=actor,
            )
        else:
            credential_ref = self.ensure_env_credential_ref(
                connection,
                project_id=project_id,
                env_key=env_credential_key,
                auth_method=auth_method,
                actor=actor,
            )
        # A SECOND ATTEMPT IS A RETRY, NOT A SECOND CONNECTION.
        #
        # Measured: the first attempt stored the connection and then failed its access test —
        # correctly, the token could not see the repository. Fixing the token and pressing
        # Connect again was refused with "Project already has a GitHub connection with this
        # label", because the label is fixed at "GitHub" and this path only ever created. So
        # the one screen whose job is to get this right could not be used twice, and the
        # operator was told to go and find another page.
        #
        # Re-pointing the existing connection at the new credential is what they meant, and it
        # keeps its id, its bindings and its history.
        existing = self._connection_by_label(connection, project_id, connection_label)
        if existing is not None:
            created_connection = self.repoint_connection(
                connection,
                project_id=project_id,
                connection_id=existing["id"],
                credential_ref=credential_ref,
                auth_method=auth_method,
                app_id=app_id,
                installation_id=installation_id,
                api_base_url=api_base_url or "https://api.github.com",
                actor=actor,
            )
        else:
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

    def _explain_repository_failure(
        self,
        connection: Connection,
        *,
        project_id: str,
        github_connection: dict[str, Any],
        binding: dict[str, Any],
        error: BaseException,
    ) -> str:
        """Why GitHub would not show us this repository, in terms the operator can act on.

        The raw message is a URL and a status code. What the operator needs is which account
        the token belongs to and which repositories it can actually reach — one extra call on
        a path that has already failed, and it turns "404 Not Found" into the sentence that
        names the fix.
        """
        base = _redact_credentials(error)
        full_name = binding.get("full_name") or ""
        if "404" not in base and "Not Found" not in base:
            return base
        hint = (f"GitHub will not confirm that {full_name} exists to this token. For a PRIVATE "
                f"repository that is what a token without access returns — it is a scope "
                f"answer, not a missing repository.")
        try:
            credential = self._resolve_secret(
                self.secret_refs.require_usable(
                    connection, project_id=project_id,
                    credential_ref=self._credential_ref(
                        connection, project_id, github_connection["id"])),
                connection)
            who = self._github_get("/user", credential) or {}
            login = str(who.get("login") or "").strip()
            repos = self._github_get(
                "/user/repos?per_page=100&affiliation=owner,collaborator,"
                "organization_member", credential)
            if login:
                hint += f" The token belongs to {login}."
            if isinstance(repos, list):
                names = sorted(str(r.get("full_name") or "") for r in repos)
                shown = ", ".join(names[:10]) or "none"
                hint += (f" It can reach {len(names)} repositor"
                         f"{'y' if len(names) == 1 else 'ies'}: {shown}.")
                if full_name and full_name not in names:
                    hint += (f" {full_name} is not among them — add it under Repository "
                             f"access on the token, or pick a repository it already covers. "
                             f"Editing the token keeps its value, so nothing needs pasting "
                             f"again.")
        except Exception as exc:  # noqa: BLE001 — the hint is a bonus, never the failure
            hint += f" (could not ask GitHub what the token covers: {type(exc).__name__})"
        return f"{base} — {hint}"

    @staticmethod
    def _github_get(path: str, credential: str, timeout: int = 15) -> Any:
        """A read against GitHub with the connection's own credential. Returns None on error."""
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"https://api.github.com{path}",
            headers={"Authorization": f"Bearer {credential}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "boss-setup-check"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode() or "null")
        except (urllib.error.URLError, OSError, ValueError):
            return None

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
        # ASK THE QUESTION DIRECTLY. `self.secret_resolver is self._resolve_secret` is always
        # False: line 61 stored a bound method and this created a NEW one to compare it
        # against, and bound methods are never identical under `is`. So the branch that passes
        # the live connection never ran, resolution fell back to `_active_connection`, and on
        # the setup path — where nothing had set it — the token could not be read at all. The
        # operator saw "Connected, but the check failed: failed".
        #
        # `_custom_secret_resolver` is the thing actually being asked about: did the caller
        # inject a resolver, or is this our own?
        if self._custom_secret_resolver is None:
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
        """owner/name, from whatever form of it somebody actually has to hand.

        A person told "the repository" reaches for the thing in their address bar, because that
        is where the repository is. Refusing a pasted GitHub URL with "repository must use a
        valid 'owner/name' format" makes them retype what they had already given us correctly,
        and the field cannot say which two words of the URL it wanted. Every form below names
        the same repository unambiguously, so all of them are accepted:

            paramg/boss-sandbox
            https://github.com/paramg/boss-sandbox
            https://github.com/paramg/boss-sandbox/tree/main/docs
            git@github.com:paramg/boss-sandbox.git
        """
        text_value = (repository or "").strip()
        # A URL or an SSH remote: take the first two path segments after the host, which is
        # where owner and name are in every GitHub address shape.
        match = re.match(
            r"^(?:https?://|git\+ssh://|ssh://)?(?:[^@/]+@)?[^/:]*github[^/:]*[/:]"
            r"(?P<owner>[^/]+)/(?P<name>[^/#?]+)",
            text_value, flags=re.IGNORECASE)
        if match:
            owner, name = match.group("owner"), match.group("name")
            # `.git` belongs to the clone URL, not to the repository's name.
            if name.endswith(".git"):
                name = name[: -len(".git")]
            text_value = f"{owner}/{name}"
        owner, separator, name = text_value.partition("/")
        # Trailing path — /tree/main, /pull/3 — is a place IN the repository, not part of it.
        name = name.split("/", 1)[0]
        if (
            not separator
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
