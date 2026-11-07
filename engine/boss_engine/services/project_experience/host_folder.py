"""FOLDER ON HOST (Add a project, screen 03: "Existing · repo or folder" → Folder on host).

A project's code can live in a folder on one of the organisation's verified servers rather than
in a GitHub repository BOSS reaches. This module is everything BOSS does with such a folder, and
all of it is READ-ONLY:

  * `hosts`     the verified servers this project may use (its own and its organisation's
                shared ones: deployment_fleet.usable_hosts_sql), by name;
  * `probe`     ONE ssh session on that server, with the host's own registered key, that runs
                read-only commands on the folder: does it exist, is it a git checkout (its
                `origin` remote and branch), how many files, and what is at its top level — from
                which the stack and the build/test commands are read (package.json → Node,
                pyproject.toml → Python, Makefile → make test, …). Nothing is written, moved,
                installed or executed from the folder;
  * `register`  records the folder as the project's source (`project_host_folders`: host,
                path, what the probe found) with a `project.host_folder_added` event. When the
                folder is a checkout of a GitHub repository, the console ALSO adds that
                repository to the project through the existing registry path, so builds,
                pull requests and onboarding work exactly as for a picked repo; a folder with no
                GitHub remote is the project's recorded source, mapped from the host.

A remote URL's userinfo (`https://user:token@github.com/…`) is dropped before anything is
returned or stored: a credential in a git remote never leaves the host.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: An absolute path of ordinary characters: no `..`, no shell metacharacters, ≤ 400 chars.
_PATH = re.compile(r"^/[A-Za-z0-9._@+\-/ ]{0,399}$")
_GITHUB = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")

#: top-level file → (stack word, build & test words). First match per stack.
_STACK: tuple[tuple[str, str, Optional[str]], ...] = (
    ("package.json", "Node", "npm install · npm test"),
    ("pyproject.toml", "Python", "pip install · pytest"),
    ("requirements.txt", "Python", "pip install -r requirements.txt · pytest"),
    ("go.mod", "Go", "go build · go test"),
    ("pom.xml", "Java", "mvn package · mvn test"),
    ("build.gradle", "Java", "gradle build · gradle test"),
    ("Cargo.toml", "Rust", "cargo build · cargo test"),
    ("Gemfile", "Ruby", "bundle install · rake test"),
    ("composer.json", "PHP", "composer install · phpunit"),
    ("docker-compose.yml", "Compose", None),
    ("Dockerfile", "Docker", None),
)


class FolderError(ValueError):
    """A refusal in words: {message} for the person, `status` for the route."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def clean_path(path: Any) -> str:
    p = str(path or "").strip()
    if len(p) > 1:
        p = p.rstrip("/")
    if not _PATH.match(p) or "/../" in f"{p}/" or "/./" in f"{p}/":
        raise FolderError("Give the folder as an absolute path, e.g. /srv/apps/helpdesk.")
    return p


