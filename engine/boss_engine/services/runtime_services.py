"""Workspace runtime services during a build (BOSSAPP-856).

A preview is one process with a TTL. An app with a database AND a worker AND an API cannot be
run that way, and an agent mid-build had no way to start the app it is writing and test against
it. So a repository may declare, in boss.verify.yaml:

    runtime:
      provision: ["npm ci"]                 # once, before any service starts (argv, no shell)
      teardown:  ["node scripts/drop.js"]   # once, when the build ends, before services stop
      services:
        - name: api
          run: "node server.js"             # argv, no shell; PORT is set for it
          port: auto                        # or a number
          depends_on: [worker]
          health: {http: /healthz}          # or {tcp: true}; none = still running after 1s
          ready_seconds: 60
          env: {LOG_LEVEL: debug}
          start: auto                       # or on_demand (an agent starts it when it needs it)
        - name: worker
          run: "node worker.js"

(`services:` at the top level is the build's throwaway Postgres — build_services — and keeps
meaning that; this is a separate key.)

Every service is supervised as a process the BUILD owns (owned_process, the run's owner scope),
so Stop ends it and nothing outlives the run; dependencies start first and stop last. Each
service sees PORT and, for every service, BOSS_SERVICE_<NAME>_PORT / _URL. Each start, stop,
restart, provision and teardown is written to the supervisor's operation log and as a
`code_task.runtime_service` event. Agents manage them mid-build with the run-scoped
`runtime_service` tool on the run's boss_run gateway server (status/start/stop/restart/logs).

A repository that declares no `runtime:` gets no supervisor, no tool and no events: the build is
unchanged.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ACTIONS = ("status", "start", "stop", "restart", "logs")
EVENT = "code_task.runtime_service"
DEFAULT_READY_SECONDS = 60
COMMAND_TIMEOUT_SECONDS = 900


class RuntimeServicesError(ValueError):
    """A declaration or a request the caller can fix, phrased for them."""


@dataclass(frozen=True)
class RuntimeService:
    name: str
    run: tuple[str, ...]
    port: Optional[int] = None          # None = allocate one
    depends_on: tuple[str, ...] = ()
    health_http: str = ""
    health_tcp: bool = False
    ready_seconds: int = DEFAULT_READY_SECONDS
    env: tuple[tuple[str, str], ...] = ()
    on_demand: bool = False
    cwd: str = "."


@dataclass(frozen=True)
class RuntimeSpec:
    services: tuple[RuntimeService, ...] = ()
    provision: tuple[str, ...] = ()
    teardown: tuple[str, ...] = ()

    def order(self) -> list[RuntimeService]:
        """Dependencies first (validated acyclic at parse)."""
        by_name = {s.name: s for s in self.services}
        out: list[RuntimeService] = []
        seen: set[str] = set()

        def visit(s: RuntimeService) -> None:
            if s.name in seen:
                return
            seen.add(s.name)
            for dep in s.depends_on:
                visit(by_name[dep])
            out.append(s)

        for s in self.services:
            visit(s)
        return out


def parse_runtime(value: Any, where: str = "boss.verify.yaml: runtime") -> Optional[RuntimeSpec]:
    """The `runtime:` block, validated. None when absent."""
    if value is None:
        return None
    from .verify_manifest import VerifyManifestError, _commands

    def fail(msg: str):
        raise VerifyManifestError(f"{where}{msg}")

    if not isinstance(value, dict):
        fail(" must be a mapping with services, provision and teardown")
    extra = set(value) - {"services", "provision", "teardown"}
    if extra:
        fail(f": unknown key(s) {sorted(extra)}")
    raw_services = value.get("services") or []
    if not isinstance(raw_services, list):
        fail(".services must be a list")
    services: list[RuntimeService] = []
    names: set[str] = set()
    for i, raw in enumerate(raw_services):
        here = f".services[{i}]"
        if not isinstance(raw, dict):
            fail(f"{here} must be a mapping")
        extra = set(raw) - {"name", "run", "port", "depends_on", "health", "ready_seconds",
                            "env", "start", "cwd"}
        if extra:
            fail(f"{here}: unknown key(s) {sorted(extra)}")
        name = str(raw.get("name") or "").strip()
        if not _NAME.match(name):
            fail(f"{here}.name must be lower-case letters, digits or '-', starting with a letter")
        if name in names:
            fail(f"{here}: service name {name!r} is used twice")
        names.add(name)
        run = _commands(raw.get("run"), f"{where}{here}.run")
        if len(run) != 1:
            fail(f"{here}.run must be one command")
        port = raw.get("port", "auto")
        if port == "auto" or port is None:
            port_value = None
        elif isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
            port_value = port
        else:
            fail(f"{here}.port must be 'auto' or a port from 1024 to 65535")
        deps = raw.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            fail(f"{here}.depends_on must be a list of service names")
        health = raw.get("health") or {}
        if not isinstance(health, dict) or set(health) - {"http", "tcp"}:
            fail(f"{here}.health must be {{http: /path}} or {{tcp: true}}")
        http = str(health.get("http") or "").strip()
        if http and not http.startswith("/"):
            fail(f"{here}.health.http must be a path starting with /")
        ready = raw.get("ready_seconds", DEFAULT_READY_SECONDS)
        if not isinstance(ready, int) or isinstance(ready, bool) or not 1 <= ready <= 1800:
            fail(f"{here}.ready_seconds must be 1..1800")
        env = raw.get("env") or {}
        if not isinstance(env, dict) or not all(
                re.match(r"^[A-Z_][A-Z0-9_]*$", str(k)) for k in env):
            fail(f"{here}.env must map UPPER_CASE names to values")
        start = str(raw.get("start") or "auto")
        if start not in ("auto", "on_demand"):
            fail(f"{here}.start must be auto or on_demand")
        cwd = str(raw.get("cwd") or ".").strip().strip("/") or "."
        if Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            fail(f"{here}.cwd must be a directory inside the repository")
        services.append(RuntimeService(
            name=name, run=tuple(shlex.split(run[0])), port=port_value,
            depends_on=tuple(d.strip() for d in deps), health_http=http,
            health_tcp=bool(health.get("tcp")), ready_seconds=ready,
            env=tuple(sorted((str(k), str(v)) for k, v in env.items())),
            on_demand=start == "on_demand", cwd=cwd))
    for s in services:
        for dep in s.depends_on:
            if dep not in names:
                fail(f": {s.name} depends on {dep!r}, which is not declared")
    # No cycles: walk each service's dependencies.
    graph = {s.name: s.depends_on for s in services}

    def cyc(n: str, path: tuple[str, ...]) -> None:
        if n in path:
            fail(f": dependency cycle {' -> '.join(path + (n,))}")
        for d in graph[n]:
            cyc(d, path + (n,))

    for n in graph:
        cyc(n, ())
    return RuntimeSpec(services=tuple(services),
                       provision=_commands(value.get("provision"), f"{where}.provision"),
                       teardown=_commands(value.get("teardown"), f"{where}.teardown"))


# ── the supervisor ────────────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _env_name(name: str) -> str:
    return name.upper().replace("-", "_")


def _healthy(service: RuntimeService, port: int) -> bool:
    if service.health_http:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{service.health_http}",
                                        timeout=2) as resp:
                return int(getattr(resp, "status", 200)) < 500
        except urllib.error.HTTPError as exc:
            return exc.code < 500
        except (urllib.error.URLError, OSError):
            return False
    if service.health_tcp:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False
    return True


@dataclass
class _Running:
    proc: Any
    port: int
    log_path: str
    started_at: float


@dataclass
class RuntimeSupervisor:
    run_id: str
    root: Path
    spec: RuntimeSpec
    owner: Any = None
    emit: Optional[Callable[[dict], None]] = None
    log_dir: str = ""
    ports: dict[str, int] = field(default_factory=dict)
    running: dict[str, _Running] = field(default_factory=dict)
    operations: list[dict] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if not self.log_dir:
            self.log_dir = tempfile.mkdtemp(prefix=f"boss-runtime-{self.run_id}-")
        for s in self.spec.services:
            self.ports[s.name] = s.port or _free_port()

    # -- helpers -------------------------------------------------------------------------------
    def _service(self, name: str) -> RuntimeService:
        for s in self.spec.services:
            if s.name == name:
                return s
        raise RuntimeServicesError(
            f"no runtime service {name!r}; declared: {[s.name for s in self.spec.services]}")

    def _record(self, op: str, service: Optional[str], ok: bool, detail: str = "") -> dict:
        entry = {"at": time.time(), "op": op, "service": service, "ok": ok,
                 "detail": detail[:500]}
        self.operations.append(entry)
        if self.emit is not None:
            try:
                self.emit(entry)
            except Exception:  # noqa: BLE001 — telling never fails the operation
                _log.debug("runtime service event not recorded", exc_info=True)
        return entry

    def env_for(self, service: Optional[RuntimeService] = None) -> dict[str, str]:
        from .agent_env import agent_environment

        env = agent_environment(os.environ)
        env.setdefault("HOME", os.environ.get("HOME", ""))
        for name, port in self.ports.items():
            env[f"BOSS_SERVICE_{_env_name(name)}_PORT"] = str(port)
            env[f"BOSS_SERVICE_{_env_name(name)}_URL"] = f"http://127.0.0.1:{port}"
        if service is not None:
            env.update(dict(service.env))
            env["PORT"] = str(self.ports[service.name])
            env["HOST"] = "127.0.0.1"
        return env

    def _command(self, argv_text: str, op: str) -> None:
        from . import owned_process

        try:
            done = owned_process.run(shlex.split(argv_text), cwd=str(self.root),
                                     env=self.env_for(), capture_output=True, text=True,
                                     timeout=COMMAND_TIMEOUT_SECONDS, owner=self.owner)
        except subprocess.TimeoutExpired:
            self._record(op, None, False, f"{argv_text}: timed out")
            raise RuntimeServicesError(f"{op}: {argv_text!r} did not finish in "
                                       f"{COMMAND_TIMEOUT_SECONDS}s") from None
        except FileNotFoundError as exc:
            self._record(op, None, False, f"{argv_text}: {exc}")
            raise RuntimeServicesError(f"{op}: {argv_text!r} could not start: {exc}") from None
        ok = done.returncode == 0
        said = ((done.stdout or "") + (done.stderr or "")).strip()[-400:]
        self._record(op, None, ok, f"{argv_text}: exit {done.returncode} {said}")
        if not ok:
            raise RuntimeServicesError(f"{op}: {argv_text!r} exited {done.returncode}: {said}")

    # -- lifecycle -----------------------------------------------------------------------------
    def provision(self) -> None:
        for command in self.spec.provision:
            self._command(command, "provision")

    def start_all(self) -> None:
        for s in self.spec.order():
            if not s.on_demand:
                self.start(s.name)

    def alive(self, name: str) -> bool:
        r = self.running.get(name)
        return r is not None and r.proc.poll() is None

    def start(self, name: str) -> dict[str, Any]:
        from . import owned_process

        with self._lock:
            service = self._service(name)
            for dep in service.depends_on:
                if not self.alive(dep):
                    self.start(dep)
            if self.alive(name):
                return self.describe(name)
            port = self.ports[name]
            log_path = os.path.join(self.log_dir, f"{name}.log")
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                proc = owned_process.popen(
                    list(service.run), cwd=str((self.root / service.cwd)), env=self.env_for(service),
                    stdout=fd, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    owner=self.owner)
            except (OSError, owned_process.OwnerCancelled) as exc:
                self._record("start", name, False, str(exc))
                raise RuntimeServicesError(f"{name} could not start: {exc}") from None
            finally:
                os.close(fd)
            self.running[name] = _Running(proc, port, log_path, time.time())
            deadline = time.monotonic() + service.ready_seconds
            # With no health check, "ready" means it is still running after a short settle: a
            # service that exits at once must not be reported started.
            settle = (time.monotonic() + min(1.0, service.ready_seconds)
                      if not (service.health_http or service.health_tcp) else 0.0)
            while True:
                if proc.poll() is not None:
                    self.running.pop(name, None)
                    self._record("start", name, False, f"exited {proc.returncode} before ready")
                    raise RuntimeServicesError(
                        f"{name} exited {proc.returncode} before it was ready: "
                        f"{self.logs(name, 20)[-400:]}")
                if time.monotonic() >= settle and _healthy(service, port):
                    break
                if time.monotonic() >= deadline:
                    self.stop(name, record=False)
                    self._record("start", name, False, f"not healthy in {service.ready_seconds}s")
                    raise RuntimeServicesError(f"{name} was not healthy within "
                                               f"{service.ready_seconds}s")
                time.sleep(0.2)
            self._record("start", name, True, f"port {port}")
            return self.describe(name)

    def stop(self, name: str, *, record: bool = True) -> dict[str, Any]:
        with self._lock:
            self._service(name)
            # Whatever depends on it stops first.
            for s in self.spec.services:
                if name in s.depends_on and self.alive(s.name):
                    self.stop(s.name)
            r = self.running.pop(name, None)
            if r is not None and r.proc.poll() is None:
                r.proc.end(reason="runtime_service_stop", grace=3.0)
            if record:
                self._record("stop", name, True)
            return {"name": name, "running": False}

    def restart(self, name: str) -> dict[str, Any]:
        with self._lock:
            self.stop(name, record=False)
            self._record("restart", name, True)
            return self.start(name)

    def logs(self, name: str, lines: int = 80) -> str:
        r = self.running.get(name)
        path = r.log_path if r else os.path.join(self.log_dir, f"{name}.log")
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        from .build_services import scrub

        return scrub("\n".join(text.splitlines()[-max(1, min(int(lines), 500)):]))

    def describe(self, name: str) -> dict[str, Any]:
        s = self._service(name)
        return {"name": name, "running": self.alive(name), "port": self.ports[name],
                "url": f"http://127.0.0.1:{self.ports[name]}", "depends_on": list(s.depends_on),
                "on_demand": s.on_demand}

    def status(self) -> dict[str, Any]:
        return {"services": [self.describe(s.name) for s in self.spec.services],
                "operations": self.operations[-20:]}

    def teardown(self) -> None:
        """The teardown commands (while the services still run), then every service, dependents
        first. Never raises: the build is ending either way."""
        for command in self.spec.teardown:
            try:
                self._command(command, "teardown")
            except RuntimeServicesError as exc:
                _log.warning("runtime teardown for %s: %s", self.run_id, exc)
        for s in reversed(self.spec.order()):
            try:
                if s.name in self.running:
                    self.stop(s.name)
            except Exception:  # noqa: BLE001
                _log.warning("runtime service %s did not stop cleanly", s.name, exc_info=True)

    def brief(self) -> str:
        names = ", ".join(f"{s.name} (port {self.ports[s.name]}"
                          f"{', on demand' if s.on_demand else ''})" for s in self.spec.services)
        return ("RUNTIME SERVICES: this build runs the app's declared services next to your "
                f"workspace: {names}. Each is reachable at http://127.0.0.1:<port>, and its "
                "address is in BOSS_SERVICE_<NAME>_URL. Use the `runtime_service` tool to see "
                "their status and logs, or to start, stop or restart one while you test.")

    # -- the agent's tool ----------------------------------------------------------------------
    def act(self, action: str, name: str = "", lines: int = 80) -> dict[str, Any]:
        if action not in ACTIONS:
            raise RuntimeServicesError(f"action must be one of {list(ACTIONS)}")
        if action == "status":
            return self.status() if not name else self.describe(name)
        if not name:
            raise RuntimeServicesError(f"{action} needs a service name")
        if action == "logs":
            return {"name": name, "logs": self.logs(name, lines)}
        return getattr(self, action)(name)


_SUPERVISORS: dict[str, RuntimeSupervisor] = {}
_REG_LOCK = threading.Lock()


def for_run(run_id: Optional[str]) -> Optional[RuntimeSupervisor]:
    if not run_id:
        return None
    with _REG_LOCK:
        return _SUPERVISORS.get(run_id)


def start_for_run(engine: Any, run_id: str, project_id: str, manifest: Any, root: Any, *,
                  use_microvm: bool = False) -> Optional[RuntimeSupervisor]:
    """Provision and start a build's declared runtime services, or None when it declares none."""
    spec = getattr(manifest, "runtime", None)
    if spec is None or not (spec.services or spec.provision or spec.teardown):
        return None
    if use_microvm:
        raise RuntimeServicesError(
            "this run executes in a microVM, which cannot reach services BOSS supervises on the "
            "host. Run it on the container sandbox, or remove runtime: from boss.verify.yaml")
    from . import owned_process

    def emit(entry: dict) -> None:
        if engine is None:
            return
        from .core import emit_event

        with engine.begin() as c:
            emit_event(c, project_id, EVENT, "boss", "code_task_run", run_id, entry)

    sup = RuntimeSupervisor(run_id=run_id, root=Path(root), spec=spec,
                            owner=owned_process.current_owner(), emit=emit)
    with _REG_LOCK:
        _SUPERVISORS[run_id] = sup
    try:
        sup.provision()
        sup.start_all()
    except Exception:
        teardown_run(run_id)
        raise
    return sup


def teardown_run(run_id: str) -> None:
    with _REG_LOCK:
        sup = _SUPERVISORS.pop(run_id, None)
    if sup is not None:
        sup.teardown()
