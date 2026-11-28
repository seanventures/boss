"""APP MAP — the single stored record of where an app lives, how it builds, tests and runs.

BOSSAPP-555 (App Map 1) & BOSSAPP-556 (App Map 2):
Previously nine independent resolvers answered "where is the app?", each defaulting to the
repository root. This module provides ONE canonical stored ``AppMapEntry`` (persisted on
``app_components`` and carried on ``code_task_runs.finalize_ctx.app_map``) read by every stage:
  * stack planning (``stacks.find_stack_root``)
  * verification gate & cwd (``verification_target.resolve_gate_plan``, ``resolve_verify_cwd``, ``app_scope``)
  * preview & serve check (``preview_service.how_app_runs``, ``app_dir_of``, ``named_app_directory``)
  * pre-check & materialization (``governed_code_task_service._precheck_app_env``, ``workflow_precheck.materialize_build``)
  * deploy manifest & connectors (``app_deploy_manifest.app_folder``, ``app_manifest``, ``deployment_connectors``)
  * review & project scope (``project_scope``)
  * write boundary (``app_root.files_outside_app``)

Core invariants:
  1. Every stage reads the stored ``AppMapEntry`` first.
  2. When a stored map names a subfolder app (``root_path != "."``), NO stage ever falls back to
     the repository root — across fresh, repair, resumed and seeded phases.
  3. With no map in an ambiguous/multi-app repository (or when ``require_map=True``), BOSS refuses
     explicitly rather than guessing the repository root.
  4. Manifest fingerprint changes are detected deterministically and trigger re-proposal
     (``status = "stale"``).
  5. Baseline install + test on the base commit is stored on the record with audit events.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text

from .core import emit_event, make_id

MAP_STATUSES = ("proposed", "confirmed", "stale", "ambiguous")

#: Manifests hashed into the AppMap fingerprint (at workspace root, app root, and immediate service dirs).
FINGERPRINT_MANIFEST_NAMES: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "yarn.lock",
    "turbo.json",
    "nx.json",
    "project.json",
    "workspace.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "composer.json",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Procfile",
    "boss.deploy.yaml",
    "boss.verify.yaml",
    "boss.preview.command",
    "boss.preview.health",
)

#: Refusal shown when no App Map is stored and BOSS cannot unambiguously place the app.
NO_MAP_REFUSAL = (
    "No App Map is configured for this app, and BOSS will not default to the repository root. "
    "Scan and confirm the App Map in the console (or add `.boss/apps.yaml`), then retry."
)


class AppMapError(ValueError):
    """Structured error for App Map validation, resolution, or persistence."""

    def __init__(self, code: str, message: str, *, status: int = 422, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), **self.extra}


class AppMapRequiredError(AppMapError):
    """Raised when a stage requires an App Map and refuses to fall back to the repository root."""

    def __init__(self, message: str = NO_MAP_REFUSAL, *, candidates: Optional[list[dict[str, Any]]] = None) -> None:
        super().__init__(
            "app_map_required",
            message,
            status=409,
            **({"candidates": candidates} if candidates else {}),
        )
        self.candidates = list(candidates or [])


def normalize_root_path(value: Any, *, allow_none: bool = False) -> str:
    """Normalize a repository-relative app root to '.' (repo root) or clean POSIX 'a/b'."""
    if value is None and allow_none:
        return "."
    raw = str(value if value is not None else ".").strip()
    if raw in ("", ".", "/", "./"):
        return "."
    raw = raw.replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.strip("/")
    if not raw or raw == ".":
        return "."
    p = PurePosixPath(raw)
    if p.is_absolute() or ".." in p.parts:
        raise AppMapError("invalid_root_path", f"App path {value!r} must stay inside the repository.")
    return p.as_posix()


def component_path_for_root(root_path: str) -> str:
    """Convert normalized root_path ('.' or 'apps/web') to app_components.path ('/' or '/apps/web')."""
    norm = normalize_root_path(root_path)
    return "/" if norm == "." else f"/{norm}"


def _jload(val: Any, default: Any) -> Any:
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return json.loads(val)
        except ValueError:
            return default
    return val


def _clean_str_tuple(items: Any) -> tuple[str, ...]:
    if not items:
        return ()
    out: list[str] = []
    for item in items:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s)
    return tuple(out)


def _clean_shared_paths(items: Any) -> tuple[str, ...]:
    if not items:
        return ()
    out: list[str] = []
    for item in items:
        raw = str(item or "").strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        raw = raw.strip("/")
        if not raw or raw == ".":
            continue
        p = PurePosixPath(raw)
        if p.is_absolute() or ".." in p.parts:
            raise AppMapError("invalid_shared_path", f"Shared path {item!r} must stay inside the repository.")
        s = p.as_posix()
        if s not in out:
            out.append(s)
    return tuple(out)


@dataclass(frozen=True)
class AppMapEntry:
    """Canonical stored App Map entry for one application/component in a repository."""

    app_key: str
    app_name: str
    root_path: str = "."
    workspace_root: str = "."
    name: str = "app"
    stack: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, Any] = field(default_factory=dict)
    run: dict[str, Any] = field(default_factory=dict)
    manifests: tuple[str, ...] = ()
    shared_paths: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    fingerprint: Optional[str] = None
    baseline: dict[str, Any] = field(default_factory=dict)
    status: str = "proposed"
    binding_id: Optional[str] = None
    repository: Optional[str] = None
    kind: str = "code"
    access: str = "write"
    depends_on: tuple[str, ...] = ()
    id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_path", normalize_root_path(self.root_path))
        object.__setattr__(self, "workspace_root", normalize_root_path(self.workspace_root))
        st = str(self.status or "proposed").strip().lower()
        if st not in MAP_STATUSES:
            st = "proposed"
        object.__setattr__(self, "status", st)
        object.__setattr__(self, "manifests", _clean_str_tuple(self.manifests))
        object.__setattr__(self, "shared_paths", _clean_shared_paths(self.shared_paths))
        object.__setattr__(self, "depends_on", _clean_str_tuple(self.depends_on))

    def is_root(self) -> bool:
        return self.root_path == "."

    def rel_or_none(self) -> Optional[str]:
        return None if self.root_path == "." else self.root_path

    def resolved_dir(self, repo_root: Path | str) -> Path:
        base = Path(repo_root)
        return base if self.root_path == "." else base / self.root_path

    def resolved_workspace_dir(self, repo_root: Path | str) -> Path:
        base = Path(repo_root)
        return base if self.workspace_root == "." else base / self.workspace_root

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app_key": self.app_key,
            "app_name": self.app_name,
            "name": self.name,
            "root_path": self.root_path,
            "workspace_root": self.workspace_root,
            "path": component_path_for_root(self.root_path),
            "stack": dict(self.stack or {}),
            "commands": dict(self.commands or {}),
            "run": dict(self.run or {}),
            "manifests": list(self.manifests),
            "shared_paths": list(self.shared_paths),
            "provenance": dict(self.provenance or {}),
            "fingerprint": self.fingerprint,
            "baseline": dict(self.baseline or {}),
            "status": self.status,
            "binding_id": self.binding_id,
            "repository": self.repository,
            "kind": self.kind,
            "access": self.access,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["AppMapEntry"]:
        if isinstance(raw, AppMapEntry):
            return raw
        if not isinstance(raw, Mapping):
            return None
        root_raw = raw.get("root_path")
        if root_raw is None:
            root_raw = raw.get("path")
        if root_raw is None:
            return None
        try:
            root_path = normalize_root_path(root_raw)
            ws_raw = raw.get("workspace_root")
            workspace_root = normalize_root_path(ws_raw) if ws_raw is not None else "."
        except AppMapError:
            return None
        app_key = str(raw.get("app_key") or raw.get("key") or raw.get("name") or (
            PurePosixPath(root_path).name if root_path != "." else "app"
        )).strip()
        app_name = str(raw.get("app_name") or raw.get("name") or app_key).strip()
        name = str(raw.get("name") or app_name or "app").strip()
        return cls(
            id=str(raw["id"]) if raw.get("id") else None,
            app_key=app_key,
            app_name=app_name,
            name=name,
            root_path=root_path,
            workspace_root=workspace_root,
            stack=dict(_jload(raw.get("stack"), {}) or {}),
            commands=dict(_jload(raw.get("commands"), {}) or {}),
            run=dict(_jload(raw.get("run"), {}) or {}),
            manifests=_clean_str_tuple(_jload(raw.get("manifests"), []) or []),
            shared_paths=_clean_shared_paths(_jload(raw.get("shared_paths"), []) or []),
            provenance=dict(_jload(raw.get("provenance"), {}) or {}),
            fingerprint=str(raw["fingerprint"]) if raw.get("fingerprint") else None,
            baseline=dict(_jload(raw.get("baseline"), {}) or {}),
            status=str(raw.get("status") or raw.get("map_status") or "proposed"),
            binding_id=str(raw["binding_id"]) if raw.get("binding_id") else None,
            repository=str(raw["repository"] or raw.get("full_name")) if (raw.get("repository") or raw.get("full_name")) else None,
            kind=str(raw.get("kind") or "code"),
            access=str(raw.get("access") or "write"),
            depends_on=_clean_str_tuple(raw.get("depends_on") or ()),
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AppMapEntry":
        root_raw = row.get("root_path")
        if not root_raw:
            root_raw = row.get("path") or "/"
        root_path = normalize_root_path(root_raw)
        ws_raw = row.get("workspace_root")
        workspace_root = normalize_root_path(ws_raw) if ws_raw else "."
        return cls(
            id=str(row["id"]) if row.get("id") else None,
            app_key=str(row.get("app_key") or ""),
            app_name=str(row.get("app_name") or row.get("app_key") or row.get("name") or ""),
            name=str(row.get("name") or "app"),
            root_path=root_path,
            workspace_root=workspace_root,
            stack=dict(_jload(row.get("stack"), {}) or {}),
            commands=dict(_jload(row.get("commands"), {}) or {}),
            run=dict(_jload(row.get("run"), {}) or {}),
            manifests=_clean_str_tuple(_jload(row.get("manifests"), []) or []),
            shared_paths=_clean_shared_paths(_jload(row.get("shared_paths"), []) or []),
            provenance=dict(_jload(row.get("provenance"), {}) or {}),
            fingerprint=str(row["fingerprint"]) if row.get("fingerprint") else None,
            baseline=dict(_jload(row.get("baseline"), {}) or {}),
            status=str(row.get("map_status") or "proposed"),
            binding_id=str(row["binding_id"]) if row.get("binding_id") else None,
            repository=str(row["full_name"]) if row.get("full_name") else None,
            kind=str(row.get("kind") or "code"),
            access=str(row.get("access") or "write"),
            depends_on=_clean_str_tuple(row.get("depends_on") or ()),
        )


@dataclass(frozen=True)
class AppMapResolution:
    """The result of resolving an app for any pipeline stage."""

    entry: Optional[AppMapEntry]
    path: Optional[Path]
    root_path: Optional[str]
    source: str  # stored_map | boss_apps_yaml | named | recorded | changed | lineage | single_app | refused
    refusal: Optional[str] = None
    candidates: tuple[dict[str, Any], ...] = ()
    stale: bool = False

    @property
    def ok(self) -> bool:
        return self.path is not None and self.refusal is None

    @property
    def rel_or_none(self) -> Optional[str]:
        if self.root_path in (None, "", "."):
            return None
        return self.root_path


# ── Manifest fingerprinting & drift detection ──────────────────────────────────────────────────

def collect_manifest_paths(
    repo_root: Path | str,
    root_path: str = ".",
    workspace_root: str = ".",
    extra_manifests: Optional[Iterable[str]] = None,
) -> list[str]:
    """Return sorted repo-relative manifest paths affecting the app at `root_path`."""
    base = Path(repo_root)
    norm_root = normalize_root_path(root_path)
    norm_ws = normalize_root_path(workspace_root)
    found: set[str] = set()

    def _check_file(rel: str) -> None:
        clean = rel.replace("\\", "/")
        while clean.startswith("./"):
            clean = clean[2:]
        clean = clean.lstrip("/")
        if not clean or ".." in PurePosixPath(clean).parts:
            return
        if (base / clean).is_file():
            found.add(clean)

    # .boss/apps.yaml always affects every app in the repo
    _check_file(".boss/apps.yaml")

    # Workspace root manifests (only workspace/config descriptors when app is in a subfolder)
    ws_dir = base if norm_ws == "." else base / norm_ws
    ws_prefix = "" if norm_ws == "." else norm_ws + "/"
    ws_names = (
        "package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml", "package-lock.json",
        "yarn.lock", "turbo.json", "nx.json", "workspace.json",
        "pom.xml", "settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts",
    )
    if ws_dir.is_dir():
        for name in ws_names:
            if (ws_dir / name).is_file():
                found.add(ws_prefix + name)

    # App root manifests
    app_dir = base if norm_root == "." else base / norm_root
    app_prefix = "" if norm_root == "." else norm_root + "/"
    if app_dir.is_dir():
        for name in FINGERPRINT_MANIFEST_NAMES:
            if (app_dir / name).is_file():
                found.add(app_prefix + name)
        # Immediate child service manifests (e.g. backend/requirements.txt, frontend/package.json)
        skip = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", "target",
                "docs", "examples", "fixtures", "test-fixtures", "testdata", "__fixtures__", "scripts"}
        try:
            children = sorted(p for p in app_dir.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            children = []
        for child in children:
            if child.name.startswith(".") or child.name in skip:
                continue
            for name in FINGERPRINT_MANIFEST_NAMES:
                if (child / name).is_file():
                    found.add(f"{app_prefix}{child.name}/{name}")

    for m in extra_manifests or ():
        _check_file(str(m))

    return sorted(found)


def compute_manifest_fingerprint(
    repo_root: Path | str,
    root_path: str = ".",
    workspace_root: str = ".",
    manifests: Optional[Iterable[str]] = None,
) -> tuple[str, list[str]]:
    """Deterministic SHA-256 hex digest over all manifest files relevant to the app."""
    base = Path(repo_root)
    paths = collect_manifest_paths(base, root_path, workspace_root, extra_manifests=manifests)
    h = hashlib.sha256()
    for rel in paths:
        try:
            data = (base / rel).read_bytes()
        except OSError:
            continue
        file_hash = hashlib.sha256(data).hexdigest()
        h.update(f"{rel}\0{file_hash}\n".encode("utf-8"))
    return h.hexdigest(), paths


def detect_manifest_drift(
    entry: AppMapEntry,
    repo_root: Path | str,
    *,
    rescan: bool = True,
) -> dict[str, Any]:
    """Compare stored fingerprint against the current tree; re-propose when manifests changed."""
    base = Path(repo_root)
    new_fp, current_manifests = compute_manifest_fingerprint(
        base, entry.root_path, entry.workspace_root, manifests=entry.manifests
    )
    old_fp = entry.fingerprint or ""
    drifted = bool(old_fp and new_fp != old_fp)
    prev_set = set(entry.manifests)
    curr_set = set(current_manifests)
    added = sorted(curr_set - prev_set)
    removed = sorted(prev_set - curr_set)
    proposal: Optional[dict[str, Any]] = None
    if drifted and rescan:
        from .app_map_scanner import scan_repository

        scan = scan_repository(base)
        matched = next((a for a in scan.apps if a.root_path == entry.root_path), None)
        if matched is not None:
            proposal = matched.as_dict()
        elif scan.selected is not None:
            proposal = scan.selected.as_dict()
        else:
            proposal = {
                "status": scan.status,
                "candidates": [a.as_dict() for a in scan.apps],
                "refusal": scan.refusal,
            }
    return {
        "drifted": drifted,
        "old_fingerprint": old_fp or None,
        "new_fingerprint": new_fp,
        "manifests": current_manifests,
        "added_manifests": added,
        "removed_manifests": removed,
        "proposal": proposal,
    }


# ── Extracting / reading / persisting AppMap records ───────────────────────────────────────────

def entry_from_run_context(
    run: Optional[Mapping[str, Any]] = None,
    *,
    spec: Optional[Mapping[str, Any]] = None,
    finalize_ctx: Optional[Mapping[str, Any]] = None,
) -> Optional[AppMapEntry]:
    """Read an embedded AppMapEntry from a run's finalize_ctx or spec."""
    fctx = finalize_ctx if finalize_ctx is not None else ((run or {}).get("finalize_ctx") or {})
    sp = spec if spec is not None else ((run or {}).get("spec") or {})
    for container in (fctx, sp):
        if isinstance(container, Mapping) and container.get("app_map"):
            entry = AppMapEntry.from_dict(container.get("app_map"))
            if entry is not None:
                return entry
    return None


