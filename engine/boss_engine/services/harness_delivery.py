"""The agent's kit — its MCP servers and its skills — handed to WHICHEVER harness runs it
(BOSSAPP-854).

WHY. Only Claude Code and the Agent SDK read `mcp_harness` (code_task_mcp). An agent whose
operator gave it "Library docs" and two skills kept them on claude_code and lost them, without a
word, the moment its job was routed to codex, cursor or anything else. The engine contract says
behaviour is identical on every engine; a kit that vanishes on an engine swap is the opposite.

So every harness DECLARES how its kit reaches it (`CliHarnessSpec.mcp_delivery`,
`CliHarnessSpec.skills_dir`), and one function writes it per run:

    mode              how the MCP servers reach the harness
    ───────────────── ──────────────────────────────────────────────────────────────────────────
    claude_flag       `--strict-mcp-config --mcp-config <file>` (+ --disallowedTools) — exists
    codex_home        a per-run CODEX_HOME whose config.toml holds `[mcp_servers.<name>]`
                      (url, http_headers, disabled_tools); everything else in the operator's
                      Codex home is linked in, so the login and resumable sessions still work
    cursor_workspace  `<workspace>/.cursor/mcp.json`, put back as it was when the run ends
    acp               the Agent Client Protocol's own negotiation: `session/new.mcpServers`,
                      sent only when the agent's `mcpCapabilities.http` says it can take them
    env_manifest      a JSON manifest at $BOSS_MCP_CONFIG (process / http harness kinds)
    none              the harness has no MCP route; the run says so in its log, never silently

Skills are a DIRECTORY (`<key>/SKILL.md` per skill) synced per run to where the engine reads
skills (`skills_dir`: "{workspace}/…", "{home}/…", or "{env}" for $BOSS_SKILLS_DIR), and removed
again when the run ends. An engine with no skills directory is recorded as such.

WHAT AN ENGINE THAT CANNOT REFUSE A TOOL GETS. claude and codex can be told which tools of a
server not to call (`--disallowedTools`, `disabled_tools`). cursor, ACP agents and custom
process/http agents cannot. With the MCP gateway on (BOSSAPP-814) that does not matter — the
gateway refuses Off/ungranted calls and parks Ask ones. With it off, a server carrying any tool
the agent may not call is WITHHELD from such an engine (recorded in `withheld`), because handing
it over would turn Off into On.

Nothing here changes a run whose agent holds no servers and no skills: no file is written, no
argv or environment changes.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

_log = logging.getLogger(__name__)

MCP_DELIVERY = ("claude_flag", "codex_home", "cursor_workspace", "acp", "env_manifest", "none")
#: Modes whose engine honours a per-tool refusal list.
_CAN_DISALLOW = frozenset({"claude_flag", "codex_home"})
#: The env names the env_manifest handoff uses (documented in HARNESS_ADAPTERS.md).
MCP_CONFIG_ENV = "BOSS_MCP_CONFIG"
SKILLS_DIR_ENV = "BOSS_SKILLS_DIR"
#: Prefix on every skill directory BOSS syncs into a place the engine reads, so cleanup removes
#: only what BOSS wrote and a repository's own skills are never touched.
SKILL_PREFIX = "boss-"


# ── reading what code_task_mcp wrote ──────────────────────────────────────────────────────────

def servers_of(mcp: Any) -> dict[str, dict[str, Any]]:
    """{name: {type, url, headers?}} from a HarnessMcp's file ({} when there is none)."""
    path = getattr(mcp, "path", "") or ""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        _log.warning("harness kit: the run's MCP file could not be read; no servers delivered")
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return {str(k): dict(v) for k, v in (servers or {}).items() if isinstance(v, dict)}


