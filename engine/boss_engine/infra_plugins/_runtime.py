"""What every official plugin shares: Docker/Compose calls, probes, secrets, redaction, naming.

One code path for every provider. A plugin renders a COMPOSE FRAGMENT (the same file format
`boss.deploy.yaml kind: compose` already deploys) and brings it up with `docker compose` against
the Docker endpoint of the process (`DOCKER_HOST` reaches a remote host the same way). The
`official.docker-compose` environment provider assembles every fragment of an app into the one
file the existing stack deploy runs, so nothing here is a second deploy path.

Rules this module enforces for every plugin, so no plugin can forget one:

  * a secret value enters through `Session.put` / `Session.get` and is REMEMBERED; every log line,
    error message and subprocess output is scrubbed of every remembered value before it leaves;
  * a value reaches a container only through the subprocess ENVIRONMENT (`${VAR}` interpolation
    in the fragment, `-e NAME` with no value on a probe), never argv and never a written file;
  * every container, volume and probe carries `boss.plugin`, `boss.resource`, `boss.app` and the
    caller's labels, so cleanup is checkable by label;
  * data services publish NO port. Verify reaches them the way the app does: a short-lived probe
    container on the app's compose network, using the binding the app gets.
"""
from __future__ import annotations

import os
import re
import secrets as _secrets
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import yaml

from ..platform.contract import (
    Hook, HookContext, InfraPlugin, OperationResult, OperationStatus, PlannedChange, PortSpec, ResourceMode, ResourcePlan, SecretRef, load_manifest,
)

#: A pinned container image: `name@sha256:<64 hex>` or `name:Tag@sha256:<64 hex>` (tags may hold upper case).
_PINNED = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9_.-]{1,128})?@sha256:[0-9a-f]{64}$")
#: Never shorter than this is a value worth scrubbing (a 1-char "secret" would scrub every log).
_MIN_SCRUB = 6
DEFAULT_TIMEOUT = 180


class HookFailure(Exception):
    """Raised inside a hook; `OfficialPlugin.run` turns it into a scrubbed `failed` result."""

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 status: OperationStatus = OperationStatus.FAILED) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable, self.status = code, message, retryable, status


def official_dir() -> Path:
    """Where the official manifests live: `plugins/official/` at the repository (or image) root,
    found the way `templates.service.builtin_dir` finds `templates/apps`."""
    configured = os.environ.get("BOSS_OFFICIAL_PLUGINS_DIR")
    if configured:
        return Path(configured)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "plugins" / "official"
        if candidate.is_dir():
            return candidate
    return here.parents[3] / "plugins" / "official"


# ---------------------------------------------------------------------------------------------
# Naming. Deterministic, so every hook (and the compose provider) derives the same names.
# ---------------------------------------------------------------------------------------------

def _slug(value: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:limit] or "x"


def project_name(ctx: HookContext) -> str:
    """One compose project per (app, environment). `bossapp-` keeps it clear of BOSS's own
    `boss-community` project, whatever an app is called."""
    return f"bossapp-{_slug(ctx.app_key)}-{_slug(ctx.resource.environment, 16)}"


def network_name(ctx: HookContext) -> str:
    return f"{project_name(ctx)}_default"


def service_name(ctx_or_name: Union[HookContext, str]) -> str:
    """`res-<resource>`: a prefix an app's own compose services do not use, so a merge can
    never silently replace an app service with a resource (or the reverse)."""
    name = ctx_or_name if isinstance(ctx_or_name, str) else ctx_or_name.resource.name
    return f"res-{_slug(name, 48)}"


def env_prefix(ctx: HookContext) -> str:
    return "BOSS_" + re.sub(r"[^A-Z0-9]+", "_", ctx.resource.name.upper())


def secret_name(ctx: HookContext, key: str) -> str:
    """The vault name for one of this resource's values: `DB_ADMIN_PASSWORD`."""
    return re.sub(r"[^A-Z0-9]+", "_", ctx.resource.name.upper()) + "_" + key


def labels(ctx: HookContext, plugin_id: str) -> dict[str, str]:
    out = {"boss.plugin": plugin_id, "boss.resource": ctx.resource_id, "boss.app": ctx.app_key}
    out.update({str(k): str(v) for k, v in (ctx.labels or {}).items()})
    return out


