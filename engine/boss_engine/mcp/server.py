"""MCP server exposing the BOSS engine tool layer to external agents.

This is how a GitHub Copilot agent, Claude, Cursor, or any MCP client becomes
"the BOSS Agent": it speaks MCP to this server, whose tools are exactly the
engine's tool layer (`boss_engine.tools`). Same tools the engine-native agent
uses — one source of truth.

The protocol logic here is **framework-agnostic** (pure dict in / dict out), so
the pure OSS engine keeps its "no HTTP framework" property. Two transports:

  * `serve_stdio()`  — the standard MCP transport, stdlib only (recommended;
    this is what Claude/Copilot MCP clients launch).
  * `asgi_app()`     — an optional HTTP/JSON-RPC transport that lazily imports
    FastAPI, matching the house style of the servers in `tools/`. Only import
    this if you actually want HTTP; it is never required by the engine.

Implements MCP JSON-RPC 2.0 on three revisions (AP-P5, D10; mcp/protocol.py):

  * 2024-11-05 (and 2025-11-25, 2025-06-18, 2025-03-26), legacy: initialize,
    notifications/initialized, ping, tools/list, tools/call — unchanged for existing clients;
  * 2026-07-28, stateless: server/discover, every request versioned in `_meta`, resultType on
    every result, deterministic tools/list with ttlMs/cacheScope, long-running tools as tasks
    (`io.modelcontextprotocol/tasks`: tasks/get, tasks/update, tasks/cancel — mcp/tasks.py),
    a person's answer asked for as `input_required` (MRTR) and validated as the caller
    (services/protocol_answers.py: decision:approve with step-up), `traceparent` from `_meta`.
"""
# NOTE: deliberately NO `from __future__ import annotations` here. `asgi_app()`
# lazily imports FastAPI's `Request` inside the function; with postponed
# annotations FastAPI would see the string "Request", fail to resolve it in
# module globals, silently treat the parameter as a query param, and 422 every
# JSON-RPC POST. Evaluating annotations at def-time keeps the lazy import valid.
import contextvars
from dataclasses import dataclass
import json
import hmac
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit

from .. import __version__
from ..tools import ToolRegistry, build_default_registry
from . import protocol as _protocol

#: The revision an `initialize` that names none gets (unchanged); every revision the server
#: speaks is `protocol.SUPPORTED` (AP-P5, D10: 2024-11-05, 2025-11-25, 2026-07-28).
PROTOCOL_VERSION = _protocol.LEGACY_DEFAULT
SERVER_INFO = {"name": "boss-engine", "version": __version__}
_UNSCOPED_SQL_TOOLS = frozenset({"query_db", "write_db"})
#: The run filesystem's tools (AP-P6, kernel/runfs.py) act on a kernel run's own files; an MCP
#: client has no run, so they are never listed here.
from ..kernel.runfs import RUN_FS_TOOLS as _RUN_FS_TOOLS  # noqa: E402
#: AP-P7: the sub-agent tools and write_todos are a coordinating run's own; never listed here.
from ..kernel.subagents import RUN_ONLY_TOOLS as _RUN_ONLY_TOOLS  # noqa: E402
#: KG P6a: the only WRITE tools `MCPServer.handle_external_agent_call` will ever dispatch to.
_EXTERNAL_AGENT_WRITE_TOOLS = frozenset({"kg_write_agent_decision", "kg_write_coverage_gap"})
#: KG P6a: the READ tools an external-agent token may use to read the evidence its contract
#: grants ("reads within scope"). Each is bounded by the token's contract-derived policy — only
#: the contract's read kinds, at most `max_nodes` rows — and refused in words for anything else.
_EXTERNAL_AGENT_READ_TOOLS = frozenset({"kg_find", "kg_get_node"})
#: The prefix of a per-execution external-agent KG token (`external_agent_tokens.py`): a
#: deliberately separate credential system, routed around `authenticate_transport` entirely.
_EXTERNAL_AGENT_TOKEN_PREFIX = "boss_ext_"

# Shown to the client model at initialize (MCP `instructions`). This is what
# keeps external agents (Copilot/Claude/Cursor) on BOSS's governed workflow
# instead of improvising: kickoffs go through the PRD meeting, not straight to
# code, and artifacts (agendas/minutes/PRDs) are produced BY BOSS, not by the
# client writing files.
SERVER_INSTRUCTIONS = (
    "BOSS is a governed AI-labor control plane. Work flows: idea -> "
    "request_meeting (agent panel debates, produces minutes + PRD inside BOSS) "
    "-> human PRD approval -> code task auto-starts -> tested, gated PR. "
    "Rules for you as the client agent: (1) To kick off ANY new product/feature "
    "idea — including requests phrased as 'build X' — call request_meeting with "
    "the directive; do not call create_code_task first, and do not author "
    "agenda/minutes/PRD documents yourself: BOSS creates those and they appear "
    "in the console's Meetings area. (2) Use create_code_task only for a "
    "direct, already-specified engineering change that needs no PRD. (3) Use "
    "create_discovery when the idea first needs web research/evidence. (4) "
    "Everything you start is inspectable: list_meetings, list_code_tasks, "
    "get_code_task, project_status."
)


#: The identity the HTTP transport authenticated for the request being handled (an AuthContext).
_REQUEST_AUTH: contextvars.ContextVar[Any] = contextvars.ContextVar("boss_mcp_request_auth", default=None)
#: An HTTP request the transport admitted WITHOUT an identity (the legacy shared token): its tool
#: calls carry none, and never fall back to the stdio session identity.
_NO_IDENTITY = object()
#: The `Authorization` value the HTTP request presented, when it verified as a BOSS identity: the
#: API-surface tools forward exactly this to the API (M8), never the server's own key.
_REQUEST_CREDENTIAL: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "boss_mcp_request_credential", default=None)


@dataclass(frozen=True)
class DatabaseConfig:
    """Where BOSS's MCP server resolved its database URL from (BOSSAPP-991)."""

    url: str
    source: str
    configured: bool
    env_file: Optional[str] = None

    @property
    def sanitized_target(self) -> str:
        return sanitize_database_url(self.url)


