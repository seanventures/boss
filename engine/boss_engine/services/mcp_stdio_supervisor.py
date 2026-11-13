"""STDIO MCP SERVERS UNDER A SUPERVISOR (BOSSAPP-850).

Most MCP servers people actually use (filesystem, git, playwright, postgres, every `npx -y …`
server) speak JSON-RPC over stdin/stdout, and BOSS's client (mcp_client) speaks streamable
HTTP. The registry used to refuse anything that was not an http(s) URL. Now a server may be
registered as ``stdio://<project_id>/<template>`` and this module makes it an HTTP server:

* SPAWN, SANDBOXED. Each instance runs in its own container: ``docker run -i --rm --init``,
  the network the template declares (default ``none`` — the bridge is the container's own
  stdin/stdout pipes, so no network is needed to reach it), a read-only root with a /tmp tmpfs,
  ``nobody``, no capabilities, no-new-privileges, memory/CPU/PID limits, labelled
  ``boss.owner=mcp_stdio`` (the container_runner pattern). Only environment names the template
  allows reach it, and their values travel in the docker CLI's environment (``-e NAME``), never
  in argv. A server's own token — resolved by the registry at call time, exactly as for an HTTP
  server — becomes the env var the template names in ``env_from_auth``.
* BRIDGE. Each instance gets a loopback listener (127.0.0.1, ephemeral port) that accepts the
  streamable-HTTP POSTs mcp_client sends and writes them to the server's stdin, and an auth
  token of its own (``X-Boss-Bridge-Token``) so nothing else on the host can use it. The bridge
  initializes the server ONCE at spawn and answers every later ``initialize`` from that, and it
  renumbers request ids, so concurrent callers (mcp_client always sends id 1) never collide.
* RESOLVE. mcp_client hands any ``stdio://`` URL to :func:`resolve`, which returns the live
  instance's loopback URL and bridge header. The registry, policy, receipts and tool_call_audit
  see an ordinary HTTP MCP server — none of them changed.
* REUSE. Instances are keyed by template, launch hash (an edited template is a new instance)
  and, for ``reuse_key: credential``, the credential's fingerprint; ``template`` shares one
  instance across callers (only allowed when no credential reaches the server).
* HEALTH AND REAPING. A dead process is respawned on the next call; an instance idle past its
  template's ``idle_ttl_seconds`` is stopped by :func:`reap_tick`, which resource_reaper.tick
  calls every minute (the BOSSAPP-824 registration pattern). Containers whose creating process
  on this node is gone are removed by the same tick.

THE SUBPROCESS MODE IS TEST-ONLY. ``BOSS_MCP_STDIO_RUNTIME=subprocess`` runs the argv directly
(no container) so tests can drive a real stdio server without Docker. It refuses unless
``BOSS_MCP_STDIO_UNSANDBOXED_FOR_TESTS=1`` AND pytest is running a test.
"""
from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import httpx

from . import owned_process
from .mcp_stdio_templates import find_template, parse_stdio_url

_log = logging.getLogger("boss.mcp_stdio")

RUNTIME_ENV = "BOSS_MCP_STDIO_RUNTIME"
TEST_ONLY_FLAG = "BOSS_MCP_STDIO_UNSANDBOXED_FOR_TESTS"
BRIDGE_HEADER = "X-Boss-Bridge-Token"
PROTOCOL_VERSION = "2025-06-18"

#: How long the first handshake may take (a container start may pull an image and `npx` may
#: download the server).
START_TIMEOUT_SECONDS = float(os.environ.get("BOSS_MCP_STDIO_START_TIMEOUT", "180"))
#: How long one forwarded request may wait for the server's answer.
CALL_TIMEOUT_SECONDS = float(os.environ.get("BOSS_MCP_STDIO_CALL_TIMEOUT", "120"))
#: Biggest request body the bridge reads.
MAX_BODY_BYTES = 8 * 1024 * 1024
CONTAINER_MEMORY = os.environ.get("BOSS_MCP_STDIO_MEMORY", "1g")
CONTAINER_CPUS = os.environ.get("BOSS_MCP_STDIO_CPUS", "1")
CONTAINER_PIDS = os.environ.get("BOSS_MCP_STDIO_PIDS", "256")