def new_password(nbytes: int = 24) -> str:
    """URL-safe without percent-encoding: hex."""
    return _secrets.token_hex(nbytes)


# ---------------------------------------------------------------------------------------------
# A config schema check: the subset of JSON Schema the official manifests use.
# ---------------------------------------------------------------------------------------------

_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list,
          "number": (int, float)}


def schema_problems(value: Any, schema: dict, where: str = "config") -> list[str]:
    """type, properties, required, additionalProperties: false, enum, pattern, minimum, maximum,
    items. Enough for every official plugin; a community plugin's richer schema is checked by
    the platform's validator, not here."""
    out: list[str] = []
    t = schema.get("type")
    if t:
        py = _TYPES[t]
        if isinstance(value, bool) and t in ("integer", "number"):
            return [f"{where} must be {t}"]
        if not isinstance(value, py):
            return [f"{where} must be {t}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where} must be one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, str) and "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        out.append(f"{where} does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{where} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{where} must be <= {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where}.{key} is required")
        for key, sub in value.items():
            if key in props:
                out.extend(schema_problems(sub, props[key], f"{where}.{key}"))
            elif schema.get("additionalProperties") is False:
                out.append(f"{where}.{key} is not a setting this plugin has")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out.extend(schema_problems(item, schema["items"], f"{where}[{i}]"))
    return out


# ---------------------------------------------------------------------------------------------
# The per-hook session: secrets remembered for redaction, subprocesses, compose, probes.
# ---------------------------------------------------------------------------------------------

class Session:
    def __init__(self, ctx: HookContext, plugin_id: str) -> None:
        self.ctx = ctx
        self.plugin_id = plugin_id
        self._values: set[str] = set()
        self.lines: list[str] = []

    # -- secrets ------------------------------------------------------------------------------
    def remember(self, value: Optional[str]) -> None:
        if value and len(value) >= _MIN_SCRUB:
            self._values.add(value)

    def put(self, key: str, value: str) -> SecretRef:
        self.remember(value)
        ref = self.ctx.secrets.put(secret_name(self.ctx, key), value)
        return ref if isinstance(ref, SecretRef) else SecretRef(secretRef=str(ref))

    def get(self, ref: Union[str, SecretRef, None]) -> str:
        if not ref:
            raise HookFailure("state_lost", "a secret reference this resource needs is missing from "
                              "its state; run provision again or restore the state record")
        value = self.ctx.secrets.resolve(ref)
        self.remember(value)
        return value

    def scrub(self, text: Any) -> str:
        s = "" if text is None else str(text)
        for v in sorted(self._values, key=len, reverse=True):
            s = s.replace(v, "[redacted]")
        # a URL's userinfo is a credential even when this session never saw the value
        return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1[redacted]@", s)

    def log(self, line: str) -> None:
        clean = self.scrub(line)
        self.lines.append(clean)
        try:
            self.ctx.log(clean)
        except Exception:  # a logger must never fail the operation
            pass

    def check_cancel(self) -> None:
        if self.ctx.is_cancelled():
            raise HookFailure("cancelled", "the operation was cancelled", status=OperationStatus.CANCELLED)

    def _timeout(self, timeout: Optional[float]) -> float:
        t = float(timeout or DEFAULT_TIMEOUT)
        if self.ctx.deadline is not None:
            t = min(t, max(1.0, self.ctx.deadline - time.monotonic()))
        return t

    # -- processes ----------------------------------------------------------------------------
    def run(self, argv: list[str], *, env: Optional[dict[str, str]] = None, stdin: Optional[bytes] = None,
            timeout: Optional[float] = None, check: bool = True, what: str = "") -> subprocess.CompletedProcess:
        """argv only (no shell). `env` values are remembered and passed in the ENVIRONMENT."""
        self.check_cancel()
        for v in (env or {}).values():
            self.remember(v)
        full_env = {**os.environ, **(env or {})}
        try:
            proc = subprocess.run(argv, input=stdin, capture_output=True, env=full_env,
                                  timeout=self._timeout(timeout))
        except subprocess.TimeoutExpired:
            raise HookFailure("timeout", f"{what or argv[0]} did not finish in time", retryable=True) from None
        except FileNotFoundError:
            raise HookFailure("missing_tool", f"{argv[0]} is not installed where BOSS runs this plugin") from None
        if check and proc.returncode != 0:
            err = self.scrub(proc.stderr.decode(errors="replace").strip() or
                             proc.stdout.decode(errors="replace").strip())[-600:]
            raise HookFailure("command_failed", f"{what or ' '.join(argv[:3])} failed: {err}",
                              retryable=True)
        return proc

    def text(self, proc: subprocess.CompletedProcess) -> str:
        return self.scrub(proc.stdout.decode(errors="replace"))

    # -- compose ------------------------------------------------------------------------------
    def fragment_path(self) -> Path:
        d = Path(self.ctx.workdir) / "compose"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.ctx.resource.name}.yaml"

    def write_fragment(self, fragment: dict) -> Path:
        """The fragment holds `${VAR}` placeholders, never a value: checked, not hoped."""
        text = yaml.safe_dump(fragment, sort_keys=False)
        for v in self._values:
            if v in text:
                raise HookFailure("secret_in_file", "a secret value reached a compose fragment; refusing to write it")
        p = self.fragment_path()
        p.write_text(text, encoding="utf-8")
        return p

    def compose(self, args: list[str], *, files: Optional[list[Path]] = None,
                env: Optional[dict[str, str]] = None, timeout: Optional[float] = None,
                check: bool = True, what: str = "") -> subprocess.CompletedProcess:
        argv = ["docker", "compose", "-p", project_name(self.ctx)]
        for f in files or [self.fragment_path()]:
            argv += ["-f", str(f)]
        return self.run(argv + args, env=env, timeout=timeout, check=check, what=what or "docker compose")

    def compose_up(self, service: str, env: dict[str, str], *, wait: bool = True,
                   timeout: float = 240) -> None:
        args = ["up", "-d"]
        if wait:
            args += ["--wait", "--wait-timeout", str(int(timeout))]
        self.compose(args + [service], env=env, timeout=timeout + 30, what=f"starting {service}")

    def container_id(self, service: str) -> Optional[str]:
        proc = self.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project_name(self.ctx)}",
                         "--filter", f"label=com.docker.compose.service={service}"], check=False)
        ids = proc.stdout.decode().split()
        return ids[0] if ids else None

    def compose_destroy(self, service: str, volumes: Iterable[str] = ()) -> list[str]:
        """Remove the service's container, its named volumes and, when nothing else uses it, the
        project network. Returns what was removed (names only)."""
        removed: list[str] = []
        cid = self.container_id(service)
        if cid:
            self.run(["docker", "rm", "-f", "-v", cid], what=f"removing {service}")
            removed.append(f"container:{service}")
        for vol in volumes:
            full = f"{project_name(self.ctx)}_{vol}"
            proc = self.run(["docker", "volume", "rm", "-f", full], check=False)
            if proc.returncode == 0:
                removed.append(f"volume:{vol}")
        self.remove_network_if_unused()
        frag = self.fragment_path()
        if frag.exists():
            frag.unlink()
            removed.append(f"file:compose/{frag.name}")
        return removed

    def remove_network_if_unused(self) -> None:
        net = network_name(self.ctx)
        proc = self.run(["docker", "network", "inspect", net, "--format", "{{len .Containers}}"], check=False)
        if proc.returncode == 0 and proc.stdout.decode().strip() == "0":
            self.run(["docker", "network", "rm", net], check=False)

    # -- probes -------------------------------------------------------------------------------
    def probe(self, image: str, argv: list[str], *, env: Optional[dict[str, str]] = None,
              stdin: Optional[bytes] = None, network: Optional[str] = None, entrypoint: Optional[str] = None,
              timeout: float = 90, check: bool = True, what: str = "probe") -> subprocess.CompletedProcess:
        """A throwaway container on the app's network. Values travel as `-e NAME` (no `=value`)
        so they are read from this process's environment and never appear in argv."""
        cmd = ["docker", "run", "--rm", "-i", "--network", network or network_name(self.ctx)]
        for k, v in {**labels(self.ctx, self.plugin_id), "boss.probe": "1"}.items():
            cmd += ["--label", f"{k}={v}"]
        for k in (env or {}):
            cmd += ["-e", k]
        if entrypoint is not None:
            cmd += ["--entrypoint", entrypoint]
        return self.run(cmd + [image] + argv, env=env, stdin=stdin, timeout=timeout, check=check, what=what)


