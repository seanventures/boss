"""Installable harness adapters: a MANIFEST, not code, behind the plugin trust gate (BOSSAPP-853).

A new coding agent should be addable without a BOSS release and without running anybody's Python
inside BOSS. So an installable harness is a `boss-harness.yaml` — what kind of agent it is
(`process`, `http`, `acp`), how to start or reach it, what it can do, and how to check the host
can run it — and BOSS drives it with its OWN adapters (custom_harness.ProcessCodeAgent /
HttpCodeAgent, acp_code_agent.AcpCodeAgent). Nothing from the plugin is imported or executed on
the hot path; the only thing it can make BOSS run is the agent itself, which is the point.

    apiVersion: boss.harness/v1
    kind: Harness
    metadata: {id: acme.review-bot, name: acme_review, version: 1.2.0, description: ...}
    spec:
      kind: process | http | acp
      capabilities: [mcp, skills, sessions, streams_turns]     # declared, shown to operators
      requires: ["ACME_TOKEN"]
      process: {argv: [...], promptVia: stdin|argv|file, timeoutSeconds, envPassthrough}
      http:    {baseUrl, tokenEnv, pollSeconds, timeoutSeconds}
      acp:     {binary, args, modelFlag, envPassthrough, timeoutSeconds}
      testEnvironment: {argv: [...], expect: <regex>, timeoutSeconds}
                     | {http: /healthz, expectStatus: 200}

WHERE THEY COME FROM
  * BOSS_HARNESS_PLUGIN_PATHS (colon-separated): a plugin directory, or a directory of them;
  * the `boss.harnesses` Python entry-point group: the entry point names its package, and the
    manifest is read from that package's files in the installed distribution — the package is
    never imported.

THE TRUST GATE (the platform plugins' rules, platform/registry.py)
  * the checksum is platform.registry.tree_checksum of the plugin's directory;
  * pinned in the harness lock (BOSS_HARNESS_LOCK, `harnesses: [{id, version, checksum}]`):
    "verified" — and refused if its files no longer match the pin;
  * not pinned: "community" — refused until an operator approves that exact checksum
    (BOSS_HARNESS_PLUGINS_APPROVED="<id>@sha256:…,…"), and refused again when its files change;
  * the `official.` namespace is reserved for BOSS; a plugin may not take a built-in's name.

Every plugin harness registers `verified=False`: the registry's honesty rule (a governed task has
completed on it) is about the AGENT, which a manifest cannot vouch for. `testEnvironment` runs
only when asked (`probe`), never while a task runs.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_log = logging.getLogger(__name__)

API_VERSION = "boss.harness/v1"
MANIFEST_FILE = "boss-harness.yaml"
ENTRY_POINT_GROUP = "boss.harnesses"
PATHS_ENV = "BOSS_HARNESS_PLUGIN_PATHS"
LOCK_ENV = "BOSS_HARNESS_LOCK"
APPROVED_ENV = "BOSS_HARNESS_PLUGINS_APPROVED"
CAPABILITIES = ("mcp", "skills", "sessions", "streams_turns", "read_only", "cost")
KINDS = ("process", "http", "acp")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class HarnessMetadata(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}\.[a-z0-9][a-z0-9.-]{1,63}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)([-+][0-9A-Za-z.-]+)?$")
    description: str = Field(min_length=1, max_length=400)


class ProcessSection(_Strict):
    argv: list[str] = Field(min_length=1)
    promptVia: Literal["stdin", "argv", "file"] = "stdin"
    timeoutSeconds: int = Field(default=2400, ge=10, le=86400)
    envPassthrough: list[str] = Field(default_factory=list)


class HttpSection(_Strict):
    baseUrl: str = Field(pattern=r"^https?://\S+$")
    tokenEnv: str = Field(default="", pattern=r"^([A-Z][A-Z0-9_]{0,63})?$")
    pollSeconds: float = Field(default=2.0, gt=0, le=60)
    timeoutSeconds: int = Field(default=2400, ge=10, le=86400)


class AcpSection(_Strict):
    binary: str = Field(pattern=r"^[A-Za-z0-9_./-]+$")
    args: list[str] = Field(default_factory=list)
    modelFlag: str = ""
    envPassthrough: list[str] = Field(default_factory=list)
    timeoutSeconds: int = Field(default=2400, ge=10, le=86400)
    signinHint: str = ""
    installHint: str = ""


class TestEnvironment(_Strict):
    argv: Optional[list[str]] = None
    expect: str = ""
    http: Optional[str] = None
    expectStatus: int = 200
    timeoutSeconds: int = Field(default=20, ge=1, le=300)

    @model_validator(mode="after")
    def _one(self) -> "TestEnvironment":
        if bool(self.argv) == bool(self.http):
            raise ValueError("testEnvironment needs exactly one of argv or http")
        if self.expect:
            re.compile(self.expect)
        return self


class HarnessPluginSpec(_Strict):
    kind: Literal["process", "http", "acp"]
    capabilities: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    process: Optional[ProcessSection] = None
    http: Optional[HttpSection] = None
    acp: Optional[AcpSection] = None
    testEnvironment: Optional[TestEnvironment] = None

    @model_validator(mode="after")
    def _section(self) -> "HarnessPluginSpec":
        for kind in KINDS:
            present = getattr(self, kind) is not None
            if kind == self.kind and not present:
                raise ValueError(f"kind {self.kind} needs a `{kind}:` section")
            if kind != self.kind and present:
                raise ValueError(f"kind {self.kind} must not carry a `{kind}:` section")
        unknown = sorted(set(self.capabilities) - set(CAPABILITIES))
        if unknown:
            raise ValueError(f"unknown capabilities {unknown}; known: {list(CAPABILITIES)}")
        return self


class HarnessManifest(_Strict):
    apiVersion: Literal["boss.harness/v1"]
    kind: Literal["Harness"]
    metadata: HarnessMetadata
    spec: HarnessPluginSpec


class HarnessManifestError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def validate_manifest(source: Union[str, bytes, dict]) -> HarnessManifest:
    data = yaml.safe_load(source) if isinstance(source, (str, bytes)) else source
    if not isinstance(data, dict):
        raise HarnessManifestError(["the manifest is not a mapping"])
    try:
        return HarnessManifest.model_validate(data)
    except ValidationError as exc:
        raise HarnessManifestError(
            [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
             for e in exc.errors()]) from None


# ── discovery and the trust gate ──────────────────────────────────────────────────────────────

@dataclass
class HarnessPlugin:
    directory: Path
    origin: str                      # "path" | "entry_point:<dist>"
    checksum: str
    manifest: Optional[HarnessManifest] = None
    source: str = "community"        # "verified" | "community"
    problems: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.manifest.metadata.id if self.manifest else f"invalid:{self.directory.name}"

    @property
    def usable(self) -> bool:
        return self.manifest is not None and not self.problems

    def view(self) -> dict[str, Any]:
        m = self.manifest
        return {"id": self.id, "name": m.metadata.name if m else None,
                "version": m.metadata.version if m else None,
                "kind": m.spec.kind if m else None, "source": self.source,
                "origin": self.origin, "checksum": self.checksum, "usable": self.usable,
                "capabilities": list(m.spec.capabilities) if m else [],
                "problems": list(self.problems), "directory": str(self.directory)}


def _checksum(directory: Path) -> str:
    from ..platform.registry import tree_checksum

    return tree_checksum(directory)


def _path_candidates() -> Iterable[tuple[str, Path]]:
    for entry in filter(None, (os.environ.get(PATHS_ENV) or "").split(":")):
        base = Path(entry)
        if (base / MANIFEST_FILE).is_file():
            yield "path", base
        elif base.is_dir():
            for d in sorted(p for p in base.iterdir() if p.is_dir()):
                if (d / MANIFEST_FILE).is_file():
                    yield "path", d


def _entry_point_candidates() -> Iterable[tuple[str, Path]]:
    """`boss.harnesses` entry points, resolved to a directory WITHOUT importing anything: the
    entry point's module names a package path inside its distribution."""
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001
        return
    for ep in eps:
        dist = getattr(ep, "dist", None)
        module = (ep.value.split(":", 1)[0] or "").strip()
        if dist is None or not module:
            continue
        try:
            directory = Path(str(dist.locate_file(module.replace(".", "/"))))
        except Exception:  # noqa: BLE001
            continue
        if (directory / MANIFEST_FILE).is_file():
            yield f"entry_point:{getattr(dist, 'name', '?')}", directory


