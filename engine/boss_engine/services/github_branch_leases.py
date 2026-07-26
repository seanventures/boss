from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
from secrets import token_urlsafe
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc
from .github_branches import GitHubBranchService
from .github_connections import (
    GitHubConnectionService,
    GitHubConnectionStateError,
)


class GitHubBranchLeaseNotFoundError(RuntimeError):
    pass


class GitHubBranchLeaseConflictError(RuntimeError):
    pass


class GitHubBranchLeaseTokenError(RuntimeError):
    pass


class GitHubBranchLeaseService:
    """Fenced TTL ownership for one mutable repository ref."""

    OWNER_KINDS = {"task", "workflow_run", "code_task_run", "agent", "manual"}
    MIN_TTL_SECONDS = 30
    MAX_TTL_SECONDS = 3600

    def __init__(
        self,
        *,
        github_connections: GitHubConnectionService,
    ) -> None:
        self.github_connections = github_connections

    def acquire(
        self,
        connection: Connection,
        *,
        project_id: str,
        repository_binding_id: str,
        ref_name: str,
        owner_kind: str,
        owner_id: str,
        run_id: Optional[str],
        worktree_path: Optional[str],
        acquire_key: str,
        ttl_seconds: int,
        actor: str,
    ) -> dict[str, Any]:
        ref = GitHubBranchService._normalize_ref(ref_name)
        owner_type = self._required_text(owner_kind, "owner_kind").lower()
        if owner_type not in self.OWNER_KINDS:
            raise ValueError(
                "owner_kind must be one of: "
                + ", ".join(sorted(self.OWNER_KINDS))
                + "."
            )
        owner = self._required_text(owner_id, "owner_id")
        dedupe_key = self._required_text(acquire_key, "acquire_key")
        ttl = self._validate_ttl(ttl_seconds)
        run = self._optional_text(run_id, "run_id", maximum=255)
        worktree = self._optional_text(
            worktree_path, "worktree_path", maximum=1024
        )

        replay = connection.execute(
            text(
                """
                SELECT id
                FROM github_branch_leases
                WHERE project_id = :project_id AND acquire_key = :acquire_key
                """
            ),
            {"project_id": project_id, "acquire_key": dedupe_key},
        ).scalar_one_or_none()
        if replay:
            lease = self.get(connection, project_id, str(replay))
            if (
                lease["repository_binding_id"] != repository_binding_id
                or lease["ref_name"] != ref
                or lease["owner_kind"] != owner_type
                or lease["owner_id"] != owner
                or lease["run_id"] != run
                or lease["worktree_path"] != worktree
            ):
                raise GitHubBranchLeaseConflictError(
                    "acquire_key is already bound to a different branch lease."
                )
            return {"lease": lease, "lease_token": None, "duplicate": True}

        binding_row = connection.execute(
            text(
                """
                SELECT id
                FROM github_repository_bindings
                WHERE project_id = :project_id
                  AND id = :repository_binding_id
                FOR UPDATE
                """
            ),
            {
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
            },
        ).first()
        if binding_row is None:
            self.github_connections.get_repository_binding(
                connection, project_id, repository_binding_id
            )
        self._assert_binding_active(
            connection, project_id, repository_binding_id
        )

        current_time = now_utc()
        active = connection.execute(
            text(
                """
                SELECT id, lease_expires_at
                FROM github_branch_leases
                WHERE repository_binding_id = :repository_binding_id
                  AND ref_name = :ref_name
                  AND status = 'active'
                FOR UPDATE
                """
            ),
            {
                "repository_binding_id": repository_binding_id,
                "ref_name": ref,
            },
        ).mappings().first()
        reclaimed_from: Optional[str] = None
        if active and active["lease_expires_at"] > current_time:
            raise GitHubBranchLeaseConflictError(
                "Branch ref already has an active unexpired lease."
            )
        if active:
            reclaimed_from = str(active["id"])
            connection.execute(
                text(
                    """
                    UPDATE github_branch_leases
                    SET status = 'expired',
                        release_reason = 'expired_before_reacquire',
                        updated_at = now()
                    WHERE id = :lease_id
                    """
                ),
                {"lease_id": reclaimed_from},
            )
            emit_event(
                connection,
                project_id,
                "github.branch_lease_expired",
                actor,
                "github_branch_lease",
                reclaimed_from,
                {
                    "repository_binding_id": repository_binding_id,
                    "ref_name": ref,
                },
            )

        raw_token = token_urlsafe(48)
        lease_id = make_id("ghlease")
        lease_expires_at = current_time + timedelta(seconds=ttl)
        fencing_token = connection.execute(
            text(
                """
                INSERT INTO github_branch_leases (
                    id, project_id, repository_binding_id, ref_name, owner_kind,
                    owner_id, run_id, worktree_path, acquire_key,
                    lease_token_hash, status, acquired_by, acquired_at,
                    heartbeat_at, lease_expires_at, reclaimed_from_lease_id
                )
                VALUES (
                    :id, :project_id, :repository_binding_id, :ref_name,
                    :owner_kind, :owner_id, :run_id, :worktree_path,
                    :acquire_key, :lease_token_hash, 'active', :acquired_by,
                    :acquired_at, :heartbeat_at, :lease_expires_at,
                    :reclaimed_from_lease_id
                )
                RETURNING fencing_token
                """
            ),
            {
                "id": lease_id,
                "project_id": project_id,
                "repository_binding_id": repository_binding_id,
                "ref_name": ref,
                "owner_kind": owner_type,
                "owner_id": owner,
                "run_id": run,
                "worktree_path": worktree,
                "acquire_key": dedupe_key,
                "lease_token_hash": self._hash_token(raw_token),
                "acquired_by": actor,
                "acquired_at": current_time,
                "heartbeat_at": current_time,
                "lease_expires_at": lease_expires_at,
                "reclaimed_from_lease_id": reclaimed_from,
            },
        ).scalar_one()
        emit_event(
            connection,
            project_id,
            "github.branch_lease_acquired",
            actor,
            "github_branch_lease",
            lease_id,
            {
                "repository_binding_id": repository_binding_id,
                "ref_name": ref,
                "owner_kind": owner_type,
                "owner_id": owner,
                "run_id": run,
                "fencing_token": fencing_token,
                "lease_expires_at": lease_expires_at.isoformat(),
                "reclaimed_from_lease_id": reclaimed_from,
            },
        )
        return {
            "lease": self.get(connection, project_id, lease_id),
            "lease_token": raw_token,
            "duplicate": False,
        }

    def heartbeat(
        self,
        connection: Connection,
        *,
        project_id: str,
        lease_id: str,
        lease_token: str,
        fencing_token: int,
        ttl_seconds: int,
        actor: str,
    ) -> dict[str, Any]:
        row = self._locked_row(connection, project_id, lease_id)
        self._verify_row(
            row,
            lease_token=lease_token,
            fencing_token=fencing_token,
        )
        ttl = self._validate_ttl(ttl_seconds)
        current_time = now_utc()
        next_expiry = current_time + timedelta(seconds=ttl)
        connection.execute(
            text(
                """
                UPDATE github_branch_leases
                SET heartbeat_at = :heartbeat_at,
                    lease_expires_at = :lease_expires_at,
                    updated_at = now()
                WHERE project_id = :project_id AND id = :lease_id
                """
            ),
            {
                "project_id": project_id,
                "lease_id": lease_id,
                "heartbeat_at": current_time,
                "lease_expires_at": next_expiry,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.branch_lease_heartbeat",
            actor,
            "github_branch_lease",
            lease_id,
            {
                "fencing_token": fencing_token,
                "lease_expires_at": next_expiry.isoformat(),
            },
        )
        return self.get(connection, project_id, lease_id)

    def release(
        self,
        connection: Connection,
        *,
        project_id: str,
        lease_id: str,
        lease_token: str,
        fencing_token: int,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        row = self._locked_row(connection, project_id, lease_id)
        self._verify_row(
            row,
            lease_token=lease_token,
            fencing_token=fencing_token,
        )
        safe_reason = self._required_text(reason, "reason")
        connection.execute(
            text(
                """
                UPDATE github_branch_leases
                SET status = 'released',
                    released_by = :actor,
                    released_at = now(),
                    release_reason = :reason,
                    updated_at = now()
                WHERE project_id = :project_id AND id = :lease_id
                """
            ),
            {
                "project_id": project_id,
                "lease_id": lease_id,
                "actor": actor,
                "reason": safe_reason,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.branch_lease_released",
            actor,
            "github_branch_lease",
            lease_id,
            {
                "fencing_token": fencing_token,
                "reason": safe_reason,
            },
        )
        return self.get(connection, project_id, lease_id)

    def reclaim_expired(
        self,
        connection: Connection,
        *,
        project_id: str,
        lease_id: str,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        row = self._locked_row(connection, project_id, lease_id)
        if row["status"] != "active":
            raise GitHubBranchLeaseConflictError(
                f"Branch lease is {row['status']} and cannot be reclaimed."
            )
        if row["lease_expires_at"] > now_utc():
            raise GitHubBranchLeaseConflictError(
                "An unexpired branch lease cannot be reclaimed."
            )
        safe_reason = self._required_text(reason, "reason")
        connection.execute(
            text(
                """
                UPDATE github_branch_leases
                SET status = 'reclaimed',
                    released_by = :actor,
                    released_at = now(),
                    release_reason = :reason,
                    updated_at = now()
                WHERE project_id = :project_id AND id = :lease_id
                """
            ),
            {
                "project_id": project_id,
                "lease_id": lease_id,
                "actor": actor,
                "reason": safe_reason,
            },
        )
        emit_event(
            connection,
            project_id,
            "github.branch_lease_reclaimed",
            actor,
            "github_branch_lease",
            lease_id,
            {
                "fencing_token": row["fencing_token"],
                "reason": safe_reason,
            },
        )
        return self.get(connection, project_id, lease_id)

    def verify(
        self,
        connection: Connection,
        *,
        project_id: str,
        lease_id: str,
        lease_token: str,
        fencing_token: int,
        repository_binding_id: str,
        ref_name: str,
    ) -> dict[str, Any]:
        row = self._row(connection, project_id, lease_id)
        self._verify_row(
            row,
            lease_token=lease_token,
            fencing_token=fencing_token,
        )
        ref = GitHubBranchService._normalize_ref(ref_name)
        if (
            row["repository_binding_id"] != repository_binding_id
            or row["ref_name"] != ref
        ):
            raise GitHubBranchLeaseTokenError(
                "Branch lease does not own the requested repository ref."
            )
        return self._serialize(row)

    def list(
        self,
        connection: Connection,
        project_id: str,
        *,
        repository_binding_id: Optional[str] = None,
        ref_name: Optional[str] = None,
        include_history: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")
        filters = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if repository_binding_id:
            filters.append("repository_binding_id = :repository_binding_id")
            params["repository_binding_id"] = repository_binding_id
        if ref_name:
            filters.append("ref_name = :ref_name")
            params["ref_name"] = GitHubBranchService._normalize_ref(ref_name)
        if not include_history:
            filters.append("status = 'active'")
        rows = connection.execute(
            text(
                f"""
                SELECT {self._columns()}
                FROM github_branch_leases
                WHERE {" AND ".join(filters)}
                ORDER BY acquired_at DESC, fencing_token DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize(row) for row in rows]

    def get(
        self,
        connection: Connection,
        project_id: str,
        lease_id: str,
    ) -> dict[str, Any]:
        return self._serialize(self._row(connection, project_id, lease_id))

    def _assert_binding_active(
        self,
        connection: Connection,
        project_id: str,
        repository_binding_id: str,
    ) -> None:
        binding = self.github_connections.get_repository_binding(
            connection, project_id, repository_binding_id
        )
        github_connection = self.github_connections.get_connection(
            connection, project_id, binding["connection_id"]
        )
        if binding["status"] != "active":
            raise GitHubConnectionStateError("GitHub repository binding is disabled.")
        if github_connection["status"] != "active":
            raise GitHubConnectionStateError(
                "GitHub connection must pass its connection test before branch leasing."
            )

    def _row(
        self,
        connection: Connection,
        project_id: str,
        lease_id: str,
    ) -> Any:
        row = connection.execute(
            text(
                f"""
                SELECT {self._columns()}
                FROM github_branch_leases
                WHERE project_id = :project_id AND id = :lease_id
                """
            ),
            {"project_id": project_id, "lease_id": lease_id},
        ).mappings().first()
        if row is None:
            raise GitHubBranchLeaseNotFoundError("GitHub branch lease not found.")
        return row

    def _locked_row(
        self,
        connection: Connection,
        project_id: str,
        lease_id: str,
    ) -> Any:
        row = connection.execute(
            text(
                f"""
                SELECT {self._columns()}
                FROM github_branch_leases
                WHERE project_id = :project_id AND id = :lease_id
                FOR UPDATE
                """
            ),
            {"project_id": project_id, "lease_id": lease_id},
        ).mappings().first()
        if row is None:
            raise GitHubBranchLeaseNotFoundError("GitHub branch lease not found.")
        return row

    def _verify_row(
        self,
        row: Any,
        *,
        lease_token: str,
        fencing_token: int,
    ) -> None:
        if row["status"] != "active":
            raise GitHubBranchLeaseConflictError(
                f"Branch lease is {row['status']}."
            )
        if row["lease_expires_at"] <= now_utc():
            raise GitHubBranchLeaseConflictError("Branch lease has expired.")
        if int(row["fencing_token"]) != int(fencing_token):
            raise GitHubBranchLeaseTokenError("Branch lease fencing token is stale.")
        raw_token = self._required_text(lease_token, "lease_token")
        if not hmac.compare_digest(
            row["lease_token_hash"],
            self._hash_token(raw_token),
        ):
            raise GitHubBranchLeaseTokenError("Branch lease token is invalid.")

    @classmethod
    def _validate_ttl(cls, ttl_seconds: int) -> int:
        ttl = int(ttl_seconds)
        if ttl < cls.MIN_TTL_SECONDS or ttl > cls.MAX_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be between {cls.MIN_TTL_SECONDS} and "
                f"{cls.MAX_TTL_SECONDS}."
            )
        return ttl

    @staticmethod
    def _hash_token(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty.")
        if "\x00" in normalized:
            raise ValueError(f"{field} contains an invalid character.")
        return normalized

    @classmethod
    def _optional_text(
        cls,
        value: Optional[str],
        field: str,
        *,
        maximum: int,
    ) -> Optional[str]:
        if value is None:
            return None
        normalized = cls._required_text(value, field)
        if len(normalized) > maximum:
            raise ValueError(f"{field} must be {maximum} characters or fewer.")
        return normalized

    @staticmethod
    def _columns() -> str:
        return """
            id, project_id, repository_binding_id, ref_name, owner_kind,
            owner_id, run_id, worktree_path, acquire_key, lease_token_hash,
            fencing_token, status, acquired_by, acquired_at, heartbeat_at,
            lease_expires_at, released_by, released_at, release_reason,
            reclaimed_from_lease_id, created_at, updated_at
        """

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "repository_binding_id": row["repository_binding_id"],
            "ref_name": row["ref_name"],
            "owner_kind": row["owner_kind"],
            "owner_id": row["owner_id"],
            "run_id": row["run_id"],
            "worktree_path": row["worktree_path"],
            "acquire_key": row["acquire_key"],
            "fencing_token": row["fencing_token"],
            "status": row["status"],
            "acquired_by": row["acquired_by"],
            "acquired_at": row["acquired_at"].isoformat(),
            "heartbeat_at": row["heartbeat_at"].isoformat(),
            "lease_expires_at": row["lease_expires_at"].isoformat(),
            "released_by": row["released_by"],
            "released_at": (
                row["released_at"].isoformat() if row["released_at"] else None
            ),
            "release_reason": row["release_reason"],
            "reclaimed_from_lease_id": row["reclaimed_from_lease_id"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