def coerce_entry(app_map: Any) -> Optional[AppMapEntry]:
    """Accept an AppMapEntry, AppMapResolution, dict, or None and return AppMapEntry | None."""
    if app_map is None:
        return None
    if isinstance(app_map, AppMapEntry):
        return app_map
    if isinstance(app_map, AppMapResolution):
        return app_map.entry
    if isinstance(app_map, Mapping):
        return AppMapEntry.from_dict(app_map)
    return None


def _default_binding_id(db: Any, project_id: str, repository: Optional[str] = None) -> Optional[str]:
    if repository:
        row = db.execute(
            text(
                "SELECT id FROM github_repository_bindings "
                "WHERE project_id = :p AND status = 'active' AND lower(full_name) = lower(:r) "
                "ORDER BY created_at LIMIT 1"
            ),
            {"p": project_id, "r": repository},
        ).scalar()
        if row:
            return str(row)
    row = db.execute(
        text(
            "SELECT id FROM github_repository_bindings "
            "WHERE project_id = :p AND status = 'active' "
            "ORDER BY (role = 'primary') DESC, created_at LIMIT 1"
        ),
        {"p": project_id},
    ).scalar()
    return str(row) if row else None


def load_app_map(
    db: Any,
    project_id: str,
    *,
    app_key: Optional[str] = None,
    repository: Optional[str] = None,
    binding_id: Optional[str] = None,
) -> Optional[AppMapEntry]:
    """Load the canonical stored AppMapEntry from ``app_components`` for a project/app."""
    if db is None or not project_id:
        return None
    clauses = ["c.project_id = :p", "c.kind = 'code'"]
    params: dict[str, Any] = {"p": project_id}
    if app_key:
        clauses.append("c.app_key = :k")
        params["k"] = str(app_key)
    if binding_id:
        clauses.append("c.binding_id = :b")
        params["b"] = str(binding_id)
    elif repository:
        clauses.append("lower(b.full_name) = lower(:r)")
        params["r"] = str(repository)
    where = " AND ".join(clauses)
    try:
        row = db.execute(
            text(
                "SELECT c.*, b.full_name "
                "FROM app_components c "
                "JOIN github_repository_bindings b ON b.id = c.binding_id "
                f"WHERE {where} "
                "ORDER BY (c.map_status = 'confirmed') DESC, c.position, c.updated_at DESC, c.name "
                "LIMIT 1"
            ),
            params,
        ).mappings().first()
    except Exception:  # noqa: BLE001 — graceful on schemas without extended columns
        return None
    if not row:
        return None
    return AppMapEntry.from_row(dict(row))