def _parse_dotenv_file(path: Path) -> dict[str, str]:
    """Read key=value entries from a `.env` file without mutating `os.environ`."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif "#" in val:
            val = val.split("#", 1)[0].strip()
        out[key] = val
    return out


def _normalize_pg_url(url: str) -> str:
    raw = str(url or "").strip()
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw[len("postgres://"):]
    return raw


def _compose_database_url(values: Mapping[str, str]) -> Optional[tuple[str, str]]:
    """Build a host-reachable DB URL from Community compose `.env` vars (BOSSAPP-991).

    `scripts/install/community-compose.yml` publishes Postgres on
    `127.0.0.1:${BOSS_POSTGRES_PORT:-5432}` with `POSTGRES_USER=boss`,
    `POSTGRES_DB=boss`, and `BOSS_DB_PASSWORD`. A host process (like MCP on a laptop)
    that reads the install `.env` needs that URL rather than the hardcoded `boss:boss@localhost:5432`.
    """
    password = (values.get("BOSS_DB_PASSWORD") or values.get("POSTGRES_PASSWORD") or "").strip()
    port = (values.get("BOSS_POSTGRES_PORT") or "").strip()
    if not password and not port:
        return None
    user = (values.get("BOSS_DB_USER") or values.get("POSTGRES_USER") or "boss").strip() or "boss"
    host = (values.get("BOSS_POSTGRES_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port_str = port or "5432"
    dbname = (values.get("BOSS_DB_NAME") or values.get("POSTGRES_DB") or "boss").strip() or "boss"
    enc_user = quote(user, safe="")
    auth = f"{enc_user}:{quote(password, safe='')}" if password else enc_user
    keys = [k for k in ("BOSS_POSTGRES_PORT", "BOSS_DB_PASSWORD", "POSTGRES_PASSWORD")
            if (values.get(k) or "").strip()]
    return f"postgresql+psycopg://{auth}@{host}:{port_str}/{dbname}", "+".join(keys)


def _candidate_env_files(
    environ: Mapping[str, str],
    *,
    cwd: Optional[Any] = None,
    install_dir: Optional[Any] = None,
    search_defaults: bool = True,
) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    explicit_env = (environ.get("BOSS_ENV_FILE") or "").strip()
    if explicit_env:
        candidates.append(("env_file", Path(explicit_env).expanduser()))
    inst = str(install_dir) if install_dir is not None else (environ.get("BOSS_INSTALL_DIR") or "").strip()
    if inst:
        candidates.append(("install_env", Path(inst).expanduser() / ".env"))
    base_cwd = Path(cwd) if cwd is not None else Path.cwd()
    candidates.append(("cwd_env", base_cwd / ".env"))
    if search_defaults:
        repo_root = Path(__file__).resolve().parents[3]
        candidates.append(("install_env", repo_root / ".env"))
        candidates.append(("install_env", Path.home() / ".boss" / ".env"))
    seen: set[Path] = set()
    unique: list[tuple[str, Path]] = []
    for label, path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append((label, path))
    return unique


def resolve_database_config(
    environ: Optional[Mapping[str, str]] = None,
    *,
    cwd: Optional[Any] = None,
    install_dir: Optional[Any] = None,
    search_defaults: bool = True,
) -> DatabaseConfig:
    """Resolve the database URL and name its exact configuration source (BOSSAPP-991).

    Precedence mirrors the API and Community install:
      1. `DATABASE_URL` / `BOSS_DATABASE_URL` in the process environment;
      2. Install `.env` (`BOSS_ENV_FILE`, `$BOSS_INSTALL_DIR/.env`, `./.env`, repo/home `.env`),
         honouring `DATABASE_URL`, `BOSS_DATABASE_URL`, or compose's `BOSS_POSTGRES_PORT` +
         `BOSS_DB_PASSWORD`;
      3. Compose DB variables in the process environment;
      4. `Settings.database_url` default (marked `configured=False`).
    """
    env = os.environ if environ is None else environ
    for key in ("DATABASE_URL", "BOSS_DATABASE_URL"):
        raw = (env.get(key) or "").strip()
        if raw:
            env_file = None
            for _label, path in _candidate_env_files(
                env, cwd=cwd, install_dir=install_dir, search_defaults=search_defaults
            ):
                if path.is_file():
                    env_file = str(path)
                    break
            return DatabaseConfig(
                url=_normalize_pg_url(raw),
                source=f"env:{key}",
                configured=True,
                env_file=env_file,
            )

    for label, path in _candidate_env_files(
        env, cwd=cwd, install_dir=install_dir, search_defaults=search_defaults
    ):
        if not path.is_file():
            continue
        values = _parse_dotenv_file(path)
        for key in ("DATABASE_URL", "BOSS_DATABASE_URL"):
            raw = (values.get(key) or "").strip()
            if raw:
                return DatabaseConfig(
                    url=_normalize_pg_url(raw),
                    source=f"{label}:{path}:{key}",
                    configured=True,
                    env_file=str(path),
                )
        composed = _compose_database_url(values)
        if composed is not None:
            url, keys = composed
            return DatabaseConfig(
                url=url,
                source=f"{label}:{path}:{keys}",
                configured=True,
                env_file=str(path),
            )

    composed_env = _compose_database_url(env)
    if composed_env is not None:
        url, keys = composed_env
        return DatabaseConfig(url=url, source=f"env:{keys}", configured=True)

    from ..config import Settings

    default_url = str(Settings.model_fields["database_url"].default)
    return DatabaseConfig(
        url=default_url,
        source="default:Settings.database_url",
        configured=False,
    )


def load_mcp_settings(
    *,
    environ: Optional[Mapping[str, str]] = None,
    cwd: Optional[Any] = None,
    install_dir: Optional[Any] = None,
    search_defaults: bool = True,
) -> tuple[Any, DatabaseConfig]:
    """Load `Settings` using the same install `.env` and DB resolution as the API (BOSSAPP-991)."""
    from ..config import Settings

    cfg = resolve_database_config(
        environ=environ, cwd=cwd, install_dir=install_dir, search_defaults=search_defaults
    )
    settings = Settings(_env_file=cfg.env_file) if cfg.env_file else Settings()
    if cfg.configured and str(settings.database_url) != cfg.url:
        settings = settings.model_copy(update={"database_url": cfg.url})
    return settings, cfg


def sanitize_database_url(url: str) -> str:
    """Format a database URL as `user@host:port/db` — never including a password."""
    raw = str(url or "").strip()
    if not raw:
        return "<unset>"
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.startswith("sqlite"):
            return f"{parsed.scheme}://{parsed.path or ':memory:'}"
        host = parsed.hostname or "localhost"
        port = f":{parsed.port}" if parsed.port else ""
        user = f"{parsed.username}@" if parsed.username else ""
        db = parsed.path.lstrip("/") or ""
        return f"{user}{host}{port}/{db}" if db else f"{user}{host}{port}"
    except Exception:  # noqa: BLE001
        return "<invalid-database-url>"


def _extract_password(url: str) -> str:
    try:
        return urlsplit(str(url or "")).password or ""
    except Exception:  # noqa: BLE001
        return ""


def is_database_error(exc: BaseException) -> bool:
    """True when `exc` (or its cause chain) is a SQLAlchemy / psycopg database failure."""
    from sqlalchemy.exc import SQLAlchemyError

    cur: Optional[BaseException] = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, SQLAlchemyError):
            return True
        mod = str(getattr(type(cur), "__module__", "") or "")
        if mod.startswith("psycopg") or "sqlalchemy" in mod:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def format_database_error(
    exc: BaseException,
    *,
    database_url: str = "",
    source: str = "",
) -> str:
    """Clear, secret-free diagnostic naming the configured DB source and sanitized target."""
    target = sanitize_database_url(database_url) if database_url else "<unknown>"
    src = source or "default:Settings.database_url"
    detail = f"{type(exc).__name__}: {exc}".strip()
    pw = _extract_password(database_url)
    if pw:
        detail = detail.replace(pw, "[REDACTED]")
    if len(detail) > 400:
        detail = detail[:397] + "..."
    hint = (
        "No DATABASE_URL or install .env was found; set DATABASE_URL or BOSS_INSTALL_DIR."
        if src.startswith("default:")
        else f"Check the database configuration from {src}."
    )
    return (
        f"Database connection failed (target={target}, configured_source={src}): "
        f"{detail}. {hint}"
    )


def _mcp_mode() -> Any:
    """The authz mode tool calls run under (router key `mcp`)."""
    from ..identity.enforcement import mode_for
    from ..tools.registry import TOOL_ROUTER_KEY

    settings, _ = load_mcp_settings()
    return mode_for(TOOL_ROUTER_KEY, global_mode=settings.authz_mode,
                    router_modes=settings.authz_router_modes)


def _is_boss_credential(token: str) -> bool:
    """A credential BOSS identity verifies itself: an API key (boss_pat_/boss_sak_) or a run token."""
    from ..identity.authn import parse_api_key

    return parse_api_key(token) is not None or (token.startswith("eyJ") and token.count(".") == 2)


def authenticate_transport(headers: Mapping[str, str], *, method: str = "POST", path: str = "/mcp",
                           client_ip: Optional[str] = None) -> Any:
    """The verified identity of an MCP caller, from what its TRANSPORT presented (an
    `Authorization` bearer: a BOSS API key, or a DPoP-bound run token with its `DPoP` proof).
    None when it presented no BOSS credential or one that does not verify — the identity gate
    then refuses the call under enforce. Never read from a tool argument; never the legacy
    network/shared-key bridges (an MCP session is not a console request)."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    scheme, _, token = lowered.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() not in ("bearer", "dpop") or not token or not _is_boss_credential(token):
        return None
    from ..identity import authn

    creds = authn.RequestCredentials(method=method, path=path, headers=lowered, cookies={},
                                     client_ip=client_ip, peer_ip=client_ip)
    try:
        return authn.authenticate(creds)
    except Exception:  # noqa: BLE001 — an unverifiable credential is no identity (fail closed)
        return None


