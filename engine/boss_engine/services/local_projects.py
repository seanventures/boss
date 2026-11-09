"""LOCAL PROJECTS in the database (BOSSAPP-918/923/924): binding a project to a repository BOSS
keeps on this machine, importing code that already exists, the onboarding agent's machine
checks, and the Delete-project hook.

A local repository is bound exactly like a GitHub one — a `github_repository_bindings` row, role
primary, now with `host = 'local'` — under a per-project "local" connection that carries no
credential. So every reader of the registry (resolve_repository_target, the build, the preview,
the deploy) finds it the way it finds a GitHub repository, and the adapter it gets
(`local_repo_host.LocalGitAdapter`) speaks the same interface.
"""
from __future__ import annotations

import io
import json
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

from . import local_repo
from .core import emit_event, make_id



class LocalProjectError(ValueError):
    """A refusal in words, with the HTTP status a route answers with."""

    def __init__(self, message: str, status: int = 422, error: str = "local_project_refused"):
        super().__init__(message)
        self.status = status
        self.error = error

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": str(self)}


# ── the binding ──────────────────────────────────────────────────────────────────────────────

def bind(c: Any, project_id: str, described: Mapping[str, Any], *, actor: str,
         role: str = "primary", origin: str = "new") -> dict[str, Any]:
    """Record the local repository as the project's (primary) repository: host = local, no
    connection (nothing to authenticate to)."""
    full_name = str(described["full_name"])
    if role == "primary":
        # One primary per project: a local repository made for a project replaces none silently.
        other = c.execute(text(
            "SELECT full_name FROM github_repository_bindings WHERE project_id = :p "
            "AND role = 'primary' AND status = 'active' AND full_name <> :f"),
            {"p": project_id, "f": full_name}).scalar()
        if other:
            role = "source"
    existing = c.execute(text(
        "SELECT id FROM github_repository_bindings WHERE project_id = :p AND full_name = :f"),
        {"p": project_id, "f": full_name}).scalar()
    meta = {"host": "local", "path": described.get("path"), "origin": origin}
    if existing:
        c.execute(text(
            "UPDATE github_repository_bindings SET host = 'local', status = 'active', "
            "connection_id = NULL, default_branch = :d, metadata = metadata || CAST(:m AS JSONB), "
            "updated_at = now() WHERE id = :id"),
            {"d": described.get("default_branch") or "main", "m": json.dumps(meta),
             "id": existing})
        binding_id = str(existing)
    else:
        binding_id = make_id("ghrepo")
        c.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, default_branch, visibility, host, metadata, checks, created_by) "
            "VALUES (:id, :p, NULL, :o, :n, :f, :r, :d, 'private', 'local', CAST(:m AS JSONB), "
            "CAST(:ck AS JSONB), :a)"),
            {"id": binding_id, "p": project_id, "o": described["owner"],
             "n": described["repo"], "f": full_name, "r": role,
             "d": described.get("default_branch") or "main", "m": json.dumps(meta),
             "ck": json.dumps({k: {"ok": True, "detail": "local"} for k in ("read", "write", "pull_requests")}),
             "a": actor})
    emit_event(c, project_id, "project.local_repo_bound", actor, "project", project_id,
               {"repository": full_name, "binding_id": binding_id, "origin": origin})
    return {"binding_id": binding_id, "host": "local",
            "role": role, **{k: described.get(k) for k in ("owner", "repo", "full_name",
                                                            "default_branch", "head_sha")}}


def local_binding(c: Any, project_id: str, *, include_mirror: bool = False
                  ) -> Optional[dict[str, Any]]:
    """The project's active local repository binding (primary first), or None. A local
    repository kept as a mirror after Publish is the project's only with `include_mirror`."""
    row = c.execute(text(
        "SELECT id, owner, name, full_name, role, default_branch, metadata FROM "
        "github_repository_bindings WHERE project_id = :p AND host = 'local' AND status = 'active' "
        "AND (:m OR role <> 'mirror') "
        "ORDER BY (role = 'primary') DESC, created_at LIMIT 1"),
        {"p": project_id, "m": bool(include_mirror)}).mappings().first()
    return dict(row) if row else None


def create_for_project(c: Any, settings: Any, project_id: str, *, name: str, actor: str,
                       title: str = "", description: str = "") -> dict[str, Any]:
    """Code location "local": the bare repository with its first commit, bound to the project."""
    _require_git()
    described = local_repo.create(project_id, name, settings=settings, title=title or name,
                                  description=description)
    return bind(c, project_id, described, actor=actor, origin="new")


