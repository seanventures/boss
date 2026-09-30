"""Model-agnostic, fail-closed intake for third-party agent tooling.

Upstream prompts, hooks, MCP configuration, commands, and scripts are a software
supply chain.  This service deliberately never fetches a repository, runs an
installer, executes a hook, or persists raw source.  A policy-guarded adapter may
provide a pinned text snapshot; BOSS records hashes, classifications, findings,
and review state so an operator can author a native capability from the useful
pattern without inheriting hidden authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id


MAX_FILES = 500
MAX_FILE_BYTES = 262_144
MAX_SNAPSHOT_BYTES = 2_097_152
MAX_CONTEXT_CHUNKS = 1000
MAX_CONTEXT_BYTES = 4_194_304
AUTHORITATIVE_CONTEXT = {"policy", "approval", "contract", "proof"}


class ToolchainImportError(ValueError):
    pass


_SCAN_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "network_install_pipe",
        "severity": "block",
        "pattern": re.compile(
            r"(?:curl|wget)[^\n|]{0,500}\|\s*(?:sudo\s+)?(?:ba|z|fi)?sh\b",
            re.IGNORECASE,
        ),
        "reason": "Downloads piped directly into a shell are not trusted imports.",
    },
    {
        "id": "powershell_remote_execute",
        "severity": "block",
        "pattern": re.compile(
            r"(?:invoke-expression|\biex\b).{0,300}(?:invoke-webrequest|downloadstring)",
            re.IGNORECASE | re.DOTALL,
        ),
        "reason": "Remote PowerShell execution is not trusted import content.",
    },
    {
        "id": "destructive_filesystem",
        "severity": "block",
        "pattern": re.compile(
            r"\brm\s+(?:-[a-z]*r[a-z]*f|-+[a-z]*f[a-z]*r)\b|"
            r"\b(?:mkfs(?:\.[a-z0-9]+)?|wipefs|shred)\b",
            re.IGNORECASE,
        ),
        "reason": "Destructive filesystem operations require a native policy gate.",
    },
    {
        "id": "destructive_git",
        "severity": "block",
        "pattern": re.compile(
            r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|push\b[^\n]*--force|"
            r"branch\s+-D|checkout\s+--\s|restore\b)",
            re.IGNORECASE,
        ),
        "reason": "Destructive Git operations cannot be imported as executable guidance.",
    },
    {
        "id": "dynamic_code_execution",
        "severity": "review",
        "pattern": re.compile(
            r"\b(?:eval|exec)\s*\(|subprocess\.(?:run|Popen)\([^\n]{0,300}shell\s*=\s*True",
            re.IGNORECASE,
        ),
        "reason": "Dynamic code execution requires independent security review.",
    },
    {
        "id": "secret_access",
        "severity": "review",
        "pattern": re.compile(
            r"(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|CLIENT_SECRET|PRIVATE_KEY|\.ssh/|\.aws/credentials)",
            re.IGNORECASE,
        ),
        "reason": "Secret or credential access must use BOSS SecretRefs.",
    },
    {
        "id": "policy_bypass_language",
        "severity": "block",
        "pattern": re.compile(
            r"(?:ignore|override|bypass|disable).{0,80}(?:policy|approval|guardrail|safety|permission)",
            re.IGNORECASE,
        ),
        "reason": "Imported instructions may not weaken policy, approval, or permission gates.",
    },
    {
        "id": "vendor_specific_instruction",
        "severity": "info",
        "pattern": re.compile(r"\b(?:Claude|Anthropic|CLAUDE\.md|PreToolUse|PostToolUse)\b"),
        "reason": "Vendor-specific instructions require a model-agnostic BOSS contract.",
    },
)


_COMMAND_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "fs.root_recursive_delete",
        "decision": "block",
        "pattern": re.compile(
            r"(?:^|[;&|]\s*)rm\s+(?:-[a-z]*r[a-z]*f|-+[a-z]*f[a-z]*r)\s+"
            r"(?:/|~|\$HOME|\.|\.\.|\*|[^\s]*/\*)\s*(?:$|[;&|])",
            re.IGNORECASE,
        ),
        "reason": "Recursive forced deletion of a broad target is blocked.",
    },
    {
        "id": "git.history_or_tree_destroy",
        "decision": "block",
        "pattern": re.compile(
            r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|push\b[^\n]*--force(?:-with-lease)?|"
            r"branch\s+-D|checkout\s+--\s|restore\s+(?:--source\s+\S+\s+)?(?:\.\s*$|:/(?:\s|$)))",
            re.IGNORECASE,
        ),
        "reason": "Destructive Git history or working-tree mutation is blocked.",
    },
    {
        "id": "host_or_device_destroy",
        "decision": "block",
        "pattern": re.compile(
            r"\b(?:mkfs(?:\.[a-z0-9]+)?|wipefs|shutdown|reboot|poweroff)\b|"
            r"\bdd\s+[^\n]*\bof=/dev/",
            re.IGNORECASE,
        ),
        "reason": "Host- or device-destructive operations are blocked.",
    },
    {
        "id": "database_destructive",
        "decision": "block",
        "pattern": re.compile(
            r"\b(?:DROP\s+(?:DATABASE|SCHEMA)|TRUNCATE\s+(?:TABLE\s+)?)\b|"
            r"\bDELETE\s+FROM\s+[A-Za-z0-9_.\"-]+\s*"
            r"(?![^;\n]*\bWHERE\b)[^;\n]*(?:;|$)",
            re.IGNORECASE,
        ),
        "reason": "Unscoped destructive database operations are blocked.",
    },
    {
        "id": "remote_script_execution",
        "decision": "block",
        "pattern": re.compile(
            r"(?:curl|wget)[^\n|]{0,500}\|\s*(?:sudo\s+)?(?:ba|z|fi)?sh\b|"
            r"(?:invoke-expression|\biex\b).{0,300}(?:invoke-webrequest|downloadstring)",
            re.IGNORECASE,
        ),
        "reason": "Remote script execution must be downloaded, hashed, reviewed, and approved first.",
    },
    {
        "id": "privilege_escalation",
        "decision": "review",
        "pattern": re.compile(r"(?:^|[;&|]\s*)sudo\b|\bsu\s+-c\b", re.IGNORECASE),
        "reason": "Privilege escalation requires explicit approval and a rollback plan.",
    },
    {
        "id": "broad_permission_change",
        "decision": "review",
        "pattern": re.compile(r"\bchmod\s+(?:-R\s+)?(?:777|a\+rwx)\b", re.IGNORECASE),
        "reason": "Broad permission changes require explicit review.",
    },
    {
        "id": "network_write",
        "decision": "review",
        "pattern": re.compile(
            r"\b(?:curl|wget)\b[^\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data|--upload-file)",
            re.IGNORECASE,
        ),
        "reason": "Network writes must use a policy-guarded adapter and Receipt.",
    },
)


class ToolchainRegistryService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _manifest_path(self):
        return self.settings.specpack_root / "toolchains" / "upstreams.yaml"

    def _load_manifest(self) -> list[dict[str, Any]]:
        path = self._manifest_path()
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("format") != "boss.toolchain-sources/v1":
            raise ToolchainImportError("Invalid BOSS toolchain source manifest.")
        sources = payload.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ToolchainImportError("Toolchain source manifest has no sources.")
        return sources

    def seed_sources(
        self,
        connection: Connection,
        project_id: str,
        actor: str = "bootstrap",
    ) -> None:
        for source in self._load_manifest():
            connection.execute(
                text(
                    """
                    INSERT INTO toolchain_sources (
                      id, project_id, source_key, display_name, repository_url,
                      pinned_revision, license_id, license_status,
                      integration_mode, status, capabilities, adopted_components,
                      provenance, created_by
                    ) VALUES (
                      :id, :project_id, :source_key, :display_name, :repository_url,
                      :pinned_revision, :license_id, :license_status,
                      :integration_mode, :status, CAST(:capabilities AS JSONB),
                      CAST(:adopted_components AS JSONB), CAST(:provenance AS JSONB),
                      :created_by
                    )
                    ON CONFLICT (project_id, source_key) DO UPDATE SET
                      display_name=EXCLUDED.display_name,
                      repository_url=EXCLUDED.repository_url,
                      pinned_revision=EXCLUDED.pinned_revision,
                      license_id=EXCLUDED.license_id,
                      license_status=EXCLUDED.license_status,
                      integration_mode=EXCLUDED.integration_mode,
                      status=EXCLUDED.status,
                      capabilities=EXCLUDED.capabilities,
                      adopted_components=EXCLUDED.adopted_components,
                      provenance=EXCLUDED.provenance,
                      updated_at=now()
                    """
                ),
                {
                    "id": make_id("tools_source"),
                    "project_id": project_id,
                    "source_key": self._required(source, "source_key"),
                    "display_name": self._required(source, "display_name"),
                    "repository_url": self._required(source, "repository_url"),
                    "pinned_revision": self._revision(source.get("pinned_revision")),
                    "license_id": self._required(source, "license_id"),
                    "license_status": source.get("license_status"),
                    "integration_mode": source.get("integration_mode"),
                    "status": source.get("status", "declared"),
                    "capabilities": json.dumps(source.get("capabilities") or []),
                    "adopted_components": json.dumps(
                        source.get("adopted_components") or []
                    ),
                    "provenance": json.dumps(source.get("provenance") or {}),
                    "created_by": actor,
                },
            )

    def list_sources(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT s.*, COALESCE((
                  SELECT count(*) FROM toolchain_import_runs r
                  WHERE r.source_id=s.id
                ), 0) AS import_count
                FROM toolchain_sources s
                WHERE s.project_id=:project_id
                ORDER BY s.source_key
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._serialize(row) for row in rows]

    def list_import_runs(
        self,
        connection: Connection,
        project_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT r.*, s.source_key, s.display_name,
                       COALESCE((SELECT count(*) FROM toolchain_candidates c
                                 WHERE c.import_run_id=r.id), 0) AS candidate_count
                FROM toolchain_import_runs r
                JOIN toolchain_sources s ON s.id=r.source_id
                WHERE r.project_id=:project_id
                ORDER BY r.created_at DESC LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": max(1, min(200, int(limit)))},
        ).mappings().all()
        return [self._serialize(row) for row in rows]

    def assess_snapshot(
        self,
        connection: Connection,
        *,
        project_id: str,
        source_key: str,
        revision: str,
        files: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        source = connection.execute(
            text(
                "SELECT * FROM toolchain_sources "
                "WHERE project_id=:project_id AND source_key=:source_key"
            ),
            {"project_id": project_id, "source_key": source_key},
        ).mappings().one_or_none()
        if source is None:
            raise ToolchainImportError("Toolchain source is not registered.")
        if not isinstance(files, list) or not files:
            raise ToolchainImportError("files must be a non-empty snapshot array.")
        if len(files) > MAX_FILES:
            raise ToolchainImportError(f"snapshot exceeds the {MAX_FILES}-file limit.")

        revision = self._revision(revision)
        global_findings: list[dict[str, Any]] = []
        if revision != source["pinned_revision"]:
            global_findings.append(
                self._finding(
                    "revision_mismatch",
                    "block",
                    "Snapshot revision does not match the reviewed pinned revision.",
                )
            )
        if source["license_status"] == "restricted":
            global_findings.append(
                self._finding(
                    "restricted_license",
                    "block",
                    "The source license forbids this integration; content import is blocked.",
                )
            )
        elif source["license_status"] in {"mixed", "missing"}:
            global_findings.append(
                self._finding(
                    "license_not_repository_wide",
                    "review",
                    "Content remains catalog-only until an item-specific license is proven.",
                )
            )

        total_bytes = 0
        inventory: Counter[str] = Counter()
        candidates: list[dict[str, Any]] = []
        snapshot_material: list[str] = []
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                raise ToolchainImportError("Every snapshot file must be an object.")
            raw_path = str(item.get("path") or "")
            content = item.get("content")
            if not isinstance(content, str):
                raise ToolchainImportError(f"Snapshot file {raw_path or index} is not text.")
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_FILE_BYTES:
                raise ToolchainImportError(
                    f"Snapshot file {raw_path or index} exceeds {MAX_FILE_BYTES} bytes."
                )
            total_bytes += len(encoded)
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise ToolchainImportError(
                    f"snapshot exceeds the {MAX_SNAPSHOT_BYTES}-byte limit."
                )
            path_findings: list[dict[str, Any]] = []
            try:
                safe_path = self._safe_path(raw_path)
            except ToolchainImportError as error:
                safe_path = f"<invalid-path-{index}>"
                path_findings.append(
                    self._finding("path_escape", "block", str(error))
                )
            kind = self._candidate_kind(safe_path)
            inventory[kind] += 1
            content_hash = hashlib.sha256(encoded).hexdigest()
            snapshot_material.append(f"{safe_path}\0{content_hash}")
            findings = [
                *global_findings,
                *path_findings,
                *self.scan_text(content),
            ]
            status, risk_tier = self._candidate_disposition(
                source, kind, findings
            )
            candidates.append(
                {
                    "id": make_id("tools_candidate"),
                    "source_path": safe_path,
                    "candidate_kind": kind,
                    "content_hash": content_hash,
                    "status": status,
                    "risk_tier": risk_tier,
                    "findings": findings,
                    "normalized_spec": self._normalized_candidate(
                        source, safe_path, kind, content_hash, risk_tier
                    ),
                }
            )

        snapshot_hash = hashlib.sha256(
            (revision + "\n" + "\n".join(sorted(snapshot_material))).encode("utf-8")
        ).hexdigest()
        statuses = {item["status"] for item in candidates}
        run_status = (
            "rejected"
            if any(item["severity"] == "block" for item in global_findings)
            else "quarantined"
            if "quarantined" in statuses
            else "review_required"
            if "review_required" in statuses
            else "approved"
        )
        run_id = make_id("tools_import")
        inserted = connection.execute(
            text(
                """
                INSERT INTO toolchain_import_runs (
                  id, project_id, source_id, revision, snapshot_hash, status,
                  file_count, total_bytes, inventory, findings, created_by
                ) VALUES (
                  :id, :project_id, :source_id, :revision, :snapshot_hash, :status,
                  :file_count, :total_bytes, CAST(:inventory AS JSONB),
                  CAST(:findings AS JSONB), :created_by
                )
                ON CONFLICT (project_id, source_id, snapshot_hash) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": run_id,
                "project_id": project_id,
                "source_id": source["id"],
                "revision": revision,
                "snapshot_hash": snapshot_hash,
                "status": run_status,
                "file_count": len(files),
                "total_bytes": total_bytes,
                "inventory": json.dumps(dict(inventory)),
                "findings": json.dumps(global_findings),
                "created_by": actor,
            },
        ).scalar_one_or_none()
        reused = inserted is None
        if reused:
            stored_run = connection.execute(
                text(
                    "SELECT id, status, inventory, findings FROM toolchain_import_runs "
                    "WHERE project_id=:project_id AND source_id=:source_id "
                    "AND snapshot_hash=:snapshot_hash"
                ),
                {
                    "project_id": project_id,
                    "source_id": source["id"],
                    "snapshot_hash": snapshot_hash,
                },
            ).mappings().one()
            run_id = stored_run["id"]
            run_status = stored_run["status"]
            inventory = Counter(stored_run["inventory"] or {})
            global_findings = list(stored_run["findings"] or [])
        else:
            for candidate in candidates:
                connection.execute(
                    text(
                        """
                        INSERT INTO toolchain_candidates (
                          id, project_id, import_run_id, source_path,
                          candidate_kind, content_hash, status, risk_tier,
                          findings, normalized_spec
                        ) VALUES (
                          :id, :project_id, :import_run_id, :source_path,
                          :candidate_kind, :content_hash, :status, :risk_tier,
                          CAST(:findings AS JSONB), CAST(:normalized_spec AS JSONB)
                        )
                        """
                    ),
                    {
                        **candidate,
                        "project_id": project_id,
                        "import_run_id": run_id,
                        "findings": json.dumps(candidate["findings"]),
                        "normalized_spec": json.dumps(
                            candidate["normalized_spec"]
                        ),
                    },
                )
            emit_event(
                connection,
                project_id,
                "toolchain.snapshot_assessed",
                actor,
                "toolchain_import_run",
                run_id,
                {
                    "source_key": source_key,
                    "revision": revision,
                    "snapshot_hash": snapshot_hash,
                    "status": run_status,
                    "file_count": len(files),
                    "candidate_statuses": dict(Counter(
                        item["status"] for item in candidates
                    )),
                },
            )
        stored_candidates = connection.execute(
            text(
                "SELECT * FROM toolchain_candidates "
                "WHERE import_run_id=:run_id ORDER BY source_path"
            ),
            {"run_id": run_id},
        ).mappings().all()
        return {
            "import_run_id": run_id,
            "source_key": source_key,
            "revision": revision,
            "snapshot_hash": snapshot_hash,
            "status": run_status,
            "reused": reused,
            "inventory": dict(inventory),
            "findings": global_findings,
            "candidates": [self._serialize(row) for row in stored_candidates],
        }

    def review_candidate(
        self,
        connection: Connection,
        *,
        project_id: str,
        candidate_id: str,
        decision: str,
        note: str,
        actor: str,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ToolchainImportError("decision must be approve or reject.")
        row = connection.execute(
            text(
                """
                SELECT c.*, s.license_status, s.integration_mode,
                       r.created_by AS import_created_by
                FROM toolchain_candidates c
                JOIN toolchain_import_runs r ON r.id=c.import_run_id
                JOIN toolchain_sources s ON s.id=r.source_id
                WHERE c.id=:candidate_id AND c.project_id=:project_id
                FOR UPDATE
                """
            ),
            {"candidate_id": candidate_id, "project_id": project_id},
        ).mappings().one_or_none()
        if row is None:
            raise ToolchainImportError("Toolchain candidate not found.")
        note = note.strip()
        if not note:
            raise ToolchainImportError("A review note is required.")
        if row["status"] in {"approved", "rejected"}:
            raise ToolchainImportError("Toolchain candidate review is already terminal.")
        findings = row["findings"] or []
        if decision == "approve" and actor == row["import_created_by"]:
            raise ToolchainImportError(
                "Candidate approval requires a reviewer independent from the importer."
            )
        if decision == "approve" and (
            row["status"] != "review_required"
            or row["license_status"] != "allowed"
            or row["integration_mode"] in {"catalog_only", "blocked"}
            or any(item.get("severity") == "block" for item in findings)
        ):
            raise ToolchainImportError(
                "Candidate cannot be approved until license and blocking findings are resolved."
            )
        status = "approved" if decision == "approve" else "rejected"
        updated = connection.execute(
            text(
                """
                UPDATE toolchain_candidates
                SET status=:status, reviewed_by=:actor, reviewed_at=now(),
                    review_note=:note, updated_at=now()
                WHERE id=:candidate_id AND project_id=:project_id
                RETURNING *
                """
            ),
            {
                "status": status,
                "actor": actor,
                "note": note,
                "candidate_id": candidate_id,
                "project_id": project_id,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            f"toolchain.candidate_{status}",
            actor,
            "toolchain_candidate",
            candidate_id,
            {"source_path": row["source_path"], "review_note": note},
        )
        return self._serialize(updated)

    @staticmethod
    def scan_text(content: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for rule in _SCAN_RULES:
            if rule["pattern"].search(content):
                findings.append(
                    ToolchainRegistryService._finding(
                        rule["id"], rule["severity"], rule["reason"]
                    )
                )
        return findings

    @staticmethod
    def evaluate_command(command: str, cwd: Optional[str] = None) -> dict[str, Any]:
        command = str(command or "").strip()
        if not command:
            raise ToolchainImportError("command is required.")
        matches = [rule for rule in _COMMAND_RULES if rule["pattern"].search(command)]
        decisions = {rule["decision"] for rule in matches}
        decision = "block" if "block" in decisions else "review" if matches else "allow"
        return {
            "decision": decision,
            "risk_tier": 3 if decision == "block" else 2 if decision == "review" else 0,
            "rule_ids": [rule["id"] for rule in matches],
            "reasons": [rule["reason"] for rule in matches],
            "cwd": cwd,
            "command_hash": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "model_override_allowed": False,
        }

    @staticmethod
    def pack_context(
        chunks: list[dict[str, Any]],
        token_budget: int,
        reserved_output_tokens: int = 0,
    ) -> dict[str, Any]:
        token_budget = int(token_budget)
        reserved_output_tokens = int(reserved_output_tokens)
        if token_budget < 128:
            raise ToolchainImportError("token_budget must be at least 128.")
        if reserved_output_tokens < 0 or reserved_output_tokens >= token_budget:
            raise ToolchainImportError(
                "reserved_output_tokens must be non-negative and below token_budget."
            )
        if not isinstance(chunks, list):
            raise ToolchainImportError("chunks must be an array.")
        if len(chunks) > MAX_CONTEXT_CHUNKS:
            raise ToolchainImportError(
                f"context exceeds the {MAX_CONTEXT_CHUNKS}-chunk limit."
            )

        available = token_budget - reserved_output_tokens
        normalized: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        duplicate_ids: list[str] = []
        total_context_bytes = 0
        for index, raw in enumerate(chunks):
            if not isinstance(raw, dict):
                raise ToolchainImportError("Every context chunk must be an object.")
            chunk_id = str(raw.get("id") or f"chunk-{index}").strip()
            body = str(raw.get("text") or "").strip()
            if not body:
                continue
            total_context_bytes += len(body.encode("utf-8"))
            if total_context_bytes > MAX_CONTEXT_BYTES:
                raise ToolchainImportError(
                    f"context exceeds the {MAX_CONTEXT_BYTES}-byte limit."
                )
            content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if content_hash in seen_hashes:
                duplicate_ids.append(chunk_id)
                continue
            seen_hashes.add(content_hash)
            authority = str(raw.get("authority") or "reference").strip().lower()
            required = bool(raw.get("required")) or authority in AUTHORITATIVE_CONTEXT
            priority = max(0, min(100, int(raw.get("priority", 50))))
            source_ref = str(raw.get("source_ref") or "").strip()
            header = f"[{chunk_id} | {authority}"
            if source_ref:
                header += f" | {source_ref}"
            rendered = f"{header}]\n{body}"
            estimated_tokens = max(1, (len(rendered) + 3) // 4)
            normalized.append(
                {
                    "id": chunk_id,
                    "authority": authority,
                    "required": required,
                    "priority": priority,
                    "rendered": rendered,
                    "estimated_tokens": estimated_tokens,
                }
            )

        authority_rank = {
            "policy": 5,
            "approval": 4,
            "contract": 3,
            "proof": 2,
            "fact": 1,
            "reference": 0,
        }
        ordered = sorted(
            normalized,
            key=lambda item: (
                not item["required"],
                -authority_rank.get(item["authority"], 0),
                -item["priority"],
                item["id"],
            ),
        )
        mandatory_tokens = sum(
            item["estimated_tokens"] for item in ordered if item["required"]
        )
        if mandatory_tokens > available:
            return {
                "context": "",
                "included_ids": [],
                "omitted_ids": [item["id"] for item in ordered],
                "duplicate_ids": duplicate_ids,
                "estimated_tokens": 0,
                "available_input_tokens": available,
                "budget_status": "blocked_authoritative_overflow",
                "required_tokens": mandatory_tokens,
                "compression_applied": False,
            }

        included: list[dict[str, Any]] = []
        omitted: list[str] = []
        used = 0
        for item in ordered:
            if item["required"] or used + item["estimated_tokens"] <= available:
                included.append(item)
                used += item["estimated_tokens"]
            else:
                omitted.append(item["id"])
        return {
            "context": "\n\n".join(item["rendered"] for item in included),
            "included_ids": [item["id"] for item in included],
            "omitted_ids": omitted,
            "duplicate_ids": duplicate_ids,
            "estimated_tokens": used,
            "available_input_tokens": available,
            "budget_status": "within_budget" if not omitted else "trimmed_non_authoritative",
            "required_tokens": mandatory_tokens,
            "compression_applied": False,
        }

    @staticmethod
    def _candidate_disposition(
        source: Any,
        kind: str,
        findings: Iterable[dict[str, Any]],
    ) -> tuple[str, int]:
        findings = list(findings)
        severity = {item.get("severity") for item in findings}
        executable = kind in {"hook", "script", "mcp", "package", "binary"}
        if (
            source["integration_mode"] == "blocked"
            or source["license_status"] == "restricted"
            or "block" in severity
            or executable
        ):
            return "quarantined", 3
        if source["integration_mode"] == "catalog_only":
            return "metadata_only", 1
        if kind in {"skill", "agent", "command", "rule", "prompt"}:
            return "review_required", 2 if "review" in severity else 1
        return "metadata_only", 1 if "review" in severity else 0

    @staticmethod
    def _normalized_candidate(
        source: Any,
        source_path: str,
        kind: str,
        content_hash: str,
        risk_tier: int,
    ) -> dict[str, Any]:
        slug = re.sub(r"[^a-z0-9]+", ".", source_path.lower()).strip(".")
        return {
            "format": "boss.toolchain-candidate/v1",
            "id": f"upstream.{source['source_key']}.{slug}"[:180],
            "kind": kind,
            "risk_tier": risk_tier,
            "source": {
                "repository_url": source["repository_url"],
                "revision": source["pinned_revision"],
                "path": source_path,
                "content_hash": content_hash,
                "license_id": source["license_id"],
            },
            "model_agnostic": True,
            "raw_content_stored": False,
            "raw_content_executable": False,
            "activation_requires_native_implementation": True,
            "activation_requires_independent_review": True,
        }

    @staticmethod
    def _candidate_kind(path: str) -> str:
        lower = path.lower()
        parts = PurePosixPath(lower).parts
        name = parts[-1] if parts else lower
        if name in {"skill.md", "skill.mdx"} or "skills" in parts:
            return "skill"
        if "agents" in parts:
            return "agent"
        if "hooks" in parts:
            return "hook"
        if "commands" in parts:
            return "command"
        if "rules" in parts or name.endswith(("rules.md", "policy.yaml", "policy.yml")):
            return "rule"
        if "mcp" in name or "mcp" in parts:
            return "mcp"
        if name in {"package.json", "pyproject.toml", "cargo.toml", "requirements.txt"}:
            return "package"
        if name.endswith((".sh", ".bash", ".zsh", ".ps1", ".py", ".js", ".ts", ".rb")):
            return "script"
        if name.endswith((".md", ".mdx", ".txt")):
            return "prompt"
        if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".zip", ".tar", ".gz", ".exe")):
            return "binary"
        return "metadata"

    @staticmethod
    def _safe_path(path: str) -> str:
        if not path or "\x00" in path or "\\" in path:
            raise ToolchainImportError("Snapshot path is empty or non-portable.")
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ToolchainImportError("Snapshot path escapes the source root.")
        normalized = candidate.as_posix()
        if len(normalized) > 512:
            raise ToolchainImportError("Snapshot path exceeds 512 characters.")
        return normalized

    @staticmethod
    def _revision(value: Any) -> str:
        revision = str(value or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ToolchainImportError("revision must be a full 40-character Git SHA.")
        return revision

    @staticmethod
    def _required(payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise ToolchainImportError(f"{key} is required in the source manifest.")
        return value

    @staticmethod
    def _finding(rule_id: str, severity: str, reason: str) -> dict[str, str]:
        return {"rule_id": rule_id, "severity": severity, "reason": reason}

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        payload = dict(row)
        for key, value in list(payload.items()):
            if hasattr(value, "isoformat"):
                payload[key] = value.isoformat()
        return payload