def list_app_maps(
    db: Any,
    project_id: str,
    *,
    repo_root: Optional[Path | str] = None,
) -> list[dict[str, Any]]:
    """List all stored AppMap records for a project, annotated with drift if `repo_root` is given."""
    rows = db.execute(
        text(
            "SELECT c.*, b.full_name "
            "FROM app_components c "
            "JOIN github_repository_bindings b ON b.id = c.binding_id "
            "WHERE c.project_id = :p AND c.kind = 'code' "
            "ORDER BY c.app_key, c.position, c.name"
        ),
        {"p": project_id},
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        entry = AppMapEntry.from_row(dict(r))
        item = entry.as_dict()
        if repo_root is not None and Path(repo_root).is_dir():
            drift = detect_manifest_drift(entry, repo_root, rescan=False)
            item["drift"] = drift
            if drift["drifted"] and item["status"] == "confirmed":
                item["status"] = "stale"
        out.append(item)
    return out


def upsert_app_map(
    db: Any,
    project_id: str,
    entry: AppMapEntry,
    *,
    binding_id: Optional[str] = None,
    actor: str = "boss",
    confirm: bool = False,
) -> AppMapEntry:
    """Persist an AppMapEntry onto ``app_components`` and emit an audit event."""
    bid = binding_id or entry.binding_id or _default_binding_id(db, project_id, entry.repository)
    if not bid:
        raise AppMapError(
            "no_repository_binding",
            "This project has no active repository binding to store an App Map against.",
            status=422,
        )
    # Tenant / project scope check on binding_id
    bound = db.execute(
        text(
            "SELECT id, full_name FROM github_repository_bindings "
            "WHERE id = :b AND project_id = :p AND status = 'active' LIMIT 1"
        ),
        {"b": bid, "p": project_id},
    ).mappings().first()
    if not bound:
        raise AppMapError(
            "repository_not_in_project",
            "Choose an active repository bound to this project.",
            status=422,
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    provenance = dict(entry.provenance or {})
    status = entry.status
    if confirm:
        status = "confirmed"
        provenance["confirmed_by"] = actor
        provenance["confirmed_at"] = now_iso
        provenance.setdefault("source", "operator")

    comp_name = (entry.name or entry.app_name or entry.app_key or "app").strip()[:48] or "app"
    existing_id = db.execute(
        text(
            "SELECT id FROM app_components "
            "WHERE project_id = :p AND app_key = :a AND name = :n LIMIT 1"
        ),
        {"p": project_id, "a": entry.app_key, "n": comp_name},
    ).scalar()
    cid = str(existing_id) if existing_id else make_id("acmp")
    comp_path = component_path_for_root(entry.root_path)

    db.execute(
        text(
            "INSERT INTO app_components ("
            "  id, project_id, app_key, app_name, name, binding_id, path, access, kind, "
            "  depends_on, ship_target, position, root_path, workspace_root, stack, commands, "
            "  run, manifests, shared_paths, provenance, fingerprint, baseline, map_status, created_by"
            ") VALUES ("
            "  :id, :p, :a, :an, :n, :b, :path, :acc, :k, "
            "  :deps, CAST(:ship AS JSONB), :pos, :rp, :wr, CAST(:stack AS JSONB), CAST(:cmds AS JSONB), "
            "  CAST(:run AS JSONB), CAST(:man AS JSONB), CAST(:sh AS JSONB), CAST(:prov AS JSONB), "
            "  :fp, CAST(:base AS JSONB), :st, :by"
            ") ON CONFLICT (project_id, app_key, name) DO UPDATE SET "
            "  app_name = EXCLUDED.app_name, binding_id = EXCLUDED.binding_id, "
            "  path = EXCLUDED.path, root_path = EXCLUDED.root_path, "
            "  workspace_root = EXCLUDED.workspace_root, stack = EXCLUDED.stack, "
            "  commands = EXCLUDED.commands, run = EXCLUDED.run, "
            "  manifests = EXCLUDED.manifests, shared_paths = EXCLUDED.shared_paths, "
            "  provenance = EXCLUDED.provenance, fingerprint = EXCLUDED.fingerprint, "
            "  baseline = CASE WHEN EXCLUDED.baseline = '{}'::jsonb THEN app_components.baseline "
            "                  ELSE EXCLUDED.baseline END, "
            "  map_status = EXCLUDED.map_status, updated_at = now()"
        ),
        {
            "id": cid,
            "p": project_id,
            "a": entry.app_key,
            "an": entry.app_name or entry.app_key,
            "n": comp_name,
            "b": bid,
            "path": comp_path,
            "acc": entry.access or "write",
            "k": entry.kind or "code",
            "deps": list(entry.depends_on or ()),
            "ship": json.dumps({}),
            "pos": 0,
            "rp": entry.root_path,
            "wr": entry.workspace_root,
            "stack": json.dumps(entry.stack or {}),
            "cmds": json.dumps(entry.commands or {}),
            "run": json.dumps(entry.run or {}),
            "man": json.dumps(list(entry.manifests or ())),
            "sh": json.dumps(list(entry.shared_paths or ())),
            "prov": json.dumps(provenance),
            "fp": entry.fingerprint,
            "base": json.dumps(entry.baseline or {}),
            "st": status,
            "by": actor,
        },
    )
    emit_event(
        db,
        project_id,
        "app_map.confirmed" if confirm else "app_map.saved",
        actor,
        "app_map",
        entry.app_key,
        {
            "component_id": cid,
            "app_key": entry.app_key,
            "root_path": entry.root_path,
            "workspace_root": entry.workspace_root,
            "status": status,
            "fingerprint": entry.fingerprint,
            "source": provenance.get("source"),
        },
    )
    row = db.execute(
        text(
            "SELECT c.*, b.full_name FROM app_components c "
            "JOIN github_repository_bindings b ON b.id = c.binding_id WHERE c.id = :id"
        ),
        {"id": cid},
    ).mappings().first()
    return AppMapEntry.from_row(dict(row))


def remember_run_app_map(db: Any, run_id: str, entry: AppMapEntry) -> bool:
    """Persist the canonical AppMapEntry onto `code_task_runs.finalize_ctx.app_map` and `app_dir`."""
    if db is None or not run_id:
        return False
    payload = entry.as_dict()
    db.execute(
        text(
            "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
            "|| jsonb_build_object('app_map', CAST(:m AS JSONB), 'app_dir', CAST(:d AS TEXT)), "
            "updated_at = now() WHERE id = :id"
        ),
        {"id": run_id, "m": json.dumps(payload), "d": entry.root_path},
    )
    return True


def confirm_app_map(
    db: Any,
    project_id: str,
    app_key: str,
    payload: Mapping[str, Any],
    *,
    repo_root: Optional[Path | str] = None,
    actor: str = "operator",
) -> AppMapEntry:
    """Confirm or edit an App Map from the console confirm card."""
    key = str(app_key or "").strip()
    if not key:
        raise AppMapError("missing_app_key", "Name the app to confirm its App Map.")
    existing = load_app_map(db, project_id, app_key=key)
    root_path = normalize_root_path(
        payload.get("root_path") if payload.get("root_path") is not None
        else (existing.root_path if existing else ".")
    )
    workspace_root = normalize_root_path(
        payload.get("workspace_root") if payload.get("workspace_root") is not None
        else (existing.workspace_root if existing else ".")
    )
    app_name = str(payload.get("app_name") or (existing.app_name if existing else key)).strip() or key
    comp_name = str(payload.get("name") or (existing.name if existing else app_name)).strip() or app_name

    stack = dict(existing.stack if existing else {})
    if isinstance(payload.get("stack"), Mapping):
        stack.update(dict(payload["stack"]))
    commands = dict(existing.commands if existing else {})
    if isinstance(payload.get("commands"), Mapping):
        commands = {str(k): (str(v).strip() if v is not None and str(v).strip() else None)
                    for k, v in payload["commands"].items()}
    run_spec = dict(existing.run if existing else {})
    if isinstance(payload.get("run"), Mapping):
        run_spec = {str(k): v for k, v in payload["run"].items() if v not in (None, "")}
    shared_paths = (
        _clean_shared_paths(payload["shared_paths"])
        if "shared_paths" in payload
        else (existing.shared_paths if existing else ())
    )
    manifests = (
        _clean_str_tuple(payload["manifests"])
        if "manifests" in payload
        else (existing.manifests if existing else ())
    )
    fingerprint = payload.get("fingerprint") or (existing.fingerprint if existing else None)
    if repo_root is not None and Path(repo_root).is_dir():
        fingerprint, computed_manifests = compute_manifest_fingerprint(
            repo_root, root_path, workspace_root, manifests=manifests
        )
        manifests = tuple(computed_manifests)

    provenance = dict(existing.provenance if existing else {})
    if isinstance(payload.get("provenance"), Mapping):
        provenance.update(dict(payload["provenance"]))
    if not provenance.get("source"):
        provenance["source"] = "operator"

    confirm_flag = bool(payload.get("confirm", True))
    status = "confirmed" if confirm_flag else str(payload.get("status") or "proposed")
    entry = AppMapEntry(
        id=existing.id if existing else None,
        app_key=key,
        app_name=app_name,
        name=comp_name,
        root_path=root_path,
        workspace_root=workspace_root,
        stack=stack,
        commands=commands,
        run=run_spec,
        manifests=manifests,
        shared_paths=shared_paths,
        provenance=provenance,
        fingerprint=str(fingerprint) if fingerprint else None,
        baseline=dict(existing.baseline if existing else {}),
        status=status,
        binding_id=str(payload["binding_id"]) if payload.get("binding_id") else (existing.binding_id if existing else None),
        repository=str(payload["repository"]) if payload.get("repository") else (existing.repository if existing else None),
    )
    return upsert_app_map(
        db,
        project_id,
        entry,
        binding_id=entry.binding_id,
        actor=actor,
        confirm=confirm_flag,
    )


# ── Seeding & single canonical resolution ──────────────────────────────────────────────────────

def _prd_app_dir(prd_text: Any) -> Optional[str]:
    if not prd_text:
        return None
    from .verification_target import brief_app_dir

    return brief_app_dir(str(prd_text))


def seed_app_map(
    repo_root: Path | str,
    *,
    db: Any = None,
    project_id: Optional[str] = None,
    run: Optional[Mapping[str, Any]] = None,
    spec: Optional[Mapping[str, Any]] = None,
    finalize_ctx: Optional[Mapping[str, Any]] = None,
    prd_text: Optional[str] = None,
    app_key: Optional[str] = None,
    app_name: Optional[str] = None,
    binding_id: Optional[str] = None,
    changed: Optional[Iterable[str]] = None,
    lineage: Optional[Iterable[str]] = None,
    actor: str = "boss",
    persist: bool = True,
) -> AppMapResolution:
    """Seed or load the canonical AppMap for an app/run from:
    1. `.boss/apps.yaml` in the repo (always wins when matching app_key or single app)
    2. Stored `app_components` record or `finalize_ctx.app_map`
    3. `spec.app_root` / `official_app_id` / brief / PRD
    4. Scanner proposal on `repo_root` (never falls back to root when ambiguous!)

    When `persist=True` and `db`/`project_id` are supplied, stores the entry in `app_components`
    (if a binding exists) and `code_task_runs.finalize_ctx.app_map` (if `run` has an id).
    """
    from . import app_root as _app_root
    from .app_map_scanner import load_boss_apps_yaml, scan_repository
    from .verification_target import app_scope, official_app_id

    base = Path(repo_root)
    sp = dict(spec if spec is not None else ((run or {}).get("spec") or {}))
    fctx = dict(finalize_ctx if finalize_ctx is not None else ((run or {}).get("finalize_ctx") or {}))
    pid = project_id or (str(run["project_id"]) if run and run.get("project_id") else None)
    run_id = str(run["id"]) if run and run.get("id") else None
    repo_full = (
        f"{sp.get('owner')}/{sp.get('repo')}".strip("/")
        if sp.get("owner") and sp.get("repo")
        else None
    )

    derived_key = app_key
    if not derived_key and run:
        try:
            from .agent_assignment import app_key as _app_key
            from .naming import project_name

            derived_key = _app_key(project_name(str(run.get("title") or ""))[0])
        except Exception:  # noqa: BLE001
            derived_key = None
    derived_name = app_name or (str(run.get("title") or "") if run else "") or derived_key or "app"

    def _finish(entry: AppMapEntry, source: str, *, stale: bool = False) -> AppMapResolution:
        fp, mans = compute_manifest_fingerprint(
            base, entry.root_path, entry.workspace_root, manifests=entry.manifests
        )
        enriched = AppMapEntry(
            id=entry.id,
            app_key=entry.app_key or derived_key or (PurePosixPath(entry.root_path).name if entry.root_path != "." else "app"),
            app_name=entry.app_name or derived_name,
            name=entry.name or entry.app_name or derived_name,
            root_path=entry.root_path,
            workspace_root=entry.workspace_root,
            stack=entry.stack,
            commands=entry.commands,
            run=entry.run,
            manifests=tuple(mans) if mans else entry.manifests,
            shared_paths=entry.shared_paths,
            provenance=entry.provenance,
            fingerprint=entry.fingerprint or fp,
            baseline=entry.baseline,
            status="stale" if stale else entry.status,
            binding_id=binding_id or entry.binding_id,
            repository=repo_full or entry.repository,
            kind=entry.kind,
            access=entry.access,
            depends_on=entry.depends_on,
        )
        if persist and db is not None and pid:
            if run_id:
                try:
                    remember_run_app_map(db, run_id, enriched)
                except Exception:  # noqa: BLE001
                    pass
            try:
                bid = binding_id or enriched.binding_id or _default_binding_id(db, pid, repo_full)
                if bid:
                    enriched = upsert_app_map(
                        db, pid, enriched, binding_id=bid, actor=actor,
                        confirm=(enriched.status == "confirmed"),
                    )
            except Exception:  # noqa: BLE001
                pass
        return AppMapResolution(
            entry=enriched,
            path=enriched.resolved_dir(base),
            root_path=enriched.root_path,
            source=source,
            stale=stale,
        )

    # 1. `.boss/apps.yaml` in the repo ALWAYS wins when present
    declared_apps = load_boss_apps_yaml(base)
    if declared_apps:
        target_hint = _app_root.clean_rel(official_app_id(sp, fctx) or app_scope(sp, fctx))
        picked = None
        if derived_key:
            picked = next(
                (a for a in declared_apps if a.app_key.lower() == derived_key.lower()),
                None,
            )
        if picked is None and target_hint:
            picked = next((a for a in declared_apps if a.root_path == target_hint), None)
        if picked is None and len(declared_apps) == 1:
            picked = declared_apps[0]
        if picked is not None:
            return _finish(picked, "boss_apps_yaml")
        # Multiple apps declared in .boss/apps.yaml and none named -> refuse, never root!
        cands = tuple(a.as_dict() for a in declared_apps)
        names = ", ".join(f"{a.app_key} ({a.root_path})" for a in declared_apps[:6])
        return AppMapResolution(
            entry=None,
            path=None,
            root_path=None,
            source="refused",
            refusal=(
                f".boss/apps.yaml declares several apps ({names}) and this run did not name one. "
                "Select an app in the App Map card or specify its folder, then retry."
            ),
            candidates=cands,
        )

    # 2. Existing stored AppMap on the run or in app_components
    stored = entry_from_run_context(run, spec=sp, finalize_ctx=fctx)
    if stored is None and db is not None and pid:
        stored = load_app_map(db, pid, app_key=derived_key, repository=repo_full, binding_id=binding_id)
    if stored is not None:
        drift = detect_manifest_drift(stored, base, rescan=False)
        stale = bool(drift["drifted"])
        if stale and db is not None and pid:
            try:
                emit_event(
                    db, pid, "app_map.drift_detected", actor, "app_map", stored.app_key,
                    {
                        "root_path": stored.root_path,
                        "old_fingerprint": drift["old_fingerprint"],
                        "new_fingerprint": drift["new_fingerprint"],
                        "added_manifests": drift["added_manifests"],
                        "removed_manifests": drift["removed_manifests"],
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return _finish(stored, "stored_map", stale=stale)

    # 3. Seed from explicit spec.app_root / brief / PRD
    named = official_app_id(sp, fctx) or app_scope(sp, fctx) or _prd_app_dir(prd_text)
    named_clean = _app_root.clean_rel(named)
    if named_clean:
        scan = scan_repository(base)
        matched = next((a for a in scan.apps if a.root_path == named_clean), None)
        if matched is not None:
            prov = {**matched.provenance, "seeded_from": "spec_or_brief"}
            entry = AppMapEntry(
                app_key=derived_key or matched.app_key,
                app_name=derived_name or matched.app_name,
                name=matched.name,
                root_path=matched.root_path,
                workspace_root=matched.workspace_root,
                stack=matched.stack,
                commands=matched.commands,
                run=matched.run,
                manifests=matched.manifests,
                shared_paths=matched.shared_paths,
                provenance=prov,
                fingerprint=matched.fingerprint,
                status="proposed",
            )
            return _finish(entry, "named")
        fp, mans = compute_manifest_fingerprint(base, named_clean, ".")
        entry = AppMapEntry(
            app_key=derived_key or PurePosixPath(named_clean).name,
            app_name=derived_name,
            name=PurePosixPath(named_clean).name,
            root_path=named_clean,
            workspace_root=".",
            manifests=tuple(mans),
            provenance={"source": "brief", "confidence": "high", "evidence": [f"named:{named_clean}"]},
            fingerprint=fp,
            status="proposed",
        )
        return _finish(entry, "named")

    # 4. Run deterministic scanner (and changed/lineage hints if unambiguous)
    scan = scan_repository(base)
    if scan.selected is not None and len(scan.apps) == 1:
        sel = scan.selected
        entry = AppMapEntry(
            app_key=derived_key or sel.app_key,
            app_name=derived_name or sel.app_name,
            name=sel.name,
            root_path=sel.root_path,
            workspace_root=sel.workspace_root,
            stack=sel.stack,
            commands=sel.commands,
            run=sel.run,
            manifests=sel.manifests,
            shared_paths=sel.shared_paths,
            provenance=sel.provenance,
            fingerprint=sel.fingerprint,
            status="proposed",
        )
        return _finish(entry, "single_app" if sel.is_root() else "recorded")

    # Multiple candidates: check if changed/lineage unambiguously pinpoint ONE scanned app
    if scan.apps and (changed or lineage):
        where = _app_root.resolve(base, changed=changed, lineage=lineage)
        if where.path is not None:
            rel = "." if where.path == base else where.path.relative_to(base).as_posix()
            matched = next((a for a in scan.apps if a.root_path == rel), None)
            if matched is not None:
                entry = AppMapEntry(
                    app_key=derived_key or matched.app_key,
                    app_name=derived_name or matched.app_name,
                    name=matched.name,
                    root_path=matched.root_path,
                    workspace_root=matched.workspace_root,
                    stack=matched.stack,
                    commands=matched.commands,
                    run=matched.run,
                    manifests=matched.manifests,
                    shared_paths=matched.shared_paths,
                    provenance={**matched.provenance, "disambiguated_by": where.source},
                    fingerprint=matched.fingerprint,
                    status="proposed",
                )
                return _finish(entry, where.source)

    # Ambiguous or empty: REFUSE, NEVER fall back to repo root!
    cands = tuple(a.as_dict() for a in scan.apps)
    return AppMapResolution(
        entry=None,
        path=None,
        root_path=None,
        source="refused",
        refusal=scan.refusal or NO_MAP_REFUSAL,
        candidates=cands,
    )


def resolve_for_stage(
    repo_root: Path | str,
    *,
    app_map: Any = None,
    db: Any = None,
    project_id: Optional[str] = None,
    run: Optional[Mapping[str, Any]] = None,
    spec: Optional[Mapping[str, Any]] = None,
    finalize_ctx: Optional[Mapping[str, Any]] = None,
    app_id: Optional[str] = None,
    recorded: Optional[str] = None,
    changed: Optional[Iterable[str]] = None,
    lineage: Optional[Iterable[str]] = None,
    require_map: bool = False,
) -> AppMapResolution:
    """THE single resolver every pipeline stage calls to learn the app's directory and AppMap.

    Precedence:
      1. Explicit `app_map` argument (or embedded `finalize_ctx.app_map` / `spec.app_map`).
      2. Stored `app_components` AppMap row (when `db` and `project_id` are given).
      3. `.boss/apps.yaml` in `repo_root` (if it uniquely identifies the app).
      4. When `require_map=True`: REFUSE immediately (no map = refuse, never root).
      5. Otherwise delegate to `app_root.resolve(...)` (which itself refuses on multi-app repos
         with no evidence — never silent root fallback!).
    """
    from . import app_root as _app_root

    base = Path(repo_root)
    entry = coerce_entry(app_map) or entry_from_run_context(run, spec=spec, finalize_ctx=finalize_ctx)
    if entry is None and db is not None and project_id:
        sp = spec if spec is not None else ((run or {}).get("spec") or {})
        repo_full = (
            f"{sp.get('owner')}/{sp.get('repo')}".strip("/")
            if isinstance(sp, Mapping) and sp.get("owner") and sp.get("repo")
            else None
        )
        entry = load_app_map(db, project_id, app_key=app_id, repository=repo_full)

    if entry is not None:
        return AppMapResolution(
            entry=entry,
            path=entry.resolved_dir(base),
            root_path=entry.root_path,
            source="stored_map",
            stale=(entry.status == "stale"),
        )

    # Check `.boss/apps.yaml` on disk even without DB
    from .app_map_scanner import load_boss_apps_yaml

    declared = load_boss_apps_yaml(base)
    if declared:
        aid_clean = _app_root.clean_rel(app_id)
        picked = None
        if app_id:
            picked = next(
                (a for a in declared if a.app_key.lower() == str(app_id).lower() or a.root_path == aid_clean),
                None,
            )
        if picked is None and len(declared) == 1:
            picked = declared[0]
        if picked is not None:
            return AppMapResolution(
                entry=picked,
                path=picked.resolved_dir(base),
                root_path=picked.root_path,
                source="boss_apps_yaml",
            )
        if require_map or len(declared) > 1:
            return AppMapResolution(
                entry=None,
                path=None,
                root_path=None,
                source="refused",
                refusal=(
                    "`.boss/apps.yaml` declares multiple apps and none was selected for this stage. "
                    "Refusing to fall back to the repository root."
                ),
                candidates=tuple(a.as_dict() for a in declared),
            )

    if require_map:
        from .app_map_scanner import scan_repository

        scan = scan_repository(base)
        return AppMapResolution(
            entry=None,
            path=None,
            root_path=None,
            source="refused",
            refusal=NO_MAP_REFUSAL,
            candidates=tuple(a.as_dict() for a in scan.apps),
        )

    where = _app_root.resolve(
        base, app_id=app_id, recorded=recorded, changed=changed, lineage=lineage
    )
    if where.path is None:
        return AppMapResolution(
            entry=None,
            path=None,
            root_path=None,
            source="refused",
            refusal=where.refusal or NO_MAP_REFUSAL,
        )
    rel = "." if where.path == base else where.path.relative_to(base).as_posix()
    return AppMapResolution(
        entry=None,
        path=where.path,
        root_path=rel,
        source=where.source,
    )


# ── Baseline verification on base commit ───────────────────────────────────────────────────────

def _git_head_sha(repo_root: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except OSError:
        pass
    return None


def _default_baseline_runner(command: str, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    """Run a single baseline command without a shell (argv split) or sh -lc only when needed."""
    started = time.monotonic()
    try:
        argv = shlex.split(command)
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": proc.returncode,
            "ok": proc.returncode == 0,
            "duration_ms": duration_ms,
            "output_tail": combined[-2000:],
        }
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": None,
            "ok": False,
            "timed_out": True,
            "duration_ms": duration_ms,
            "output_tail": f"Timed out after {timeout_seconds}s: {exc}",
        }
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": 127,
            "ok": False,
            "duration_ms": duration_ms,
            "output_tail": f"{type(exc).__name__}: {exc}",
        }


def baseline_steps_for_entry(entry: AppMapEntry, repo_root: Path | str) -> list[tuple[str, str, Path]]:
    """Return ordered (step_name, command, cwd) tuples for baseline install + test verification."""
    base = Path(repo_root)
    app_dir = entry.resolved_dir(base)
    ws_dir = entry.resolved_workspace_dir(base)
    cmds = dict(entry.commands or {})
    steps: list[tuple[str, str, Path]] = []
    if cmds.get("install"):
        install_cwd = ws_dir if entry.workspace_root != entry.root_path and any(
            (ws_dir / f).is_file() for f in ("pnpm-workspace.yaml", "turbo.json", "nx.json")
        ) else app_dir
        steps.append(("install", str(cmds["install"]), install_cwd))
    if cmds.get("test"):
        steps.append(("test", str(cmds["test"]), app_dir))
    elif cmds.get("build"):
        steps.append(("build", str(cmds["build"]), app_dir))
    return steps


def verify_baseline(
    repo_root: Path | str,
    entry: AppMapEntry,
    *,
    db: Any = None,
    project_id: Optional[str] = None,
    commit_sha: Optional[str] = None,
    runner: Optional[Callable[[str, Path, int], Mapping[str, Any]]] = None,
    timeout_seconds: int = 300,
    actor: str = "boss",
) -> dict[str, Any]:
    """Run baseline install + test on the base commit and store the outcome on the AppMap."""
    base = Path(repo_root)
    app_dir = entry.resolved_dir(base)
    if not app_dir.is_dir():
        raise AppMapError(
            "app_dir_missing",
            f"App directory {entry.root_path!r} does not exist in the repository checkout.",
            status=422,
        )

    sha = commit_sha or _git_head_sha(base) or "working-tree"
    steps = baseline_steps_for_entry(entry, base)
    if not steps:
        raise AppMapError(
            "no_baseline_commands",
            f"App Map {entry.app_key!r} declares neither an install nor a test command to verify.",
            status=422,
        )

    execute = runner or _default_baseline_runner
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    all_ok = True
    first_error: Optional[str] = None

    for step_name, command, cwd in steps:
        res = dict(execute(command, cwd, timeout_seconds))
        ok = bool(res.get("ok", res.get("exit_code") == 0))
        step_rec = {
            "name": step_name,
            "command": command,
            "cwd": "." if cwd == base else cwd.relative_to(base).as_posix(),
            "exit_code": res.get("exit_code"),
            "ok": ok,
            "duration_ms": int(res.get("duration_ms") or 0),
            "output_tail": str(res.get("output_tail") or "")[-1500:],
        }
        results.append(step_rec)
        if not ok:
            all_ok = False
            first_error = f"{step_name} failed (exit {step_rec['exit_code']}): {step_rec['output_tail'][-300:]}"
            break

    total_ms = int((time.monotonic() - started) * 1000)
    fp, mans = compute_manifest_fingerprint(base, entry.root_path, entry.workspace_root, entry.manifests)
    baseline = {
        "status": "passed" if all_ok else "failed",
        "commit_sha": sha,
        "root_path": entry.root_path,
        "fingerprint": fp,
        "steps": results,
        "duration_ms": total_ms,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "error": first_error,
    }

    if db is not None and project_id:
        updated = AppMapEntry(
            id=entry.id,
            app_key=entry.app_key,
            app_name=entry.app_name,
            name=entry.name,
            root_path=entry.root_path,
            workspace_root=entry.workspace_root,
            stack=entry.stack,
            commands=entry.commands,
            run=entry.run,
            manifests=tuple(mans) if mans else entry.manifests,
            shared_paths=entry.shared_paths,
            provenance=entry.provenance,
            fingerprint=fp,
            baseline=baseline,
            status=entry.status,
            binding_id=entry.binding_id,
            repository=entry.repository,
        )
        upsert_app_map(
            db,
            project_id,
            updated,
            binding_id=updated.binding_id,
            actor=actor,
            confirm=(updated.status == "confirmed"),
        )
        emit_event(
            db,
            project_id,
            "app_map.baseline_verified" if all_ok else "app_map.baseline_failed",
            actor,
            "app_map",
            entry.app_key,
            {
                "root_path": entry.root_path,
                "commit_sha": sha,
                "status": baseline["status"],
                "duration_ms": total_ms,
                "steps": [{"name": s["name"], "ok": s["ok"], "exit_code": s["exit_code"]} for s in results],
            },
        )
    return baseline
