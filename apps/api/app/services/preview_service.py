"""Tier-1 "1-box" app previews.

After a code task produces a green PR, an operator wants to *see the working website*
before merging. This runs the built app from its branch as a managed, auto-expiring local
process on the dev box and hands back a live preview URL — zero extra compute.

Process-based (not containerized) for v1: simplest, works with loopback-binding apps on
macOS where container host-networking is awkward. Containerized isolation is the v2 hardening.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .code_task_service import load_code_task_run
from .core import emit_event, make_id

PREVIEW_ROOT = Path(os.environ.get("BOSS_PREVIEW_ROOT", "/tmp/boss-previews"))


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _detect_command(workdir: Path) -> Optional[list[str]]:
    """Heuristically determine how to start the app. Apps that read PORT/HOST from env get
    the allocated port; others (e.g. a hardcoded port) are discovered post-launch via lsof."""
    for entry in ("server.py", "app.py", "wsgi.py", "main.py"):
        if (workdir / entry).exists():
            return ["python3", entry]
    if (workdir / "package.json").exists():
        return ["sh", "-c", "npm install --no-audit --no-fund --loglevel=error && "
                "(npm run build --if-present || true) && npm run start"]
    return None


def _descendant_pids(pid: int) -> list[int]:
    pids = [pid]
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5).stdout
        for child in out.split():
            pids.extend(_descendant_pids(int(child)))
    except Exception:
        pass
    return pids


def _http_ok(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # any HTTP response (even 4xx) means the app is serving
    except Exception:
        return False


def _candidate_ports(log_path: str, port_hint: int) -> list[int]:
    """Ordered candidates that unambiguously belong to THIS app:
    1) the URL/port the framework printed in its own log, then
    2) the PORT we injected via env (apps that honour it).
    Deliberately NOT process-tree lsof — on a dev box that picks up unrelated local services
    (Docker, Neo4j, IDEs) that also answer HTTP."""
    ordered: list[int] = []
    seen: set[int] = set()

    def add(p: int):
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    try:
        log = open(log_path, errors="ignore").read()
        for m in re.finditer(r"https?://[^\s/]*?:(\d{2,5})\b", log):
            add(int(m.group(1)))
        for m in re.finditer(r"(?:port|listening on)\D{0,8}(\d{3,5})\b", log, re.I):
            add(int(m.group(1)))
    except Exception:
        pass
    add(port_hint)
    return ordered


def _discover_working_port(log_path: str, port_hint: int, timeout: float = 20.0) -> Optional[int]:
    """Return the port the app actually serves HTTP on — its log-announced port first, then the
    injected PORT — each health-checked, so a neighboring local service is never mistaken for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in _candidate_ports(log_path, port_hint):
            if _http_ok(port):
                return port
        time.sleep(0.8)
    return None


def start_preview(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Clone the run's branch, launch the app, discover its port, register an expiring preview."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    branch = (run.get("finalize_ctx") or {}).get("work_branch") or f"boss/code/{run_id}"
    project_id = run["project_id"]
    preview_id = make_id("prev")

    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))
    log_path = str(workdir / "_preview.log")

    def _fail(msg: str) -> dict:
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,status,error,log_path,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,'failed',:e,:lg,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "e": msg[:500], "lg": log_path})
        return {"id": preview_id, "status": "failed", "error": msg}

    token = settings.github_token
    clone_url = (f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
                 if token else f"https://github.com/{owner}/{repo}.git")
    clone = subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
                           capture_output=True, text=True, timeout=120)
    if clone.returncode != 0:
        return _fail(f"git clone failed: {(clone.stderr or '')[-300:]}")

    command = _detect_command(workdir)
    if not command:
        return _fail("could not detect how to run the app (no server.py/app.py/main.py/package.json)")

    port_hint = _free_port()
    env = {**os.environ, "PORT": str(port_hint), "HOST": "0.0.0.0", "PYTHONUNBUFFERED": "1"}
    logf = open(log_path, "w")
    proc = subprocess.Popen(command, cwd=str(workdir), env=env, stdout=logf, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)

    port = _discover_working_port(log_path, port_hint)
    if port is None:
        _kill(proc.pid)
        tail = ""
        try:
            tail = Path(log_path).read_text(errors="ignore")[-300:]
        except Exception:
            pass
        return _fail(f"app did not serve HTTP within timeout. log tail: {tail}")

    url = f"http://127.0.0.1:{port}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,port,url,pid,status,log_path,expires_at) "
            "VALUES (:id,:p,:r,:repo,:b,:cmd,:port,:url,:pid,'running',:lg,:exp)"),
            {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}", "b": branch,
             "cmd": " ".join(command), "port": port, "url": url, "pid": proc.pid,
             "lg": log_path, "exp": expires})
        emit_event(c, project_id, "app_preview.started", "operator", "app_preview", preview_id,
                   {"run_id": run_id, "url": url, "repo": f"{owner}/{repo}", "branch": branch})
    return {"id": preview_id, "status": "running", "url": url, "port": port,
            "pid": proc.pid, "expires_at": expires.isoformat(), "run_id": run_id,
            "repo": f"{owner}/{repo}", "branch": branch}


def _kill(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), 9)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def stop_preview(engine, preview_id: str) -> dict:
    with begin(engine) as c:
        row = c.execute(text("SELECT pid FROM app_previews WHERE id=:id"), {"id": preview_id}).mappings().first()
        if row is None:
            raise ValueError("preview not found")
        _kill(row["pid"])
        c.execute(text("UPDATE app_previews SET status='stopped' WHERE id=:id"), {"id": preview_id})
    return {"id": preview_id, "status": "stopped"}


def list_previews(engine, project_id: str, limit: int = 50) -> list[dict]:
    """Reap expired previews (kill + mark) and return recent ones."""
    with begin(engine) as c:
        expired = c.execute(text(
            "SELECT id,pid FROM app_previews WHERE status='running' AND expires_at < now()")).mappings().all()
        for r in expired:
            _kill(r["pid"])
            c.execute(text("UPDATE app_previews SET status='expired' WHERE id=:id"), {"id": r["id"]})
        rows = c.execute(text(
            "SELECT id,run_id,repo,branch,url,port,status,error,expires_at,created_at "
            "FROM app_previews WHERE project_id=:p ORDER BY created_at DESC LIMIT :l"),
            {"p": project_id, "l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]
