"""Deploy CONFIGURATION, not a built app (AMEX-G4, BOSSAPP-1140): `deploy_config` and
`promote_config`.

BOSS could deploy an app it had built — a commit to a host, through a ChangeSet, a person's
approval, a receipt and a rollback — and nothing else. The AMEX onboarding workflow ends by
putting ONE FILE in a directory: a validated `tenants/<tenant>.yaml` into the gateway's staging
config dir, the gateway hot-reloads, a health check passes, and the same bytes go to production
once a person agrees. This module is that primitive, built on the host deployment's governance
rather than beside it:

  target   a `deployment_targets` row of `target_kind: config_dir` — a directory on a host
           (`local:` the worker's own filesystem, or a verified Fleet host over SSH), an optional
           reload (a command run in the directory, or an HTTP hook) and a health URL.
  plan     `plan_config_deployment`: the files are stored as ONE artifact (a `config_bundle`:
           every byte, every sha256) and the ChangeSet carries their names and hashes; a zero
           budget reservation; a decision for a person — or, for an environment whose policy says
           so (deployment_environment_policies.requires_approval=false, set by a person), the
           policy's approval (deploy_policy.pre_approval), exactly as an app deploy — and a
           deployment run `awaiting_approval`. Nothing touches the directory.
  approve  the decisions API, a person (governed_host_deploy.on_decision_approved: it is the same
           `deploy_host` decision, keyed on the run), which queues `deployment.host_apply`.
  apply    governed_host_deploy.execute_host_job → apply_on_host → `apply_config` here: the
           previous version of every file is KEPT beside the directory, each file is written to
           a temp name and renamed into place (atomic), the reload runs, the health URL is probed.
           A failed reload or health check restores the kept versions and reloads again: the run
           says `rolled_back`, and what was serving before is serving after.
  promote  `promote_config` plans the SAME bundle artifact — the exact bytes staging passed on,
           by sha256 — to the production target, through the same gates (production's policy
           asks a person unless someone set it otherwise; dual control is honoured).

Everything a person approves names files by path and sha256; secrets never travel here (a
config file that is a secret belongs in the vault, not in a ChangeSet).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import posixpath
import secrets as _secrets
import shlex
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_blocking_decision, emit_event, make_id
from .local_fenced import file_bytes, safe_relative_path

log = logging.getLogger(__name__)

TARGET_KIND = "config_dir"
OPERATION_SKILL = "config_dir.files.deploy"
RESOURCE_TYPE = "config_dir.files"
PACKET_KIND = "config"
BUNDLE_ARTIFACT_TYPE = "config_bundle"
PRODUCED_ARTIFACT_TYPES = frozenset({"produced_file", BUNDLE_ARTIFACT_TYPE})
#: Where the previous versions live: beside the files, under the run that replaced them.
VERSIONS_DIR = ".boss-versions"
#: How many runs' previous versions are kept per directory.
KEEP_VERSIONS = 10
#: Files per deployment, and bytes per file. A tenant config is kilobytes.
MAX_FILES = 50
MAX_FILE_BYTES = 2_000_000
#: How long a health URL may take to answer well after a reload.
HEALTH_WAIT_SECONDS = 30
RELOAD_TIMEOUT_SECONDS = 120


class ConfigDeployError(ValueError):
    """A plan BOSS refuses before anything is recorded: headline + the next step."""

    def __init__(self, headline: str, action: str = ""):
        super().__init__(f"{headline} {action}".strip())
        self.headline = headline
        self.action = action


# ── The target ────────────────────────────────────────────────────────────────────────────────

@dataclass
class ConfigTarget:
    """A config_dir target, read."""

    id: str
    app_name: str
    environment: str
    directory: str
    transport: str                       # local | ssh
    host_id: Optional[str]
    reload_command: Optional[str]
    reload_url: Optional[str]
    health_url: Optional[str]
    health_expected: int = 200
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def deploy_roots(settings: Any) -> list[str]:
    """BOSS_CONFIG_DEPLOY_ROOTS: the only directory trees a config target may write under."""
    raw = str(getattr(settings, "config_deploy_roots", "") or "")
    roots = []
    for part in raw.replace("\n", ",").split(","):
        part = part.strip()
        if part.startswith("/"):
            roots.append(posixpath.normpath(part))
    return roots


def reload_commands(settings: Any) -> list[str]:
    """BOSS_CONFIG_RELOAD_COMMANDS: the exact reload commands a target may run (one per line,
    or a JSON list)."""
    raw = str(getattr(settings, "config_reload_commands", "") or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return [str(c).strip() for c in json.loads(raw) if str(c).strip()]
        except ValueError:
            return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def root_problem(settings: Any, directory: str) -> Optional[str]:
    """Why `directory` may not be written, or None: it must sit strictly under an allowed root,
    with no `..` (checked again on the machine against the resolved path, preflight)."""
    roots = deploy_roots(settings)
    if not roots:
        return ("no config deploy roots are configured (BOSS_CONFIG_DEPLOY_ROOTS), so BOSS writes "
                "configuration nowhere. Set it to the directory trees configuration may go to.")
    raw = str(directory or "")
    norm = posixpath.normpath(raw)
    if not raw.startswith("/") or ".." in raw.split("/") or norm in ("/",):
        return f"{raw!r} is not an absolute, normalised path."
    # A root as written and as this machine resolves it (/var → /private/var): the preflight
    # compares the directory's RESOLVED path, so a symlink cannot walk it out of a root.
    for root in {*roots, *(os.path.realpath(r) for r in roots)}:
        if norm != root and norm.startswith(root.rstrip("/") + "/"):
            return None
    return (f"{norm} is outside the allowed config deploy roots ({', '.join(roots)}). "
            "Choose a directory beneath one of them, or ask the operator to add a root.")


def reload_urls(settings: Any) -> list[str]:
    """BOSS_CONFIG_RELOAD_URLS: reload hooks allowed beyond loopback and the target's own host."""
    raw = str(getattr(settings, "config_reload_urls", "") or "")
    return [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]


_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def reload_problem(settings: Any, command: Optional[str], url: Optional[str] = None,
                   host_address: Optional[str] = None) -> Optional[str]:
    if command and command.strip() not in reload_commands(settings):
        return (f"the reload command {command.strip()[:80]!r} is not in the operator's allowlist "
                "(BOSS_CONFIG_RELOAD_COMMANDS). Use a listed command, a reload_url, or none.")
    if url:
        from urllib.parse import urlsplit

        host = (urlsplit(str(url)).hostname or "").lower()
        allowed = str(url).strip() in reload_urls(settings) or host in _LOOPBACK or (
            host_address and host == str(host_address).strip().lower())
        if not allowed:
            return (f"the reload URL {str(url)[:80]!r} is neither loopback, the target's own host, "
                    "nor in the operator's allowlist (BOSS_CONFIG_RELOAD_URLS).")
    return None


def _host_address(connection: Connection, host_id: Optional[str]) -> Optional[str]:
    if not host_id:
        return None
    from .deployment_fleet import get_host

    host = get_host(connection, host_id)
    return str(host.get("address") or "") if host else None


def versions_root(directory: str) -> str:
    """Where previous versions live: BESIDE the watched directory, never in it (a gateway that
    loads every *.yaml under its directory must not load last week's tenant file)."""
    norm = posixpath.normpath(directory)
    return f"{posixpath.dirname(norm)}/{VERSIONS_DIR}/{posixpath.basename(norm)}"