def _oauth_issuer() -> str:
    """The authorization server for this MCP server (BOSSAPP-862): BOSS_MCP_OAUTH_ISSUER (the
    API's public origin). Opt-in: unset, OAuth is not advertised and nothing changes."""
    import os

    return (os.environ.get("BOSS_MCP_OAUTH_ISSUER") or "").rstrip("/")


def oauth_resource_metadata() -> Optional[dict]:
    issuer = _oauth_issuer()
    if not issuer:
        return None
    import os

    from ..services.mcp_oauth_server import protected_resource_metadata

    resource = (os.environ.get("BOSS_MCP_PUBLIC_URL") or f"{issuer}/mcp").rstrip("/")
    return protected_resource_metadata(resource, issuer)


def _bearer_challenge() -> str:
    """`Bearer`, plus RFC 9728's resource_metadata pointer when OAuth is configured (unchanged
    otherwise)."""
    import os

    issuer = _oauth_issuer()
    if not issuer:
        return "Bearer"
    base = (os.environ.get("BOSS_MCP_PUBLIC_URL") or f"{issuer}/mcp").rstrip("/")
    origin = base.rsplit("/mcp", 1)[0] if base.endswith("/mcp") else base
    return f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource"'


class MCPServer:
    """Bridges MCP JSON-RPC calls onto the engine tool registry.

    project_id/actor are fixed for the session (the transport authenticates the
    caller). `read_only=True` hides write tools entirely — useful for exposing a
    safe, inspection-only engine view.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        project_id: str,
        actor: str = "mcp",
        registry: Optional[ToolRegistry] = None,
        read_only: bool = False,
        http_token: Optional[str] = None,
        api_surface: Any = None,
        api_domains: Optional[set[str]] = None,
        identity: Optional[Callable[[], Any]] = None,
        session_credential: Optional[str] = None,
    ):
        """`identity` returns the verified caller identity for one call (stdio: the session's
        BOSS API key, re-checked live on every call). The HTTP transport authenticates each
        request's bearer instead."""
        self.ctx = ctx
        self.identity = identity
        #: stdio: the `Authorization` value of the key the session was launched with (the same
        #: key `identity` verifies). The API-surface tools call the API as this caller.
        self.session_credential = session_credential
        self.project_id = project_id
        self.actor = actor
        self.registry = registry or build_default_registry()
        self.http_token = http_token
        # In API-backed mode every call must use the scoped API credential.
        # Native reads as well as writes could otherwise use raw DATABASE_URL
        # under actor "mcp" and bypass that member's API permissions.
        self.api_only = api_surface is not None
        self.api_surface = api_surface
        self.api_tool_names: set[str] = set()
        self.read_only = read_only
        self.catalog_only = not getattr(ctx, "durable_workspace_configured", True)
        if api_surface is not None:
            pinned = getattr(api_surface, "pinned_project_id", None)
            if pinned is not None and pinned != project_id:
                raise ValueError("MCP API project must match the pinned engine project")
            from .api_surface import register_api_tools

            existing = set(self.registry.names())
            register_api_tools(
                self.registry, api_surface, read_only=self.read_only,
                domains=api_domains,
            )
            self.api_tool_names = set(self.registry.names()) - existing

    # ---- schema ----------------------------------------------------------
    def _sync_agent_tools(self) -> None:
        """AP-P3: one `agent_<key>` tool per agent deployed to prod with MCP exposure
        (mcp/agent_tools.py), refreshed on every list. Never raises."""
        if self.read_only:
            return
        try:
            from .agent_tools import sync

            names = sync(self.registry, project_id=self.project_id,
                         engine=None if self.api_only else getattr(self.ctx, "engine", None),
                         surface=self.api_surface if self.api_only else None)
            if self.api_only:
                self.api_tool_names |= names
        except Exception:  # noqa: BLE001 — no agent tools is better than no tools
            pass

    def _tools(self) -> list[dict]:
        self._sync_agent_tools()
        tools = [spec.mcp() for spec in self.registry.specs() if (
            spec.name not in _UNSCOPED_SQL_TOOLS
            and spec.name not in _RUN_FS_TOOLS
            and spec.name not in _RUN_ONLY_TOOLS
            and
            (not self.api_only or spec.name in self.api_tool_names)
            and
            (not self.read_only or spec.kind == "read")
            and (not self.catalog_only or spec.kind == "read"
                 or not spec.requires_durable_workspace)
        )]
        # These fields are transport/session authority, never model arguments.
        for tool in tools:
            schema = tool.get("inputSchema") or {}
            properties = dict(schema.get("properties") or {})
            for reserved in ("project_id", "_operator_confirmed", "_writes_allowed"):
                properties.pop(reserved, None)
            tool["inputSchema"] = {
                **schema,
                "properties": properties,
                "required": [
                    field for field in schema.get("required", []) if field in properties
                ],
            }
        # DETERMINISTIC ORDER (2026-07-28: "SHOULD return tools in a deterministic order" for
        # client caching and prompt-cache hits): by name, in every revision.
        return sorted(tools, key=lambda tool: str(tool.get("name") or ""))

    def _tool_allowed(self, name: str) -> bool:
        try:
            spec = self.registry.get(name)
            return (spec.name not in _UNSCOPED_SQL_TOOLS
                    and (not self.api_only or spec.name in self.api_tool_names)
                    and (not self.read_only or spec.kind == "read")
                    and (not self.catalog_only or spec.kind == "read"
                         or not spec.requires_durable_workspace))
        except Exception:
            return False

    # ---- JSON-RPC dispatch ----------------------------------------------
    def handle(self, request: dict) -> Optional[dict]:
        """Handle one JSON-RPC request. Returns a response dict, or None for
        notifications (which take no reply).

        AP-P5 (mcp/protocol.py): a request whose `_meta` names a protocol version is MODERN
        (2026-07-28, stateless); `initialize` selects a legacy revision (2024-11-05 by default,
        exactly as before). `server/discover` answers in every era."""
        if not isinstance(request, dict):
            return _err(None, -32600, "invalid JSON-RPC request")
        method = request.get("method")
        rid = request.get("id")
        raw_params = request.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        version = _protocol.modern_version(params)
        if version is not None and version not in _protocol.SUPPORTED:
            return {"jsonrpc": "2.0", "id": rid, "error": _protocol.unsupported_version(version)}
        modern = version == _protocol.MODERN
        # TRACE CONTEXT (SEP-414): a caller's `traceparent` in `_meta` makes the runs this call
        # starts children of the caller's span (kernel/trace.py reads CURRENT; a deployed agent's
        # run carries it to its worker in finalize_ctx).
        traceparent = _protocol.traceparent_of(params)
        trace_token = _trace_current().set(traceparent) if traceparent else None
        try:
            return self._dispatch(method, rid, raw_params, params, modern)
        finally:
            if trace_token is not None:
                _trace_current().reset(trace_token)

    def _dispatch(self, method: Any, rid: Any, raw_params: Any, params: dict,
                  modern: bool) -> Optional[dict]:
        if method == "server/discover":
            return _ok(rid, _protocol.discover_result(SERVER_INFO, SERVER_INSTRUCTIONS))
        if method == "initialize":
            return _ok(rid, {
                "protocolVersion": _protocol.negotiate_initialize(params.get("protocolVersion")),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            })
        if method in ("notifications/initialized", "initialized"):
            return None  # notification: no response
        if method == "ping":
            return _ok(rid, {})
        if method == "tools/list":
            tools = self._tools()
            if modern:
                return _ok(rid, _protocol.complete(
                    {"tools": tools, "ttlMs": _protocol.LIST_TTL_MS, "cacheScope": "private"},
                    SERVER_INFO))
            return _ok(rid, {"tools": tools})
        if method == "tools/call":
            if not isinstance(raw_params, dict) and raw_params is not None:
                return _err(rid, -32602, "tools/call params must be an object")
            if modern:
                return self._tools_call_modern(rid, params)
            return self._tools_call(rid, params)
        if method in ("tasks/get", "tasks/update", "tasks/cancel"):
            if not modern:
                return _err(rid, -32601, f"method not found: {method} (the tasks extension "
                                         f"is offered on protocol {_protocol.MODERN})")
            return self._tasks(method, rid, params)
        return _err(rid, -32601, f"method not found: {method}")

    def _call_args(self, rid: Any, params: dict) -> tuple[Optional[str], Optional[dict],
                                                           Optional[dict]]:
        """(name, args, refusal): the tool and its arguments with the session's authority fields
        removed (session project_id is authoritative — never a tool argument's)."""
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return None, None, _err(rid, -32602, "tools/call requires a tool name")
        supplied_args = params.get("arguments") or {}
        if not isinstance(supplied_args, dict):
            return None, None, _err(rid, -32602, "tool arguments must be an object")
        # Session project_id is authoritative — never let tool arguments override
        # into another tenant (write tools historically honored args.project_id).
        args = dict(supplied_args)
        for reserved in ("project_id", "_operator_confirmed", "_writes_allowed"):
            args.pop(reserved, None)
        if name.startswith("agent_") and name not in self.registry.names():
            self._sync_agent_tools()
        if not self._tool_allowed(name):
            reason = ("read-only mode" if self.read_only else
                      "API-backed sessions require API-authorized operations" if self.api_only
                      else "durable workspace required")
            return None, None, _ok(rid, _tool_result(
                {"ok": False, "error": f"tool '{name}' is not available ({reason})"},
                is_error=True,
            ))
        return name, args, None

    def _invoke_tool(self, name: str, args: dict, auth: Any) -> dict:
        """One registry call as `auth` (the identity the transport verified). Raises what the
        registry raises (an unknown tool)."""
        from ..tools.registry import CURRENT_TOOL_AUTH
        from .api_surface import CALLER_AUTHORIZATION

        tool_token = CURRENT_TOOL_AUTH.set(auth)
        cred_token = CALLER_AUTHORIZATION.set(self._caller_credential(auth))
        try:
            if auth is not None:
                from ..identity import lineage

                with lineage.acting(auth):
                    return self.registry.invoke(
                        name, self.ctx, project_id=self.project_id, actor=self.actor, args=args, auth=auth)
            return self.registry.invoke(
                name, self.ctx, project_id=self.project_id, actor=self.actor, args=args, auth=None)
        finally:
            CALLER_AUTHORIZATION.reset(cred_token)
            CURRENT_TOOL_AUTH.reset(tool_token)

    def _database_error_message(self, exc: BaseException) -> str:
        settings = getattr(self.ctx, "settings", None)
        db_url = str(getattr(settings, "database_url", "") or "")
        source = str(getattr(self.ctx, "database_source", "") or "")
        if not source:
            cfg = resolve_database_config()
            source = cfg.source
            if not db_url:
                db_url = cfg.url
        return format_database_error(exc, database_url=db_url, source=source)

    def _tools_call(self, rid: Any, params: dict) -> dict:
        name, args, refused = self._call_args(rid, params)
        if refused is not None:
            return refused
        # WHO IS CALLING: the identity the transport authenticated, never a tool argument. Every
        # tool call is decided as that caller (the registry's identity gate; fail closed under
        # enforce when there is none), and jobs it enqueues carry it (R8).
        auth = self._caller()
        try:
            result = self._invoke_tool(name, args, auth)
        except Exception as exc:  # unknown tool, DB auth/connection failure, etc.
            message = (
                self._database_error_message(exc)
                if is_database_error(exc)
                else f"{type(exc).__name__}: {exc}"
            )
            return _ok(rid, _tool_result({"ok": False, "error": message}, is_error=True))
        return _ok(rid, _tool_result(result, is_error=not result.get("ok", True)))

    # ---- 2026-07-28: tasks and multi round-trip requests --------------------------------------
    def _tools_call_modern(self, rid: Any, params: dict) -> dict:
        """tools/call, stateless. A long-running tool (a deployed agent, create_code_task)
        becomes a TASK when the client declared the tasks extension; a person's answer is asked
        for with `input_required` (and comes back on the retry), never a server request."""
        from . import tasks as _tasks

        name, args, refused = self._call_args(rid, params)
        if refused is not None:
            return _modernise(refused)
        auth = self._caller()
        if "requestState" in params or "inputResponses" in params:
            return self._mrtr_retry(rid, params, name, args, auth)
        as_task = _protocol.wants_tasks(params) and _tasks.is_task_tool(name)
        call_args = dict(args)
        if as_task and name.startswith(_tasks.AGENT_PREFIX):
            call_args["timeout"] = 0          # start it; the client polls the task
        try:
            result = self._invoke_tool(name, call_args, auth)
        except Exception as exc:  # unknown tool, DB auth/connection failure, etc.
            message = (
                self._database_error_message(exc)
                if is_database_error(exc)
                else f"{type(exc).__name__}: {exc}"
            )
            return _ok(rid, _protocol.complete(_tool_result(
                {"ok": False, "error": message}, is_error=True),
                SERVER_INFO))
        handle = _tasks.handle_for_tool_result(name, result) if _tasks.is_task_tool(name) \
            else None
        if handle and as_task:
            try:
                state = _tasks.backend_for(self).state(_tasks.parse_handle(handle), auth)
            except _tasks.TaskError as exc:
                return {"jsonrpc": "2.0", "id": rid, "error": exc.error()}
            task = _tasks.task_object(handle, state, elicit=set())
            created = {k: task[k] for k in ("taskId", "status", "statusMessage", "createdAt",
                                            "lastUpdatedAt", "ttlMs", "pollIntervalMs")
                       if k in task}
            out = _protocol.complete(created, SERVER_INFO)
            out["resultType"] = "task"
            return _ok(rid, out)
        if handle and name.startswith(_tasks.AGENT_PREFIX):
            answered = self._input_required_for(rid, handle, name, args, auth,
                                                _protocol.elicitation_modes(params))
            if answered is not None:
                return answered
        return _ok(rid, _protocol.complete(
            _tool_result(result, is_error=not result.get("ok", True)), SERVER_INFO))

    def _input_required_for(self, rid: Any, handle: str, name: str, args: dict, auth: Any,
                            elicit: set, refused: Optional[dict] = None) -> Optional[dict]:
        """The InputRequiredResult for a run waiting on a person, or None when it is not (or the
        client declared no elicitation: it then gets the run, as before, with its decisions)."""
        from . import tasks as _tasks

        if not elicit:
            return None
        try:
            state = _tasks.backend_for(self).state(_tasks.parse_handle(handle), auth)
        except _tasks.TaskError:
            return None
        if state["status"] != "input_required":
            return None
        refused = {**self._unanswerable(state.get("pending") or [], auth), **(refused or {})}
        principal = getattr(getattr(auth, "principal", None), "id", None)
        sealed = _protocol.seal_state({"h": handle}, principal=principal,
                                      digest=_protocol.request_digest("tools/call", name, args))
        return _ok(rid, {"resultType": "input_required",
                         "inputRequests": _tasks.input_requests(state["pending"], elicit=elicit,
                                                                refused=refused),
                         "requestState": sealed,
                         "_meta": {_protocol.META_SERVER_INFO: SERVER_INFO}})

    def _unanswerable(self, pending: list, auth: Any) -> dict:
        """{decision_id: reason} for the open interrupts THIS caller cannot answer in-band (the
        PDP's decision:approve with step-up), so it is shown the console link instead of a form
        it would be refused on. Native only; the API bridge learns it from the route."""
        if self.api_only or auth is None or not pending:
            return {}
        engine = getattr(self.ctx, "engine", None)
        if engine is None:
            return {}
        from ..db import begin
        from ..identity import authz
        from ..identity.scope import ResolvedScope

        out = {}
        try:
            with begin(engine) as c:
                for item in pending:
                    did = str(item["decision_id"])
                    verdict = authz.check(auth, "decision:approve", ResolvedScope.in_project(
                        auth.principal.org_id, self.project_id, object_kind="decision",
                        object_id=did), db=c)
                    if not verdict.allowed:
                        out[did] = str(verdict.reason or "missing_permission")
        except Exception:  # noqa: BLE001 — unknown answerability shows the form
            return {}
        return out

    def _apply_answers(self, handle: str, responses: Any, auth: Any) -> dict:
        """Apply the client's inputResponses to the run's OPEN interrupts only (a key naming any
        other decision is ignored, per spec), each as the caller. {decision_id: refusal code}."""
        from ..services import protocol_answers as A
        from . import tasks as _tasks

        backend = _tasks.backend_for(self)
        state = backend.state(_tasks.parse_handle(handle), auth)
        open_ids = {str(p["decision_id"]) for p in state.get("pending") or []}
        refused: dict = {}
        for key, value in (responses.items() if isinstance(responses, dict) else ()):
            did = A.decision_of_key(key)
            if did is None or did not in open_ids:
                continue
            verdict = A.parse_response(value)
            if verdict is None:
                continue
            try:
                backend.answer(did, verdict, auth)
            except A.AnswerRefused as exc:
                refused[did] = exc.code
        return refused

    def _mrtr_retry(self, rid: Any, params: dict, name: str, args: dict, auth: Any) -> dict:
        """The client's retry of a tools/call that answered `input_required`: verify the sealed
        state (this caller, this request, not expired), apply the answers as the caller, then
        answer the call — again `input_required` while a person is still needed."""
        import time

        from . import tasks as _tasks

        principal = getattr(getattr(auth, "principal", None), "id", None)
        state = _protocol.open_state(params.get("requestState"), principal=principal,
                                     digest=_protocol.request_digest("tools/call", name, args))
        if state is None or not isinstance(state.get("h"), str):
            return _err(rid, -32602, "requestState is not valid for this caller and request "
                                     "(expired, altered, or minted for another call)")
        handle = state["h"]
        elicit = _protocol.elicitation_modes(params) or {"form"}
        try:
            refused = self._apply_answers(handle, params.get("inputResponses"), auth)
            if refused:
                again = self._input_required_for(rid, handle, name, args, auth, elicit, refused)
                if again is not None:
                    return again
            backend = _tasks.backend_for(self)
            deadline = time.monotonic() + max(0.0, min(float(args.get("timeout") or 120), 600.0))
            while True:
                current = backend.state(_tasks.parse_handle(handle), auth)
                if current["status"] != "working" or time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
        except _tasks.TaskError as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": exc.error()}
        if current["status"] == "input_required":
            again = self._input_required_for(rid, handle, name, args, auth, elicit)
            if again is not None:
                return again
        if current["status"] == "completed":
            return _ok(rid, _protocol.complete(current["result"], SERVER_INFO))
        payload = {"ok": current["status"] != "failed", "status": current["status"],
                   "task": handle, "message": current.get("message"),
                   "error": current.get("error")}
        return _ok(rid, _protocol.complete(_tool_result(
            {k: v for k, v in payload.items() if v is not None},
            is_error=current["status"] == "failed"), SERVER_INFO))

    def _tasks(self, method: str, rid: Any, params: dict) -> dict:
        """tasks/get, tasks/update, tasks/cancel of the `io.modelcontextprotocol/tasks`
        extension, each decided as the caller of THIS request (mcp/tasks.py)."""
        from ..services import protocol_answers as A
        from . import tasks as _tasks

        auth = self._caller()
        try:
            handle = _tasks.parse_handle(params.get("taskId"))
            backend = _tasks.backend_for(self)
            if method == "tasks/get":
                state = backend.state(handle, auth)
                elicit = _protocol.elicitation_modes(params) or {"form"}
                if state["status"] == "input_required":
                    state = {**state, "refused": self._unanswerable(state["pending"], auth)}
                return _ok(rid, _protocol.complete(
                    _tasks.task_object(params["taskId"], state, elicit=elicit), SERVER_INFO))
            if method == "tasks/cancel":
                backend.cancel(handle, auth)
                return _ok(rid, _protocol.complete({}, SERVER_INFO))
            refused = self._apply_answers(params["taskId"], params.get("inputResponses"), auth)
        except _tasks.TaskError as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": exc.error()}
        if refused:
            did, code = next(iter(refused.items()))
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": _protocol.ANSWER_REFUSED,
                "message": f"answer refused: {code}",
                "data": {"refused": refused, "approve_url": A.console_url(did)}}}
        return _ok(rid, _protocol.complete({}, SERVER_INFO))

    def _caller_credential(self, auth: Any) -> Optional[str]:
        """The caller's own credential to forward to the API (M8): the HTTP request's bearer when
        it verified as `auth`, else the stdio session's launch key. Never the shared
        BOSS_MCP_HTTP_TOKEN, never the surface's configured key on another caller's behalf."""
        request_auth = _REQUEST_AUTH.get()
        if request_auth is _NO_IDENTITY:
            return None
        if request_auth is not None:
            return _REQUEST_CREDENTIAL.get() if auth is not None else None
        # stdio: the key this session was launched with IS the caller's own credential (the
        # operator started the server with it); the API verifies it on every call.
        return self.session_credential

    def _caller(self) -> Any:
        """The verified identity for this call: the HTTP request's, else the session's."""
        request_auth = _REQUEST_AUTH.get()
        if request_auth is _NO_IDENTITY:
            return None
        if request_auth is not None:
            return request_auth
        if self.identity is None:
            return None
        try:
            return self.identity()
        except Exception:  # noqa: BLE001 — a credential that no longer verifies is no identity
            return None

    # ---- external-agent entry point (KG P6a) ------------------------------
    def handle_external_agent_call(self, token: str, request: dict) -> Optional[dict]:
        """The ONLY entry point a ``boss_ext_...`` token may use. Verifies the token live
        against its stored record — never cached, never from a request argument — and refuses
        anything but a ``tools/call`` of a Context-Graph write tool the token's contract-derived
        policy actually grants. ``run_id``/``step_id``/``project_id`` come from the verified
        claims, never from the caller's arguments, so a token minted for one run/step cannot be
        redirected to write into another."""
        from ..services import agent_turns
        from ..services.knowledge import context_graph, external_agent_tokens, external_agents, scopes

        def _record(tool_name: Optional[str], ok: bool, detail: Any) -> None:
            # FR2's reads/writes table renders from exactly this: one row per KG tool call an
            # external agent actually made, keyed off the token's own verified claims (never the
            # caller's arguments), so a token cannot attribute a call to a run/step it is not
            # scoped to.
            agent_turns.record_turns(
                connection,
                [{"turn_index": 0, "direction": "tool_call", "tool_name": tool_name or "?",
                  "content": detail, "ok": ok, "latency_ms": 0}],
                project_id=claims.project_id, code_task_run_id=claims.run_id, stage=claims.step_id)

        rid = request.get("id") if isinstance(request, dict) else None
        with self.ctx.begin() as connection:
            claims = external_agent_tokens.verify(connection, token)
            if claims is None:
                return _err(rid, -32001, external_agents.plain_reason("token_refused"))
            if not isinstance(request, dict) or request.get("method") != "tools/call":
                return _err(rid, -32601, "an external agent token may only call tools/call")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                return _err(rid, -32602, "tools/call params must be an object")
            name = params.get("name")
            args = params.get("arguments") or {}
            actor_ref = f"external_agent:{claims.external_agent_id}"
            if name in _EXTERNAL_AGENT_READ_TOOLS:
                # Reads within scope: only the contract's declared read kinds (`policy.kinds`),
                # at most `policy.max_nodes` rows. A kind the contract never granted is refused
                # in words, never silently returned empty.
                allowed_kinds = claims.policy.kinds
                if not allowed_kinds:
                    _record(name, False, {"error": "token_refused"})
                    return _err(rid, -32003, external_agents.plain_reason("token_refused"))
                # max_nodes == 0 means literally zero, same as every other reader of this field
                # (store.py:908's `[:policy.max_nodes]`) — never silently substituted for a
                # default, or a step whose contract set no bound would read more than it declared.
                limit = claims.policy.max_nodes
                try:
                    if name == "kg_find":
                        kind = str(args.get("kind") or "").strip()
                        if kind not in allowed_kinds:
                            _record(name, False, {"kind": kind, "error": "token_refused"})
                            return _err(rid, -32003, external_agents.plain_reason("token_refused"))
                        nodes = context_graph.find_nodes(
                            connection, claims.project_id, kind=kind,
                            query=(args.get("query") or None), limit=limit)
                        _record(name, True, {"kind": kind, "count": len(nodes)})
                    else:  # kg_get_node
                        node_key = str(args.get("node_key") or "").strip()
                        if not node_key:
                            return _err(rid, -32602, "node_key is required")
                        node = context_graph.get_node(
                            connection, claims.project_id, node_key=node_key,
                            kinds=allowed_kinds)
                        if node is None:
                            _record(name, False, {"node_key": node_key, "error": "token_refused"})
                            return _err(rid, -32003, external_agents.plain_reason("token_refused"))
                        nodes = [node]
                        _record(name, True, {"node_key": node_key})
                except Exception as exc:  # noqa: BLE001 — surfaced to the agent as a tool error
                    _record(name, False, {"error": f"{type(exc).__name__}: {exc}"})
                    return _ok(rid, _tool_result(
                        {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, is_error=True))
                return _ok(rid, _tool_result({"ok": True, "nodes": nodes}))
            if (name not in _EXTERNAL_AGENT_WRITE_TOOLS
                    or not scopes.offers_tool(claims.policy, name)):
                _record(name, False, {"error": "token_refused"})
                return _err(rid, -32003, external_agents.plain_reason("token_refused"))
            try:
                if name == "kg_write_agent_decision":
                    decision_type = str(args.get("decision_type") or "").strip()
                    statement = str(args.get("statement") or "").strip()
                    if not decision_type or not statement:
                        return _err(rid, -32602, "decision_type and statement are required")
                    result = context_graph.write_agent_decision(
                        connection, claims.project_id, step_id=claims.step_id, run_id=claims.run_id,
                        decision_type=decision_type, statement=statement, policy_version="",
                        workflow_version="", agent_fingerprint="", actor_ref=actor_ref)
                    _record(name, True, {"decision_type": decision_type, "statement": statement})
                else:
                    statement = str(args.get("statement") or "").strip()
                    if not statement:
                        return _err(rid, -32602, "statement is required")
                    result = context_graph.write_coverage_gap(
                        connection, claims.project_id, step_id=claims.step_id, run_id=claims.run_id,
                        statement=statement, actor_ref=actor_ref)
                    _record(name, True, {"statement": statement})
            except Exception as exc:  # noqa: BLE001 — surfaced to the agent as a tool error
                _record(name, False, {"error": f"{type(exc).__name__}: {exc}"})
                return _ok(rid, _tool_result(
                    {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, is_error=True))
        return _ok(rid, _tool_result({"ok": True, **result}))

    # ---- transports ------------------------------------------------------
    def serve_stdio(self, *, stdin=None, stdout=None) -> None:
        """Standard MCP stdio transport: one JSON-RPC message per line on stdin,
        one response line per request on stdout. Stdlib only."""
        rin = stdin or sys.stdin
        rout = stdout or sys.stdout
        for line in rin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _write(rout, _err(None, -32700, "parse error"))
                continue
            response = self.handle(request)
            if response is not None:
                _write(rout, response)

    def asgi_app(self):
        """Optional HTTP/JSON-RPC transport (POST /mcp, GET /health) plus the
        community Mission Control page (GET /). Lazily imports FastAPI so the
        pure engine never depends on it."""
        from fastapi import FastAPI, Request  # noqa: PLC0415 (intentionally lazy)
        from fastapi import HTTPException  # noqa: PLC0415
        from fastapi.responses import HTMLResponse  # noqa: PLC0415

        from .ui import mission_control_html  # noqa: PLC0415

        if not self.read_only and not self.http_token:
            raise ValueError("write-capable HTTP MCP requires BOSS_MCP_HTTP_TOKEN")

        app = FastAPI(title="boss-engine-mcp")
        server = self

        async def admitted(request: Request, *, liveness: bool = False) -> Any:
            """Admit the request and return its verified identity. The legacy shared
            BOSS_MCP_HTTP_TOKEN admits but identifies nobody (None), so its tool calls are refused
            under enforce. A BOSS API key or run token is verified by identity, live; one that does
            not verify is refused, never downgraded to anonymous.

            `liveness` (GET /health only): the shared token still admits under enforce. A health
            probe does nothing for anybody, so "nothing is done for nobody" does not apply; the
            compose healthcheck holds only that token, and refusing it marked a working kg-mcp
            unhealthy forever (2026-11-06, 401 on every probe)."""
            authorization = request.headers.get("authorization", "")
            scheme, _, candidate = authorization.partition(" ")
            candidate = candidate.strip()
            if server.http_token and scheme.lower() == "bearer" and hmac.compare_digest(
                candidate.encode(), server.http_token.encode()
            ):
                from ..identity.enforcement import AuthzMode  # noqa: PLC0415

                if not liveness and _mcp_mode() == AuthzMode.ENFORCE:
                    # The shared token says the request came from someone holding it, not WHO:
                    # under enforce nothing is done for nobody (M8/H2).
                    raise HTTPException(
                        status_code=401,
                        detail="identity required: present your BOSS API key or run token",
                        headers={"WWW-Authenticate": _bearer_challenge()})
                return None
            if candidate and _is_boss_credential(candidate):
                from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

                found = await run_in_threadpool(
                    authenticate_transport, dict(request.headers), method=request.method,
                    path=request.url.path, client_ip=request.client.host if request.client else None)
                if found is not None:
                    return found
            elif not server.http_token:
                return None
            raise HTTPException(
                status_code=401,
                detail="MCP bearer token required",
                headers={"WWW-Authenticate": _bearer_challenge()},
            )

        @app.post("/mcp")
        async def mcp(request: Request):  # type: ignore[no-redef]
            authorization = request.headers.get("authorization", "")
            scheme, _, bearer = authorization.partition(" ")
            bearer = bearer.strip()
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return _err(None, -32700, "parse error")
            if scheme.lower() == "bearer" and bearer.startswith(_EXTERNAL_AGENT_TOKEN_PREFIX):
                # Deliberately not part of `identity`/`authenticate_transport`: this credential
                # is a separate, narrow system (external_agent_tokens.py) for a remote party
                # BOSS does not run in-process.
                resp = server.handle_external_agent_call(bearer, body)
                return resp if resp is not None else {"jsonrpc": "2.0", "result": {}}
            auth = await admitted(request)
            token = _REQUEST_AUTH.set(auth if auth is not None else _NO_IDENTITY)
            cred = _REQUEST_CREDENTIAL.set(
                request.headers.get("authorization") if auth is not None else None)
            try:
                resp = server.handle(body)
            finally:
                _REQUEST_CREDENTIAL.reset(cred)
                _REQUEST_AUTH.reset(token)
            return resp if resp is not None else {"jsonrpc": "2.0", "result": {}}

        @app.get("/.well-known/oauth-protected-resource")
        @app.get("/.well-known/oauth-protected-resource/mcp")
        async def protected_resource():  # type: ignore[no-redef]
            """RFC 9728 (BOSSAPP-862): where a 401'd MCP client finds BOSS's authorization
            server. 404 until BOSS_MCP_OAUTH_ISSUER names it."""
            meta = oauth_resource_metadata()
            if meta is None:
                raise HTTPException(status_code=404, detail="OAuth is not configured")
            return meta

        @app.get("/health")
        async def health(request: Request):  # type: ignore[no-redef]
            await admitted(request, liveness=True)
            return {"status": "ok", "server": SERVER_INFO["name"],
                    "tools": [t["name"] for t in server._tools()]}

        @app.get("/", response_class=HTMLResponse)
        async def mission_control():  # type: ignore[no-redef]
            # Read-only by construction: the page only ever calls read tools
            # through the same /mcp endpoint (see mcp/ui.py).
            return mission_control_html()

        return app


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------
def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _modernise(response: dict) -> dict:
    """A legacy-shaped response, answered to a modern request: its result gains resultType."""
    if isinstance(response.get("result"), dict):
        response = {**response, "result": _protocol.complete(response["result"], SERVER_INFO)}
    return response


def _trace_current() -> Any:
    from ..kernel.trace import CURRENT

    return CURRENT


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


#: PE-Phase 3 (BOSSAPP-791, §3I): default compact character budget for MCP tool results so
#: the server never dumps unbounded JSON into an agent's context window.
DEFAULT_TOOL_OUTPUT_BUDGET_CHARS = 16_000


def budget_tool_payload(
    payload: Any,
    *,
    max_chars: int = DEFAULT_TOOL_OUTPUT_BUDGET_CHARS,
    more_handle: Optional[str] = None,
) -> tuple[str, bool]:
    """Serialize an MCP tool payload within ``max_chars``; when over budget, return a compact
    envelope with ``truncated=True`` and a ``more_handle`` pointer instead of unbounded JSON."""
    raw = json.dumps(payload, default=str)
    cap = max(256, int(max_chars))
    if len(raw) <= cap:
        return raw, False
    handle = more_handle or f"more:{abs(hash(raw)) % 10_000_000:07d}"
    preview_len = max(120, cap - 240)
    envelope: dict[str, Any] = {
        "truncated": True,
        "total_chars": len(raw),
        "budget_chars": cap,
        "more_handle": handle,
        "preview": raw[:preview_len],
    }
    if isinstance(payload, dict):
        envelope["keys"] = sorted(str(k) for k in payload.keys())[:40]
    return json.dumps(envelope, default=str), True


def _tool_result(
    payload: dict,
    *,
    is_error: bool = False,
    max_chars: int = DEFAULT_TOOL_OUTPUT_BUDGET_CHARS,
    more_handle: Optional[str] = None,
) -> dict:
    """MCP tools/call result: budgeted content blocks + isError flag."""
    text_out, _truncated = budget_tool_payload(payload, max_chars=max_chars, more_handle=more_handle)
    return {
        "content": [{"type": "text", "text": text_out}],
        "isError": bool(is_error),
    }


def _write(stream, obj: dict) -> None:
    stream.write(json.dumps(obj) + "\n")
    stream.flush()
