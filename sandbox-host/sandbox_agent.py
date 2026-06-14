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

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
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
                    and now - r.updated_at > IDLE_TTL_SECONDS]:
            try:
                self.destroy(sid)
            except Exception:
                pass

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

        # Run the in-guest agent inside the live container.
        ex = self._docker(["exec", cid, "python3", "/opt/boss/in_guest_agent.py"],
                          timeout=spec.get("timeout", 1200) + 180)
        result_path = work / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else \
            {"status": "error", "error": f"no result (exit {ex.returncode}): {(ex.stderr or '')[:300]}"}
        rec.status = "idle" if result.get("status") == "ok" else "failed"
        rec.updated_at = time.time()
        rec.meta = {k: result.get(k) for k in ("engine", "summary", "diff_summary", "tests", "tests_passed")}
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
        return rec.summary()

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


@app.delete("/v1/sandboxes/{sandbox_id}")
def sandbox_destroy(sandbox_id: str) -> dict:
    try:
        _backend.destroy(sandbox_id)
        return {"status": "destroyed", "sandbox_id": sandbox_id}
    except KeyError:
        raise HTTPException(status_code=404, detail="sandbox not found")