def _disallowed_by_server(mcp: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in getattr(mcp, "disallowed", None) or []:
        parts = str(item).split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp":
            out.setdefault(parts[1], []).append(parts[2])
    return out


def skills_in(source: Optional[str]) -> list[Path]:
    """The skill directories (each holding a SKILL.md) in a bundle directory."""
    if not source or not os.path.isdir(source):
        return []
    return sorted(p for p in Path(source).iterdir() if (p / "SKILL.md").is_file())


# ── the delivery ──────────────────────────────────────────────────────────────────────────────

@dataclass
class KitDelivery:
    """What one run's harness was given. `cleanup` undoes every file it wrote."""

    engine: str
    mode: str
    servers: list[str] = field(default_factory=list)
    #: Servers held but not handed over, with why ("cannot_disallow", "no_http_capability",
    #: "no_mcp_route").
    withheld: dict[str, str] = field(default_factory=dict)
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    #: ACP `session/new.mcpServers` entries (spec: McpServer, http variant).
    acp_servers: list[dict[str, Any]] = field(default_factory=list)
    #: Where the skills were synced ("" none) and which.
    skills_path: str = ""
    skills: list[str] = field(default_factory=list)
    skills_skipped: str = ""
    written: list[str] = field(default_factory=list)
    _undo: list[Callable[[], None]] = field(default_factory=list, repr=False)
    _codex_homes: dict[str, str] = field(default_factory=dict, repr=False)
    _codex_plan: Optional[dict[str, Any]] = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {"engine": self.engine, "mcp_delivery": self.mode, "servers": list(self.servers),
                "withheld": dict(self.withheld), "skills": list(self.skills),
                "skills_path": self.skills_path, "skills_skipped": self.skills_skipped}

    def env_for(self, base: dict[str, str], *, default_home: str = "") -> dict[str, str]:
        """The env additions for a child whose environment is `base`. codex_home is built
        lazily from the CODEX_HOME the child would otherwise use (a job's API-key pick moves it),
        once per source home."""
        out = dict(self.env)
        if self._codex_plan is not None:
            source = os.path.expanduser(base.get("CODEX_HOME") or default_home or "~/.codex")
            if source not in self._codex_homes:
                self._codex_homes[source] = self._build_codex_home(source)
            out["CODEX_HOME"] = self._codex_homes[source]
        return out

    def _build_codex_home(self, source: str) -> str:
        plan = self._codex_plan or {}
        home = tempfile.mkdtemp(prefix="boss-codex-home-")
        os.chmod(home, 0o700)
        self.written.append(home)
        if os.path.isdir(source):
            for entry in os.listdir(source):
                if entry in ("config.toml", "skills"):
                    continue
                os.symlink(os.path.join(source, entry), os.path.join(home, entry))
        config: dict[str, Any] = {}
        original = os.path.join(source, "config.toml")
        if os.path.isfile(original):
            try:
                with open(original, "rb") as handle:
                    config = tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError):
                _log.warning("codex kit: the operator's config.toml could not be read; "
                             "the run's config carries only the agent's servers")
                config = {}
        # STRICT, like claude's --strict-mcp-config: the agent's servers and nothing else the
        # operator's own config names.
        config.pop("mcp_servers", None)
        if plan.get("servers"):
            config["mcp_servers"] = plan["servers"]
        with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write(toml_dumps(config))
        os.chmod(os.path.join(home, "config.toml"), 0o600)
        if plan.get("skills_source"):
            self.skills = _sync_skills(plan["skills_source"], os.path.join(home, "skills"),
                                       prefix="")
            self.skills_path = os.path.join(home, "skills")

        def _undo() -> None:
            # A login Codex rotated during the run was written in place of the link: keep it.
            auth = os.path.join(home, "auth.json")
            if os.path.isfile(auth) and not os.path.islink(auth) and os.path.isdir(source):
                with contextlib.suppress(OSError):
                    shutil.copyfile(auth, os.path.join(source, "auth.json"))
            shutil.rmtree(home, ignore_errors=True)

        self._undo.append(_undo)
        return home

    def acp_servers_for(self, *, http: bool) -> list[dict[str, Any]]:
        """ACP's negotiation: the servers go in `session/new` only when the agent's
        `initialize` answer said `mcpCapabilities.http`; otherwise each is recorded withheld."""
        if http or not self.acp_servers:
            return list(self.acp_servers)
        for entry in self.acp_servers:
            self.withheld[entry["name"]] = "no_http_capability"
        _log.warning("harness kit: %s does not take HTTP MCP servers over ACP; withheld %s",
                     self.engine, ", ".join(e["name"] for e in self.acp_servers))
        self.servers = [s for s in self.servers if s not in self.withheld]
        self.acp_servers = []
        return []

    def cleanup(self) -> None:
        while self._undo:
            undo = self._undo.pop()
            try:
                undo()
            except Exception:  # noqa: BLE001 — cleanup never fails a run
                _log.debug("harness kit: cleanup step failed", exc_info=True)


def _sync_skills(source: str, target: str, *, prefix: str = SKILL_PREFIX) -> list[str]:
    os.makedirs(target, exist_ok=True)
    names = []
    for skill in skills_in(source):
        name = f"{prefix}{skill.name}" if prefix and not skill.name.startswith(prefix) \
            else skill.name
        dest = os.path.join(target, name)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(skill, dest, symlinks=False)
        names.append(name)
    return names