def _lock() -> dict[str, dict[str, Any]]:
    path = os.environ.get(LOCK_ENV) or ""
    if not path or not os.path.isfile(path):
        return {}
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        _log.warning("harness lock %s could not be read; no harness plugin is verified", path)
        return {}
    return {f"{e.get('id')}@{e.get('version', '')}": e for e in data.get("harnesses") or []
            if isinstance(e, dict) and e.get("id")}


def _approved() -> dict[str, str]:
    out: dict[str, str] = {}
    for item in filter(None, (os.environ.get(APPROVED_ENV) or "").split(",")):
        plugin_id, _, checksum = item.strip().partition("@")
        if plugin_id and checksum:
            out[plugin_id] = checksum
    return out


def discover() -> list[HarnessPlugin]:
    """Every harness plugin BOSS can see, with its trust problems. Reads files; runs nothing."""
    lock, approved = _lock(), _approved()
    seen: dict[str, Path] = {}
    out: list[HarnessPlugin] = []
    for origin, directory in [*_path_candidates(), *_entry_point_candidates()]:
        plugin = HarnessPlugin(directory=directory, origin=origin,
                               checksum=_checksum(directory))
        try:
            plugin.manifest = validate_manifest(
                (directory / MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, HarnessManifestError, yaml.YAMLError) as exc:
            plugin.problems.extend(getattr(exc, "reasons", None) or [str(exc)])
            out.append(plugin)
            continue
        meta = plugin.manifest.metadata
        key = f"{meta.id}@{meta.version}"
        if key in seen:
            plugin.problems.append(f"{key} is also at {seen[key]}; ids and versions are unique")
        seen[key] = directory
        if meta.id.split(".", 1)[0] == "official":
            plugin.problems.append("the official. namespace is reserved for BOSS")
        pin = lock.get(key)
        if pin is not None:
            plugin.source = "verified"
            if pin.get("checksum") != plugin.checksum:
                plugin.problems.append(f"{key} does not match its pinned checksum; its files "
                                       "changed")
        else:
            plugin.source = "community"
            wanted = approved.get(meta.id)
            if wanted is None:
                plugin.problems.append(
                    f"{meta.id} is not approved; approve this exact build with "
                    f"{APPROVED_ENV}={meta.id}@{plugin.checksum}")
            elif wanted != plugin.checksum:
                plugin.problems.append("its files changed after it was approved; approve it "
                                       "again")
        out.append(plugin)
    return out


# ── building and registering ──────────────────────────────────────────────────────────────────

def builder_for(manifest: HarnessManifest):
    """A registry builder that constructs BOSS's OWN adapter from the manifest's data."""
    spec, name = manifest.spec, manifest.metadata.name
    description = manifest.metadata.description

    if spec.kind == "process":
        from .custom_harness import ProcessHarnessConfig

        p = spec.process
        config = ProcessHarnessConfig(name=name, argv=tuple(p.argv), prompt_via=p.promptVia,
                                      timeout=p.timeoutSeconds,
                                      env_passthrough=tuple(p.envPassthrough),
                                      description=description)

        def build_process(settings: Any) -> Any:
            from .custom_harness import ProcessCodeAgent

            return ProcessCodeAgent(settings, config)

        return build_process
    if spec.kind == "http":
        from .custom_harness import HttpHarnessConfig

        h = spec.http
        config_h = HttpHarnessConfig(name=name, base_url=h.baseUrl, token_env=h.tokenEnv,
                                     poll_seconds=h.pollSeconds, timeout=h.timeoutSeconds,
                                     description=description)

        def build_http(settings: Any) -> Any:
            from .custom_harness import HttpCodeAgent

            return HttpCodeAgent(settings, config_h)

        return build_http
    from .cli_harness import CliHarnessSpec

    a = spec.acp
    cli = CliHarnessSpec(name=name, binary=a.binary, args=tuple(a.args), protocol="acp",
                         model_flag=a.modelFlag, timeout=a.timeoutSeconds,
                         env_passthrough=tuple(a.envPassthrough), description=description,
                         signin_hint=a.signinHint, install_hint=a.installHint,
                         mcp_delivery="acp", registers_harness=False)

    def build_acp(settings: Any) -> Any:
        from .acp_code_agent import AcpCodeAgent

        return AcpCodeAgent(settings, cli)

    return build_acp


def register(plugin: HarnessPlugin) -> Optional[Any]:
    """Register one usable plugin's harness (verified=False). Refuses a name already taken."""
    if not plugin.usable:
        return None
    from .harness_registry import _REGISTRY, register_harness

    m = plugin.manifest
    name = m.metadata.name
    existing = _REGISTRY.get(name)
    if existing is not None and getattr(existing, "source", "builtin") != f"plugin:{m.metadata.id}":
        plugin.problems.append(f"harness name {name!r} is already registered")
        return None
    return register_harness(
        name, builder_for(m), kind=m.spec.kind, verified=False, replace=existing is not None,
        description=f"{m.metadata.description} [plugin {m.metadata.id}@{m.metadata.version}, "
                    f"{plugin.source}]",
        requires=tuple(m.spec.requires) or (f"the {m.spec.kind} agent the plugin describes",),
        source=f"plugin:{m.metadata.id}")


def load_harness_plugins() -> list[HarnessPlugin]:
    """Discover and register every usable harness plugin; return them all (problems included).
    A broken plugin is reported, never raised: it must not take the built-ins down with it."""
    try:
        plugins = discover()
    except Exception:  # noqa: BLE001
        _log.warning("harness plugins: discovery failed", exc_info=True)
        return []
    for plugin in plugins:
        if plugin.usable:
            try:
                register(plugin)
            except Exception as exc:  # noqa: BLE001
                plugin.problems.append(f"registration failed: {exc}")
        if plugin.problems:
            _log.warning("harness plugin %s not loaded: %s", plugin.id,
                         "; ".join(plugin.problems))
    return plugins


def probe(plugin: HarnessPlugin, *, opener: Any = None) -> dict[str, Any]:
    """Run the plugin's testEnvironment check, on demand only. {ok, detail}."""
    if plugin.manifest is None:
        return {"ok": False, "detail": "; ".join(plugin.problems) or "invalid manifest"}
    test = plugin.manifest.spec.testEnvironment
    if test is None:
        return {"ok": True, "detail": "the plugin declares no environment check"}
    if test.argv:
        from .agent_env import agent_environment

        try:
            done = subprocess.run(list(test.argv), capture_output=True, text=True,
                                  timeout=test.timeoutSeconds, env=agent_environment(os.environ),
                                  check=False)
        except FileNotFoundError:
            return {"ok": False, "detail": f"{test.argv[0]} is not installed"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": f"no answer within {test.timeoutSeconds}s"}
        said = f"{done.stdout or ''}\n{done.stderr or ''}"
        ok = done.returncode == 0 and (not test.expect or re.search(test.expect, said)
                                       is not None)
        return {"ok": ok, "detail": said.strip()[:300] or f"exit {done.returncode}"}
    import urllib.error
    import urllib.request

    base = plugin.manifest.spec.http.baseUrl if plugin.manifest.spec.http else ""
    url = base.rstrip("/") + "/" + str(test.http).lstrip("/") if base else str(test.http)
    try:
        with (opener or urllib.request.urlopen)(url, timeout=test.timeoutSeconds) as resp:
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "detail": f"{url}: {exc}"}
    return {"ok": status == test.expectStatus, "detail": f"{url} answered {status}"}