def is_config_target(target: Any) -> bool:
    kind = target.get("target_kind") if hasattr(target, "get") else None
    return str(kind or "").strip().lower() == TARGET_KIND


def is_config_run(row: Any) -> bool:
    packet = (row.get("release_packet") if hasattr(row, "get") else None) or {}
    if isinstance(packet, str):
        try:
            packet = json.loads(packet or "{}")
        except ValueError:
            return False
    return isinstance(packet, dict) and packet.get("kind") == PACKET_KIND


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return fallback if value is None else value


def _spec_of(target: Mapping[str, Any]) -> dict[str, Any]:
    for res in _loads(target.get("required_resources"), []) or []:
        if isinstance(res, dict) and res.get("kind") == TARGET_KIND:
            return res
    return {}


def read_target(target: Mapping[str, Any]) -> ConfigTarget:
    if not is_config_target(target):
        raise ConfigDeployError(
            f"{target.get('app_name') or 'This target'} is a {target.get('target_kind')} target, "
            "not a config directory.",
            "deploy_config ships files to a config_dir target; create one with "
            "create_config_target.")
    spec = _spec_of(target)
    directory = str(spec.get("directory") or "").strip()
    ref = str(target.get("target_ref") or "")
    transport = str(spec.get("transport") or ("local" if ref.startswith("local:") else "ssh"))
    if not directory:
        directory = ref.split(":", 1)[1] if ref.startswith("local:") else ref.rsplit(":", 1)[-1]
    if not directory.startswith("/"):
        raise ConfigDeployError(f"{target.get('app_name')}'s config directory must be an absolute "
                                f"path (got {directory!r}).", "Recreate the target with one.")
    checks = _loads(target.get("health_checks"), []) or []
    health = next((c for c in checks if isinstance(c, dict) and c.get("url")), None)
    return ConfigTarget(
        id=str(target["id"]), app_name=str(target.get("app_name") or ""),
        environment=str(target.get("environment") or ""), directory=directory,
        transport=transport, host_id=(str(target["host_id"]) if target.get("host_id") else None),
        reload_command=(str(spec["reload_command"]).strip() or None) if spec.get("reload_command") else None,
        reload_url=(str(spec["reload_url"]).strip() or None) if spec.get("reload_url") else None,
        health_url=(str(health["url"]).strip() if health else None),
        health_expected=int((health or {}).get("expected_status") or 200),
        raw=dict(target))


def create_config_target(connection: Connection, settings: Settings, *, project_id: str,
                         app_name: str, environment: str, directory: str, actor: str,
                         host_id: Optional[str] = None, reload_command: Optional[str] = None,
                         reload_url: Optional[str] = None, health_url: Optional[str] = None,
                         health_expected_status: int = 200) -> dict[str, Any]:
    """A config_dir deployment target: the directory, how it is reloaded, how it is checked.

    `host_id` names a verified Fleet host (files travel over SSH, under the host's pinned key);
    without one the directory is on the worker's own filesystem (`local:`), which is what a demo
    gateway in the same compose project, or a laptop, needs. One target per app and environment,
    like any other (the row's unique key)."""
    from .resource_plane import ResourcePlaneService

    app = str(app_name or "").strip()
    env = str(environment or "staging").strip().lower() or "staging"
    directory = str(directory or "").strip().rstrip("/") or ""
    if not app:
        raise ConfigDeployError("app_name is required.", "Name the system this configuration belongs to.")
    if not directory.startswith("/"):
        raise ConfigDeployError("directory must be an absolute path on the target machine.",
                                "For example /srv/northgate/staging/config/tenants.")
    if not health_url:
        raise ConfigDeployError(f"{app} needs a health URL to check after a reload.",
                                "Give the gateway's /readyz (or equivalent) for this environment.")
    for url, what in ((health_url, "health_url"), (reload_url, "reload_url")):
        if url and not str(url).startswith(("http://", "https://")):
            raise ConfigDeployError(f"{what} must be an http(s) URL.", f"Got {str(url)[:80]!r}.")
    directory = posixpath.normpath(directory)
    problem = root_problem(settings, directory) or reload_problem(
        settings, reload_command, reload_url, _host_address(connection, host_id))
    if problem:
        raise ConfigDeployError(problem)
    if reload_command or reload_url:
        from .agent_identity import is_person

        if not is_person(actor, connection, project_id):
            raise ConfigDeployError(
                "A reload runs something on the target after every deploy, so a person sets it.",
                "Ask a person to create this target, or create it without a reload.")
    transport = "ssh" if host_id else "local"
    if host_id:
        from .deployment_fleet import get_host, host_usable_by

        host = get_host(connection, host_id)
        if not host or not host_usable_by(connection, host_id, project_id):
            raise ConfigDeployError("That deployment host is not one this project may use.",
                                    "Pick a host from Infra › Fleet, or leave host_id empty for "
                                    "a directory on this machine.")
        if host.get("status") != "verified":
            raise ConfigDeployError(f"Host {host.get('name')} is {host.get('status')}, not verified.",
                                    "In Infra, add the host's public key to the VM and press Verify.")
        ref = f"{host.get('ssh_user') or 'root'}@{host['address']}:{int(host.get('ssh_port') or 22)}:{directory}"
    else:
        ref = f"local:{directory}"
    rp = ResourcePlaneService(settings)
    row = rp.create_deployment_target(
        connection, project_id=project_id, app_name=app, repo=f"config:{app}", environment=env,
        provider_account_id=None, target_kind=TARGET_KIND, target_ref=ref,
        domains=[str(health_url)],
        required_resources=[{"kind": TARGET_KIND, "directory": directory, "transport": transport,
                             "reload_command": (reload_command or "").strip() or None,
                             "reload_url": (reload_url or "").strip() or None}],
        secret_refs=[],
        health_checks=[{"url": str(health_url), "expected_status": int(health_expected_status or 200)}],
        config_target=True,
        rollback_points=[{"step": f"Restore the previous file versions kept under "
                                  f"{versions_root(directory)}/<run> and reload."}],
        actor=actor)
    connection.execute(
        text("UPDATE deployment_targets SET host_id = :h, status = 'active', updated_at = now() "
             "WHERE id = :i"), {"h": host_id, "i": row["id"]})
    return {"ok": True, "target_id": row["id"], "app_name": app, "environment": env,
            "target_kind": TARGET_KIND, "directory": directory, "transport": transport,
            "health_url": health_url, "reload_command": reload_command, "reload_url": reload_url}


def find_target(connection: Connection, project_id: str, *, target_id: Optional[str] = None,
                app_name: Optional[str] = None, environment: Optional[str] = None) -> dict[str, Any]:
    """The config_dir target named by id, or by app and environment."""
    if target_id:
        row = connection.execute(text("SELECT * FROM deployment_targets WHERE id = :i AND project_id = :p"),
                                 {"i": target_id, "p": project_id}).mappings().first()
        if not row:
            raise ConfigDeployError("No such deployment target in this project.",
                                    "list_deployments shows the targets that exist.")
        return dict(row)
    env = str(environment or "").strip().lower()
    app = str(app_name or "").strip()
    if not app or not env:
        raise ConfigDeployError("Name the target: target_id, or app_name and environment.",
                                "create_config_target makes one.")
    rows = connection.execute(
        text("SELECT * FROM deployment_targets WHERE project_id = :p AND target_kind = :k "
             "AND lower(environment) = :e AND status <> 'retired' ORDER BY created_at"),
        {"p": project_id, "k": TARGET_KIND, "e": env}).mappings().all()
    for row in rows:
        if str(row["app_name"]).strip().lower() == app.lower():
            return dict(row)
    have = sorted({f"{r['app_name']} ({r['environment']})" for r in connection.execute(
        text("SELECT app_name, environment FROM deployment_targets WHERE project_id = :p "
             "AND target_kind = :k AND status <> 'retired'"), {"p": project_id, "k": TARGET_KIND}).mappings()})
    listed = ("Config targets here: " + ", ".join(have) + ". ") if have else ""
    raise ConfigDeployError(f"{app} has no {env} config target.",
                            listed + "create_config_target makes one.")