def status(c: Any, settings: Any, project_id: str) -> dict[str, Any]:
    """What the console shows about a project's local repository."""
    bound = local_binding(c, project_id)
    if bound is None:
        return {"project_id": project_id, "local": False}
    out: dict[str, Any] = {"project_id": project_id, "local": True, "binding_id": bound["id"],
                           "full_name": bound["full_name"], "role": bound["role"],
                           "exists": local_repo.exists(bound["owner"], bound["name"], settings)}
    if out["exists"]:
        described = local_repo.describe(bound["owner"], bound["name"], settings)
        out.update({k: described[k] for k in ("default_branch", "head_sha", "branches")})
        out["published"] = (described.get("marker") or {}).get("published")
    open_sets = c.execute(text(
        "SELECT count(*) FROM local_change_sets WHERE project_id = :p AND repository = :r "
        "AND state = 'open'"), {"p": project_id, "r": bound["full_name"]}).scalar()
    out["open_change_sets"] = int(open_sets or 0)
    return out


# ── Delete project (feat/px-delete wires this in) ────────────────────────────────────────────

def delete_local_repo(project_id: str, settings: Any = None) -> bool:
    """THE DELETE-PROJECT HOOK: remove every bare repository BOSS keeps for this project
    (`<repo root>/<project_id>/`). Idempotent; True when something was removed. GitHub mirrors
    are not the project's and are kept. Call it after the project's rows are gone."""
    return local_repo.delete_project_repos(project_id, settings)


# ── importing code that already exists (BOSSAPP-923) ─────────────────────────────────────────

def import_root(settings: Any) -> Optional[Path]:
    configured = str(getattr(settings, "local_import_root", "") or "").strip()
    return Path(configured) if configured else None


def resolve_import_path(settings: Any, path: Any) -> Path:
    """A folder on this machine, ONLY inside the install's import root. Never an arbitrary path:
    no `..`, no symlink out of the root."""
    allowed = import_root(settings)
    if allowed is None:
        raise LocalProjectError(
            "Importing a folder from this machine is off on this install. Put the folder under "
            "the import directory (BOSS_LOCAL_IMPORT_ROOT) or import it from a verified server.",
            403, "import_root_not_configured")
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        raise LocalProjectError("Give the folder to import.")
    candidate = Path(raw) if raw.startswith("/") else allowed / raw
    if ".." in candidate.parts:
        raise LocalProjectError("A folder path cannot contain '..'.")
    try:
        resolved = candidate.resolve(strict=True)
        base = allowed.resolve(strict=True)
    except (OSError, RuntimeError):
        raise LocalProjectError(f"There is no folder {raw} in the import directory.", 404,
                                "folder_not_found") from None
    if resolved != base and base not in resolved.parents:
        raise LocalProjectError(f"{raw} is outside the import directory ({allowed}).", 403,
                                "outside_import_root")
    if not resolved.is_dir():
        raise LocalProjectError(f"{raw} is not a folder.")
    return resolved


def folder_facts(source: Path) -> dict[str, Any]:
    """Has history? A GitHub remote? (The choice "keep it on GitHub or bring it local" needs it.)"""
    from .project_experience.host_folder import _GITHUB, _strip_userinfo

    facts: dict[str, Any] = {"path": str(source), "git": False, "remote": None, "github": None}
    if (source / ".git").exists():
        head = local_repo.git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=source, check=False)
        facts["git"] = bool(head.strip())
        remote = local_repo.git(["remote", "get-url", "origin"], cwd=source, check=False).strip()
        if remote:
            facts["remote"] = _strip_userinfo(remote)
            found = _GITHUB.search(facts["remote"])
            facts["github"] = f"{found.group(1)}/{found.group(2)}" if found else None
    return facts


TarFetcher = Callable[[Mapping[str, Any], str], bytes]


