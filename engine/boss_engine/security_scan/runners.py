"""THE SCANNERS, RUN IN THE SANDBOX (spec §4.2, §4.5).

Every command here is spawned through owned_process inside the scan's owner scope, so with a bound
sandbox identity (service.run_scan binds one) it runs AS THE RUN'S UID: no credentials, its own
HOME, network limited to loopback, the API and boss-egress (OSV reaches api.osv.dev through the
egress CONNECT proxy: D6). The checkout and the outputs live in the run's directory.

A tool that times out, exits with an error code, or writes something that does not parse is
`failed` with the reason. It never becomes "0 findings".
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..services import owned_process, toolchains
from . import parsers, rulepack
from .findings import Finding, ParseError

TOOLCHAIN = "security"
RULES_DIR = "/opt/toolchains/security/semgrep-rules"
RULES_DIR_ENV = "BOSS_SECURITY_RULES_DIR"
#: Seconds (spec §4.5), each overridable: BOSS_SECURITY_TIMEOUT_SEMGREP=900 etc.
DEFAULT_TIMEOUTS = {"semgrep": 600, "osv": 300, "zap_baseline": 600, "zap_api": 1800, "app_start": 300}
#: Code the inventory never lists (it is the dependency tree, not the app).
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "build", "dist", ".npm-cache",
              "__pycache__", ".gradle", ".m2", ".boss-scan"}

Exec = Callable[..., subprocess.CompletedProcess]


def timeout_for(key: str) -> int:
    raw = os.environ.get(f"BOSS_SECURITY_TIMEOUT_{key.upper()}")
    try:
        return max(10, int(raw)) if raw else DEFAULT_TIMEOUTS[key]
    except ValueError:
        return DEFAULT_TIMEOUTS[key]


def installed() -> dict[str, str]:
    """{"semgrep": "1.180.0", "semgrep_rules": "sha256:…", "osv_scanner": "2.6.0", "zap": "2.17.0"}"""
    return toolchains.installed().get(TOOLCHAIN, {})


def missing_message() -> str:
    return ("Security scanning needs the security scanners, which are not turned on for this BOSS. "
            "Turn them on with `boss toolchains enable security` on the BOSS host (or set "
            "BOSS_TOOLCHAINS=security in .env and run `boss update`), then scan again.")


def rules_dir() -> Path:
    return Path(os.environ.get(RULES_DIR_ENV) or RULES_DIR)


@dataclass
class ToolResult:
    name: str
    status: str = "done"                # done | failed | skipped
    version: str = ""
    rules_digest: Optional[str] = None
    image_digest: Optional[str] = None
    exit: Optional[int] = None
    duration_ms: int = 0
    error: Optional[str] = None
    truncated: bool = False
    findings: list[Finding] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        """What security_scans.tools keeps (spec §4.4): never the findings themselves."""
        out = {"name": self.name, "status": self.status, "version": self.version,
               "image_digest": self.image_digest or os.environ.get("BOSS_IMAGE_DIGEST") or None,
               "rules_digest": self.rules_digest, "exit": self.exit,
               "duration_ms": self.duration_ms, "findings": len(self.findings)}
        if self.error:
            out["error"] = self.error[:500]
        if self.truncated:
            out["truncated"] = True
        return out


def _tail(proc: subprocess.CompletedProcess, n: int = 400) -> str:
    text = ((proc.stderr or "") if isinstance(proc.stderr, str) else "") or \
           ((proc.stdout or "") if isinstance(proc.stdout, str) else "")
    from .redact import redact_text
    return redact_text(text.strip()[-n:])


def _run(exec_fn: Exec, argv: list[str], *, cwd: Path, timeout: int, env: Optional[dict] = None) -> tuple[Optional[subprocess.CompletedProcess], int, Optional[str]]:
    started = time.monotonic()
    try:
        proc = exec_fn(argv, cwd=str(cwd), timeout=timeout, capture_output=True, text=True, env=env)
    except subprocess.TimeoutExpired:
        return None, int((time.monotonic() - started) * 1000), f"timed out after {timeout}s"
    except (OSError, owned_process.OwnerCancelled) as exc:
        return None, int((time.monotonic() - started) * 1000), f"could not start: {exc}"
    return proc, int((time.monotonic() - started) * 1000), None


def _read(path: Path) -> bytes:
    if not path.is_file():
        raise ParseError(f"the tool wrote no output ({path.name})")
    from .findings import MAX_RAW_BYTES
    if path.stat().st_size > MAX_RAW_BYTES:
        raise ParseError(f"{path.name} is over the {MAX_RAW_BYTES}-byte limit")
    return path.read_bytes()


def tool_env(home: Path) -> dict[str, str]:
    """The tools' own environment: PATH with the toolchains, nothing of BOSS's. (The sandbox wrap
    then sets HOME/TMPDIR to the run's and adds the egress proxy.)"""
    from ..services.agent_env import agent_environment

    env = agent_environment(os.environ, {"SEMGREP_SEND_METRICS": "off",
                                         "SEMGREP_ENABLE_VERSION_CHECK": "0"})
    env = {k: v for k, v in env.items()
           if not re.search(r"(?i)(anthropic|openai|claude|token|secret|password|key)", k)}
    path = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    if "/opt/toolchains/bin" not in path.split(":"):
        path = f"{path}:/opt/toolchains/bin"
    env.update({"PATH": path, "HOME": str(home)})
    return env


# ---- Semgrep -----------------------------------------------------------------------------------

def run_semgrep(workdir: Path, outdir: Path, *, exec_fn: Exec = owned_process.run,
                env: Optional[dict] = None) -> ToolResult:
    res = ToolResult("semgrep", version=installed().get("semgrep", ""))
    rules = rules_dir()
    res.rules_digest = installed().get("semgrep_rules") or (rulepack.digest(rules) if rules.is_dir() else None)
    out = outdir / "semgrep.sarif"
    timeout = timeout_for("semgrep")
    argv = ["semgrep", "scan", "--config", str(rules), "--sarif", "--output", str(out),
            "--metrics=off", "--disable-version-check", "--quiet", "--timeout", "30",
            "--max-target-bytes", "1000000", "--exclude", ".boss-scan", "."]
    proc, res.duration_ms, err = _run(exec_fn, argv, cwd=workdir, timeout=timeout, env=env)
    if err:
        res.status, res.error = "failed", err
        return res
    res.exit = proc.returncode
    if proc.returncode not in (0, 1):       # 1 = findings with --error; anything else is an error
        res.status, res.error = "failed", f"semgrep exited {proc.returncode}: {_tail(proc)}"
        return res
    try:
        res.findings, meta = parsers.parse_semgrep_sarif(_read(out), root=workdir)
        res.version = res.version or meta.get("version") or ""
        res.truncated = bool(meta.get("truncated"))
    except ParseError as exc:
        res.status, res.error = "failed", str(exc)
    return res


# ---- OSV-Scanner -------------------------------------------------------------------------------

def run_osv(workdir: Path, outdir: Path, *, exec_fn: Exec = owned_process.run,
            env: Optional[dict] = None) -> ToolResult:
    res = ToolResult("osv", version=installed().get("osv_scanner", ""))
    out = outdir / "osv.json"
    argv = ["osv-scanner", "scan", "source", "--format", "json", "--output-file", str(out),
            "--recursive", "."]
    proc, res.duration_ms, err = _run(exec_fn, argv, cwd=workdir, timeout=timeout_for("osv"), env=env)
    if err:
        res.status, res.error = "failed", err
        return res
    res.exit = proc.returncode
    if proc.returncode == 128:              # "no package sources found": nothing to audit
        res.error = "no lockfile or manifest found; nothing to audit"
        return res
    if proc.returncode not in (0, 1):       # 1 = vulnerabilities found
        res.status, res.error = "failed", f"osv-scanner exited {proc.returncode}: {_tail(proc)}"
        return res
    try:
        res.findings, meta = parsers.parse_osv_json(_read(out), root=workdir)
        res.truncated = bool(meta.get("truncated"))
    except ParseError as exc:
        res.status, res.error = "failed", str(exc)
    return res


# ---- the app, started inside the scan's sandbox (ZAP target `sandbox`) -------------------------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3):        # noqa: S310 — loopback only
            return True
    except urllib.error.HTTPError:
        return True                          # any HTTP answer means it is serving
    except (urllib.error.URLError, OSError, ValueError):
        return False


class AppDidNotStart(RuntimeError):
    pass


def start_app(workdir: Path, log_path: Path) -> tuple[owned_process.OwnedPopen, str]:
    """Start the app the way its preview starts (preview_service.how_app_runs), as the run's uid,
    on a loopback port: nothing outside this container can reach it."""
    from ..services import preview_service

    run = preview_service.how_app_runs(workdir)
    if run is None or not run.command:
        raise AppDidNotStart("could not tell how this app runs (declare boss.preview.command)")
    port = free_port()
    env = preview_service.app_environment(workdir, port)
    env["HOST"] = "127.0.0.1"
    env["PATH"] = tool_env(workdir)["PATH"]
    log = open(log_path, "w")
    try:
        proc = owned_process.popen(run.command, cwd=str(workdir), env=env, stdout=log,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    finally:
        log.close()
    url = f"http://127.0.0.1:{port}"
    health = url + preview_service.preview_health_path(workdir)
    deadline = time.monotonic() + timeout_for("app_start")
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log_path.read_text(errors="replace")[-400:] if log_path.exists() else ""
            from .redact import redact_text
            raise AppDidNotStart(f"the app exited ({proc.returncode}) before serving: {redact_text(tail)}")
        if _answers(health):
            return proc, url
        time.sleep(1.0)
    proc.end(reason="security_scan_app_start_timeout", grace=2.0)
    raise AppDidNotStart(f"the app did not serve HTTP within {timeout_for('app_start')}s")


# ---- ZAP ---------------------------------------------------------------------------------------

def _find_openapi(workdir: Path) -> Optional[Path]:
    for name in ("openapi.json", "openapi.yaml", "openapi.yml", "swagger.json",
                 "api/openapi.json", "api/openapi.yaml", "docs/openapi.yaml", "docs/openapi.json"):
        p = workdir / name
        if p.is_file():
            return p
    return None


def zap_plan(target: str, outdir: Path, *, mode: str, openapi: Optional[Path],
             active_minutes: int) -> dict[str, Any]:
    """An Automation Framework plan: the target is the ONLY in-scope URL prefix."""
    scope = re.escape(target.rstrip("/")) + "(/.*)?$"
    jobs: list[dict[str, Any]] = [
        {"type": "passiveScan-config", "parameters": {"maxAlertsPerRule": 10, "scanOnlyInScope": True}},
        {"type": "spider", "parameters": {"context": "boss", "maxDuration": 2}},
    ]
    if mode == "api":
        api = {"context": "boss", "targetUrl": target}
        if openapi is not None:
            api["apiFile"] = str(openapi)
        else:
            api["apiUrl"] = target.rstrip("/") + "/openapi.json"
        jobs.append({"type": "openapi", "parameters": api})
    jobs.append({"type": "passiveScan-wait", "parameters": {"maxDuration": 5}})
    if mode == "api":
        jobs.append({"type": "activeScan", "parameters": {"context": "boss",
                                                          "maxScanDurationInMins": active_minutes}})
    jobs += [
        {"type": "export", "parameters": {"context": "boss", "type": "url", "source": "all",
                                          "fileName": str(outdir / "zap-urls.txt")}},
        {"type": "report", "parameters": {"template": "traditional-json", "reportDir": str(outdir),
                                          "reportFile": "zap"}},
    ]
    return {"env": {"contexts": [{"name": "boss", "urls": [target], "includePaths": [scope]}],
                    "parameters": {"failOnError": True, "failOnWarning": False,
                                   "progressToStdout": True}},
            "jobs": jobs}


def run_zap(target: str, workdir: Path, outdir: Path, *, mode: str = "baseline",
            exec_fn: Exec = owned_process.run, env: Optional[dict] = None) -> ToolResult:
    res = ToolResult("zap", version=installed().get("zap", ""))
    key = "zap_api" if mode == "api" else "zap_baseline"
    timeout = timeout_for(key)
    plan = outdir / "zap-plan.json"
    plan.write_text(json.dumps(zap_plan(target, outdir, mode=mode, openapi=_find_openapi(workdir),
                                        active_minutes=max(1, timeout // 60 - 2))))
    os.chmod(plan, 0o644)
    argv = ["zap.sh", "-cmd", "-silent", "-host", "127.0.0.1", "-port", str(free_port()),
            "-dir", str(outdir / "zap-home"), "-autorun", str(plan)]
    proc, res.duration_ms, err = _run(exec_fn, argv, cwd=outdir, timeout=timeout, env=env)
    if err:
        res.status, res.error = "failed", err
        return res
    res.exit = proc.returncode
    if proc.returncode != 0:
        res.status, res.error = "failed", f"zap exited {proc.returncode}: {_tail(proc)}"
        return res
    try:
        res.findings, meta = parsers.parse_zap_json(_read(outdir / "zap.json"))
        res.version = res.version or meta.get("version") or ""
        res.truncated = bool(meta.get("truncated"))
    except ParseError as exc:
        res.status, res.error = "failed", str(exc)
        return res
    from .fingerprints import path_template
    urls = outdir / "zap-urls.txt"
    if urls.is_file():
        res.routes = sorted({path_template(u.strip()) for u in
                             urls.read_text(errors="replace").splitlines()[:20000] if u.strip()})
    return res


# ---- inventory (what exists in this scan: a gone finding is "removed", not "fixed") ------------

def code_inventory(workdir: Path, limit: int = 20000) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workdir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            out.append(str((Path(dirpath) / name).relative_to(workdir)).replace(os.sep, "/"))
            if len(out) >= limit:
                return sorted(out)
    return sorted(out)