class StdioUnavailable(httpx.ConnectError):
    """The stdio server could not be started or reached. A transport failure on purpose: the
    registry records the server unreachable with this sentence, as for a dead HTTP server."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ------------------------------------------------------------------------------------------ #
# runtime selection
# ------------------------------------------------------------------------------------------ #

def runtime() -> str:
    mode = (os.environ.get(RUNTIME_ENV) or "docker").strip().lower()
    if mode == "subprocess":
        if os.environ.get(TEST_ONLY_FLAG) != "1" or "PYTEST_CURRENT_TEST" not in os.environ:
            raise StdioUnavailable(
                "stdio MCP servers run only in a sandboxed container; the subprocess runtime "
                f"is for tests ({TEST_ONLY_FLAG}=1 under pytest)")
        return "subprocess"
    if mode != "docker":
        raise StdioUnavailable(f"{RUNTIME_ENV} must be docker (or subprocess in tests)")
    return "docker"


def _docker_binary(settings: Any) -> str:
    name = getattr(settings, "sandbox_docker_binary", "docker") or "docker"
    return shutil.which(name) or name


def _secret_from_headers(headers: Optional[dict[str, str]]) -> Optional[str]:
    """The one credential value the registry resolved for this server: a bearer token, else
    the single auth header's value."""
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() == "authorization":
            scheme, _, rest = str(value).partition(" ")
            return rest.strip() if scheme.lower() == "bearer" and rest.strip() else str(value)
    values = [str(v) for v in headers.values() if str(v).strip()]
    return values[0] if len(values) == 1 else None


def _substitute(argv: list[str], values: dict[str, str]) -> list[str]:
    out = []
    for arg in argv:
        for name, value in values.items():
            arg = arg.replace("${" + name + "}", value)
        out.append(arg)
    return out


def launch_plan(template: dict[str, Any], secret: Optional[str], *, mode: str,
                docker: str = "docker", name: str = "") -> tuple[list[str], dict[str, str]]:
    """(argv, env) for one instance. The env is what the spawned CLI process gets; in docker
    mode only the allowlisted names are forwarded into the container (by name)."""
    allow = [str(e) for e in template.get("env_allowlist") or []]
    values = {k: str(v) for k, v in (template.get("env_defaults") or {}).items() if k in allow}
    target = template.get("env_from_auth")
    if target and secret is not None and target in allow:
        values[target] = secret
    argv = _substitute([str(a) for a in template["argv"]],
                       {k: v for k, v in (template.get("env_defaults") or {}).items()})
    base_env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "LANG", "TZ") if k in os.environ}
    if mode == "subprocess":
        return argv, {**base_env, "HOME": tempfile.gettempdir(), **values}
    from .resource_ledger import node_id

    docker_env = {k: os.environ[k] for k in os.environ
                  if k.startswith("DOCKER_") or k in ("XDG_RUNTIME_DIR",)}
    full = [
        docker, "run", "-i", "--rm", "--init",
        "--label", "boss.owner=mcp_stdio",
        "--label", f"boss.mcp_template={template['id']}",
        "--label", f"boss.mcp_node={node_id()}",
        "--label", f"boss.mcp_pid={os.getpid()}",
        *(["--label", f"boss.install_id={os.environ['BOSS_INSTALL_ID']}"]
          if os.environ.get("BOSS_INSTALL_ID") else []),
        "--name", name or f"boss-mcp-{template['name']}-{secrets.token_hex(4)}",
        "--network", str(template.get("network") or "none"),
        "--memory", CONTAINER_MEMORY, "--cpus", CONTAINER_CPUS, "--pids-limit", CONTAINER_PIDS,
        "--read-only", "--tmpfs", "/tmp:rw,exec,size=512m",
        "--user", "65534:65534", "--env", "HOME=/tmp",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        *[x for k in sorted(values) for x in ("--env", k)],
        str(template["image"]), *argv,
    ]
    return full, {**base_env, "HOME": os.environ.get("HOME", "/tmp"), **docker_env, **values}


# ------------------------------------------------------------------------------------------ #
# one instance: the process and its bridge
# ------------------------------------------------------------------------------------------ #

