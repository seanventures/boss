"""Sandbox Agent — runs ON the sandbox host (a Linux KVM box in prod; your Mac
via Docker for the stand-in). Launches the isolated execution unit, runs the
coding task inside it, and — unlike a one-shot `docker run --rm` — keeps the
sandbox alive (task-scoped lifecycle, architecture §5.4) so the execution plane
is observable and operable:

  • visibility   — list sandboxes, browse the working tree, read tracked files
  • operability  — run allowlisted helper commands; raw shell only as break-glass

The control plane (BOSS) calls this over HTTP/mTLS. The backend that launches the
isolated unit is pluggable (ContainerBackend now; KataBackend/FirecrackerBackend
later) — swapping it does NOT change this contract.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel

# Commands the governed helper console allows without break-glass, grouped for
# display. Read-only / navigation / diagnostics / re-run-tests only. Anything
# that mutates outside the working tree, reaches the network, or could leak
# secrets (env/printenv/set/export) needs an explicit break-glass raw shell.
HELPER_COMMANDS = {
    "Version control": ["git"],
    "Navigate & read": [
        "ls", "cat", "head", "tail", "wc", "stat", "file", "pwd", "tree",
        "find", "realpath", "basename", "dirname",
    ],
    "Search & text": [
        "grep", "sed", "awk", "sort", "uniq", "cut", "tr", "diff", "cmp",
        "comm", "column", "echo", "which",
    ],
    "Build & run tests": [
        "mvn", "mvnw", "./mvnw", "gradle", "gradlew", "./gradlew",
        "java", "javac", "python3", "node", "npm", "pnpm", "go",
    ],
    "Resources & processes": [
        "ps", "top", "free", "df", "du", "uptime", "vmstat", "nproc",
        "uname", "hostname", "whoami", "id", "date",
    ],
    "Checksums & inspect": ["sha256sum", "md5sum", "cksum", "xxd", "od"],
}
# Flat set used for the actual allowlist enforcement.
HELPER_ALLOW = {c for cmds in HELPER_COMMANDS.values() for c in cmds}
IDLE_TTL_SECONDS = int(os.environ.get("BOSS_SANDBOX_IDLE_TTL_SECONDS", "1800"))
# Per-sandbox resource caps (architecture §5.4 cgroup limits) — also what makes
# the telemetry meaningful (usage vs limit, OOM detectable).
SANDBOX_MEM = os.environ.get("BOSS_SANDBOX_MEM", "2g")
SANDBOX_CPUS = os.environ.get("BOSS_SANDBOX_CPUS", "2")


@dataclass
class SandboxRecord:
    sandbox_id: str
    container_id: str
    tenant_id: str
    repo: str
    title: str
    work_dir: str        # host path; /work/repo inside is the working tree
    status: str = "running"   # running | idle | failed | quarantined | destroyed
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)  # diff summary, tests, engine (NO source)
    # operator forensics
    preserved: bool = False
    preserve_reason: str = ""
    preserved_by: str = ""
    snapshot_image: str = ""

    def summary(self) -> dict:
        return {
            "sandbox_id": self.sandbox_id, "tenant_id": self.tenant_id, "repo": self.repo,
            "title": self.title, "status": self.status, "container_id": self.container_id[:12],
            "created_at": self.created_at, "updated_at": self.updated_at,
            "age_seconds": int(time.time() - self.created_at), "meta": self.meta,
            "preserved": self.preserved, "preserve_reason": self.preserve_reason,
            "preserved_by": self.preserved_by, "snapshot_image": self.snapshot_image,
        }


class ContainerBackend:
    kind = "container"

    def __init__(self, image: str, cache_root: str, runtime: Optional[str] = None,
                 work_root: Optional[str] = None):
        self.image = image
        self.cache_root = Path(cache_root).expanduser()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.work_root = Path(work_root or (Path.home() / ".boss-sandbox" / "work")).expanduser()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.runtime = runtime
        self.sandboxes: dict[str, SandboxRecord] = {}
        # Registry is persisted to disk so an agent restart doesn't blank the
        # console (in-memory only would lose every sandbox on restart).
        self.state_path = Path(
            os.environ.get("BOSS_SANDBOX_STATE")
            or (self.work_root.parent / "registry.json")
        ).expanduser()
        self._restore()

    # -- docker helpers -----------------------------------------------------
    def _docker(self, args, timeout=60, check=False):
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise RuntimeError(f"docker {args[0]} failed: {(p.stderr or p.stdout)[:300]}")
        return p

    def _cache_volume(self, tenant_id: str, repo: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{tenant_id}__{repo}")
        vol = self.cache_root / safe
        vol.mkdir(parents=True, exist_ok=True)
        return vol

    def _reap_idle(self):
        now = time.time()
        for sid in [s for s, r in self.sandboxes.items()
                    if r.status not in ("destroyed", "quarantined")  # never reap a preserved VM
                    and float(r.meta.get("hold_until") or 0) <= now
                    and now - r.updated_at > IDLE_TTL_SECONDS]:
            try:
                self.destroy(sid)
            except Exception:
                pass

    # -- registry persistence + restore (survive agent restarts) -----------
    def _persist(self):
        """Write the live registry to disk so a restart doesn't lose sandboxes."""
        try:
            data = {sid: asdict(r) for sid, r in self.sandboxes.items()
                    if r.status != "destroyed"}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _container_status(self, cid: str) -> Optional[str]:
        if not cid:
            return None
        p = self._docker(["inspect", "-f", "{{.State.Status}}", cid])
        return p.stdout.strip() if p.returncode == 0 else None

    def _restore(self):
        """Rebuild the registry on startup: load the persisted file, reconcile
        against live Docker, and recover orphaned containers / quarantine
        snapshots not in the file (e.g. created before persistence existed)."""
        try:
            if self.state_path.exists():
                for sid, d in json.loads(self.state_path.read_text()).items():
                    try:
                        self.sandboxes[sid] = SandboxRecord(**d)
                    except Exception:
                        pass
        except Exception:
            pass
        # Drop stale records whose container is gone (keep quarantined — the
        # snapshot image is the artifact; its container is expected to be gone).
        for sid, rec in list(self.sandboxes.items()):
            if rec.status == "quarantined":
                continue
            st = self._container_status(rec.container_id)
            if st is None:
                del self.sandboxes[sid]
            elif st != "running" and rec.status not in ("failed", "idle"):
                rec.status = "failed"
        # Recover preserved snapshots (quarantine images) not already tracked.
        try:
            imgs = self._docker(["images", "--format", "{{.Repository}}:{{.Tag}}",
                                 "boss-sandbox-quarantine"])
            for line in (imgs.stdout or "").splitlines():
                line = line.strip()
                if not line.startswith("boss-sandbox-quarantine:"):
                    continue
                sid = line.split(":", 1)[1]
                if sid and sid not in self.sandboxes:
                    self.sandboxes[sid] = SandboxRecord(
                        sandbox_id=sid, container_id="", tenant_id="unknown",
                        repo="unknown", title="(recovered preserved snapshot)",
                        work_dir="", status="quarantined", preserved=True,
                        snapshot_image=line, preserve_reason="recovered after agent restart")
        except Exception:
            pass
        # Recover live/stopped sandbox containers (by label) not already tracked.
        try:
            ps = self._docker(["ps", "-a", "--filter", "label=boss.sandbox", "--format",
                               '{{.ID}}|{{.State}}|{{.Label "boss.sandbox"}}|{{.Label "boss.tenant"}}'])
            for line in (ps.stdout or "").splitlines():
                parts = line.split("|")
                if len(parts) < 4 or not parts[2]:
                    continue
                cid, state, sid, tenant = parts[0], parts[1], parts[2], parts[3]
                if sid not in self.sandboxes:
                    self.sandboxes[sid] = SandboxRecord(
                        sandbox_id=sid, container_id=cid, tenant_id=tenant or "unknown",
                        repo="unknown", title="(recovered container)", work_dir="",
                        status="idle" if state == "running" else "failed")
        except Exception:
            pass
        self._persist()

    # -- lifecycle ----------------------------------------------------------
    def run_task(self, spec: dict, *, tenant_id: str) -> dict:
        self._reap_idle()
        repo = spec.get("repo", "norepo")
        sandbox_id = "sbx_" + uuid.uuid4().hex[:16]
        work = Path(tempfile.mkdtemp(prefix=f"{sandbox_id}-", dir=self.work_root))
        (work / "spec.json").write_text(json.dumps(spec))
        cache = self._cache_volume(tenant_id, repo)

        # Persistent, single-tenant container (idle entrypoint so it survives the
        # task for inspection/console; reaped on TTL or explicit destroy).
        run_cmd = ["run", "-d",
                   "--label", f"boss.tenant={tenant_id}", "--label", f"boss.sandbox={sandbox_id}",
                   "--memory", SANDBOX_MEM, "--memory-swap", SANDBOX_MEM,  # hard mem cap (OOM observable)
                   "--cpus", SANDBOX_CPUS, "--pids-limit", "1024",
                   "-v", f"{work}:/work", "-v", f"{cache}:/cache", "-e", "HOME=/work",
                   "--entrypoint", "sleep"]
        if self.runtime:
            run_cmd[1:1] = ["--runtime", self.runtime]
        run_cmd += [self.image, "infinity"]
        cp = self._docker(run_cmd, check=True)
        cid = cp.stdout.strip()
        rec = SandboxRecord(sandbox_id, cid, tenant_id, repo, spec.get("title", ""), str(work))
        self.sandboxes[sandbox_id] = rec
        self._persist()

        # Run the in-guest agent inside the live container.
        ex = self._docker(["exec", cid, "python3", "/opt/boss/in_guest_agent.py"],
                          timeout=spec.get("timeout", 1200) + 180)
        result_path = work / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else \
            {"status": "error", "error": f"no result (exit {ex.returncode}): {(ex.stderr or '')[:300]}"}
        rec.status = "idle" if result.get("status") == "ok" else "failed"
        rec.updated_at = time.time()
        rec.meta = {k: result.get(k) for k in ("engine", "summary", "diff_summary", "tests", "tests_passed")}
        self._persist()
        result["sandbox_id"] = sandbox_id
        return result

    def destroy(self, sandbox_id: str):
        rec = self.sandboxes.get(sandbox_id)
        if not rec:
            raise KeyError(sandbox_id)
        self._docker(["rm", "-f", rec.container_id])
        shutil.rmtree(rec.work_dir, ignore_errors=True)
        rec.status = "destroyed"
        rec.updated_at = time.time()
        self._persist()

    # -- visibility ---------------------------------------------------------
    def _repo_root(self, rec: SandboxRecord) -> Path:
        return Path(rec.work_dir) / "repo"

    def tree(self, sandbox_id: str, limit: int = 500) -> list[str]:
        rec = self._require(sandbox_id)
        root = self._repo_root(rec)
        skip = {".git", "target", "node_modules", ".gradle", "build", "__pycache__"}
        out = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in skip for part in rel.parts):
                continue
            out.append(str(rel) + ("/" if p.is_dir() else ""))
            if len(out) >= limit:
                break
        return out

    def read_file(self, sandbox_id: str, rel_path: str, max_bytes: int = 200_000) -> str:
        rec = self._require(sandbox_id)
        root = self._repo_root(rec).resolve()
        target = (root / rel_path).resolve()
        if root not in target.parents and target != root:
            raise PermissionError("path escapes the working tree")
        if not target.is_file():
            raise FileNotFoundError(rel_path)
        return target.read_text(encoding="utf-8", errors="replace")[:max_bytes]

    # -- operability (governed exec) ---------------------------------------
    def exec(self, sandbox_id: str, command: str, *, break_glass: bool = False) -> dict:
        rec = self._require(sandbox_id)
        argv = shlex.split(command)
        if not argv:
            raise ValueError("empty command")
        if not break_glass and argv[0].lstrip("./") not in {c.lstrip("./") for c in HELPER_ALLOW}:
            raise PermissionError(
                f"'{argv[0]}' is not an allowlisted helper command. Use break-glass for a raw shell."
            )
        # top/htop are curses programs that need a TTY; docker exec here is
        # non-interactive, so force batch mode for a one-shot snapshot.
        if argv[0] == "top" and "-b" not in argv:
            argv = ["top", "-b", "-n1", *argv[1:]]
            command = shlex.join(argv)
        rec.updated_at = time.time()
        # run inside the container, cwd = the working tree
        p = self._docker(["exec", "-w", "/work/repo", rec.container_id, "bash", "-lc", command],
                         timeout=300)
        return {"exit_code": p.returncode, "stdout": p.stdout[-20000:], "stderr": p.stderr[-8000:],
                "break_glass": break_glass}

    # -- telemetry (operator: "did it have CPU/memory problems?") ----------
    def stats(self, sandbox_id: str) -> dict:
        rec = self._require(sandbox_id)
        out = {"sandbox_id": sandbox_id, "status": rec.status}
        s = self._docker(["stats", "--no-stream", "--format", "{{json .}}", rec.container_id], timeout=20)
        if s.returncode == 0 and s.stdout.strip():
            try:
                d = json.loads(s.stdout.strip().splitlines()[0])
                out.update({"cpu_pct": d.get("CPUPerc"), "mem_usage": d.get("MemUsage"),
                            "mem_pct": d.get("MemPerc"), "net_io": d.get("NetIO"),
                            "block_io": d.get("BlockIO"), "pids": d.get("PIDs")})
            except Exception:
                pass
        i = self._docker(["inspect", rec.container_id], timeout=20)
        if i.returncode == 0:
            try:
                info = json.loads(i.stdout)[0]
                state, host = info.get("State", {}), info.get("HostConfig", {})
                out.update({
                    "oom_killed": state.get("OOMKilled"), "exit_code": state.get("ExitCode"),
                    "container_status": state.get("Status"),
                    "mem_limit_bytes": host.get("Memory"), "nano_cpus": host.get("NanoCpus"),
                    "restart_count": info.get("RestartCount"),
                })
            except Exception:
                pass
        return out

    # -- preserve / quarantine for a customer escalation -------------------
    def preserve(self, sandbox_id: str, *, reason: str, actor: str) -> dict:
        rec = self._require(sandbox_id)
        # snapshot the sandbox filesystem for forensics (image survives the container)
        image = f"boss-sandbox-quarantine:{sandbox_id}"
        self._docker(["commit", rec.container_id, image], timeout=120)
        rec.preserved = True
        rec.preserve_reason = reason
        rec.preserved_by = actor
        rec.snapshot_image = image
        rec.status = "quarantined"   # excluded from the reaper; kept for investigation
        rec.updated_at = time.time()
        self._persist()
        return rec.summary()

    # -- retained-rootfs preview ------------------------------------------
    def hold(self, sandbox_id: str, *, ttl_seconds: int, reason: str) -> dict:
        rec = self._require(sandbox_id)
        rec.meta["hold_until"] = time.time() + max(60, min(int(ttl_seconds), 7200))
        rec.meta["hold_reason"] = reason[:300]
        rec.updated_at = time.time()
        self._persist()
        return {"sandbox_task_id": sandbox_id, "hold_until": rec.meta["hold_until"]}

    def _preview_command(self, rec: SandboxRecord, explicit: str = "") -> str:
        root = self._repo_root(rec)
        if explicit.strip():
            return explicit.strip()
        hint = root / "boss.preview.command"
        if hint.is_file() and hint.read_text(errors="ignore").strip():
            return hint.read_text(errors="ignore").strip()
        package = root / "package.json"
        if package.is_file():
            try:
                scripts = (json.loads(package.read_text()).get("scripts") or {})
            except (OSError, json.JSONDecodeError):
                scripts = {}
            if scripts.get("dev"):
                return "npm run dev -- --host 0.0.0.0"
            if scripts.get("start"):
                return "npm run start"
        procfile = root / "Procfile"
        if procfile.is_file():
            for line in procfile.read_text(errors="ignore").splitlines():
                kind, sep, command = line.partition(":")
                if sep and kind.strip().lower() == "web" and command.strip():
                    return command.strip()
        for entry in ("server.py", "app.py", "wsgi.py", "main.py"):
            if (root / entry).is_file():
                return f"python3 {shlex.quote(entry)}"
        raise ValueError("could not detect a preview command in retained rootfs")

    def _container_ip(self, rec: SandboxRecord) -> str:
        result = self._docker(["inspect", rec.container_id], timeout=10, check=True)
        info = json.loads(result.stdout)[0].get("NetworkSettings") or {}
        if info.get("IPAddress"):
            return info["IPAddress"]
        for network in (info.get("Networks") or {}).values():
            if network.get("IPAddress"):
                return network["IPAddress"]
        raise RuntimeError("sandbox container has no routable IP")

    def start_preview(
        self, sandbox_id: str, *, ttl_seconds: int, command: str = ""
    ) -> dict:
        rec = self._require(sandbox_id)
        existing = rec.meta.get("preview") or {}
        if existing.get("port") and float(existing.get("expires_at") or 0) > time.time():
            return {**existing, "sandbox_task_id": sandbox_id}
        command = self._preview_command(rec, command)
        port = 4100 + (int(sandbox_id[-4:], 16) % 700)
        launch = (
            f"cd /work/repo && export PORT={port} HOST=0.0.0.0 PYTHONUNBUFFERED=1; "
            f"({command}) > /work/preview.log 2>&1 & echo $! > /work/preview.pid"
        )
        result = self._docker(
            ["exec", rec.container_id, "bash", "-lc", launch], timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "preview launch failed")[-500:])
        ip = self._container_ip(rec)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                response = urllib.request.urlopen(f"http://{ip}:{port}/", timeout=2)
                if 200 <= response.status < 400:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            logs = self._docker(
                ["exec", rec.container_id, "tail", "-n", "80", "/work/preview.log"], timeout=10
            )
            raise RuntimeError(f"rootfs preview did not become healthy: {logs.stdout[-500:]}")
        expires_at = time.time() + max(60, min(int(ttl_seconds), 7200))
        rec.meta["preview"] = {
            "port": port, "command": command, "expires_at": expires_at,
            "url": f"/v1/sandbox/tasks/{sandbox_id}/preview/web/",
        }
        rec.meta["hold_until"] = expires_at
        rec.updated_at = time.time()
        self._persist()
        return {**rec.meta["preview"], "sandbox_task_id": sandbox_id}

    def stop_preview(self, sandbox_id: str) -> None:
        rec = self._require(sandbox_id)
        self._docker(
            ["exec", rec.container_id, "bash", "-lc",
             "test ! -f /work/preview.pid || kill $(cat /work/preview.pid) 2>/dev/null || true"],
            timeout=10,
        )
        rec.meta.pop("preview", None)
        rec.updated_at = time.time()
        self._persist()

    def proxy_preview(
        self, sandbox_id: str, path: str, *, method: str, query: str,
        body: bytes, content_type: str,
    ) -> tuple[int, str, bytes]:
        target = self.preview_target(sandbox_id, path, query=query, websocket=False)
        request = urllib.request.Request(target, data=body or None, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            response = urllib.request.urlopen(request, timeout=25)
            return response.status, response.headers.get("Content-Type", "application/octet-stream"), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", "text/plain"), exc.read()

    def preview_target(
        self, sandbox_id: str, path: str, *, query: str = "", websocket: bool = False
    ) -> str:
        rec = self._require(sandbox_id)
        preview = rec.meta.get("preview") or {}
        if not preview.get("port") or float(preview.get("expires_at") or 0) <= time.time():
            raise ValueError("rootfs preview is not running")
        scheme = "ws" if websocket else "http"
        target = f"{scheme}://{self._container_ip(rec)}:{preview['port']}/{path.lstrip('/')}"
        return target + ("?" + query if query else "")

    def _require(self, sandbox_id: str) -> SandboxRecord:
        rec = self.sandboxes.get(sandbox_id)
        if not rec or rec.status == "destroyed":
            raise KeyError(sandbox_id)
        return rec


def make_backend() -> ContainerBackend:
    return ContainerBackend(
        image=os.environ.get("BOSS_SANDBOX_IMAGE", "boss-sandbox-guest:latest"),
        cache_root=os.environ.get("BOSS_SANDBOX_CACHE_ROOT", "~/.boss-sandbox/cache"),
        work_root=os.environ.get("BOSS_SANDBOX_WORK_ROOT") or None,
        runtime=os.environ.get("BOSS_SANDBOX_CONTAINER_RUNTIME") or None,
    )


# --------------------------------------------------------------------------- #
#  HTTP API                                                                   #
# --------------------------------------------------------------------------- #
app = FastAPI(title="BOSS Sandbox Agent")
_backend = make_backend()


class RunTaskRequest(BaseModel):
    tenant_id: str
    spec: dict


class ExecRequest(BaseModel):
    command: str
    break_glass: bool = False
    reason: Optional[str] = None  # required by the control plane for break-glass (audited there)


class PreserveRequest(BaseModel):
    reason: str
    actor: str = "operator"


class HoldRequest(BaseModel):
    ttl_seconds: int = 7200
    reason: str = "preview retention"


class PreviewRequest(BaseModel):
    ttl_seconds: int = 3600
    command: str = ""


@app.get("/healthz")
def healthz() -> dict:
    live = [r.summary() for r in _backend.sandboxes.values() if r.status != "destroyed"]
    return {"status": "ok", "backend": _backend.kind, "image": _backend.image, "sandboxes": len(live)}


@app.get("/v1/sandbox/helpers")
def sandbox_helpers() -> dict:
    """The governed helper allowlist (commands runnable without break-glass),
    grouped for display. Single source of truth for the console UI."""
    return {"categories": HELPER_COMMANDS, "commands": sorted(HELPER_ALLOW)}


@app.post("/v1/sandbox/run-task")
def run_task(req: RunTaskRequest) -> dict:
    if not req.tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id required")
    return _backend.run_task(req.spec, tenant_id=req.tenant_id)


@app.get("/v1/sandboxes")
def list_sandboxes() -> dict:
    return {"sandboxes": [r.summary() for r in _backend.sandboxes.values() if r.status != "destroyed"]}


@app.get("/v1/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str) -> dict:
    try:
        return _backend._require(sandbox_id).summary()
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")


@app.get("/v1/sandboxes/{sandbox_id}/tree")
def sandbox_tree(sandbox_id: str) -> dict:
    try:
        return {"tree": _backend.tree(sandbox_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")


@app.get("/v1/sandboxes/{sandbox_id}/file")
def sandbox_file(sandbox_id: str, path: str) -> dict:
    try:
        return {"path": path, "content": _backend.read_file(sandbox_id, path)}
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")
    except (FileNotFoundError, PermissionError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/v1/sandboxes/{sandbox_id}/exec")
def sandbox_exec(sandbox_id: str, req: ExecRequest) -> dict:
    try:
        return _backend.exec(sandbox_id, req.command, break_glass=req.break_glass)
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")
    except (PermissionError, ValueError) as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.get("/v1/sandboxes/{sandbox_id}/stats")
def sandbox_stats(sandbox_id: str) -> dict:
    try:
        return _backend.stats(sandbox_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")


@app.post("/v1/sandboxes/{sandbox_id}/preserve")
def sandbox_preserve(sandbox_id: str, req: PreserveRequest) -> dict:
    try:
        return _backend.preserve(sandbox_id, reason=req.reason, actor=req.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")


@app.delete("/v1/sandbox/tasks/{sandbox_id}")
@app.delete("/v1/sandboxes/{sandbox_id}")
def sandbox_destroy(sandbox_id: str) -> dict:
    try:
        _backend.destroy(sandbox_id)
        return {"status": "destroyed", "sandbox_id": sandbox_id}
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")


@app.post("/v1/sandboxes/{sandbox_id}/hold")
@app.post("/v1/sandbox/tasks/{sandbox_id}/hold")
def sandbox_hold(sandbox_id: str, req: HoldRequest) -> dict:
    try:
        return _backend.hold(sandbox_id, ttl_seconds=req.ttl_seconds, reason=req.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")


@app.post("/v1/sandboxes/{sandbox_id}/preview")
@app.post("/v1/sandbox/tasks/{sandbox_id}/preview")
def sandbox_start_preview(sandbox_id: str, req: PreviewRequest, request: Request) -> dict:
    try:
        result = _backend.start_preview(
            sandbox_id, ttl_seconds=req.ttl_seconds, command=req.command
        )
        path = result["url"]
        return {**result, "url": str(request.base_url).rstrip("/") + path}
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.delete("/v1/sandboxes/{sandbox_id}/preview")
@app.delete("/v1/sandbox/tasks/{sandbox_id}/preview")
def sandbox_stop_preview(sandbox_id: str) -> dict:
    try:
        _backend.stop_preview(sandbox_id)
        return {"status": "stopped", "sandbox_task_id": sandbox_id}
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")


@app.api_route(
    "/v1/sandbox/tasks/{sandbox_id}/preview/web/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@app.api_route(
    "/v1/sandbox/tasks/{sandbox_id}/preview/web/",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def sandbox_preview_web(sandbox_id: str, request: Request, path: str = ""):
    try:
        status, content_type, content = _backend.proxy_preview(
            sandbox_id, path, method=request.method, query=request.url.query,
            body=await request.body(), content_type=request.headers.get("content-type", ""),
        )
        return Response(content=content, status_code=status, media_type=content_type)
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.websocket("/v1/sandbox/tasks/{sandbox_id}/preview/web/")
@app.websocket("/v1/sandbox/tasks/{sandbox_id}/preview/web/{path:path}")
async def sandbox_preview_websocket(websocket: WebSocket, sandbox_id: str, path: str = ""):
    await websocket.accept()
    try:
        target = _backend.preview_target(
            sandbox_id, path, query=websocket.url.query, websocket=True
        )
        import websockets
        async with websockets.connect(target) as upstream:
            async def client_to_upstream():
                while True:
                    message = await websocket.receive()
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    else:
                        return

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