def hosts(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Verified servers this project may read a folder on, by name."""
    from ..deployment_fleet import usable_hosts_sql

    rows = connection.execute(text(
        f"SELECT id, name, address, project_id FROM deployment_hosts WHERE status = 'verified' "
        f"AND {usable_hosts_sql()} ORDER BY (project_id = :p) DESC, name"),
        {"p": project_id}).mappings().all()
    return [{"id": r["id"], "name": r["name"], "address": r["address"],
             "shared": r["project_id"] != project_id} for r in rows]


def _host(connection: Connection, project_id: str, host_id: str) -> dict[str, Any]:
    from ..deployment_fleet import usable_hosts_sql

    row = connection.execute(text(
        f"SELECT * FROM deployment_hosts WHERE id = :h AND status = 'verified' "
        f"AND {usable_hosts_sql()}"), {"h": host_id, "p": project_id}).mappings().first()
    if row is None:
        raise FolderError("That server is not a verified server this project can use.", 404)
    return dict(row)


def command_for(path: str) -> str:
    """The one read-only shell line the probe runs (every value quoted)."""
    p = shlex.quote(path)
    return (
        f"P={p}; if [ ! -d \"$P\" ]; then echo NODIR; exit 0; fi; echo DIR; "
        "if git -C \"$P\" rev-parse --is-inside-work-tree >/dev/null 2>&1; then echo GIT; "
        "git -C \"$P\" remote get-url origin 2>/dev/null | sed 's/^/REMOTE /'; "
        "git -C \"$P\" rev-parse --abbrev-ref HEAD 2>/dev/null | sed 's/^/BRANCH /'; "
        "echo \"FILES $(git -C \"$P\" ls-files 2>/dev/null | wc -l)\"; "
        "else echo \"FILES $(find \"$P\" -type f -not -path '*/.git/*' "
        "-not -path '*/node_modules/*' 2>/dev/null | head -50000 | wc -l)\"; fi; "
        "ls -1A \"$P\" 2>/dev/null | head -200 | sed 's/^/TOP /'")


def _strip_userinfo(url: str) -> str:
    return re.sub(r"^(\w+://)[^/@]*@", r"\1", url.strip())


def parse(path: str, stdout: str) -> dict[str, Any]:
    """What the probe printed → facts (names and counts only)."""
    lines = [ln.rstrip("\r") for ln in (stdout or "").splitlines()]
    if "NODIR" in lines or "DIR" not in lines:
        return {"path": path, "exists": False}
    remote = next((ln[7:] for ln in lines if ln.startswith("REMOTE ")), None)
    branch = next((ln[7:] for ln in lines if ln.startswith("BRANCH ")), None)
    files_line = next((ln[6:] for ln in lines if ln.startswith("FILES ")), "0")
    top = [ln[4:] for ln in lines if ln.startswith("TOP ")]
    remote = _strip_userinfo(remote) if remote else None
    found = _GITHUB.search(remote or "")
    stack: list[str] = []
    build = None
    for name, word, cmd in _STACK:
        if name in top and word not in stack:
            stack.append(word)
            build = build or cmd
    if "Makefile" in top:
        build = "make setup · make test" if not build else build
    try:
        files = int(files_line.strip() or 0)
    except ValueError:
        files = 0
    return {"path": path, "exists": True, "git": "GIT" in lines, "remote": remote,
            "github": f"{found.group(1)}/{found.group(2)}" if found else None,
            "branch": branch if branch and branch != "HEAD" else None, "files": files,
            "top": top[:40], "stack": stack, "build": build,
            "suggested_name": _name_from(path)}


def _name_from(path: str) -> str:
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    words = re.sub(r"[-_.]+", " ", tail).strip()
    return words[:1].upper() + words[1:] if words else "Project"


Runner = Callable[[Mapping[str, Any], str], tuple[int, str, str]]


def _ssh_runner(settings: Any) -> Runner:
    def run(host: Mapping[str, Any], command: str) -> tuple[int, str, str]:
        from ...db import get_engine
        from ..deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
        from ..secret_backends import resolve_target_secret

        probe = {"secret_refs": ([{"name": "ssh_key", "secret_ref_id": host["key_secret_ref"]}]
                                 if host.get("key_secret_ref") else []),
                 "host_id": host["id"]}
        with get_engine(settings).begin() as c:
            material = resolve_target_secret(c, probe, "ssh_key", settings)
        key = _materialise_key(material) if material else _ssh_key_file(settings)
        try:
            res = _ssh(key, host["address"], host.get("ssh_user") or "root",
                       int(host.get("ssh_port") or 22), command, timeout=45)
        finally:
            if material:
                _discard_key_file(key)
        return res.returncode, res.stdout or "", res.stderr or ""
    return run


def run_read(host: Mapping[str, Any], command: str, runner: Runner) -> tuple[int, str, str]:
    """One read over ssh; a server BOSS cannot reach (no usable key, no answer) is a refusal in
    words, never a 500."""
    try:
        return runner(host, command)
    except FolderError:
        raise
    except Exception as error:  # noqa: BLE001 — the transport's own words, without a credential
        said = str(error).splitlines()[0][:160] if isinstance(error, (RuntimeError, OSError)) \
            else type(error).__name__
        raise FolderError(f"{host.get('name') or 'The server'} did not answer: {said}", 502) \
            from None


def probe(connection: Connection, settings: Any, project_id: str, host_id: str, path: Any,
          *, runner: Optional[Runner] = None) -> dict[str, Any]:
    """Read the folder on the server (read-only) → facts, with the server's name."""
    clean = clean_path(path)
    host = _host(connection, project_id, str(host_id or ""))
    code, out, err = run_read(host, command_for(clean), runner or _ssh_runner(settings))
    if code not in (0, None) and "DIR" not in (out or ""):
        raise FolderError(f"{host['name']} did not answer the read ({(err or '').strip()[:160] or f'exit {code}'}).",
                          502)
    facts = parse(clean, out)
    facts.update({"host_id": host["id"], "host": host["name"]})
    return facts


def register(connection: Connection, project_id: str, host_id: str, path: Any,
             facts: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """Record the folder as the project's source (idempotent per project, host and path)."""
    from ..core import emit_event

    clean = clean_path(path)
    host = _host(connection, project_id, str(host_id or ""))
    if not facts.get("exists", True):
        raise FolderError(f"There is no folder {clean} on {host['name']}.")
    keep = {k: facts.get(k) for k in ("git", "remote", "github", "branch", "files", "stack",
                                       "build") if k in facts}
    row = connection.execute(text(
        "INSERT INTO project_host_folders (project_id, host_id, path, facts, created_by) "
        "VALUES (:p, :h, :path, CAST(:f AS JSONB), :a) "
        "ON CONFLICT (project_id, host_id, path) DO UPDATE SET facts = EXCLUDED.facts, "
        "updated_at = now() RETURNING id, created_at"),
        {"p": project_id, "h": host["id"], "path": clean, "f": json.dumps(keep), "a": actor}
    ).mappings().first()
    emit_event(connection, project_id, "project.host_folder_added", actor, "project",
               project_id, {"host_id": host["id"], "path": clean,
                            "github": keep.get("github")})
    return {"id": row["id"], "project_id": project_id, "host_id": host["id"],
            "host": host["name"], "path": clean, "facts": keep}


def listing(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT f.id, f.host_id, f.path, f.facts, f.created_at, h.name AS host "
        "FROM project_host_folders f LEFT JOIN deployment_hosts h ON h.id = f.host_id "
        "WHERE f.project_id = :p ORDER BY f.created_at"), {"p": project_id}).mappings().all()
    return [{"id": r["id"], "host_id": r["host_id"], "host": r["host"], "path": r["path"],
             "facts": r["facts"] if isinstance(r["facts"], dict) else json.loads(r["facts"] or "{}")}
            for r in rows]


__all__ = ["FolderError", "clean_path", "command_for", "hosts", "listing", "parse", "probe",
           "register", "run_read"]