class StdioInstance:
    """One running stdio server, its stdout reader and its loopback bridge."""

    def __init__(self, key: str, template: dict[str, Any], argv: list[str], env: dict[str, str],
                 *, mode: str, container: Optional[str], docker: str) -> None:
        self.key = key
        self.template = template
        self.mode = mode
        self.container = container
        self.docker = docker
        self.started_at = time.time()
        self.last_used = time.monotonic()
        self.calls = 0
        self.bridge_token = secrets.token_urlsafe(32)
        self.init_result: Optional[dict[str, Any]] = None
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1000
        self._stopped = False
        try:
            self.proc = owned_process.popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, sandboxed=False, keep_tree_on_exit=False)
        except (OSError, owned_process.OwnerCancelled) as exc:
            raise StdioUnavailable(f"could not start the stdio server: {exc}") from exc
        threading.Thread(target=self._read_stdout, name=f"mcp-stdio-out-{template['name']}",
                         daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-stdio-err-{template['name']}",
                         daemon=True).start()
        self._server = _BridgeServer(("127.0.0.1", 0), _BridgeHandler)
        self._server.instance = self
        self.port = int(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, name="mcp-stdio-bridge",
                         daemon=True).start()

    # -- the process side -------------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def alive(self) -> bool:
        return not self._stopped and self.proc.poll() is None

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    def _read_stdout(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        for raw in iter(stream.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.stderr_tail.append("stdout (not JSON): " + line[:300].decode("utf-8", "replace"))
                continue
            for item in message if isinstance(message, list) else [message]:
                self._dispatch(item)
        self._fail_pending("the stdio server exited")

    def _read_stderr(self) -> None:
        stream = self.proc.stderr
        assert stream is not None
        for raw in iter(stream.readline, b""):
            self.stderr_tail.append(raw.decode("utf-8", "replace").rstrip()[:500])

    def _dispatch(self, item: Any) -> None:
        if not isinstance(item, dict):
            return
        rid = item.get("id")
        if "method" in item:
            if rid is not None:
                # A request FROM the server (sampling, roots, elicitation): BOSS offers none of
                # those capabilities, so it says so instead of leaving the server waiting.
                self._write({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32601, "message": f"{item.get('method')} is not supported by BOSS"}})
            return
        with self._pending_lock:
            slot = self._pending.get(rid) if isinstance(rid, int) else None
        if slot is not None:
            slot["answer"] = item
            slot["event"].set()

    def _fail_pending(self, why: str) -> None:
        with self._pending_lock:
            slots = list(self._pending.values())
        for slot in slots:
            slot.setdefault("answer", {"jsonrpc": "2.0", "error": {"code": -32000,
                                                                   "message": why}})
            slot["event"].set()

    def _write(self, message: dict[str, Any]) -> None:
        stream = self.proc.stdin
        if stream is None:
            raise StdioUnavailable("the stdio server has no stdin")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        with self._write_lock:
            try:
                stream.write(data)
                stream.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise StdioUnavailable(f"the stdio server stopped reading: {exc}") from exc

    def request(self, method: str, params: Any, *, timeout: float = CALL_TIMEOUT_SECONDS
                ) -> dict[str, Any]:
        """Send one request with a fresh internal id; the server's whole answer message."""
        if not self.alive():
            raise StdioUnavailable("the stdio server is not running" + self._said())
        with self._pending_lock:
            self._next_id += 1
            rid = self._next_id
            slot: dict[str, Any] = {"event": threading.Event()}
            self._pending[rid] = slot
        try:
            message: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                message["params"] = params
            self._write(message)
            if not slot["event"].wait(timeout):
                return {"jsonrpc": "2.0", "error": {
                    "code": -32001, "message": f"the stdio server did not answer {method} "
                                               f"within {int(timeout)}s"}}
            return dict(slot["answer"])
        finally:
            with self._pending_lock:
                self._pending.pop(rid, None)
            self.touch()

    def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def handshake(self, timeout: float = START_TIMEOUT_SECONDS) -> None:
        answer = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "boss-stdio-bridge", "version": "1.0.0"}}, timeout=timeout)
        if "result" not in answer:
            error = answer.get("error") or {}
            raise StdioUnavailable("the stdio server did not initialize: "
                                   + str(error.get("message") or answer)[:300] + self._said())
        self.init_result = answer["result"]
        self.notify("notifications/initialized")

    def ping(self, timeout: float = 5.0) -> bool:
        try:
            return "result" in self.request("ping", None, timeout=timeout)
        except StdioUnavailable:
            return False

    def _said(self) -> str:
        tail = " | ".join(list(self.stderr_tail)[-3:])
        return f" (stderr: {tail[:400]})" if tail else ""

    def stop(self, reason: str = "stop") -> None:
        if self._stopped:
            return
        self._stopped = True
        self._fail_pending(f"the stdio server was stopped ({reason})")
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.end(reason=reason, grace=2.0)
        except Exception:  # noqa: BLE001
            pass
        if self.container:
            try:
                owned_process.run([self.docker, "rm", "-f", self.container],
                                  capture_output=True, text=True, timeout=30)
            except Exception:  # noqa: BLE001 — --rm and the closed stdin end it anyway
                pass

    def describe(self) -> dict[str, Any]:
        return {"key": self.key, "template_id": self.template["id"],
                "template": self.template["name"], "project_id": self.template["project_id"],
                "mode": self.mode, "container": self.container, "alive": self.alive(),
                "started_at": self.started_at, "idle_seconds": int(self.idle_seconds()),
                "idle_ttl_seconds": int(self.template.get("idle_ttl_seconds") or 600),
                "calls": self.calls,
                "server": (self.init_result or {}).get("serverInfo")}


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    instance: StdioInstance