def _workspace_skills(source: str, target: str) -> tuple[list[str], Callable[[], None]]:
    """Sync into a directory inside the workspace; the undo removes exactly what was added
    (and the directories BOSS had to create), never a repository's own skills."""
    created: list[str] = []
    probe = Path(target)
    while not probe.exists():
        created.append(str(probe))
        probe = probe.parent
    names = _sync_skills(source, target)

    def undo() -> None:
        for name in names:
            shutil.rmtree(os.path.join(target, name), ignore_errors=True)
        for path in created:  # deepest first
            with contextlib.suppress(OSError):
                os.rmdir(path)

    return names, undo


def _http_headers(entry: dict[str, Any]) -> dict[str, str]:
    headers = entry.get("headers") or {}
    return {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}


def deliver(*, engine: str, mode: str, skills_dir: str = "", mcp: Any = None,
            workspace: Optional[Path] = None, gateway: Optional[bool] = None) -> KitDelivery:
    """Write the kit for one run. Callers MUST call `cleanup()` (see `kit_for_run`)."""
    mode = mode if mode in MCP_DELIVERY else "none"
    kit = KitDelivery(engine=engine, mode=mode)
    servers = servers_of(mcp) if mcp is not None else {}
    if gateway is None:
        gateway = bool(getattr(mcp, "gateway_token", ""))
    disallowed = _disallowed_by_server(mcp) if mcp is not None else {}
    skills_source = getattr(mcp, "skills_dir", "") if mcp is not None else ""

    # Which servers this engine may be handed.
    usable: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if mode == "none":
            kit.withheld[name] = "no_mcp_route"
        elif mode not in _CAN_DISALLOW and not gateway and disallowed.get(name):
            kit.withheld[name] = "cannot_disallow"
        else:
            usable[name] = entry
    kit.servers = sorted(usable)
    if kit.withheld:
        _log.warning("harness kit: %s did not receive MCP server(s) %s (%s)", engine,
                     ", ".join(sorted(kit.withheld)),
                     ", ".join(sorted(set(kit.withheld.values()))))
    if usable and mcp is not None and mode != "claude_flag":
        # The gateway token belongs to the run whose session is starting (claude does this in
        # HarnessMcp.argv()).
        with contextlib.suppress(Exception):
            mcp.bind_session()
    if usable and mcp is not None and hasattr(mcp, "env"):
        kit.env.update(mcp.env())

    if mode == "claude_flag":
        # The driver appends HarnessMcp.argv() where its argv is built (ClaudeCodeAgent,
        # AgentSdk, GenericCliCodeAgent._argv); computing it here would bind the gateway
        # token a second time.
        pass
    elif mode == "codex_home" and (usable or skills_in(skills_source)):
        timeout = None
        if gateway:
            with contextlib.suppress(Exception):
                from .mcp_gateway import ask_timeout_seconds
                timeout = int(ask_timeout_seconds() + 300)
        table: dict[str, Any] = {}
        for name, entry in usable.items():
            row: dict[str, Any] = {"url": entry.get("url", "")}
            headers = _http_headers(entry)
            if headers:
                row["http_headers"] = headers
            if disallowed.get(name):
                row["disabled_tools"] = sorted(disallowed[name])
            if timeout:
                row["tool_timeout_sec"] = timeout
            table[name] = row
        kit._codex_plan = {"servers": table,
                           "skills_source": skills_source if skills_dir else ""}
    elif mode == "cursor_workspace" and usable and workspace is not None:
        target = Path(workspace) / ".cursor" / "mcp.json"
        existed_dir = target.parent.exists()
        before = target.read_bytes() if target.is_file() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"mcpServers": {
            name: {"url": e.get("url", ""), **({"headers": _http_headers(e)}
                                               if _http_headers(e) else {})}
            for name, e in usable.items()}}, indent=2), encoding="utf-8")
        os.chmod(target, 0o600)
        kit.written.append(str(target))

        def _restore() -> None:
            if before is not None:
                target.write_bytes(before)
            else:
                with contextlib.suppress(OSError):
                    target.unlink()
                if not existed_dir:
                    with contextlib.suppress(OSError):
                        target.parent.rmdir()

        kit._undo.append(_restore)
    elif mode == "acp":
        kit.acp_servers = [
            {"type": "http", "name": name, "url": e.get("url", ""),
             "headers": [{"name": k, "value": v} for k, v in _http_headers(e).items()]}
            for name, e in sorted(usable.items())]
    elif mode == "env_manifest" and (usable or skills_in(skills_source)):
        directory = tempfile.mkdtemp(prefix="boss-kit-")
        os.chmod(directory, 0o700)
        manifest = os.path.join(directory, "mcp.json")
        with open(manifest, "w", encoding="utf-8") as handle:
            json.dump({"contract": "boss.harness.kit/v1",
                       "mcpServers": usable,
                       "disallowed": {k: sorted(v) for k, v in disallowed.items() if k in usable},
                       "gateway": bool(gateway)}, handle)
        os.chmod(manifest, 0o600)
        kit.env[MCP_CONFIG_ENV] = manifest
        kit.written.append(manifest)
        kit._undo.append(lambda: shutil.rmtree(directory, ignore_errors=True))
        if skills_dir == "{env}" and skills_in(skills_source):
            target = os.path.join(directory, "skills")
            kit.skills = _sync_skills(skills_source, target, prefix="")
            kit.skills_path = target
            kit.env[SKILLS_DIR_ENV] = target

    # Skills into a workspace path (claude, opencode) — codex/env handled above.
    if skills_in(skills_source) and not kit.skills_path and kit._codex_plan is None:
        if skills_dir.startswith("{workspace}") and workspace is not None:
            target = os.path.join(str(workspace), skills_dir[len("{workspace}"):].lstrip("/"))
            kit.skills, undo = _workspace_skills(skills_source, target)
            kit.skills_path = target
            kit._undo.append(undo)
        elif not skills_dir or skills_dir == "{env}":
            kit.skills_skipped = "no_skills_route"
            _log.warning("harness kit: %s has no skills directory; %d skill(s) not delivered",
                         engine, len(skills_in(skills_source)))
    return kit


