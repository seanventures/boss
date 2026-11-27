"""Gate integrity: the last gate run must execute against the exact committed tree that lands (BOSSAPP-1029).

Why this exists:
  On the bench's clean-checkout acceptance (textkit-word-count), a run reported
  completed=True, verify_passed=True, review_passed=True, gates_passed=True while a clean
  checkout of its landed branch failed the repository's own tests. Any edit after the last
  authoritative test run — or any mismatch between the tree that tests verified and the tree
  committed to the branch — invalidates the prior gate pass until re-verified on the exact
  landing tree/commit.

Contract:
  1. Every authoritative verify run snapshots the exact tree fingerprint (git tree OID via an
     ephemeral index when in a git checkout, plus canonical SHA-256 of the changed/deleted file
     set).
  2. Any edit after the last test run invalidates the prior pass (`check_integrity` returns
     `ok=False, invalidated=True`).
  3. Before landing on the branch (`propose_pull_request`), the landing file set is checked
     against the verified digest; any mismatch raises `GateIntegrityError` and blocks landing.
  4. Once committed at `commit_sha`, a `quality_gate.verify` receipt is recorded carrying that
     exact `commit_sha` and tree digest, and stamped onto `quality_gates.evidence`, the test
     report artifact, and the merge decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Optional, Sequence

from sqlalchemy import text

_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"})


class GateIntegrityError(RuntimeError):
    """Raised when the tree being landed does not match the tree the last gate verified."""

    code = "gate_integrity_violation"

    def __init__(
        self,
        message: str,
        *,
        verified_tree_sha: Optional[str] = None,
        current_tree_sha: Optional[str] = None,
        verified_commit_sha: Optional[str] = None,
        landing_commit_sha: Optional[str] = None,
    ):
        super().__init__(message)
        self.verified_tree_sha = verified_tree_sha
        self.current_tree_sha = current_tree_sha
        self.verified_commit_sha = verified_commit_sha
        self.landing_commit_sha = landing_commit_sha


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_changes_digest(changes: Optional[Sequence[tuple[str, Optional[str]]]]) -> str:
    """Canonical SHA-256 over a sorted list of ``(path, content_or_None)`` landing changes."""
    h = hashlib.sha256()
    normalized: list[tuple[str, Optional[str]]] = []
    for path, content in changes or ():
        clean = str(path or "").strip().lstrip("/")
        if not clean:
            continue
        normalized.append((clean, None if content is None else str(content)))
    normalized.sort(key=lambda item: item[0])
    for path, content in normalized:
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        if content is None:
            h.update(b"<DELETED>")
        else:
            h.update(hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest().encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _git_tree_oid(root: Path) -> Optional[dict[str, Any]]:
    """Compute HEAD sha, dirty flag, and exact working-tree git tree OID without mutating .git/index."""
    if not (root / ".git").exists():
        return None
    try:
        head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        head_sha = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        dirty = bool(status_proc.stdout.strip()) if status_proc.returncode == 0 else True
        fd, tmp_index = tempfile.mkstemp(prefix="boss-gate-idx-")
        os.close(fd)
        try:
            os.unlink(tmp_index)
        except OSError:
            pass
        try:
            env = {**os.environ, "GIT_INDEX_FILE": tmp_index}
            if head_sha:
                subprocess.run(
                    ["git", "read-tree", "HEAD"],
                    cwd=str(root),
                    env=env,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            add_proc = subprocess.run(
                ["git", "add", "-A"],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if add_proc.returncode != 0:
                return None
            tree_proc = subprocess.run(
                ["git", "write-tree"],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if tree_proc.returncode != 0:
                return None
            tree_sha = tree_proc.stdout.strip()
            if not tree_sha:
                return None
            return {
                "tree_sha": tree_sha,
                "head_sha": head_sha or None,
                "dirty": dirty,
                "kind": "git_tree",
            }
        finally:
            try:
                os.unlink(tmp_index)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — fallback to filesystem digest
        return None


def _fs_tree_digest(root: Path) -> dict[str, Any]:
    """Deterministic content digest of a directory tree when git metadata is absent."""
    h = hashlib.sha256()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fname in sorted(filenames):
            fp = Path(dirpath) / fname
            rel = str(fp.relative_to(root)).replace("\\", "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            try:
                h.update(hashlib.sha256(fp.read_bytes()).hexdigest().encode("ascii"))
            except OSError:
                h.update(b"<UNREADABLE>")
            h.update(b"\n")
            count += 1
    return {
        "tree_sha": h.hexdigest(),
        "head_sha": None,
        "dirty": False,
        "kind": "fs_digest",
        "files": count,
    }


def compute_tree_fingerprint(
    root: Optional[Path] = None,
    *,
    changes: Optional[Sequence[tuple[str, Optional[str]]]] = None,
) -> dict[str, Any]:
    """Return exact tree fingerprint for a workspace ``root`` and/or landing ``changes``."""
    info: dict[str, Any] = {}
    if root is not None and Path(root).exists():
        root_path = Path(root)
        info = _git_tree_oid(root_path) or _fs_tree_digest(root_path)
    if changes is not None:
        info["changes_digest"] = compute_changes_digest(changes)
    return info


@dataclass
class GateVerificationTracker:
    """Tracks the exact tree/changes verified by the last authoritative gate run in a build."""

    verified_tree_sha: Optional[str] = None
    verified_changes_digest: Optional[str] = None
    verified_head_sha: Optional[str] = None
    verified_passed: bool = False
    verified_command: str = ""
    verified_exit_code: Optional[int] = None
    verified_at: Optional[str] = None
    invalidated: bool = False
    invalidation_reason: Optional[str] = None
    verifications: int = 0

    def record_verification(
        self,
        *,
        root: Optional[Path] = None,
        changes: Optional[Sequence[tuple[str, Optional[str]]]] = None,
        passed: bool,
        command: str = "",
        exit_code: Optional[int] = None,
    ) -> dict[str, Any]:
        snap = compute_tree_fingerprint(root, changes=changes)
        self.verified_tree_sha = snap.get("tree_sha")
        self.verified_changes_digest = snap.get("changes_digest")
        self.verified_head_sha = snap.get("head_sha")
        self.verified_passed = bool(passed)
        self.verified_command = str(command or "")
        self.verified_exit_code = exit_code
        self.verified_at = _now_iso()
        self.invalidated = False
        self.invalidation_reason = None
        self.verifications += 1
        return self.as_dict()

    def invalidate(self, reason: str) -> None:
        self.invalidated = True
        self.invalidation_reason = str(reason or "tree modified after last gate run")
        self.verified_passed = False

    def check_integrity(
        self,
        *,
        root: Optional[Path] = None,
        changes: Optional[Sequence[tuple[str, Optional[str]]]] = None,
    ) -> dict[str, Any]:
        """Verify that the current workspace tree and landing changes match the last verified run."""
        current = compute_tree_fingerprint(root, changes=changes)
        cur_tree = current.get("tree_sha")
        cur_changes = current.get("changes_digest")

        if self.invalidated:
            return {
                "ok": False,
                "invalidated": True,
                "reason": self.invalidation_reason or "prior gate pass was invalidated by a later edit",
                "verified_tree_sha": self.verified_tree_sha,
                "current_tree_sha": cur_tree,
                "verified_changes_digest": self.verified_changes_digest,
                "current_changes_digest": cur_changes,
                "verified_head_sha": self.verified_head_sha,
            }
        if self.verifications == 0:
            return {
                "ok": False,
                "invalidated": True,
                "reason": "no authoritative gate test run was recorded",
                "verified_tree_sha": None,
                "current_tree_sha": cur_tree,
                "verified_changes_digest": None,
                "current_changes_digest": cur_changes,
                "verified_head_sha": None,
            }
        if self.verified_tree_sha and cur_tree and cur_tree != self.verified_tree_sha:
            reason = (
                f"working tree changed after last gate test run "
                f"(verified tree {self.verified_tree_sha[:12]} != current {cur_tree[:12]})"
            )
            self.invalidate(reason)
            return {
                "ok": False,
                "invalidated": True,
                "reason": reason,
                "verified_tree_sha": self.verified_tree_sha,
                "current_tree_sha": cur_tree,
                "verified_changes_digest": self.verified_changes_digest,
                "current_changes_digest": cur_changes,
                "verified_head_sha": self.verified_head_sha,
            }
        if (
            self.verified_changes_digest
            and cur_changes
            and cur_changes != self.verified_changes_digest
        ):
            reason = (
                f"landing file set differs from last gate test run "
                f"(verified {self.verified_changes_digest[:12]} != landing {cur_changes[:12]})"
            )
            self.invalidate(reason)
            return {
                "ok": False,
                "invalidated": True,
                "reason": reason,
                "verified_tree_sha": self.verified_tree_sha,
                "current_tree_sha": cur_tree,
                "verified_changes_digest": self.verified_changes_digest,
                "current_changes_digest": cur_changes,
                "verified_head_sha": self.verified_head_sha,
            }
        return {
            "ok": True,
            "invalidated": False,
            "reason": None,
            "verified_tree_sha": self.verified_tree_sha,
            "current_tree_sha": cur_tree,
            "verified_changes_digest": self.verified_changes_digest,
            "current_changes_digest": cur_changes,
            "verified_head_sha": self.verified_head_sha,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified_tree_sha": self.verified_tree_sha,
            "verified_changes_digest": self.verified_changes_digest,
            "verified_head_sha": self.verified_head_sha,
            "verified_passed": self.verified_passed,
            "verified_command": self.verified_command,
            "verified_exit_code": self.verified_exit_code,
            "verified_at": self.verified_at,
            "invalidated": self.invalidated,
            "invalidation_reason": self.invalidation_reason,
            "verifications": self.verifications,
        }


def verify_landing_integrity(
    *,
    expected_changes_digest: Optional[str],
    landing_changes: Sequence[tuple[str, Optional[str]]],
    gate_passed: bool = True,
    invalidated: bool = False,
    invalidation_reason: Optional[str] = None,
    verified_tree_sha: Optional[str] = None,
) -> dict[str, Any]:
    """Fail-closed check at the landing boundary (SoftwareFactoryService.propose_pull_request)."""
    landing_digest = compute_changes_digest(landing_changes)
    if invalidated:
        raise GateIntegrityError(
            "Gate integrity violation: "
            + (invalidation_reason or "a repair or edit after the last gate run invalidated the prior pass; re-run verification before landing."),
            verified_tree_sha=verified_tree_sha,
            current_tree_sha=landing_digest,
        )
    if expected_changes_digest and gate_passed and landing_digest != expected_changes_digest:
        raise GateIntegrityError(
            f"Gate integrity violation: landing files ({landing_digest[:12]}) do not match "
            f"the exact tree verified by the last passing gate run ({expected_changes_digest[:12]}). "
            "Re-run verification on the exact landing tree before opening the pull request.",
            verified_tree_sha=expected_changes_digest,
            current_tree_sha=landing_digest,
        )
    return {
        "verified_changes_digest": expected_changes_digest,
        "landing_changes_digest": landing_digest,
        "verified_tree_sha": verified_tree_sha,
        "matched": True,
    }


def record_gate_receipt(
    connection: Any,
    receipts_service: Any,
    *,
    org_id: str,
    workspace_id: str,
    project_id: str,
    run_id: Optional[str],
    quality_gate_id: Optional[str],
    commit_sha: str,
    verified_tree_sha: Optional[str],
    verified_changes_digest: Optional[str],
    tests_passed: Optional[bool],
    gates_passed: Optional[Sequence[str]] = None,
    gates_failed: Optional[Sequence[str]] = None,
    created_by_job_id: Optional[str] = None,
) -> Optional[str]:
    """Record a durable ``quality_gate.verify`` receipt carrying the exact landing commit SHA."""
    sha = str(commit_sha or "").strip()
    if not sha or receipts_service is None:
        return None
    dedupe_key = f"quality_gate.verify:{run_id or quality_gate_id or 'pr'}:{sha}"
    payload = {
        "commit_sha": sha,
        "verified_commit_sha": sha,
        "verified_tree_sha": verified_tree_sha,
        "verified_changes_digest": verified_changes_digest,
        "quality_gate_id": quality_gate_id,
        "run_id": run_id,
        "tests_passed": tests_passed,
        "gates_passed": list(gates_passed or ()),
        "gates_failed": list(gates_failed or ()),
    }
    receipt, _created = receipts_service.record_completed(
        connection,
        org_id,
        workspace_id,
        project_id,
        dedupe_key,
        "quality_gate.verify",
        {"project_id": project_id, "run_id": run_id, "commit_sha": sha},
        payload,
        provider_ref=f"commit:{sha}",
        created_by_job_id=created_by_job_id,
        data_boundary="internal",
    )
    return str(receipt.get("id") or "") or None


def stamp_verified_commit(
    connection: Any,
    *,
    project_id: str,
    run_id: Optional[str],
    quality_gate_id: Optional[str],
    commit_sha: str,
    verified_tree_sha: Optional[str] = None,
    verified_changes_digest: Optional[str] = None,
    gate_receipt_id: Optional[str] = None,
) -> None:
    """Stamp the exact landed commit SHA + gate receipt onto ``quality_gates`` and ``code_task_runs``."""
    sha = str(commit_sha or "").strip()
    if not sha or connection is None or not hasattr(connection, "execute"):
        return
    patch: dict[str, Any] = {
        "verified_commit_sha": sha,
        "gate_integrity_verified": True,
        "gate_integrity_invalidated": False,
    }
    if verified_tree_sha:
        patch["verified_tree_sha"] = verified_tree_sha
    if verified_changes_digest:
        patch["verified_changes_digest"] = verified_changes_digest
    if gate_receipt_id:
        patch["gate_receipt_id"] = gate_receipt_id
    patch_json = json.dumps(patch)
    if quality_gate_id:
        connection.execute(
            text(
                "UPDATE quality_gates SET evidence = COALESCE(evidence, '{}'::jsonb) || CAST(:p AS JSONB) "
                "WHERE id = :qg AND project_id = :proj"
            ),
            {"p": patch_json, "qg": quality_gate_id, "proj": project_id},
        )
    elif run_id:
        connection.execute(
            text(
                "UPDATE quality_gates SET evidence = COALESCE(evidence, '{}'::jsonb) || CAST(:p AS JSONB) "
                "WHERE code_task_run_id = :run AND project_id = :proj"
            ),
            {"p": patch_json, "run": run_id, "proj": project_id},
        )
    if run_id:
        connection.execute(
            text(
                "UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) || CAST(:p AS JSONB), "
                "updated_at = now() WHERE id = :run AND project_id = :proj"
            ),
            {"p": patch_json, "run": run_id, "proj": project_id},
        )


def check_commit_sha_matches(
    verified_commit_sha: Optional[str],
    landing_commit_sha: Optional[str],
    *,
    invalidated: bool = False,
) -> Optional[dict[str, str]]:
    """Return a structured block reason if the verified SHA disagrees with the landing SHA."""
    if invalidated:
        return {
            "status": "blocked_gate_sha_mismatch",
            "event_reason": "gate_integrity_invalidated",
            "reason": "A repair or edit after the last gate run invalidated the prior gate pass.",
            "next_action": "Re-run the quality gate on the exact landing commit before merging.",
        }
    v = str(verified_commit_sha or "").strip().lower()
    h = str(landing_commit_sha or "").strip().lower()
    if v and h and v != h:
        return {
            "status": "blocked_gate_sha_mismatch",
            "event_reason": "gate_sha_mismatch",
            "reason": (
                f"The last quality gate ran on commit {v[:12]}, but the branch head is now "
                f"{h[:12]}. Re-verify on the exact landing commit before merging."
            ),
            "next_action": "Re-run verification on the current branch head commit so the gate receipt matches.",
        }
    return None