class _BridgeHandler(BaseHTTPRequestHandler):
    """Streamable HTTP in, stdio JSON-RPC out. POST /mcp only; JSON answers (no SSE)."""

    server: _BridgeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # noqa: D401 — quiet
        return

    def _send(self, status: int, body: Any = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _authorized(self) -> bool:
        presented = self.headers.get(BRIDGE_HEADER) or ""
        return hmac.compare_digest(presented.encode(), self.server.instance.bridge_token.encode())

    def do_GET(self) -> None:  # noqa: N802 — no server-initiated stream
        self._send(405 if self._authorized() else 401)

    def do_DELETE(self) -> None:  # noqa: N802 — a session end is a no-op here
        self._send(200 if self._authorized() else 401)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/mcp":
            self._send(404)
            return
        if not self._authorized():
            self._send(401, {"error": "bridge token required"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413)
            return
        try:
            message = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}})
            return
        instance = self.server.instance
        instance.touch()
        if isinstance(message, list):
            answers = [a for a in (self._one(instance, m) for m in message) if a is not None]
            self._send(200, answers) if answers else self._send(202)
            return
        answer = self._one(instance, message)
        self._send(202) if answer is None else self._send(200, answer)

    @staticmethod
    def _one(instance: StdioInstance, message: Any) -> Optional[dict[str, Any]]:
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "invalid request"}}
        method, rid = message["method"], message.get("id")
        if rid is None:
            # The client's own `initialized` was already sent once, at spawn.
            if method != "notifications/initialized":
                try:
                    instance.notify(method, message.get("params"))
                except StdioUnavailable:
                    pass
            return None
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": instance.init_result or {}}
        instance.calls += 1
        try:
            answer = instance.request(method, message.get("params"))
        except StdioUnavailable as exc:
            answer = {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(exc)}}
        answer["id"] = rid
        answer["jsonrpc"] = "2.0"
        return answer


# ------------------------------------------------------------------------------------------ #
# the supervisor
# ------------------------------------------------------------------------------------------ #

