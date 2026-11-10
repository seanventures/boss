"""THE LOCAL GIT REMOTE (BOSSAPP-948): git's smart HTTP protocol over a local project's bare
repository, so plain `git clone / pull / push` works against BOSS. Design:
docs/product/LOCAL_GIT_REMOTE_AND_REVIEW.md §1.

    /git/<project>.git/...          the project's primary local repository
    /git/<project>/<repo>.git/...   any other local repository of the project

The engine is git's own `git http-backend` (CGI), run per request in the API: no pack or protocol
code of ours. The API route decides who may (repository:read; repository:push for
receive-pack, checked at `info/refs?service=git-receive-pack` so git asks for credentials before
it uploads anything), and this module runs the backend with:

  * the hooks from the image (`core.hooksPath` = services/git_hooks, set through GIT_CONFIG_*
    environment, never written into the repository, so no clone can change them): pre-receive
    refuses the default branch, tags, force pushes, deletes and a boss/* branch a run is building;
    post-receive opens or updates the change set and queues its review (local_git_hooks);
  * `receive.maxInputSize` (BOSS_GIT_REMOTE_MAX_PUSH_MB, 200 MB);
  * the pusher's actor in the environment the hooks read.

Reach: loopback only by default (`is_local_request`): the web and API publish on 127.0.0.1, and
/git/* also refuses a client that is not on this machine unless BOSS_GIT_REMOTE_REACH=network.
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from sqlalchemy import text

from . import local_repo

HOOKS_DIR = Path(__file__).resolve().parent / "git_hooks"
SERVICES = ("git-upload-pack", "git-receive-pack")
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]", "host.docker.internal"}


class GitRemoteError(ValueError):
    def __init__(self, message: str, status: int = 404, error: str = "git_remote_refused"):
        super().__init__(message)
        self.status = status
        self.error = error


def max_push_bytes(settings: Any) -> int:
    return int(getattr(settings, "git_remote_max_push_mb", 200) or 200) * 1024 * 1024


def _private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def is_local_request(settings: Any, *, client_ip: Optional[str], host: Optional[str]) -> bool:
    """Is this request from this machine? The client is on loopback (or the container bridge the
    console proxies from) AND it named this machine (localhost / 127.0.0.1). A tailnet or LAN
    client names the machine's network address, so it is refused unless reach is `network`."""
    if str(getattr(settings, "git_remote_reach", "local") or "local").lower() == "network":
        return True
    name = str(host or "").strip().lower()
    if name.startswith("["):
        name = name.split("]", 1)[0] + "]"
    else:
        name = name.rsplit(":", 1)[0] if name.count(":") == 1 else name
    if name not in _LOOPBACK_NAMES:
        return False
    return bool(client_ip) and _private(str(client_ip))


def resolve_repo(c: Any, settings: Any, project_id: str, repo: Optional[str] = None
                 ) -> dict[str, Any]:
    """The local repository /git/<project>[/<repo>].git names: the binding and its bare path."""
    row = c.execute(text(
        "SELECT owner, name, full_name, role, default_branch FROM github_repository_bindings "
        "WHERE project_id = :p AND host = 'local' AND status = 'active' "
        "AND (CAST(:r AS TEXT) IS NULL OR name = :r) "
        "ORDER BY (role = 'primary') DESC, created_at LIMIT 1"),
        {"p": project_id, "r": repo}).mappings().first()
    if row is None:
        raise GitRemoteError(f"{project_id} has no local repository"
                             + (f" named {repo}" if repo else "") + ".", 404, "no_local_repo")
    git_dir = local_repo.bare_path(row["owner"], row["name"], settings)
    if not (git_dir / "HEAD").is_file():
        raise GitRemoteError(f"The local repository {row['full_name']} is missing on this machine.",
                             404, "no_local_repo")
    return {**dict(row), "git_dir": git_dir}


def busy_branches(c: Any, project_id: str, repository: str) -> list[str]:
    """boss/* branches a run is building right now (the pre-receive hook refuses pushes to them)."""
    rows = c.execute(text(
        "SELECT id, finalize_ctx->>'root_run_id' AS root, finalize_ctx->>'local_change_branch' AS lcb "
        "FROM code_task_runs WHERE project_id = :p AND status IN "
        "('queued', 'running', 'pending', 'in_progress', 'provisioning', 'building') "
        "AND COALESCE(spec->>'owner', '') || '/' || COALESCE(spec->>'repo', '') = :r"),
        {"p": project_id, "r": repository}).mappings().all()
    out = set()
    for r in rows:
        out.add(f"boss/code/{r['root'] or r['id']}")
        # A person's own branch stays theirs to push, even while its review runs (BOSSAPP-948
        # answer 6 fences boss/* branches only).
        if r["lcb"] and str(r["lcb"]).startswith("boss/"):
            out.add(str(r["lcb"]))
    return sorted(out)


