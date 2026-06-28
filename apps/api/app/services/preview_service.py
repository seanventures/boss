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


def _generate_dockerfile(workdir: Path) -> int:
    """Ensure the app has a Dockerfile; return the container port the app listens on.
    Generated images set HOST=0.0.0.0 + PORT so deploy-ready apps bind correctly for -p mapping."""
    existing = workdir / "Dockerfile"
    if existing.exists():
        m = re.search(r"EXPOSE\s+(\d{2,5})", existing.read_text(errors="ignore"))
        return int(m.group(1)) if m else 8000
    py_entry = next((e for e in ("server.py", "app.py", "wsgi.py", "main.py") if (workdir / e).exists()), None)
    if py_entry:
        port = 8000
        reqs = "COPY requirements.txt ./\nRUN pip install --no-cache-dir -r requirements.txt\n" \
            if (workdir / "requirements.txt").exists() else ""
        existing.write_text(
            "FROM python:3.11-slim\nWORKDIR /app\n" + reqs +
            f"COPY . .\nENV HOST=0.0.0.0 PORT={port} PYTHONUNBUFFERED=1\nEXPOSE {port}\n"
            f'CMD ["python3", "{py_entry}"]\n')
        return port
    if (workdir / "package.json").exists():
        port = 3000
        existing.write_text(
            "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\n"
            "RUN npm install --no-audit --no-fund --loglevel=error\nCOPY . .\n"
            "RUN npm run build --if-present || true\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\nCMD [\"npm\", \"run\", \"start\"]\n")
        return port
    raise RuntimeError("no Dockerfile and could not infer a stack (server.py/app.py/package.json)")


def start_container_preview(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Tier-2 dev-container preview: build a Docker image from the run's branch and run it with a
    mapped port — isolated, reproducible, the same image that later ships to remote compute."""
    if shutil.which("docker") is None:
        raise RuntimeError("docker is not available on this host")
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
    tag = f"boss-prev-{preview_id.replace('_', '-').lower()}"
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))

    def _fail(msg: str) -> dict:
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,mode,status,error,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,'docker','failed',:e,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "e": msg[:500]})
        return {"id": preview_id, "status": "failed", "error": msg, "mode": "docker"}

    token = settings.github_token
    clone_url = (f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
                 if token else f"https://github.com/{owner}/{repo}.git")
    clone = subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
                           capture_output=True, text=True, timeout=120)
    if clone.returncode != 0:
        return _fail(f"git clone failed: {(clone.stderr or '')[-300:]}")

    try:
        container_port = _generate_dockerfile(workdir)
    except RuntimeError as exc:
        return _fail(str(exc))

    build = subprocess.run(["docker", "build", "-t", tag, str(workdir)],
                           capture_output=True, text=True, timeout=600)
    if build.returncode != 0:
        return _fail(f"docker build failed: {(build.stderr or build.stdout or '')[-400:]}")

    host_port = _free_port()
    run_cmd = subprocess.run(
        ["docker", "run", "-d", "--name", tag, "-p", f"{host_port}:{container_port}",
         "-e", f"PORT={container_port}", "-e", "HOST=0.0.0.0", tag],
        capture_output=True, text=True, timeout=60)
    if run_cmd.returncode != 0:
        return _fail(f"docker run failed: {(run_cmd.stderr or '')[-300:]}")
    container_id = run_cmd.stdout.strip()[:12]

    url = f"http://127.0.0.1:{host_port}"
    ok = False
    for _ in range(20):
        if _http_ok(host_port):
            ok = True
            break
        time.sleep(1.0)
    if not ok:
        logs = subprocess.run(["docker", "logs", "--tail", "20", tag], capture_output=True, text=True).stdout
        subprocess.run(["docker", "rm", "-f", tag], capture_output=True)
        return _fail(f"container did not serve HTTP. logs: {logs[-300:]}")

    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,repo,branch,mode,container_id,port,url,status,expires_at) "
            "VALUES (:id,:p,:r,:repo,:b,'docker',:cid,:port,:url,'running',:exp)"),
            {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}", "b": branch,
             "cid": container_id, "port": host_port, "url": url, "exp": expires})
        emit_event(c, project_id, "app_preview.started", "operator", "app_preview", preview_id,
                   {"run_id": run_id, "url": url, "mode": "docker", "image": tag})
    return {"id": preview_id, "status": "running", "url": url, "port": host_port, "mode": "docker",
            "container_id": container_id, "image": tag, "expires_at": expires.isoformat(),
            "run_id": run_id, "repo": f"{owner}/{repo}", "branch": branch}


def _docker_rm(container_id: Optional[str]) -> None:
    if container_id:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


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


def _teardown(row) -> None:
    if row.get("mode") == "docker":
        _docker_rm(row.get("container_id"))
    else:
        _kill(row.get("pid"))


def stop_preview(engine, preview_id: str) -> dict:
    with begin(engine) as c:
        row = c.execute(text("SELECT pid,mode,container_id FROM app_previews WHERE id=:id"),
                        {"id": preview_id}).mappings().first()
        if row is None:
            raise ValueError("preview not found")
        _teardown(dict(row))
        c.execute(text("UPDATE app_previews SET status='stopped' WHERE id=:id"), {"id": preview_id})
    return {"id": preview_id, "status": "stopped"}


def list_previews(engine, project_id: str, limit: int = 50) -> list[dict]:
    """Reap expired previews (tear down + mark) and return recent ones."""
    with begin(engine) as c:
        expired = c.execute(text(
            "SELECT id,pid,mode,container_id FROM app_previews WHERE status='running' AND expires_at < now()")).mappings().all()
        for r in expired:
            _teardown(dict(r))
            c.execute(text("UPDATE app_previews SET status='expired' WHERE id=:id"), {"id": r["id"]})
        rows = c.execute(text(
            "SELECT id,run_id,repo,branch,url,port,mode,status,error,expires_at,created_at "
            "FROM app_previews WHERE project_id=:p ORDER BY created_at DESC LIMIT :l"),
            {"p": project_id, "l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]