class StdioSupervisor:
    """Every stdio instance this process runs, by reuse key."""

    def __init__(self) -> None:
        self._instances: dict[str, StdioInstance] = {}
        self._lock = threading.Lock()
        self._spawning: dict[str, threading.Lock] = {}
        self._last_reap = time.monotonic()

    def _template(self, settings: Any, project_id: str, name: str) -> dict[str, Any]:
        from ..db import get_engine

        try:
            with get_engine(settings).begin() as c:
                template = find_template(c, project_id, name)
        except Exception as exc:  # noqa: BLE001
            raise StdioUnavailable(f"could not read the stdio template: {exc}") from exc
        if template is None:
            raise StdioUnavailable(f"no stdio template '{name}' in project {project_id}")
        if template["status"] != "active":
            raise StdioUnavailable(f"the stdio template '{name}' is disabled")
        return template

    @staticmethod
    def reuse_key(template: dict[str, Any], secret: Optional[str]) -> str:
        fingerprint = "-"
        if template.get("reuse_key", "credential") == "credential" and secret is not None:
            fingerprint = hashlib.sha256(secret.encode()).hexdigest()[:16]
        return f"{template['id']}:{template['launch_hash']}:{fingerprint}"

    def acquire(self, settings: Any, url: str, headers: Optional[dict[str, str]] = None
                ) -> StdioInstance:
        parsed = parse_stdio_url(url)
        if parsed is None:
            raise StdioUnavailable(f"not a stdio server address: {url!r}")
        # The reaper thread runs in the API only; a worker's instances are reaped here, on use.
        if time.monotonic() - self._last_reap > 30:
            self._last_reap = time.monotonic()
            self.reap()
        template = self._template(settings, *parsed)
        secret = _secret_from_headers(headers)
        key = self.reuse_key(template, secret)
        with self._lock:
            gate = self._spawning.setdefault(key, threading.Lock())
        with gate:
            with self._lock:
                current = self._instances.get(key)
                if current is not None and current.alive():
                    current.touch()
                    return current
                stale = [k for k, i in self._instances.items()
                         if k == key or (i.template["id"] == template["id"]
                                         and i.template["launch_hash"] != template["launch_hash"])]
                gone = [self._instances.pop(k) for k in stale]
            for instance in gone:
                instance.stop("replaced")
            instance = self._spawn(settings, key, template, secret)
            with self._lock:
                self._instances[key] = instance
            return instance

    def _spawn(self, settings: Any, key: str, template: dict[str, Any],
               secret: Optional[str]) -> StdioInstance:
        mode = runtime()
        docker = _docker_binary(settings)
        container = None
        if mode == "docker":
            from .container_runner import docker_available

            ok, reason = docker_available(docker)
            if not ok:
                raise StdioUnavailable(f"Docker is not usable for stdio servers: {reason}")
            container = f"boss-mcp-{template['name']}-{secrets.token_hex(4)}"
        argv, env = launch_plan(template, secret, mode=mode, docker=docker, name=container or "")
        instance = StdioInstance(key, template, argv, env, mode=mode, container=container,
                                 docker=docker)
        try:
            instance.handshake()
        except BaseException:
            instance.stop("handshake failed")
            raise
        _log.info("stdio MCP server %s started (%s%s)", template["name"], mode,
                  f", container {container}" if container else "")
        return instance

    def resolve(self, settings: Any, url: str, headers: Optional[dict[str, str]] = None
                ) -> tuple[str, dict[str, str]]:
        instance = self.acquire(settings, url, headers)
        return instance.url, {BRIDGE_HEADER: instance.bridge_token}

    def reap(self, *, now: Optional[float] = None) -> dict[str, Any]:
        """Stop idle instances (past their template's TTL) and drop dead ones."""
        idle, dead = [], []
        with self._lock:
            for key, instance in list(self._instances.items()):
                ttl = int(instance.template.get("idle_ttl_seconds") or 600)
                if not instance.alive():
                    dead.append(self._instances.pop(key))
                elif instance.idle_seconds() > ttl:
                    idle.append(self._instances.pop(key))
        for instance in idle:
            instance.stop("idle")
        for instance in dead:
            instance.stop("exited")
        return {"stopped_idle": [i.template["name"] for i in idle],
                "dropped_dead": [i.template["name"] for i in dead],
                "running": len(self._instances)}

    def health(self, project_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._instances.values())
        return [i.describe() for i in items
                if project_id is None or i.template["project_id"] == project_id]

    def stop_all(self, reason: str = "shutdown") -> int:
        with self._lock:
            items = list(self._instances.values())
            self._instances.clear()
        for instance in items:
            instance.stop(reason)
        return len(items)


SUPERVISOR = StdioSupervisor()
atexit.register(lambda: SUPERVISOR.stop_all("exit"))


def resolve(settings: Any, url: str, headers: Optional[dict[str, str]] = None
            ) -> tuple[str, dict[str, str]]:
    """mcp_client's hook: the loopback URL and bridge header that reach this stdio server."""
    return SUPERVISOR.resolve(settings, url, headers)


def sweep_orphan_containers(binary: str = "docker") -> list[str]:
    """Containers this node started for stdio servers whose creating process is gone (a
    crashed API or worker). `--rm` plus the closed stdin end almost all of them; this is the
    backstop."""
    from .resource_ledger import node_id
    from .resource_reaper import _docker, docker_available

    if not docker_available(binary):
        return []
    fmt = '{{.ID}}\t{{.Label "boss.mcp_pid"}}'
    removed = []
    out = _docker(["ps", "-a", "--filter", "label=boss.owner=mcp_stdio",
                   "--filter", f"label=boss.mcp_node={node_id()}", "--format", fmt],
                  binary=binary)
    for line in out.splitlines():
        cid, _, pid = (line.split("\t") + ["", ""])[:3]
        if not cid or not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            os.kill(int(pid), 0)
            continue  # its creator is alive: its supervisor owns it
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        _docker(["rm", "-f", cid], binary=binary)
        removed.append(cid)
    return removed


def reap_tick() -> dict[str, Any]:
    """resource_reaper.tick's step: idle and dead instances, then orphaned containers."""
    report = SUPERVISOR.reap()
    try:
        report["orphan_containers"] = sweep_orphan_containers()
    except Exception as exc:  # noqa: BLE001
        report["orphan_containers"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return report