def hook_env(*, project_id: str, repository: str, default_branch: str, pusher: str,
             pusher_kind: str, busy: list[str], settings: Any) -> dict[str, str]:
    """What the hooks read: who pushes, where, and what they may not touch. The hooks are git's
    own children, so they also inherit DATABASE_URL and the rest of the API's environment."""
    engine_dir = str(Path(__file__).resolve().parents[2])
    return {
        "BOSS_HOOK_PYTHON": sys.executable,
        "PYTHONPATH": os.pathsep.join(p for p in [engine_dir, os.environ.get("PYTHONPATH", "")] if p),
        "BOSS_GIT_PROJECT": project_id,
        "BOSS_GIT_REPOSITORY": repository,
        "BOSS_GIT_DEFAULT_BRANCH": default_branch,
        "BOSS_GIT_PUSHER": pusher,
        "BOSS_GIT_PUSHER_KIND": pusher_kind,
        "BOSS_GIT_BUSY_BRANCHES": json.dumps(busy),
        "BOSS_GIT_CONSOLE": str(getattr(settings, "public_url", "") or "").rstrip("/"),
    }


def git_config_env(settings: Any, *, push: bool) -> dict[str, str]:
    """Configuration for this one backend run, as environment (GIT_CONFIG_COUNT): nothing is
    written to the repository's own config."""
    pairs = [("core.hooksPath", str(HOOKS_DIR)),
             ("http.receivepack", "true" if push else "false"),
             ("http.uploadpack", "true"),
             ("receive.maxInputSize", str(max_push_bytes(settings))),
             ("receive.denyNonFastForwards", "true"),
             ("receive.denyDeletes", "true"),
             ("uploadpack.allowFilter", "true")]
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


class BackendResponse:
    """git http-backend's CGI answer: status, headers, and the body as a stream."""

    def __init__(self, status: int, headers: list[tuple[str, str]], body: Iterator[bytes]):
        self.status, self.headers, self.body = status, headers, body


def run_backend(git_dir: Path, *, method: str, path_info: str, query: str, content_type: str,
                body: bytes, extra_env: Mapping[str, str], content_encoding: str = "",
                git_protocol: str = "", remote_user: str = "") -> BackendResponse:
    """Run `git http-backend` for one request. The request body is written on a thread while
    the answer is read, so a large pack never deadlocks on full pipes."""
    env = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": os.environ.get("HOME") or "/tmp",
        "GIT_PROJECT_ROOT": str(git_dir.parent),
        "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": f"/{git_dir.name}{path_info}",
        "REQUEST_METHOD": method.upper(),
        "QUERY_STRING": query or "",
        "CONTENT_TYPE": content_type or "",
        "CONTENT_LENGTH": str(len(body)),
        "REMOTE_USER": remote_user or "boss",
        "REMOTE_ADDR": "127.0.0.1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    for key in ("DATABASE_URL", "BOSS_DATABASE_URL", "BOSS_TEST_DATABASE_URL", "LANG",
                "BOSS_LOCAL_REPO_ROOT", "BOSS_IDENTITY_SERVER_KEY"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if content_encoding:
        env["HTTP_CONTENT_ENCODING"] = content_encoding
    if git_protocol:
        env["GIT_PROTOCOL"] = git_protocol
    env.update(extra_env)
    proc = subprocess.Popen(["git", "http-backend"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)

    def feed() -> None:
        try:
            if body:
                proc.stdin.write(body)  # type: ignore[union-attr]
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except OSError:
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    stderr_chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()),  # type: ignore[union-attr]
                              daemon=True)
    reader.start()
    out = proc.stdout  # type: ignore[assignment]
    head = b""
    while b"\r\n\r\n" not in head and b"\n\n" not in head:
        chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
        if not chunk:
            break
        head += chunk
    sep = b"\r\n\r\n" if b"\r\n\r\n" in head else b"\n\n"
    raw_headers, _, rest = head.partition(sep)
    status = 200
    headers: list[tuple[str, str]] = []
    for line in raw_headers.decode("latin-1").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k.strip().lower() == "status":
            try:
                status = int(v.strip().split()[0])
            except (ValueError, IndexError):
                status = 500
        else:
            headers.append((k.strip(), v.strip()))
    if not raw_headers and not rest:
        writer.join(timeout=5)
        proc.wait(timeout=30)
        reader.join(timeout=5)
        said = b"".join(stderr_chunks).decode("utf-8", "replace").strip()[-300:]
        raise GitRemoteError(f"git http-backend answered nothing: {said or proc.returncode}", 500,
                             "git_backend_failed")

    def stream() -> Iterator[bytes]:
        try:
            if rest:
                yield rest
            while True:
                chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            writer.join(timeout=30)
            try:
                proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
            reader.join(timeout=5)

    return BackendResponse(status, headers, stream())


__all__ = ["BackendResponse", "GitRemoteError", "HOOKS_DIR", "busy_branches", "git_config_env",
           "hook_env", "is_local_request", "max_push_bytes", "resolve_repo", "run_backend"]