# ── TOML (only what Codex's config needs: tables, strings, numbers, bools, lists) ──────────

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(str(k))} = {_value(x)}" for k, x in v.items()) + " }"
    if hasattr(v, "isoformat"):
        return v.isoformat()
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def toml_dumps(data: dict[str, Any], _prefix: str = "") -> str:
    """A TOML writer for a parsed config (tomllib's output shape). Scalars and inline values
    first, then sub-tables as `[a.b]` sections; arrays of tables as `[[a]]`."""
    lines: list[str] = []
    tables: list[tuple[str, dict]] = []
    arrays: list[tuple[str, list]] = []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            arrays.append((k, v))
        else:
            lines.append(f"{_key(str(k))} = {_value(v)}")
    out = "\n".join(lines) + ("\n" if lines else "")
    for k, v in tables:
        name = f"{_prefix}{_key(str(k))}"
        out += f"\n[{name}]\n" + toml_dumps(v, f"{name}.")
    for k, items in arrays:
        name = f"{_prefix}{_key(str(k))}"
        for item in items:
            out += f"\n[[{name}]]\n" + toml_dumps(item, f"{name}.")
    return out


# ── per-run lifetime ──────────────────────────────────────────────────────────────────────────

_CURRENT: contextvars.ContextVar[Optional[KitDelivery]] = contextvars.ContextVar(
    "boss_harness_kit", default=None)


def current() -> Optional[KitDelivery]:
    """The kit of the run_role this thread is inside (None outside one)."""
    return _CURRENT.get()


def kit_profile(agent: Any) -> tuple[str, str]:
    """(mcp_delivery, skills_dir) the agent declares: its CLI spec's fields, else the class's
    KIT_PROFILE, else ("none", "")."""
    spec = getattr(agent, "spec", None)
    mode = getattr(spec, "mcp_delivery", "") if spec is not None else ""
    if mode:
        return mode, getattr(spec, "skills_dir", "") or ""
    profile = getattr(agent, "KIT_PROFILE", None)
    if profile:
        return profile
    return "none", ""


@contextlib.contextmanager
def kit_for_run(agent: Any, cwd: Optional[Path], **extra: Any) -> Iterator[Optional[KitDelivery]]:
    """Deliver `agent.mcp_harness` (+ its skills) for one session and undo it afterwards.

    Nested calls (a contracted wrapper around a driver that is itself wrapped) reuse the outer
    delivery. An agent holding nothing gets None and nothing is written."""
    outer = _CURRENT.get()
    if outer is not None:
        yield outer
        return
    mcp = getattr(agent, "mcp_harness", None)
    if mcp is None:
        yield None
        return
    mode, skills_dir = kit_profile(agent)
    kit = deliver(engine=str(getattr(agent, "engine", "") or ""), mode=mode,
                  skills_dir=skills_dir, mcp=mcp, workspace=cwd, **extra)
    token = _CURRENT.set(kit)
    try:
        yield kit
    finally:
        _CURRENT.reset(token)
        kit.cleanup()


def delivers_kit(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for `run_role(self, prompt, cwd, ...)`: the kit lives for the session."""

    @functools.wraps(fn)
    def wrapper(self: Any, prompt: str, cwd: Any = None, *args: Any, **kwargs: Any) -> Any:
        where = cwd if cwd is not None else kwargs.get("cwd")
        with kit_for_run(self, Path(where) if where is not None else None):
            if cwd is None:
                return fn(self, prompt, *args, **kwargs)
            return fn(self, prompt, cwd, *args, **kwargs)

    return wrapper