# ---------------------------------------------------------------------------------------------
# The base class
# ---------------------------------------------------------------------------------------------

class OfficialPlugin(InfraPlugin):
    """An official plugin: the manifest is `plugins/official/<directory>/boss-plugin.yaml`.

    Subclasses implement `_validate`, `_changes` and the hook methods prefixed `do_`; this class
    owns schema checks, the attached-mode and approved-plan guards, timing and redaction."""

    directory: str = ""
    #: `boss.plugin` label value and receipts; read from the manifest.
    _session: Optional[Session] = None

    def __init__(self) -> None:
        if self.manifest is None:
            type(self).manifest = load_manifest(official_dir() / self.directory)

    @property
    def plugin_id(self) -> str:
        return self.manifest.id if self.manifest else self.directory

    # -- dispatch -----------------------------------------------------------------------------
    def run(self, ctx: HookContext) -> OperationResult:
        ctx.plugin_id = ctx.plugin_id or self.plugin_id
        ctx.plugin_version = ctx.plugin_version or (self.manifest.version if self.manifest else "")
        session = Session(ctx, self.plugin_id)
        started = time.monotonic()
        if not self.manifest.implements(ctx.hook):
            result = ctx.result(OperationStatus.UNSUPPORTED)
        else:
            try:
                if ctx.hook is not Hook.VALIDATE:
                    problems = self.problems(ctx)
                    if problems:
                        raise HookFailure("invalid_request", "; ".join(problems))
                if (ctx.resource.mode is ResourceMode.ATTACHED
                        and ctx.hook in (Hook.PROVISION, Hook.CONFIGURE)):
                    # Nothing to create: the app binds to the vault secrets the operator set.
                    result = ctx.result(bindings=self.attached_bindings(ctx), state={"mode": "attached"},
                                        evidence=[evidence("attach", True, "config", "bound to existing secrets")])
                elif ctx.resource.mode is ResourceMode.ATTACHED and ctx.hook in (Hook.BACKUP, Hook.RESTORE):
                    raise HookFailure("attached_resource", "BOSS does not back up or restore a resource it "
                                      "did not create", status=OperationStatus.REFUSED)
                else:
                    result = getattr(self, f"do_{ctx.hook.value}")(ctx, session)
            except HookFailure as exc:
                result = ctx.result(exc.status, errors=[{"code": exc.code, "message": session.scrub(exc.message),
                                                         "retryable": exc.retryable}])
            except Exception as exc:  # a plugin bug is a failed operation, never a crashed worker
                result = ctx.fail("plugin_error", session.scrub(f"{type(exc).__name__}: {exc}")[:900])
        result.durationMs = int((time.monotonic() - started) * 1000)
        # Last line of defence: nothing the session saw leaves in the result.
        dumped = result.model_dump_json()
        if session.scrub(dumped) != dumped:
            return ctx.fail("secret_in_result", "the result carried a secret value; it was withheld")
        return result

    # -- validate -----------------------------------------------------------------------------
    def problems(self, ctx: HookContext) -> list[str]:
        r = ctx.resource
        out = []
        if r.capability not in self.manifest.spec.capabilities:
            out.append(f"{self.plugin_id} provides {', '.join(self.manifest.spec.capabilities)}, not {r.capability}")
        if r.engine and self.manifest.spec.engines and r.engine not in self.manifest.spec.engines:
            out.append(f"engine {r.engine!r} is not one of {', '.join(self.manifest.spec.engines)}")
        out += schema_problems(r.config, self.manifest.spec.configSchema or {"type": "object"})
        image = r.config.get("image") if isinstance(r.config, dict) else None
        if image and not _PINNED.fullmatch(str(image)):
            out.append("config.image must be pinned by digest (name@sha256:...)")
        if r.public and not self.public_allowed(ctx):
            out.append(f"{r.capability} is never published publicly by {self.plugin_id}; "
                       "the app reaches it on its private compose network")
        out += self._validate(ctx)
        return out

    def public_allowed(self, ctx: HookContext) -> bool:
        return False

    def _validate(self, ctx: HookContext) -> list[str]:
        return []

    def do_validate(self, ctx: HookContext, s: Session) -> OperationResult:
        problems = self.problems(ctx)
        if problems:
            return ctx.result(OperationStatus.INVALID,
                              errors=[{"code": "invalid_request", "message": p[:1000]} for p in problems])
        return ctx.result()

    # -- plan ---------------------------------------------------------------------------------
    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return []

    def _ports(self, ctx: HookContext) -> list[PortSpec]:
        return []

    def _risks(self, ctx: HookContext) -> list[str]:
        return []

    def do_plan(self, ctx: HookContext, s: Session) -> OperationResult:
        r = ctx.resource
        if ctx.state.get("desired") == "absent":
            if r.mode is ResourceMode.ATTACHED:
                changes = [PlannedChange(action="noop", target=ctx.resource_id,
                                         detail="attached: BOSS never deletes a resource it did not create")]
            else:
                changes = [PlannedChange(action="delete", target=ctx.resource_id, destructive=True,
                                         detail="remove the resource and its data")]
        elif r.mode is ResourceMode.ATTACHED:
            changes = [PlannedChange(action="attach", target=ctx.resource_id,
                                     detail="bind to the existing resource's vault secrets")]
        else:
            changes = self._changes(ctx)
        ports = self._ports(ctx)
        plan = ResourcePlan(
            resourceId=ctx.resource_id, pluginId=self.plugin_id, pluginVersion=self.manifest.version,
            mode=r.mode, changes=changes, ports=ports, publicAccess=any(p.public for p in ports),
            bindings=[r.bindings.get(b, b) for b in self.manifest.spec.bindings],
            dependsOn=list(r.dependsOn), risks=self._risks(ctx),
            limitations=list(self.manifest.spec.limitations),
            cost={"monthlyUsd": 0.0, "basis": "runs on the app's own host; no metered service"})
        return ctx.result(plan=plan)

    # -- guards -------------------------------------------------------------------------------
    def guard_destroy(self, ctx: HookContext) -> None:
        """Refuse an attached resource, one other resources still depend on, and any destroy
        without an approved plan that deletes THIS resource."""
        if ctx.resource.mode is ResourceMode.ATTACHED:
            raise HookFailure("attached_resource", f"{ctx.resource_id} is attached: BOSS did not create it "
                              "and never destroys it", status=OperationStatus.REFUSED)
        dependents = [d for d in (ctx.state.get("dependents") or []) if d]
        if dependents:
            raise HookFailure("has_dependents", f"{ctx.resource_id} is still used by {', '.join(dependents)}; "
                              "destroy those first", status=OperationStatus.REFUSED)
        plan = ctx.plan
        if plan is None or not any(c.action == "delete" and c.target == ctx.resource_id for c in plan.changes):
            raise HookFailure("needs_approved_plan", "destroy runs only against an approved plan that deletes "
                              f"{ctx.resource_id}", status=OperationStatus.REFUSED)

    def bindings(self, ctx: HookContext, refs: dict[str, Union[SecretRef, str]]) -> dict[str, SecretRef]:
        """Plugin binding names -> the env names the app gets (the request's rename map)."""
        out = {}
        for name, ref in refs.items():
            out[ctx.resource.bindings.get(name, name)] = ref if isinstance(ref, SecretRef) else SecretRef(secretRef=str(ref))
        return out

    def attached_bindings(self, ctx: HookContext) -> dict[str, SecretRef]:
        """An attached resource's bindings are the vault secrets the operator already set."""
        base = f"app_secret://{ctx.resource.environment}/{ctx.app_key}/"
        return {env: SecretRef(secretRef=base + secret) for env, secret in ctx.resource.attach.bindings.items()}


def evidence(check: str, passed: bool, kind: str = "other", detail: str = "") -> dict:
    return {"check": check, "passed": passed, "kind": kind, "detail": detail[:500]}