def _ssh_tar(settings: Any) -> TarFetcher:
    """The folder on a verified server as a tar stream, over ONE ssh session with the host's own
    registered key (read-only: `tar -c` of the folder, dependencies left out)."""
    def fetch(host: Mapping[str, Any], path: str) -> bytes:
        from ..db import get_engine
        from .deploy_service import _discard_key_file, _materialise_key, _ssh_key_file
        from .host_key_pin import ssh_options
        from .secret_backends import resolve_target_secret

        probe = {"secret_refs": ([{"name": "ssh_key", "secret_ref_id": host["key_secret_ref"]}]
                                 if host.get("key_secret_ref") else []), "host_id": host["id"]}
        with get_engine(settings).begin() as c:
            material = resolve_target_secret(c, probe, "ssh_key", settings)
        key = _materialise_key(material) if material else _ssh_key_file(settings)
        excludes = " ".join(f"--exclude={shlex.quote(e.rstrip('/'))}" for e in
                            ("node_modules", ".venv", "__pycache__", "dist", ".next", "target"))
        command = f"tar -C {shlex.quote(path)} {excludes} -czf - ."
        try:
            done = subprocess.run(
                ["ssh", "-i", key, *ssh_options(), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                 "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-n",
                 "-p", str(int(host.get("ssh_port") or 22)),
                 f"{host.get('ssh_user') or 'root'}@{host['address']}", command],
                capture_output=True, timeout=900)
        finally:
            if material:
                _discard_key_file(key)
        if done.returncode != 0:
            raise LocalProjectError(f"{host.get('name') or 'The server'} did not send the folder: "
                                    f"{(done.stderr or b'').decode('utf-8', 'replace').strip()[-200:]}",
                                    502, "host_read_failed")
        return done.stdout
    return fetch