# ── The bundle: what is deployed, byte for byte ───────────────────────────────────────────────

def _artifact_bytes(connection: Connection, project_id: str, artifact_id: str) -> tuple[bytes, str]:
    row = connection.execute(
        text("SELECT artifact_type, content FROM artifacts WHERE id = :i AND project_id = :p"),
        {"i": artifact_id, "p": project_id}).mappings().first()
    if not row:
        raise ConfigDeployError(f"No artifact {artifact_id!r} in this project.",
                                "A produced file is named by the step that produced it "
                                "(produced_artifacts).")
    content = _loads(row["content"], {}) or {}
    if str(row["artifact_type"]) not in PRODUCED_ARTIFACT_TYPES:
        raise ConfigDeployError(f"Artifact {artifact_id!r} is a {row['artifact_type']}, not a produced file.",
                                "Only files a step produced (or a config bundle) can be deployed.")
    if content.get("base64") is not None:
        data = base64.b64decode(str(content["base64"]))
    elif content.get("text") is not None:
        data = str(content["text"]).encode("utf-8")
    else:
        raise ConfigDeployError(f"Artifact {artifact_id!r} holds no file content.", "")
    return data, str(content.get("path") or content.get("name") or "")


def bundle_from_inputs(connection: Connection, project_id: str, *,
                       files: Optional[Mapping[str, Any]] = None,
                       produced_artifacts: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """The files to deploy: `files` maps a destination path (relative to the target directory)
    to its content — a string, {text|content|base64}, an artifact id (`art_…`) or
    {"artifact_id"}; `produced_artifacts` (a sandboxed_command step's output, name → artifact id)
    deploys each produced file at the path it was produced under."""
    out: dict[str, dict[str, Any]] = {}

    def add(dest: str, data: bytes) -> None:
        try:
            rel = safe_relative_path(dest)
        except ValueError as exc:
            raise ConfigDeployError(f"files[{dest!r}]: {exc}", "Name each file by its path inside the "
                                    "target directory.") from exc
        if rel.startswith(VERSIONS_DIR):
            raise ConfigDeployError(f"{rel!r} is where BOSS keeps previous versions.", "Pick another name.")
        if len(data) > MAX_FILE_BYTES:
            raise ConfigDeployError(f"{rel} is {len(data)} bytes; a config file is capped at "
                                    f"{MAX_FILE_BYTES}.", "")
        out[rel] = {"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "base64": base64.b64encode(data).decode("ascii")}

    for dest, source in (files or {}).items():
        if isinstance(source, str) and source.startswith("art_"):
            data, _path = _artifact_bytes(connection, project_id, source)
        elif isinstance(source, Mapping) and source.get("artifact_id"):
            data, _path = _artifact_bytes(connection, project_id, str(source["artifact_id"]))
        else:
            try:
                data = file_bytes(source)
            except ValueError as exc:
                raise ConfigDeployError(f"files[{dest!r}]: {exc}", "") from exc
        add(str(dest), data)
    for name, artifact_id in (produced_artifacts or {}).items():
        if not artifact_id:
            continue
        data, path = _artifact_bytes(connection, project_id, str(artifact_id))
        add(path or str(name), data)
    if not out:
        raise ConfigDeployError("Nothing to deploy: no files were given.",
                                "Pass files (path → content or artifact id) or a step's produced_artifacts.")
    if len(out) > MAX_FILES:
        raise ConfigDeployError(f"{len(out)} files; a config deployment is capped at {MAX_FILES}.", "")
    return [out[k] for k in sorted(out)]


def files_from_build(connection: Connection, settings: Any, project_id: str, run_id: str,
                     source_paths: Any) -> tuple[dict[str, str], dict[str, Any]]:
    """The files a BUILD shipped, read from its branch (BOSSAPP-1377): what a custom workflow's
    `config_deploy` step gets when it names the preceding build step's code_task_run_id, the way a
    catalogue deploy names its build. `source_paths` is a list of repo paths (a path ending in
    `/` is a folder, whose files land directly in the target directory) or {destination: repo path}. Returns ({destination: text}, source) where
    source names the run, its branch, the head SHA the files were read at and its pull request,
    so a config deployment is traceable to the change it ships, never the default branch."""
    from .code_task_service import work_branch_for

    run = connection.execute(
        text("SELECT id, project_id, status, spec, finalize_ctx, pull_request_url "
             "FROM code_task_runs WHERE id = :i AND project_id = :p"),
        {"i": run_id, "p": project_id}).mappings().first()
    if not run:
        raise ConfigDeployError(f"No build {run_id!r} in this project.",
                                "Name the build step's code_task_run_id.")
    if str(run["status"]) not in ("completed", "succeeded", "passed"):
        raise ConfigDeployError(f"Build {run_id} is {run['status']!r}, not a successful build.",
                                "Config is deployed from a build that finished.")
    spec = _loads(run["spec"], {}) or {}
    owner, repo = str(spec.get("owner") or ""), str(spec.get("repo") or "")
    if not (owner and repo):
        raise ConfigDeployError(f"Build {run_id} names no repository.", "")
    row = dict(run)
    row["finalize_ctx"] = _loads(run["finalize_ctx"], {}) or {}
    branch = work_branch_for(row)
    from .local_repo import is_local_owner

    if is_local_owner(owner):
        from .local_repo_host import adapter_for

        adapter = adapter_for(settings, owner, repo)
    else:
        from .github_actions import adapter_for_repository

        adapter = adapter_for_repository(connection, settings, project_id, f"{owner}/{repo}").adapter
    snap = adapter.branch_snapshot(branch)
    head = str(snap.get("sha") or snap.get("commit_sha") or "")
    if not head:
        raise ConfigDeployError(f"The branch {branch} of build {run_id} is gone.",
                                "Config is deployed from the build's branch.")
    wanted: dict[str, str] = {}
    if isinstance(source_paths, Mapping):
        wanted = {str(d): str(src) for d, src in source_paths.items()}
    else:
        listing = None
        for entry in (source_paths if isinstance(source_paths, (list, tuple)) else [source_paths]):
            entry = str(entry or "").strip()
            if not entry:
                continue
            if entry.endswith("/"):
                listing = listing if listing is not None else adapter.list_paths(branch)
                for path in listing:
                    if path.startswith(entry):
                        # a folder's files land directly in the target directory
                        wanted[path[len(entry):]] = path
            else:
                wanted[entry] = entry
    if not wanted:
        raise ConfigDeployError("Nothing to deploy from the build: no source_paths matched.",
                                "Name the files (or folders ending in /) the build wrote.")
    out: dict[str, str] = {}
    for dest, src in wanted.items():
        body = adapter.read_text_file(branch, src)
        if body is None:
            raise ConfigDeployError(f"{src} is not on {branch} (build {run_id}).",
                                    "Name a file the build committed.")
        out[dest] = body
    return out, {"code_task_run_id": run_id, "branch": branch, "head_sha": head,
                 "pull_request_url": run.get("pull_request_url"), "paths": sorted(wanted.values())}


def bundle_sha256(files: list[Mapping[str, Any]]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda f: str(f["path"])):
        h.update(f"{f['path']}\0{f['sha256']}\n".encode("utf-8"))
    return h.hexdigest()


def store_bundle(connection: Connection, settings: Settings, *, project_id: str,
                 files: list[dict[str, Any]], title: str,
                 created_by_job_id: Optional[str] = None) -> str:
    from .core import create_artifact

    return create_artifact(
        connection, settings, project_id, None, BUNDLE_ARTIFACT_TYPE, title,
        {"kind": PACKET_KIND, "bundle_sha256": bundle_sha256(files), "files": files},
        created_by_job_id=created_by_job_id)


def load_bundle(connection: Connection, project_id: str, artifact_id: str) -> list[dict[str, Any]]:
    row = connection.execute(
        text("SELECT content FROM artifacts WHERE id = :i AND project_id = :p AND artifact_type = :t"),
        {"i": artifact_id, "p": project_id, "t": BUNDLE_ARTIFACT_TYPE}).mappings().first()
    if not row:
        raise ConfigDeployError(f"The config bundle {artifact_id!r} is gone.", "Deploy the files again.")
    files = (_loads(row["content"], {}) or {}).get("files") or []
    for f in files:
        data = base64.b64decode(str(f.get("base64") or ""))
        if hashlib.sha256(data).hexdigest() != f.get("sha256"):
            raise ConfigDeployError(f"The stored bytes of {f.get('path')} no longer match their hash.",
                                    "Deploy the files again.")
    return files


def _manifest(files: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Names and hashes only: what a person approves and what the ChangeSet carries."""
    return [{"path": f["path"], "sha256": f["sha256"], "size": f["size"]} for f in files]


# ── Plan ──────────────────────────────────────────────────────────────────────────────────────

def _policy_problem(connection: Connection, project_id: str, target: ConfigTarget) -> Optional[dict[str, str]]:
    row = connection.execute(
        text("SELECT allowed_target_kinds, requires_health_check, status FROM "
             "deployment_environment_policies WHERE project_id = :p AND lower(environment) = :e"),
        {"p": project_id, "e": target.environment.lower()}).mappings().first()
    if not row or row["status"] != "active":
        return None
    kinds = _loads(row["allowed_target_kinds"], []) or []
    if kinds and TARGET_KIND not in kinds:
        return {"headline": f"The {target.environment} policy does not allow config_dir targets.",
                "action": "Add config_dir to the environment's allowed target kinds in Infra › Deploys › Rules.",
                "reason": "target_kind_not_allowed_by_environment_policy"}
    if row["requires_health_check"] and not target.health_url:
        return {"headline": f"{target.app_name} has no health URL and the {target.environment} policy requires one.",
                "action": "Recreate the target with a health URL.",
                "reason": "health_check_required_by_environment_policy"}
    return None


def plan_problem(connection: Connection, project_id: str, target: ConfigTarget,
                 settings: Any = None) -> Optional[dict[str, str]]:
    if settings is not None:
        why = root_problem(settings, target.directory) or reload_problem(
            settings, target.reload_command, target.reload_url,
            _host_address(connection, target.host_id))
        if why:
            return {"headline": why, "action": "Recreate the target within the operator's rules.",
                    "reason": "config_target_not_allowed"}
    if str(target.raw.get("status") or "active") == "retired":
        return {"headline": f"{target.app_name}'s {target.environment} config target is retired.",
                "action": "Create a new target.", "reason": "target_retired"}
    if target.transport == "ssh":
        from .deployment_fleet import get_host, host_usable_by

        host = get_host(connection, target.host_id) if target.host_id else None
        if not host or not host_usable_by(connection, target.host_id, project_id):
            return {"headline": f"{target.app_name} is not attached to a deployment host.",
                    "action": "Attach the target to a verified host in Infra, then plan again.",
                    "reason": "host_not_attached"}
        if host.get("status") != "verified":
            return {"headline": f"Host {host.get('name')} is {host.get('status')}, not verified.",
                    "action": "In Infra, add the host's public key to the VM and press Verify.",
                    "reason": "host_not_verified"}
    if not target.health_url:
        return {"headline": f"{target.app_name} declares no health URL.",
                "action": "Recreate the target with one; a config deploy is verified by it.",
                "reason": "health_check_missing"}
    return _policy_problem(connection, project_id, target)


def plan_config_deployment(rp: Any, connection: Connection, *, project_id: str, target: dict[str, Any],
                           files: list[dict[str, Any]], bundle_artifact_id: str, actor: str,
                           approval_decision_id: Optional[str] = None, person_required: bool = False,
                           promoted_from: Optional[str] = None, trigger: str = "manual",
                           workflow_run_id: Optional[str] = None,
                           source: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Record a deployment of `files` into `target`, held for a person's approval — or running
    under the approval it already has (the released gate, or the environment's policy)."""
    from datetime import timedelta

    from . import governed_host_deploy as governed
    from .assurance import AssuranceService
    from .budget import BudgetGovernor
    from .core import now_utc
    from .deploy_policy import pre_approval, record_pre_approval
    from .resource_plane import _SURFACE_LEASE_SECONDS

    cfg = read_target(target)
    from ..identity.object_rules import is_production_environment

    if is_production_environment(cfg.environment):
        # PRODUCTION IS A PERSON'S ANSWER (review of #119): no environment policy and no gate a
        # step passes in approves configuration for production; a person approves this run.
        person_required, approval_decision_id = True, None
    problem = plan_problem(connection, project_id, cfg, rp.settings)
    if problem:
        return governed.refuse_plan(connection, project_id=project_id, target=target, actor=actor,
                                    headline=problem["headline"], action=problem["action"],
                                    reason=problem["reason"])
    manifest = _manifest(files)
    release = bundle_sha256(files)
    existing = governed.live_run(connection, cfg.id, release)
    if existing:
        return {**rp._get_deployment_run(connection, project_id, existing), "reused": True}
    governed.supersede_replaced(connection, rp.settings, cfg.id, release, actor=actor)
    lease = AssuranceService(rp.settings).acquire_surface(
        connection, project_id=project_id, surface_key=f"deployment_target:{cfg.id}",
        environment=cfg.environment, owner_ref=actor,
        coordination_route=f"deployment-target:{cfg.id}",
        reason="Deployment planning requires one fenced surface owner.",
        dedupe_key=f"deployment-surface:{cfg.id}:{actor}", actor=actor,
        expires_at=now_utc() + timedelta(seconds=_SURFACE_LEASE_SECONDS),
        stale_after=timedelta(seconds=_SURFACE_LEASE_SECONDS))
    if not lease["allowed"]:
        return governed.refuse_plan(
            connection, project_id=project_id, target=target, actor=actor,
            headline=f"{lease['ownership']['owner_ref']} is deploying {cfg.app_name} right now.",
            action="Wait for that deployment to finish, then plan again.",
            reason="surface_ownership_conflict")
    guard = lease.get("ownership") or {}
    ownership_guard = {"ownership_id": guard.get("id"), "owner_ref": guard.get("owner_ref"),
                       "version": guard.get("version")}

    host = governed._host_for(connection, target) or {}
    where = (str(host.get("name") or host.get("address") or "") if cfg.transport == "ssh"
             else "this machine")
    previous = governed.previous_success(connection, cfg.id)
    deployment_run_id = make_id("deprun")
    risk_tier = 3 if cfg.environment == "production" else 2
    names = ", ".join(f["path"] for f in manifest[:5]) + (" …" if len(manifest) > 5 else "")

    reservation = BudgetGovernor(rp.settings).reserve(
        connection, project_id, "provider", 0,
        f"Config deployment {deployment_run_id}: {cfg.app_name} {names} to {where}",
        expires_in_minutes=7 * 24 * 60, risk_tier=risk_tier)
    if reservation.get("status") == "denied" or not reservation.get("reservation_id"):
        governed.release_guard(connection, ownership_guard)
        raise ConfigDeployError("The budget refused to record this deployment's reservation.",
                                str(reservation.get("message") or "Review the budget decision in Approvals."))
    reservation_id = reservation["reservation_id"]
    reload = {"command": cfg.reload_command, "url": cfg.reload_url}
    desired = {
        "deployment_run_id": deployment_run_id, "deployment_target_id": cfg.id,
        "target_kind": TARGET_KIND, "environment": cfg.environment, "app_name": cfg.app_name,
        "directory": cfg.directory, "transport": cfg.transport, "host_id": cfg.host_id,
        "host": where, "files": manifest, "bundle_sha256": release,
        "bundle_artifact_id": bundle_artifact_id, "reload": reload,
        "health": {"url": cfg.health_url, "expected_status": cfg.health_expected},
        "previous_versions": f"{versions_root(cfg.directory)}/{deployment_run_id}",
        "rollback_to": ({"deployment_run_id": previous["id"], "bundle_sha256": previous.get("commit_ref")}
                        if previous else None),
        "promoted_from": promoted_from, "surface_ownership": ownership_guard,
    }
    rollback_plan = [f"Restore the previous version of each file from {desired['previous_versions']}",
                     "Run the reload again", "Check the health URL"]
    changeset = rp.create_changeset(
        connection, project_id=project_id, provider_account_id=None,
        title=f"Deploy config for {cfg.app_name} to {where} · {cfg.environment}",
        summary=(f"Write {len(manifest)} file(s) ({names}) into {cfg.directory} atomically, keep "
                 f"the previous versions, reload, and check {cfg.health_url}; roll back on a "
                 "failed check."),
        operations=[{"skill_id": OPERATION_SKILL, "action": "deploy", "resource_type": RESOURCE_TYPE,
                     "resource_ref": f"{cfg.id}:{cfg.app_name}", "desired_state": desired,
                     "risk_tier": 1, "production_affecting": cfg.environment == "production"}],
        expected_cost_usd_micros=0, reservation_id=reservation_id, rollback_plan=rollback_plan,
        risk_tier=1, approval_required=False, actor=actor)
    review = {
        "app_name": cfg.app_name, "environment": cfg.environment, "host": where,
        "directory": cfg.directory, "files": manifest, "bundle_sha256": release,
        "reload": reload, "health_url": cfg.health_url, "promoted_from": promoted_from,
        **({"source": source} if source else {}),
        "steps": ["Check the directory (read-only)", "Keep the previous versions",
                  "Write each file to a temp name and rename it into place", "Reload",
                  "Health check; roll back on failure"],
        "rollback": rollback_plan,
        "cost": {"expected_cost_usd_micros": 0, "reservation_id": reservation_id,
                 "note": "Your own host: no provider cost."},
    }
    question = (f"{'Promote' if promoted_from else 'Deploy'} config for {cfg.app_name} to {where} "
                f"· {cfg.environment}")
    recommendation = {**review, "action": governed.DECISION_ACTION,
                      "deployment_run_id": deployment_run_id, "changeset_id": changeset["id"],
                      "target_id": cfg.id, "kind": PACKET_KIND}
    cost = {"expected_cost_usd_micros": 0, "reservation_id": reservation_id,
            "cost_confidence": "no_provider_spend"}
    approval = pre_approval(connection, project_id, cfg.environment,
                            gate_decision_id=approval_decision_id, person_required=person_required)
    if approval and rp.required_approvers_for_changeset(connection, changeset["id"]):
        approval = None      # dual control is two people's answer; a policy cannot give it
    if approval:
        decision_id = approval["decision_id"]
        review["approval"] = {k: approval[k] for k in ("source", "decision_id", "approved_by")}
    else:
        decision_id = create_blocking_decision(
            connection, project_id, None, question, recommendation, cost, governed.REASON_CODE,
            risk_tier=risk_tier, default_action="hold")
    connection.execute(
        text("UPDATE changesets SET status = 'pending_approval', risk_tier = :r, "
             "approval_decision_id = :d, updated_at = now() WHERE id = :c"),
        {"d": decision_id, "c": changeset["id"], "r": risk_tier})
    connection.execute(text("UPDATE changeset_operations SET risk_tier = :r WHERE changeset_id = :c"),
                       {"c": changeset["id"], "r": risk_tier})
    if not approval:
        connection.execute(text("UPDATE decisions SET external_ref = :c WHERE id = :d"),
                           {"c": changeset["id"], "d": decision_id})
    review = {**review, "decision_id": decision_id, "changeset_id": changeset["id"]}
    packet = {"kind": PACKET_KIND, "source": trigger, "requested_by": actor,
              "bundle_artifact_id": bundle_artifact_id, "bundle_sha256": release, "files": manifest,
              "directory": cfg.directory, "transport": cfg.transport, "reload": reload,
              "health": {"url": cfg.health_url, "expected_status": cfg.health_expected},
              "promoted_from": promoted_from, "workflow_run_id": workflow_run_id,
              **({"source": source} if source else {}),
              "surface_ownership": ownership_guard, "governed": True, "plan_review": review}
    connection.execute(
        text("""
            INSERT INTO deployment_runs (
                id, project_id, deployment_target_id, changeset_id, decision_id,
                release_artifact_id, app_name, environment, repo, target_kind, target_ref,
                commit_ref, status, release_packet, rollback_plan, release_receipt_ids,
                created_by, started_at, reservation_id, trigger
            ) VALUES (
                :id, :p, :t, :cs, :d, :ra, :app, :env, :repo, :kind, :ref, :commit,
                'awaiting_approval', CAST(:packet AS JSONB), CAST(:rb AS JSONB),
                CAST(:receipts AS JSONB), :actor, now(), :res, :trigger
            )"""),
        {"id": deployment_run_id, "p": project_id, "t": cfg.id, "cs": changeset["id"],
         "d": decision_id, "ra": bundle_artifact_id, "app": cfg.app_name, "env": cfg.environment,
         "repo": target.get("repo") or f"config:{cfg.app_name}", "kind": TARGET_KIND,
         "ref": target.get("target_ref") or "", "commit": release,
         "packet": json.dumps(packet), "rb": json.dumps(rollback_plan),
         "receipts": json.dumps(changeset["expected_receipts"]), "actor": actor,
         "res": reservation_id, "trigger": "promotion" if promoted_from else trigger})
    emit_event(connection, project_id, "deployment_run.planned", actor, "deployment_run",
               deployment_run_id,
               {"deployment_target_id": cfg.id, "changeset_id": changeset["id"],
                "decision_id": decision_id, "environment": cfg.environment, "governed": True,
                "kind": PACKET_KIND, "files": [f["path"] for f in manifest],
                "bundle_sha256": release, "promoted_from": promoted_from})
    if approval:
        record_pre_approval(connection, project_id, approval, entity_type="deployment_run",
                            entity_id=deployment_run_id, actor=actor)
        governed.approve_and_queue(connection, rp.settings, run_id=deployment_run_id,
                                   approver=approval["approved_by"], decision_id=decision_id)
    return rp._get_deployment_run(connection, project_id, deployment_run_id)


# ── The tools' entry points ───────────────────────────────────────────────────────────────────

def _planned(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("refused"):
        return {"status": "blocked", "reason": run.get("failure_reason"), "next": run.get("next"),
                "reason_code": run.get("reason_code"), "deployment_target_id": run.get("deployment_target_id")}
    packet = _loads(run.get("release_packet"), {}) or {}
    review = packet.get("plan_review") or {}
    run_id = str(run.get("id") or run.get("deployment_run_id") or "")
    return {"status": "planned", "deployment_run_id": run_id, "deployment_id": run_id,
            "decision_id": run.get("decision_id"), "changeset_id": run.get("changeset_id"),
            "run_status": run.get("status"), "environment": run.get("environment"),
            "bundle_sha256": packet.get("bundle_sha256"), "files": packet.get("files"),
            "approval": review.get("approval"), "reused": bool(run.get("reused")),
            "reason": ("Running under an approval it already has." if review.get("approval")
                       else "Waiting for a person's approval in Approvals.")}


def deploy_files(engine: Any, settings: Settings, *, project_id: str, actor: str,
                 files: Optional[Mapping[str, Any]] = None,
                 produced_artifacts: Optional[Mapping[str, Any]] = None,
                 target_id: Optional[str] = None, app_name: Optional[str] = None,
                 environment: Optional[str] = None, approval_decision_id: Optional[str] = None,
                 person_required: bool = False, workflow_run_id: Optional[str] = None,
                 created_by_job_id: Optional[str] = None,
                 code_task_run_id: Optional[str] = None,
                 source_paths: Any = None) -> dict[str, Any]:
    """`deploy_config`: plan a deployment of these files to a config_dir target. With
    `code_task_run_id` the files are read from THAT BUILD'S branch (`source_paths`), at its head
    commit, and the plan records the run, branch, SHA and pull request."""
    from ..db import begin
    from .resource_plane import ResourcePlaneService

    rp = ResourcePlaneService(settings)
    try:
        with begin(engine) as c:
            target = find_target(c, project_id, target_id=target_id, app_name=app_name,
                                 environment=environment or ("staging" if not target_id else None))
            cfg = read_target(target)
            source = None
            if code_task_run_id:
                if not source_paths:
                    raise ConfigDeployError(
                        "A config deployment from a build needs source_paths: the files the "
                        "build wrote.", "Name them, e.g. source_paths: [\"tenants/\"].")
                built, source = files_from_build(c, settings, project_id, str(code_task_run_id),
                                                 source_paths)
                files = {**dict(files or {}), **built}
            bundle = bundle_from_inputs(c, project_id, files=files, produced_artifacts=produced_artifacts)
            artifact_id = store_bundle(
                c, settings, project_id=project_id, files=bundle,
                title=f"config bundle for {cfg.app_name} · {cfg.environment}",
                created_by_job_id=created_by_job_id)
            run = plan_config_deployment(
                rp, c, project_id=project_id, target=target, files=bundle,
                bundle_artifact_id=artifact_id, actor=actor,
                approval_decision_id=approval_decision_id, person_required=person_required,
                trigger="manual", workflow_run_id=workflow_run_id, source=source)
            planned = _planned(run)
            return {**planned, "source": source} if source else planned
    except ConfigDeployError as exc:
        return {"status": "failed", "reason": str(exc), "headline": exc.headline, "next": exc.action}


def promote(engine: Any, settings: Settings, *, project_id: str, actor: str,
            deployment_run_id: Optional[str] = None, app_name: Optional[str] = None,
            from_environment: str = "staging", environment: str = "production",
            target_id: Optional[str] = None, approval_decision_id: Optional[str] = None,
            person_required: bool = False, workflow_run_id: Optional[str] = None,
            expected_bundle_sha256: Optional[str] = None) -> dict[str, Any]:
    """`promote_config`: the exact bundle a SUCCEEDED config deployment shipped (named by its
    id — never "the latest for the app") planned to the next environment's config target.

    INSIDE A WORKFLOW RUN (`workflow_run_id`, set by the runner) the source is never guessed
    (AMEX-G9, BOSSAPP-1167): the id AND the bundle hash its staging step recorded are
    required, and a source that shipped other bytes is refused. "The latest staging deploy for
    the app" is how two tenants in flight promoted each other's file."""
    from ..db import begin
    from .resource_plane import ResourcePlaneService

    rp = ResourcePlaneService(settings)
    try:
        with begin(engine) as c:
            if workflow_run_id and not (deployment_run_id and expected_bundle_sha256):
                raise ConfigDeployError(
                    "A workflow promotes only the deployment its own staging step shipped.",
                    "Bind deployment_id and bundle_sha256 from this run's config_deploy step.")
            # NAMED, NEVER GUESSED (review of #125): "the latest succeeded deployment for the
            # app" is how two tenants in flight promoted each other's file, from a chat or an
            # MCP call as much as from a workflow. The caller names the deployment run.
            if not deployment_run_id:
                raise ConfigDeployError(
                    "Name the deployment to promote (its deployment_run_id).",
                    "list_deployments shows the succeeded config deployments.")
            src = c.execute(text("SELECT * FROM deployment_runs WHERE id = :i AND project_id = :p"),
                            {"i": deployment_run_id, "p": project_id}).mappings().first()
            if not src or not is_config_run(src):
                raise ConfigDeployError("No config deployment to promote.",
                                        f"Deploy the files to {from_environment} first and let it succeed.")
            if src["status"] != "succeeded":
                raise ConfigDeployError(
                    f"The {src['environment']} deployment is {src['status']}, not succeeded.",
                    "Only configuration that passed its health check in the lower environment is promoted.")
            packet = _loads(src["release_packet"], {}) or {}
            if workflow_run_id and str(packet.get("workflow_run_id") or "") != workflow_run_id:
                raise ConfigDeployError(
                    f"Deployment {src['id']} was not planned by this workflow run.",
                    "A workflow promotes only the deployment its own staging step shipped.")
            artifact_id = str(packet.get("bundle_artifact_id") or src.get("release_artifact_id") or "")
            bundle = load_bundle(c, project_id, artifact_id)
            if bundle_sha256(bundle) != str(src.get("commit_ref") or ""):
                raise ConfigDeployError(
                    "The stored bundle is not the one that deployment shipped.",
                    "Deploy the files to the lower environment again, then promote.")
            expected = str(expected_bundle_sha256 or "").strip()
            if expected and expected != str(src.get("commit_ref") or ""):
                raise ConfigDeployError(
                    f"Deployment {src['id']} shipped a different bundle "
                    f"({str(src.get('commit_ref') or '')[:12]}…) than the one expected "
                    f"({expected[:12]}…); nothing was promoted.",
                    "Promote the deployment this run's own staging step shipped.")
            target = find_target(c, project_id, target_id=target_id,
                                 app_name=app_name or src["app_name"], environment=environment)
            if str(target["id"]) == str(src["deployment_target_id"]):
                raise ConfigDeployError("That is the target the files are already on.",
                                        "Promote to the next environment's target.")
            from ..identity.object_rules import is_production_environment

            # PRODUCTION IS A PERSON'S ANSWER: no environment policy and no gate passed in by a
            # step answers a promotion to production; a person approves this very deployment.
            to_production = is_production_environment(str(target.get("environment") or ""))
            run = plan_config_deployment(
                rp, c, project_id=project_id, target=target, files=bundle,
                bundle_artifact_id=artifact_id, actor=actor,
                approval_decision_id=None if to_production else approval_decision_id,
                person_required=person_required or to_production,
                promoted_from=str(src["id"]), trigger="promotion", workflow_run_id=workflow_run_id)
            return {**_planned(run), "promoted_from": str(src["id"])}
    except ConfigDeployError as exc:
        return {"status": "failed", "reason": str(exc), "headline": exc.headline, "next": exc.action}


# ── Apply: the directory, through one shell, locally or over SSH ──────────────────────────────

@dataclass
class ShellResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class LocalShell:
    """`/bin/sh` on the worker's own filesystem, as BOSS's own uid (it is BOSS's write, under an
    approved ChangeSet — not a run's workload, so never the sandbox uid)."""

    def __init__(self, directory: str):
        self.directory = directory

    def run(self, script: str, *, stdin: Optional[str] = None, timeout: int = 60,
            write: bool = False) -> ShellResult:
        from . import owned_process

        try:
            proc = owned_process.run(["/bin/sh", "-c", script], input=(stdin or "").encode("utf-8") if stdin is not None else None,
                                     capture_output=True, timeout=timeout, sandboxed=False,
                                     cwd="/", env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                   "LANG": "C.UTF-8"})
        except subprocess.TimeoutExpired:
            return ShellResult(124, "", f"timed out after {timeout}s")
        out = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
        err = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
        return ShellResult(int(proc.returncode), out, err)


class SshShell:
    """The same scripts, over SSH to a verified Fleet host under its pinned key; every write
    carries the ChangeSet it applies (ssh_host_adapter.stack_exec)."""

    def __init__(self, access: Any, changeset_id: str):
        self.access = access
        self.changeset_id = changeset_id

    def run(self, script: str, *, stdin: Optional[str] = None, timeout: int = 60,
            write: bool = False) -> ShellResult:
        result = self.access.run(script, timeout=timeout, write=write,
                                 changeset_id=self.changeset_id if write else None,
                                 stdin_text=stdin)
        return ShellResult(int(result.code), str(result.stdout or ""), str(result.stderr or ""))


def _q(value: str) -> str:
    return shlex.quote(value)


def preflight_script(directory: str) -> str:
    d = _q(directory)
    return (f'D={d}; [ -d "$D" ] || {{ echo "BOSS_NO_DIR $D"; exit 3; }}; '
            f'[ -w "$D" ] || {{ echo "BOSS_NOT_WRITABLE $D"; exit 4; }}; '
            f'command -v base64 >/dev/null 2>&1 || {{ echo BOSS_NO_BASE64; exit 5; }}; '
            f'echo "BOSS_REAL $(cd "$D" && pwd -P)"; echo BOSS_DIR_OK')


def keep_script(directory: str, versions: str, paths: list[str]) -> str:
    lines = [f'set -eu; D={_q(directory)}; V={_q(versions)}; umask 077; mkdir -p "$V"']
    for p in paths:
        f = _q(p)
        lines.append(f'if [ -f "$D"/{f} ]; then mkdir -p "$V"/"$(dirname {f})"; cp -p "$D"/{f} "$V"/{f}; '
                     f'echo "BOSS_KEPT {p}"; else echo "BOSS_ABSENT {p}"; fi')
    return "; ".join(lines)


def write_script(directory: str, path: str, token: str) -> str:
    f = _q(path)
    return (f'set -eu; D={_q(directory)}; umask 022; T="$D/.boss-tmp.{token}"; '
            f'mkdir -p "$D"/"$(dirname {f})"; base64 -d > "$T"; (sync "$T" 2>/dev/null || sync 2>/dev/null || true); '
            f'mv -f "$T" "$D"/{f}; echo "BOSS_WRITTEN {path}"')


def restore_script(directory: str, versions: str, kept: list[str], absent: list[str]) -> str:
    lines = [f'set -eu; D={_q(directory)}; V={_q(versions)}']
    for p in kept:
        f = _q(p)
        lines.append(f'if [ -f "$V"/{f} ]; then mv -f "$V"/{f} "$D"/{f}; echo "BOSS_RESTORED {p}"; '
                     f'else echo "BOSS_NOT_KEPT {p}"; fi')
    for p in absent:
        lines.append(f'rm -f "$D"/{_q(p)}; echo "BOSS_REMOVED {p}"')
    return "; ".join(lines)


def prune_script(directory: str, keep: int = KEEP_VERSIONS) -> str:
    return (f'D={_q(versions_root(directory))}; [ -d "$D" ] || exit 0; '
            f'ls -1dt "$D"/*/ 2>/dev/null | tail -n +{keep + 1} | while read -r old; do rm -rf "$old"; done; true')


def reload_script(directory: str, command: str) -> str:
    return f'cd {_q(directory)} && ( {command} )'


def probe(url: str, *, expected: int = 200, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — the target's own URL
            ok = resp.status == expected or (expected == 200 and 200 <= resp.status < 300)
            return ok, f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def wait_healthy(url: str, *, expected: int = 200, wait_seconds: float = HEALTH_WAIT_SECONDS,
                 probe_fn: Any = None) -> dict[str, Any]:
    probe_fn = probe_fn or probe
    deadline = time.monotonic() + wait_seconds
    attempts = 0
    while True:
        attempts += 1
        ok, said = probe_fn(url, expected=expected)
        if ok or time.monotonic() >= deadline:
            return {"healthy": ok, "url": url, "expected": expected, "said": said,
                    "attempts": attempts, "checks": [{"url": url, "status": said, "expected": expected,
                                                      **({} if ok else {"error": said})}]}
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))


def post_hook(url: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def shell_for(connection: Connection, settings: Settings, cfg: ConfigTarget, *, changeset_id: str) -> Any:
    if cfg.transport == "ssh":
        from .deployment_fleet import get_host
        from .stack_deploy import host_access_for

        host = get_host(connection, cfg.host_id) if cfg.host_id else None
        if not host:
            raise ConfigDeployError(f"{cfg.app_name}'s host is gone.", "Attach the target to a host.")
        return SshShell(host_access_for(connection, settings, host), changeset_id)
    return LocalShell(cfg.directory)


def apply_config(engine: Any, settings: Settings, *, row: dict, governance: dict,
                 shell: Any = None, probe_fn: Any = None) -> dict[str, Any]:
    """Phase two of a config deployment, under the approved ChangeSet: keep, write, reload,
    check; restore on failure. Every step recorded on the run."""
    from ..db import begin
    from . import governed_host_deploy as governed
    from .deployment_steps import StepRecorder

    run_id = row["id"]
    with begin(engine) as c:
        governed.require_governed_host_write(c, governance)
        trow = c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                         {"t": row["deployment_target_id"]}).mappings().first()
        if not trow:
            return {"status": "failed", "reason": "the deployment target no longer exists"}
        cfg = read_target(dict(trow))
        refused = root_problem(settings, cfg.directory) or reload_problem(
            settings, cfg.reload_command, cfg.reload_url, _host_address(c, cfg.host_id))
        if refused:
            return {"status": "blocked", "reason": refused}
        packet = _loads(row.get("release_packet"), {}) or {}
        project_id = row.get("project_id") or trow["project_id"]
        try:
            files = load_bundle(c, project_id, str(packet.get("bundle_artifact_id") or row.get("release_artifact_id") or ""))
            sh = shell or shell_for(c, settings, cfg, changeset_id=str(row["changeset_id"]))
        except ConfigDeployError as exc:
            return {"status": "blocked", "reason": str(exc)}
    versions = f"{versions_root(cfg.directory)}/{run_id}"
    paths = [str(f["path"]) for f in files]
    rec = StepRecorder(engine, run_id)
    kept: list[str] = []
    absent: list[str] = []
    written: list[str] = []
    health: dict[str, Any] = {}

    def say(handle: Any, value: Any) -> None:
        handle.log((value if isinstance(value, str) else json.dumps(value, default=str))[:2000])

    def reload_once(handle: Any) -> Optional[str]:
        """Run the reload (command, then HTTP hook); the reason it failed, or None."""
        if cfg.reload_command:
            done = sh.run(reload_script(cfg.directory, cfg.reload_command), timeout=RELOAD_TIMEOUT_SECONDS,
                          write=True)
            say(handle, f"$ {cfg.reload_command}\n{done.stdout[-1200:]}{done.stderr[-600:]}")
            if not done.ok:
                return f"the reload command exited {done.code}: {(done.stderr or done.stdout).strip()[-300:]}"
        if cfg.reload_url:
            ok, said = post_hook(cfg.reload_url)
            say(handle, f"POST {cfg.reload_url} → {said}")
            if not ok:
                return f"the reload hook {cfg.reload_url} answered {said}"
        if not cfg.reload_command and not cfg.reload_url:
            say(handle, "no reload declared: the target watches its directory")
        return None

    def restore(handle: Any, why: str) -> dict[str, Any]:
        back = sh.run(restore_script(cfg.directory, versions, kept, absent), timeout=60, write=True)
        say(handle, f"{why}\n{back.stdout[-1500:]}{back.stderr[-400:]}")
        again = reload_once(handle)
        restored = back.ok and "BOSS_NOT_KEPT" not in back.stdout
        if not restored:
            handle.failed("the previous versions could not all be restored: check the directory")
        elif again:
            handle.failed(f"restored, but {again}")
        return {"status": "rolled_back" if restored else "failed", "restored": restored,
                "reload_after_restore": again}

    try:
        with rec.step("resolve") as h:
            say(h, f"target={cfg.app_name} env={cfg.environment}\ndirectory={cfg.directory} "
                   f"transport={cfg.transport}\nfiles={', '.join(paths)}\n"
                   f"bundle={packet.get('bundle_sha256')}\nchangeset={row['changeset_id']}")
        with rec.step("preflight") as h:
            pre = sh.run(preflight_script(cfg.directory), timeout=30)
            say(h, (pre.stdout + pre.stderr).strip())
            if not pre.ok:
                reason = {3: "the directory does not exist", 4: "the directory is not writable",
                          5: "the host has no base64"}.get(pre.code, (pre.stderr or pre.stdout).strip()[:200])
                h.failed(f"{cfg.directory}: {reason}")
            if pre.ok:
                real = next((line[len("BOSS_REAL "):].strip() for line in pre.stdout.splitlines()
                             if line.startswith("BOSS_REAL ")), "")
                outside = root_problem(settings, real) if real else "the directory could not be resolved"
                if outside:
                    reason = f"resolved to {real or '?'}: {outside}"
                    h.failed(reason)
                    pre = ShellResult(6, pre.stdout, reason)
        if not pre.ok:
            rec.finish()
            return {"status": "blocked", "reason": f"{cfg.directory}: {reason}"}
        with rec.step("keep", label="Keep the previous versions") as h:
            kept_out = sh.run(keep_script(cfg.directory, versions, paths), timeout=60, write=True)
            say(h, (kept_out.stdout + kept_out.stderr).strip())
            if not kept_out.ok:
                h.failed(f"could not keep the previous versions: {(kept_out.stderr or kept_out.stdout).strip()[-300:]}")
            for line in kept_out.stdout.splitlines():
                if line.startswith("BOSS_KEPT "):
                    kept.append(line[len("BOSS_KEPT "):])
                elif line.startswith("BOSS_ABSENT "):
                    absent.append(line[len("BOSS_ABSENT "):])
        if not kept_out.ok:
            rec.finish()
            return {"status": "failed", "reason": "the previous versions could not be kept; nothing was written"}
        with rec.step("transfer", label="Write the files") as h:
            problem = None
            for f in files:
                token = _secrets.token_hex(6)
                done = sh.run(write_script(cfg.directory, str(f["path"]), token), stdin=str(f["base64"]),
                              timeout=60, write=True)
                say(h, f"{f['path']} sha256={f['sha256']} → {(done.stdout + done.stderr).strip()[-200:]}")
                if not done.ok:
                    problem = f"writing {f['path']} failed: {(done.stderr or done.stdout).strip()[-300:]}"
                    h.failed(problem)
                    break
                written.append(str(f["path"]))
        if problem:
            with rec.step("rollback", label="Roll back") as h:
                outcome = restore(h, problem)
            rec.finish()
            return {**outcome, "reason": f"{problem}; the previous versions were "
                                         f"{'restored' if outcome['restored'] else 'NOT all restored'}."}
        with rec.step("reload", label="Reload") as h:
            failed_reload = reload_once(h)
            if failed_reload:
                h.failed(failed_reload)
        if failed_reload:
            with rec.step("rollback", label="Roll back") as h:
                outcome = restore(h, failed_reload)
            rec.finish()
            return {**outcome, "reason": f"{failed_reload}; BOSS restored the previous versions and reloaded."}
        with rec.step("observe", label="Health check") as h:
            health = wait_healthy(str(cfg.health_url), expected=cfg.health_expected, probe_fn=probe_fn)
            say(h, health)
            if not health["healthy"]:
                h.failed(f"{cfg.health_url} answered {health['said']}, expected {cfg.health_expected}")
        if not health["healthy"]:
            with rec.step("rollback", label="Roll back") as h:
                outcome = restore(h, f"health check failed ({health['said']})")
            rec.finish()
            return {**outcome, "health": health,
                    "reason": (f"The health check failed after the reload ({health['said']}); BOSS restored "
                               f"the previous versions" + (" and reloaded." if outcome["restored"] else
                                                           ", but not all of them: check the directory."))}
        with rec.step("prune", label="Tidy old versions") as h:
            pruned = sh.run(prune_script(cfg.directory), timeout=30, write=True)
            say(h, f"kept the last {KEEP_VERSIONS} runs' versions under {versions_root(cfg.directory)}"
                   + ("" if pruned.ok else f" (prune said: {pruned.stderr.strip()[-200:]})"))
        rec.finish()
        where = cfg.raw.get("target_ref") or cfg.directory
        return {"status": "succeeded", "health": health, "url": cfg.health_url,
                "applied": {"operation": "config_deployed", "resource_type": RESOURCE_TYPE,
                            "provider_ref": f"{cfg.transport}://{where}", "app": cfg.app_name,
                            "host": where, "deployed_sha": packet.get("bundle_sha256"),
                            "files": paths, "previous_versions": versions,
                            "rollback_point": {"versions_dir": versions, "kept": kept, "absent": absent}}}
    except Exception as exc:  # noqa: BLE001 — a sentence, never a stack trace
        log.warning("config deploy %s failed", run_id, exc_info=True)
        rec.finish()
        said = str(exc).strip() or "The config deploy stopped unexpectedly."
        return {"status": "failed", "reason": (said[:1].upper() + said[1:])[:400]}