def _extract(data: bytes, into: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts or member.issym() or member.islnk():
                if member.issym() or member.islnk():
                    continue
                raise LocalProjectError("The folder's archive names a path outside the folder.")
            if not (member.isfile() or member.isdir()):
                continue
            tar.extract(member, into)


def import_code(c: Any, settings: Any, project_id: str, *, name: str, actor: str,
                path: Optional[str] = None, host_id: Optional[str] = None,
                keep_history: bool = True, fetch_tar: Optional[TarFetcher] = None) -> dict[str, Any]:
    """Import a folder — on this machine (inside the import root) or on a verified server — as
    the project's local repository. Its git history is kept when it has one (every branch and
    tag); otherwise its files become one initial commit."""
    _require_git()
    if host_id:
        from .project_experience import host_folder

        try:
            clean = host_folder.clean_path(path)
            host = host_folder._host(c, project_id, str(host_id))  # noqa: SLF001
        except host_folder.FolderError as error:
            raise LocalProjectError(str(error), error.status) from None
        data = (fetch_tar or _ssh_tar(settings))(host, clean)
        tmp = Path(tempfile.mkdtemp(prefix="boss-host-import-"))
        try:
            _extract(data, tmp)
            facts = folder_facts(tmp)
            described = local_repo.import_tree(project_id, name, tmp, settings=settings,
                                               keep_history=keep_history, origin="host")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        facts.update({"host_id": host["id"], "host": host["name"], "path": clean})
    else:
        source = resolve_import_path(settings, path)
        facts = folder_facts(source)
        try:
            described = local_repo.import_tree(project_id, name, source, settings=settings,
                                               keep_history=keep_history, origin="folder")
        except local_repo.LocalRepoError as error:
            raise LocalProjectError(str(error), 409) from None
    bound = bind(c, project_id, described, actor=actor, origin="import")
    history = (described.get("marker") or {}).get("history")
    emit_event(c, project_id, "project.local_code_imported", actor, "project", project_id,
               {"repository": described["full_name"], "history": history,
                "github_remote": facts.get("github"), "from_host": facts.get("host")})
    return {**bound, "history": history, "facts": facts,
            "branches": described.get("branches") or []}


# ── the onboarding agent's machine checks (BOSSAPP-924, backend) ──────────────────────────────

def _disk_free_gb(path: Path) -> Optional[float]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(str(probe))
    except OSError:
        return None
    return round(usage.free / (1024 ** 3), 1)


def _online(timeout: float = 3.0) -> bool:
    import socket

    for host in ("api.github.com", "github.com"):
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _model_is_local(c: Any, settings: Any, project_id: str) -> tuple[Optional[bool], str]:
    """Is the model this project builds with on this machine (or reachable without the internet)?"""
    try:
        from .model_router.stages import lane_engines

        _s, build_engine, _r = lane_engines(c, settings, project_id, {})
    except Exception:  # noqa: BLE001 — unknown is an answer
        build_engine = str(getattr(settings, "code_agent_engine", "") or "")
    engine = str(build_engine or "").lower()
    local_words = ("ollama", "localhost", "127.0.0.1", "host.docker.internal", "lmstudio", "vllm")
    if any(w in engine for w in local_words):
        return True, engine
    # A direct-provider engine is local when ITS endpoint is on this machine.
    base = str(getattr(settings, "openai_base_url", "") or "").lower()
    if engine in ("native", "openai", "openai_compatible") and any(w in base for w in local_words):
        return True, engine
    if not engine:
        return None, ""
    return False, engine


def machine_checks(c: Any, settings: Any, project_id: str, *, fix: bool = True, actor: str = "agent:onboarding",
                   online: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """The onboarding agent's first stage for a local project: git, the local repository, disk,
    the network and the model. It fixes what it can (makes the repository and its first commit)
    and says the rest in words — notably an offline machine whose model is remote, which cannot
    build until a local engine is configured."""
    checks: list[dict[str, Any]] = []
    fixed: list[str] = []

    version = local_repo.git_available()
    checks.append({"key": "git", "ok": bool(version), "label": "git",
                   "words": version or "git is not installed on this machine; install git, then "
                                       "run onboarding again."})

    binding = local_binding(c, project_id)
    repo_ok = False
    repo_words = "This project's code is not local."
    if binding is not None and version:
        owner, name = binding["owner"], binding["name"]
        path = local_repo.bare_path(owner, name, settings)
        title = c.execute(text("SELECT name FROM projects WHERE id = :p"), {"p": project_id}).scalar()
        if not local_repo.exists(owner, name, settings) and fix:
            local_repo.init_bare(path, marker={"project_id": project_id, "name": name,
                                               "origin": "onboarding"})
            fixed.append("made the local repository")
        if local_repo.exists(owner, name, settings):
            default = binding.get("default_branch") or local_repo.default_branch(path)
            if not local_repo.branch_sha(path, default) and fix:
                local_repo.commit_files(path, default, local_repo.initial_files(
                    str(title or name)), "Initial commit (BOSS)")
                fixed.append("made the first commit")
            repo_ok = bool(local_repo.branch_sha(path, default))
            repo_words = (f"{binding['full_name']} on this machine, branch {default}."
                          if repo_ok else f"{binding['full_name']} has no commit on {default} yet.")
        else:
            repo_words = f"The local repository {binding['full_name']} is missing at {path}."
    checks.append({"key": "local_repo", "ok": repo_ok if binding else None, "label": "Local repository",
                   "words": repo_words})

    free = _disk_free_gb(local_repo.root(settings))
    disk_ok = free is None or free >= 2.0
    checks.append({"key": "disk", "ok": disk_ok, "label": "Disk",
                   "words": (f"{free} GB free where BOSS keeps repositories." if free is not None
                             else "Could not read free disk space.")
                   + ("" if disk_ok else " Builds need at least 2 GB: free some space.")})

    is_online = (online or _online)()
    model_local, engine = _model_is_local(c, settings, project_id)
    checks.append({"key": "network", "ok": True, "label": "Network",
                   "words": "Online: GitHub is reachable." if is_online else
                   "Offline: GitHub is not reachable. Local projects build and merge on this machine; "
                   "publishing waits until you are online."})
    if is_online:
        model_ok, model_words = True, (f"Builds use {engine}." if engine else "Builds use the configured model.")
    elif model_local:
        model_ok, model_words = True, f"Offline, and builds use {engine} on this machine: they can run."
    else:
        model_ok = False
        model_words = (f"This machine is offline and the configured model ({engine or 'remote'}) is "
                       "remote, so a build cannot run. An offline build needs a local engine "
                       "(for example Ollama): set it in Repo and models, or reconnect.")
    checks.append({"key": "model", "ok": model_ok, "label": "Model", "words": model_words})
    if fixed:
        emit_event(c, project_id, "project.local_checks_fixed", actor, "project", project_id,
                   {"fixed": fixed})
    blocking = [ch for ch in checks if ch["ok"] is False]
    return {"project_id": project_id, "ok": not blocking, "online": is_online, "checks": checks,
            "fixed": fixed,
            "summary": ("Ready to build on this machine." if not blocking else
                        " ".join(ch["words"] for ch in blocking))}


def _require_git() -> None:
    if not local_repo.git_available():
        raise LocalProjectError("git is not installed on this machine; install git first.", 503,
                                "git_missing")


__all__ = ["LocalProjectError", "bind", "create_for_project", "delete_local_repo", "folder_facts",
           "import_code", "import_root", "local_binding", "machine_checks",
           "resolve_import_path", "status"]
